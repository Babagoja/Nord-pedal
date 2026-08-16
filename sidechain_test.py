"""
Phase A bench for CEFFECT_3 — fake sidechain (pedal-triggered volume duck).

Standalone. Does NOT import or modify Nord6.py.

Purpose, in order of importance:
  1. Settle whether CC7 or CC11 actually ducks this Nord over channel 16.
  2. Confirm whether the Nord reflects our channel-15 sends back to our input
     (which would rule CC11 out without bounce suppression).
  3. Let the envelope shape be tuned by ear, and by eye with no hardware.

Run:  python sidechain_test.py
Then type `help`.
"""

import sys
import threading
import time

try:
    import mido
except ImportError:
    mido = None


# ----------------------------
# CONSTANTS
# ----------------------------
CHANNEL = 15                 # MIDI channel 16, matching Nord6.py
SC_DT = 0.01                 # 100 Hz output loop
SUSTAIN_CC = 64

SC_FLOOR_DEFAULT = 38
SC_LENGTH_DEFAULT = 0.35
SC_CURVE_DEFAULT = 2.0

SC_LENGTH_MIN = 0.03
SC_LENGTH_MAX = 1.5
SC_CURVE_MIN = 1.0
SC_CURVE_MAX = 4.0

KNOB_FLOOR = 102
KNOB_LENGTH = 103
KNOB_CURVE = 107

