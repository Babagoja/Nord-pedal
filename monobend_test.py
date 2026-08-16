"""
Bench for tinkering with the monobend effect (CEFFECT_5).

Standalone. Does NOT import or modify Nord6.py. Stop nord6.service first, or
both programs will drive the Nord at once.

Mirrors Nord6.py's monobend, then exposes the parts that are currently
hardcoded so they can be judged by ear:

  - semitone range (fixed at 2 in Nord6.py, because that is the Nord's own
    default pitch-bend range — `bendrange` here changes the instrument's, via
    RPN, so wider bends actually work)
  - glide shape: linear ramp, exponential ease, or fixed duration
  - what an out-of-range note should do: ignore it, retrigger, or bend as far
    as the range allows

Run:  python3 monobend_test.py
Then play the keyboard and type `help`.
"""

import sys
import threading
import time

try:
    import mido
except ImportError:
    mido = None


CHANNEL = 15                 # MIDI channel 16, matching Nord6.py
MB_DT = 0.01                 # 100 Hz output loop
MAX_BEND = 8191

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class MonobendBench:

    @staticmethod
    def _open_ports():
        if mido is None:
            print("[PORTS] mido not installed")
            return None, None
        inputs, outputs = mido.get_input_names(), mido.get_output_names()
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
            print(f"[PORTS] No Nord found — falling back to {out_name!r}")
        inp = mido.open_input(in_name) if in_name else None
        out = mido.open_output(out_name) if out_name else None
        print(f"Using input:  {in_name}")
        print(f"Using output: {out_name}")
        return inp, out

    def __init__(self):
        self.inp, self.out = self._open_ports()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

        # ----------------------------
        # MONOBEND STATE
        # ----------------------------
        self.held = []               # notes physically down, in press order
        self.sounding = None         # the one note actually playing
        self.pitch = 0.0
        self.target = 0
        self.last_sent = None

        # ----------------------------
        # TUNABLES
        # ----------------------------
        self.semitone_range = 2      # how far a bend can reach
        self.bend_speed = 2000       # linear: bend units per tick
        self.ease_factor = 0.25      # ease: fraction of remaining distance per tick
        self.glide_ms = 120          # time: ms to cover the whole span
        self.glide = "linear"        # linear | ease | time
        self.out_of_range = "ignore" # ignore | retrigger | clamp
        self.monitor = False
        self.velocity = 100

    # ----------------------------
    # OUTPUT
    # ----------------------------
    def _send(self, msg):
        if self.out is not None:
            self.out.send(msg)

    def _note_on(self, note):
        self._send(mido.Message("note_on", note=note, velocity=self.velocity, channel=CHANNEL))

    def _note_off(self, note):
        self._send(mido.Message("note_off", note=note, velocity=0, channel=CHANNEL))

    def _bend(self, value):
        self._send(mido.Message("pitchwheel", pitch=int(value), channel=CHANNEL))

    def _target_for(self, note):
        diff = note - self.sounding
        return int(clamp(diff / self.semitone_range, -1.0, 1.0) * MAX_BEND)

    # ----------------------------
    # NOTE HANDLERS
    # ----------------------------
    def _on(self, note):
        with self.lock:
            if note not in self.held:
                self.held.append(note)

            if self.sounding is None:
                self._note_on(note)
                self.sounding = note
                self.pitch = 0.0
                self.target = 0
                self._bend(0)
                self.last_sent = 0
                return

            diff = note - self.sounding
            if abs(diff) <= self.semitone_range:
                self.target = self._target_for(note)
            elif self.out_of_range == "retrigger":
                self._retrigger(note)
            elif self.out_of_range == "clamp":
                self.target = MAX_BEND if diff > 0 else -MAX_BEND
            # "ignore": leave the sounding voice alone, matching Nord6.py today

    def _off(self, note):
        with self.lock:
            if note in self.held:
                self.held.remove(note)

            if not self.held:
                if self.sounding is not None:
                    self._note_off(self.sounding)
                self.sounding = None
                self.target = 0
                return

            newest = self.held[-1]
            diff = newest - self.sounding
            if abs(diff) <= self.semitone_range:
                self.target = self._target_for(newest)
            else:
                self._retrigger(newest)

    def _retrigger(self, note):
        """Swap the sounding voice. Caller holds the lock."""
        if self.sounding is not None:
            self._note_off(self.sounding)
        self._note_on(note)
        self.sounding = note
        self.target = 0
        self.pitch = 0.0
        self._bend(0)
        self.last_sent = 0

    # ----------------------------
    # PITCH LOOP
    # ----------------------------
    def _loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                target = self.target
                if self.glide == "linear":
                    step = self.bend_speed
                elif self.glide == "time":
                    # Cover the full -8191..8191 span in glide_ms regardless of
                    # how far this particular bend has to travel, so every bend
                    # moves at the same rate rather than the same duration.
                    span = 2 * MAX_BEND
                    step = span * (MB_DT * 1000.0 / max(self.glide_ms, 1))
                else:
                    step = None      # ease handles its own step

                if step is None:
                    self.pitch += (target - self.pitch) * self.ease_factor
                    if abs(target - self.pitch) < 1:
                        self.pitch = target
                elif self.pitch < target:
                    self.pitch = min(self.pitch + step, target)
                elif self.pitch > target:
                    self.pitch = max(self.pitch - step, target)

                value = int(self.pitch)
                send = value != self.last_sent and self.sounding is not None
                if send:
                    self.last_sent = value

            # Only on change — Nord6.py currently emits 100 msgs/sec even when
            # the pitch is sitting still at its target.
            if send:
                self._bend(value)

            time.sleep(MB_DT)

    # ----------------------------
    # MIDI INPUT
    # ----------------------------
    def _midi_loop(self):
        if self.inp is None:
            return
        try:
            for msg in self.inp:
                if self.stop_event.is_set():
                    return
                if msg.type == "note_on" and msg.velocity > 0:
                    if self.monitor:
                        print(f"[IN] on  {note_name(msg.note)}")
                    self._on(msg.note)
                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    if self.monitor:
                        print(f"[IN] off {note_name(msg.note)}")
                    self._off(msg.note)
        except Exception as e:
            if not self.stop_event.is_set():
                print(f"[MIDI] loop error: {e}")

    # ----------------------------
    # PITCH BEND RANGE (RPN 0)
    # ----------------------------
    def _set_bend_range(self, semitones):
        """Tell the Nord how far a full pitchwheel deflection should reach.

        Without this the instrument stays at its default of 2 semitones, which
        is the real reason Nord6.py's MB_SEMITONE_RANGE is stuck at 2 — asking
        for a wider bend in software just makes the same tone arrive sooner.
        """
        for control, value in ((101, 0), (100, 0), (6, semitones), (38, 0),
                               (101, 127), (100, 127)):
            self._send(mido.Message("control_change", control=control,
                                    value=value, channel=CHANNEL))
        print(f"[BEND RANGE] asked the Nord for +/-{semitones} semitones")
        print("             if bends still sound like a tone, it ignored the RPN")

    # ----------------------------
    # CONSOLE
    # ----------------------------
    HELP = """
  range <n>          how far a bend may reach, in semitones (software side)
  bendrange <n>      tell the NORD its pitch-bend range, via RPN — do both
  glide <mode>       linear | ease | time
  speed <n>          linear: bend units per 10ms tick (Nord6 default 2000)
  ease <0.01-1.0>    ease: fraction of the remaining distance per tick
  ms <n>             time: milliseconds to cover the full bend span
  oor <mode>         out-of-range note: ignore | retrigger | clamp
  vel <1-127>        velocity for triggered notes
  mon <on|off>       print incoming notes
  status             show everything
  panic              all notes off, pitchwheel centred
  quit               panic, then exit
"""

    def _command(self, line):
        parts = line.strip().split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]

        def num(i, cast=float):
            return cast(args[i])

        try:
            if cmd in ("help", "?"):
                print(self.HELP)
            elif cmd == "range":
                self.semitone_range = int(clamp(num(0, int), 1, 24))
                print(f"[MB] software range = +/-{self.semitone_range} semitones")
            elif cmd == "bendrange":
                self._set_bend_range(int(clamp(num(0, int), 1, 24)))
            elif cmd == "glide":
                if args and args[0] in ("linear", "ease", "time"):
                    self.glide = args[0]
                    print(f"[MB] glide = {self.glide}")
                else:
                    print("usage: glide <linear|ease|time>")
            elif cmd == "speed":
                self.bend_speed = int(clamp(num(0, int), 1, MAX_BEND))
                print(f"[MB] speed = {self.bend_speed}/tick")
            elif cmd == "ease":
                self.ease_factor = clamp(num(0), 0.01, 1.0)
                print(f"[MB] ease factor = {self.ease_factor}")
            elif cmd == "ms":
                self.glide_ms = int(clamp(num(0, int), 10, 5000))
                print(f"[MB] glide time = {self.glide_ms}ms for the full span")
            elif cmd == "oor":
                if args and args[0] in ("ignore", "retrigger", "clamp"):
                    self.out_of_range = args[0]
                    print(f"[MB] out-of-range = {self.out_of_range}")
                else:
                    print("usage: oor <ignore|retrigger|clamp>")
            elif cmd == "vel":
                self.velocity = int(clamp(num(0, int), 1, 127))
                print(f"[MB] velocity = {self.velocity}")
            elif cmd == "mon":
                self.monitor = bool(args) and args[0].lower() == "on"
                print(f"[MON] {'on' if self.monitor else 'off'}")
            elif cmd == "status":
                print(f"range=+/-{self.semitone_range}  glide={self.glide}  "
                      f"speed={self.bend_speed}  ease={self.ease_factor}  "
                      f"ms={self.glide_ms}  oor={self.out_of_range}  "
                      f"vel={self.velocity}\n"
                      f"held={[note_name(n) for n in self.held]}  "
                      f"sounding={note_name(self.sounding) if self.sounding is not None else None}  "
                      f"pitch={int(self.pitch)} target={self.target}")
            elif cmd == "panic":
                self._panic()
                print("[PANIC] notes off, pitch centred")
            elif cmd in ("quit", "exit", "q"):
                return False
            else:
                print(f"unknown command: {cmd}  (try `help`)")
        except (IndexError, ValueError):
            print(f"bad arguments for `{cmd}` — see `help`")
        return True

    def _panic(self):
        with self.lock:
            if self.sounding is not None:
                self._note_off(self.sounding)
            self.held = []
            self.sounding = None
            self.pitch = 0.0
            self.target = 0
            self.last_sent = None
        self._bend(0)
        self._send(mido.Message("control_change", control=123, value=0, channel=CHANNEL))

    def run(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._midi_loop, daemon=True).start()
        print("\n[monobend_test] ready. Play the keyboard; `help` for commands.")
        try:
            while True:
                try:
                    line = input("mb> ")
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
            print("\n[monobend_test] notes off, pitch centred, ports closed.")


if __name__ == "__main__":
    MonobendBench().run()