NOTE_BASE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def parse_note(text):
    """'B3' / 'c#4' / 'Eb2' / '59' -> MIDI note number, or None."""
    text = text.strip().lower()
    if text.isdigit():
        n = int(text)
        return n if 0 <= n <= 127 else None
    if not text or text[0] not in NOTE_BASE:
        return None
    semis = NOTE_BASE[text[0]]
    i = 1
    while i < len(text) and text[i] in "#b":
        semis += 1 if text[i] == "#" else -1
        i += 1
    try:
        octave = int(text[i:])
    except ValueError:
        return None
    n = (octave + 1) * 12 + semis          # C4 = 60
    return n if 0 <= n <= 127 else None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class SidechainBench:

    # ----------------------------
    # PORTS
    # ----------------------------
    @staticmethod
    def _open_ports():
        """Nord ports if present; otherwise first available output, console only.

        Mirrors Nord6._open_nord_ports, but never raises — the envelope math and
        `plot` are useful with no hardware attached at all.
        """
        if mido is None:
            print("[PORTS] mido not installed — offline mode (plot/params only)")
            return None, None

        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
        print("Inputs: ", inputs)
        print("Outputs:", outputs)

        in_name = next((n for n in inputs if "Nord Electro" in n and "MIDI 0" in n), None)
        out_name = next((n for n in outputs if "Nord Electro" in n and "MIDI 1" in n), None)
        if in_name is None:
            in_name = next((n for n in inputs if "Nord Electro" in n), None)
        if out_name is None:
            out_name = next((n for n in outputs if "Nord Electro" in n), None)

        if out_name is None and outputs:
            out_name = outputs[0]
            print(f"[PORTS] No Nord found — falling back to output {out_name!r}")

        inp = mido.open_input(in_name) if in_name else None
        out = mido.open_output(out_name) if out_name else None

        print(f"Using input:  {in_name}")
        print(f"Using output: {out_name}")
        if inp is None:
            print("[PORTS] No input — pedal/knob tests unavailable, console still works")
        if out is None:
            print("[PORTS] No output — offline mode (plot/params only)")
        return inp, out

    def __init__(self):
        self.inp, self.out = self._open_ports()
        self.stop_event = threading.Event()

        # ----------------------------
        # ENVELOPE STATE
        # ----------------------------
        self.sc_floor = SC_FLOOR_DEFAULT
        self.sc_length = SC_LENGTH_DEFAULT
        self.sc_curve = SC_CURVE_DEFAULT
        self.sc_trigger_time = None      # None = idle, at full volume
        self.sc_last_sent = 127
        self.sc_volume_cc = 7            # 7 = channel volume (default), 11 = expression

        # ----------------------------
        # BENCH STATE
        # ----------------------------
        self.monitor = False
        self.drone_note = None
        self.auto_bpm = None
        self.cc_in_count = {}            # incoming CC -> count, for reflection detection
        self._probing = False            # suppresses the output loop during a probe

    # ----------------------------
    # ENVELOPE
    # ----------------------------
    def _sc_value(self, elapsed):
        """Instant drop to floor, then an accelerating power-curve climb to 127."""
        if elapsed >= self.sc_length:
            return 127
        frac = elapsed / self.sc_length
        return int(round(self.sc_floor + (127 - self.sc_floor) * (frac ** self.sc_curve)))

    def _sc_trigger(self):
        self.sc_trigger_time = time.time()

    def _sc_send(self, value):
        if self.out is None:
            return
        self.out.send(mido.Message(
            "control_change", control=self.sc_volume_cc, value=value, channel=CHANNEL
        ))

    def _sc_restore(self):
        """Volume back to full. Every exit path must land here."""
        self.sc_trigger_time = None
        self.sc_last_sent = 127
        self._sc_send(127)

    # ----------------------------
    # OUTPUT LOOP (100 Hz)
    # ----------------------------
    def _sc_loop(self):
        while not self.stop_event.is_set():
            if not self._probing:
                t = self.sc_trigger_time
                if t is None:
                    value = 127
                else:
                    elapsed = time.time() - t
                    value = self._sc_value(elapsed)
                    if elapsed >= self.sc_length:
                        self.sc_trigger_time = None

                # Emit only on change, mirroring _pan_loop's last_sent_pan idiom.
                if value != self.sc_last_sent:
                    self._sc_send(value)
                    self.sc_last_sent = value

            time.sleep(SC_DT)

    # ----------------------------
    # AUTO-FIRE LOOP
    # ----------------------------
    def _auto_loop(self):
        while not self.stop_event.is_set():
            bpm = self.auto_bpm
            if bpm is None:
                time.sleep(0.02)
                continue
            self._sc_trigger()
            time.sleep(60.0 / bpm)

    # ----------------------------
    # MIDI INPUT LOOP
    # ----------------------------
    def _midi_loop(self):
        if self.inp is None:
            return
        try:
            for msg in self.inp:
                if self.stop_event.is_set():
                    return
                if msg.type != "control_change":
                    continue

                self.cc_in_count[msg.control] = self.cc_in_count.get(msg.control, 0) + 1
                if self.monitor:
                    print(f"[CC IN] {msg.control} = {msg.value}")

                # One-shot on press. Release does nothing — no sustain, no freeze.
                if msg.control == SUSTAIN_CC:
                    if msg.value >= 64:
                        self._sc_trigger()
                        print("[SC] trigger (pedal)")

                elif msg.control == KNOB_FLOOR:
                    self.sc_floor = msg.value
                    print(f"[SC] floor = {self.sc_floor}")

                elif msg.control == KNOB_LENGTH:
                    self.sc_length = SC_LENGTH_MIN + (msg.value / 127) * (SC_LENGTH_MAX - SC_LENGTH_MIN)
                    print(f"[SC] length = {round(self.sc_length, 3)}s")

                elif msg.control == KNOB_CURVE:
                    self.sc_curve = SC_CURVE_MIN + (msg.value / 127) * (SC_CURVE_MAX - SC_CURVE_MIN)
                    print(f"[SC] curve = {round(self.sc_curve, 2)}")

        except Exception as e:
            if not self.stop_event.is_set():
                print(f"[MIDI] loop error: {e}")

    # ----------------------------
    # DRONE
    # ----------------------------
    def _drone_off(self):
        if self.drone_note is not None and self.out is not None:
            self.out.send(mido.Message(
                "note_off", note=self.drone_note, velocity=0, channel=CHANNEL
            ))
        self.drone_note = None

    def _drone_on(self, note):
        self._drone_off()
        if self.out is None:
            print("[DRONE] no output")
            return
        self.out.send(mido.Message(
            "note_on", note=note, velocity=100, channel=CHANNEL
        ))
        self.drone_note = note
        print(f"[DRONE] note {note} sounding")

    # ----------------------------
    # PROBE — the critical test
    # ----------------------------
    def _probe(self):
        """Sweep CC11 then CC7 on channel 16 and report which one ducks.

        Also counts incoming messages for the CC being swept: any arriving
        during our own sweep means the Nord reflects channel-15 output back to
        our input, which is what would force bounce suppression on CC11.
        """
        if self.out is None:
            print("[PROBE] no output — connect the Nord first")
            return
        if self.drone_note is None:
            print("[PROBE] nothing sounding. Run `drone B3` (or play a key) first.")
            return

        self._probing = True
        try:
            for cc in (11, 7):
                before = self.cc_in_count.get(cc, 0)
                print(f"\n[PROBE] CC{cc}: 127 -> 0 -> 127 over ~6s. LISTEN.")
                time.sleep(0.6)

                for v in range(127, -1, -2):
                    self.out.send(mido.Message(
                        "control_change", control=cc, value=v, channel=CHANNEL
                    ))
                    time.sleep(0.035)
                time.sleep(0.5)
                for v in range(0, 128, 2):
                    self.out.send(mido.Message(
                        "control_change", control=cc, value=v, channel=CHANNEL
                    ))
                    time.sleep(0.035)

                self.out.send(mido.Message(
                    "control_change", control=cc, value=127, channel=CHANNEL
                ))
                reflected = self.cc_in_count.get(cc, 0) - before
                if reflected:
                    print(f"[PROBE] !! {reflected} incoming CC{cc} during our own sweep "
                          f"— the Nord REFLECTS output back to input.")
                else:
                    print(f"[PROBE] no CC{cc} reflection detected.")
                time.sleep(0.5)
        finally:
            self._probing = False
            self.sc_last_sent = 127
            self._sc_send(127)

        print("\n[PROBE] done. Which one changed the volume? Set it with `cc 7` or `cc 11`.")
        print("        If NEITHER ducked, stop here — the approach needs rethinking.")

    # ----------------------------
    # PLOT — envelope shape, no hardware needed
    # ----------------------------
    def _plot(self, rows=16, cols=56):
        vals = [self._sc_value(self.sc_length * (c / (cols - 1))) for c in range(cols)]
        print(f"\nfloor={self.sc_floor}  length={round(self.sc_length, 3)}s  "
              f"curve={round(self.sc_curve, 2)}  cc={self.sc_volume_cc}")
        for r in range(rows):
            hi = 127 * (rows - r) / rows
            lo = 127 * (rows - r - 1) / rows
            label = f"{int(round(hi)):3d} |"
            line = "".join("#" if lo <= v <= hi or (r == 0 and v >= hi) else " " for v in vals)
            print(label + line)
        print("    +" + "-" * cols)
        print("     0" + " " * (cols - 8) + f"{round(self.sc_length, 2)}s")

    # ----------------------------
    # CONSOLE
    # ----------------------------
    HELP = """
  probe             sweep CC11 then CC7 while a drone sounds — the critical test
  drone <note|off>  hold/release a sustained note (e.g. drone B3)
  hit               fire one envelope manually
  floor <0-127>     duck floor (0 = silence on each hit, 127 = no duck)
  len <sec>         recovery length (0.03 - 1.5)
  curve <1-4>       power-curve exponent; higher hangs lower for longer
  cc <7|11>         switch duck target (sends 127 to the old CC first)
  plot              ASCII render of the current envelope
  mon <on|off>      print incoming CC
  auto <bpm|off>    fire repeatedly at a tempo
  status            show current params
  panic             restore volume to 127, notes off
  quit              panic, then exit
"""

    def _command(self, line):
        parts = line.strip().split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("help", "?"):
            print(self.HELP)

        elif cmd == "probe":
            self._probe()

        elif cmd == "drone":
            if not args:
                print("usage: drone <note|off>")
            elif args[0].lower() == "off":
                self._drone_off()
                print("[DRONE] off")
            else:
                note = parse_note(args[0])
                if note is None:
                    print(f"bad note: {args[0]}")
                else:
                    self._drone_on(note)

        elif cmd == "hit":
            self._sc_trigger()
            print("[SC] trigger")

        elif cmd == "floor":
            try:
                self.sc_floor = int(clamp(int(args[0]), 0, 127))
                print(f"[SC] floor = {self.sc_floor}")
            except (IndexError, ValueError):
                print("usage: floor <0-127>")

        elif cmd == "len":
            try:
                self.sc_length = clamp(float(args[0]), SC_LENGTH_MIN, SC_LENGTH_MAX)
                print(f"[SC] length = {round(self.sc_length, 3)}s")
            except (IndexError, ValueError):
                print(f"usage: len <{SC_LENGTH_MIN}-{SC_LENGTH_MAX}>")

        elif cmd == "curve":
            try:
                self.sc_curve = clamp(float(args[0]), SC_CURVE_MIN, SC_CURVE_MAX)
                print(f"[SC] curve = {round(self.sc_curve, 2)}")
            except (IndexError, ValueError):
                print(f"usage: curve <{SC_CURVE_MIN}-{SC_CURVE_MAX}>")

        elif cmd == "cc":
            if not args or args[0] not in ("7", "11"):
                print("usage: cc <7|11>")
            else:
                # Restore the old CC before switching, so it can't be left ducked.
                self._sc_restore()
                self.sc_volume_cc = int(args[0])
                self.sc_last_sent = 127
                self._sc_send(127)
                print(f"[SC] duck target = CC{self.sc_volume_cc}")

        elif cmd == "plot":
            self._plot()

        elif cmd == "mon":
            self.monitor = bool(args) and args[0].lower() == "on"
            print(f"[MON] {'on' if self.monitor else 'off'}")

        elif cmd == "auto":
            if args and args[0].lower() == "off":
                self.auto_bpm = None
                print("[AUTO] off")
            else:
                try:
                    self.auto_bpm = clamp(float(args[0]), 20.0, 300.0)
                    print(f"[AUTO] {round(self.auto_bpm, 1)} bpm")
                except (IndexError, ValueError):
                    print("usage: auto <bpm|off>")

        elif cmd == "status":
            print(f"floor={self.sc_floor}  length={round(self.sc_length, 3)}s  "
                  f"curve={round(self.sc_curve, 2)}  cc={self.sc_volume_cc}  "
                  f"drone={self.drone_note}  auto={self.auto_bpm}  mon={self.monitor}")

        elif cmd == "panic":
            self._panic()
            print("[PANIC] volume 127, notes off")

        elif cmd in ("quit", "exit", "q"):
            return False

        else:
            print(f"unknown command: {cmd}  (try `help`)")

        return True

    def _panic(self):
        self.auto_bpm = None
        self.sc_trigger_time = None
        self._drone_off()
        if self.out is not None:
            # Both CCs, not just the active one — a probe may have left the other down.
            for cc in (7, 11):
                self.out.send(mido.Message(
                    "control_change", control=cc, value=127, channel=CHANNEL
                ))
            self.out.send(mido.Message(
                "control_change", control=123, value=0, channel=CHANNEL
            ))
        self.sc_last_sent = 127

    # ----------------------------
    # RUN
    # ----------------------------
    def run(self):
        threading.Thread(target=self._sc_loop, daemon=True).start()
        threading.Thread(target=self._auto_loop, daemon=True).start()
        threading.Thread(target=self._midi_loop, daemon=True).start()

        print("\n[sidechain_test] ready. `help` for commands, `probe` for the critical test.")
        try:
            while True:
                try:
                    line = input("sc> ")
                except EOFError:
                    break
                if not self._command(line):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            self._panic()
            time.sleep(0.05)
            if self.inp is not None:
                self.inp.close()
            if self.out is not None:
                self.out.close()
            print("\n[sidechain_test] volume restored, ports closed.")


if __name__ == "__main__":
    SidechainBench().run()
