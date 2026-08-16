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
        self.out_of_range = "reanchor"   # reanchor | ignore | retrigger | clamp
        self.recentre_delay = 1.5    # seconds of silence before the wheel resets
        self.silent_since = None
        # Step mode: the pitch we are gliding toward, as an absolute MIDI note.
        # Only step mode needs it; the other modes work straight off `target`.
        self.goal = None
        self.step_velocity = 55      # re-articulation velocity when stepping
        self.monitor = False
        self.trace = True            # print the branch taken on every note event
        self.velocity = 100

    def _t(self, msg):
        if self.trace:
            print(f"      {msg}")

    # ----------------------------
    # OUTPUT
    # ----------------------------
    def _send(self, msg):
        if self.out is not None:
            self.out.send(msg)

    def _note_on(self, note, velocity=None):
        self._send(mido.Message("note_on", note=note,
                                velocity=self.velocity if velocity is None else velocity,
                                channel=CHANNEL))

    def _heard(self):
        """The pitch actually sounding: base note plus whatever bend is applied."""
        if self.sounding is None:
            return None
        return self.sounding + (self.pitch / MAX_BEND) * self.semitone_range

    def _step(self, direction):
        """Shift the base a whole range and drop the wheel, in one move.

        base + range with the wheel centred is the same pitch as base with the
        wheel at full deflection, so the two cancel and the glide continues
        without a jump. That buys unlimited travel from a wheel that only
        reaches a whole tone. The cost is a re-articulation — softened by
        step_velocity — which also masks the wheel snapping back.
        """
        new_base = clamp(self.sounding + direction * self.semitone_range, 0, 127)
        if new_base == self.sounding:
            return False
        self._note_off(self.sounding)
        self._bend(0)
        self.last_sent = 0
        self.pitch = 0.0
        self._note_on(new_base, self.step_velocity)
        self.sounding = new_base
        self.target = self._target_for(self.goal) if self.goal is not None else 0
        self._t(f"   step {'up' if direction > 0 else 'down'} to base "
                f"{note_name(new_base)} (pitch continuous)")
        return True

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
                self.silent_since = None
                # Centre before the note_on. A phrase that starts on a
                # deflected wheel has its base below the pitch you hear, so
                # every upward move is out of range and reanchors instead of
                # bending — the bend appears to stop working.
                was = int(self.pitch)
                self.pitch = 0.0
                self.target = 0
                if self.last_sent != 0:
                    self._bend(0)
                    self.last_sent = 0
                self._note_on(note)
                self.sounding = note
                self._t(f"press {note_name(note)}: nothing sounding -> play it"
                        + (f" (wheel recentred from {was})" if was else ""))
                return

            self.goal = note
            diff = note - self.sounding
            if abs(diff) <= self.semitone_range:
                self.target = self._target_for(note)
                self._t(f"press {note_name(note)}: {diff:+d} semis from "
                        f"{note_name(self.sounding)} -> BEND (target {self.target})")
            elif self.out_of_range == "step":
                self.target = MAX_BEND if diff > 0 else -MAX_BEND
                self._t(f"press {note_name(note)}: {diff:+d} semis -> GLIDE, "
                        f"stepping the base as the wheel runs out")
            elif self.out_of_range == "reanchor":
                self._t(f"press {note_name(note)}: {diff:+d} semis, out of range "
                        f"-> REANCHOR")
                self._reanchor(note)
            elif self.out_of_range == "retrigger":
                self._t(f"press {note_name(note)}: {diff:+d} semis, out of range "
                        f"-> RETRIGGER as a new note")
                self._retrigger(note)
            elif self.out_of_range == "clamp":
                self.target = MAX_BEND if diff > 0 else -MAX_BEND
                self._t(f"press {note_name(note)}: out of range -> CLAMP to max bend")
            else:
                self._t(f"press {note_name(note)}: {diff:+d} semis, out of range "
                        f"-> IGNORED (this is Nord6.py's behaviour today)")

    def _off(self, note):
        with self.lock:
            if note in self.held:
                self.held.remove(note)

            if not self.held:
                if self.sounding is not None:
                    self._note_off(self.sounding)
                self._t(f"release {note_name(note)}: nothing held -> note off, "
                        f"wheel frozen at {int(self.pitch)}")
                self.sounding = None
                # Freeze rather than glide back to centre: the wheel is
                # channel-wide and would drag the note's release tail with it.
                self.target = int(self.pitch)
                self.silent_since = time.time()
                return

            newest = self.held[-1]
            self.goal = newest
            diff = newest - self.sounding
            if abs(diff) <= self.semitone_range:
                self.target = self._target_for(newest)
                self._t(f"release {note_name(note)}: newest held is "
                        f"{note_name(newest)} -> BEND (target {self.target})")
                return

            if self.out_of_range == "step":
                self.target = MAX_BEND if diff > 0 else -MAX_BEND
                self._t(f"release {note_name(note)}: gliding to "
                        f"{note_name(newest)}, stepping as needed")
                return

            if self.out_of_range == "reanchor":
                self._t(f"release {note_name(note)}: newest held "
                        f"{note_name(newest)} out of range -> REANCHOR")
                self._reanchor(newest)
                return
            if self.out_of_range == "retrigger":
                self._t(f"release {note_name(note)}: newest held "
                        f"{note_name(newest)} out of range -> RETRIGGER")
                self._retrigger(newest)
                return
            if self.out_of_range == "clamp":
                self.target = MAX_BEND if diff > 0 else -MAX_BEND
                self._t(f"release {note_name(note)}: out of range -> CLAMP")
                return

            # "ignore": a note ignored on the way in must stay ignored on the
            # way out. Otherwise C-D-E with E out of range bends C up to D,
            # silently drops E, and then retriggers on the E when D is
            # released — honouring a note it had already refused.
            in_range = [n for n in self.held
                        if abs(n - self.sounding) <= self.semitone_range]
            if in_range:
                self.target = self._target_for(in_range[-1])
                self._t(f"release {note_name(note)}: {note_name(newest)} was already "
                        f"ignored -> BEND to {note_name(in_range[-1])} instead")
            elif self.sounding in self.held:
                self.target = 0
                self._t(f"release {note_name(note)}: nothing reachable -> "
                        f"bend back to {note_name(self.sounding)}")
            else:
                self._t(f"release {note_name(note)}: sounding voice gone -> RETRIGGER")
                # The sounding note itself is gone and nothing reachable is
                # left, so there is no voice to bend — take the newest.
                self._retrigger(newest)

    def _reanchor(self, want):
        """Move to `want` without touching the wheel.

        The wheel is channel-wide, so recentring it also drags any voice still
        in its release tail. That is what makes the original note audibly slide
        back in on a retrigger. Here the wheel stays put and we trigger a note
        offset by however much bend is currently applied, so the new voice
        lands on the right pitch and nothing already sounding moves.
        """
        bend_semis = (self.pitch / MAX_BEND) * self.semitone_range
        base = int(round(want - bend_semis))
        residual = want - base                       # semitones the wheel must supply
        new_pitch = clamp(residual / self.semitone_range * MAX_BEND, -MAX_BEND, MAX_BEND)

        if self.sounding is not None:
            self._note_off(self.sounding)
        # Usually zero: when the wheel is parked at full deflection the offset
        # is a whole number of semitones and nothing needs to move at all.
        if int(new_pitch) != self.last_sent:
            self._bend(int(new_pitch))
            self.last_sent = int(new_pitch)
        self.pitch = float(new_pitch)
        self.target = int(new_pitch)
        self._note_on(base)
        self.sounding = base
        self._t(f"   reanchor: play {note_name(base)} with wheel at "
                f"{int(new_pitch)} -> sounds {note_name(want)}"
                + ("  (wheel did not move)" if residual == 0 else ""))

    def _retrigger(self, note):
        """Swap the sounding voice. Caller holds the lock."""
        if self.sounding is not None:
            self._note_off(self.sounding)
        # Centre the wheel BEFORE the new note speaks. Sending note_on first
        # lets the new note sound at the outgoing bend until the next tick —
        # up to 10ms of audibly wrong pitch on the attack.
        self._bend(0)
        self.last_sent = 0
        self.pitch = 0.0
        self.target = 0
        self._note_on(note)
        self.sounding = note

    # ----------------------------
    # PITCH LOOP
    # ----------------------------
    def _loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                if (self.sounding is None and self.silent_since is not None
                        and time.time() - self.silent_since >= self.recentre_delay):
                    self.pitch = 0.0
                    self.target = 0
                    self.silent_since = None
                    self._t("(silence) wheel recentred")
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

                # Step mode: pinned at the wheel's limit with further to go, so
                # hand the remaining travel to the base note and carry on.
                if (self.out_of_range == "step" and self.sounding is not None
                        and self.goal is not None and abs(self.pitch) >= MAX_BEND - 1):
                    heard = self._heard()
                    if abs(self.goal - heard) > 0.01:
                        if self._step(1 if self.goal > heard else -1):
                            time.sleep(MB_DT)
                            continue

                value = int(self.pitch)
                # Also emit while nothing sounds, so the delayed recentre lands.
                send = value != self.last_sent
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
    # MEASURE THE REAL BEND RANGE
    # ----------------------------
    def _sweep(self, note=60):
        """Hold a note and sweep the wheel to both extremes, slowly.

        Answers by ear what the Nord actually does, rather than trusting either
        its documentation or our assumption of a whole tone. Play the target
        interval afterwards to compare — if full deflection matches the note a
        tone up, the range really is 2.
        """
        if self.out is None:
            print("[SWEEP] no output")
            return
        self._panic()
        print(f"\n[SWEEP] holding {note_name(note)}. Wheel goes centre -> up -> "
              f"centre -> down -> centre, ~2s each.")
        print(f"        Compare the top against {note_name(note + 2)} "
              f"(a tone) and {note_name(note + 12)} (an octave).")
        self._note_on(note)
        self.sounding = note
        try:
            legs = ((0, MAX_BEND), (MAX_BEND, 0), (0, -MAX_BEND), (-MAX_BEND, 0))
            for start, end in legs:
                steps = 60
                for i in range(steps + 1):
                    self._bend(start + (end - start) * i / steps)
                    time.sleep(2.0 / steps)
        finally:
            self._bend(0)
            self.last_sent = 0
            self.pitch = 0.0
            self._note_off(note)
            self.sounding = None
        print("[SWEEP] done. `range <n>` to match what you measured.")

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
  recentre <sec>     silence before the wheel resets to centre (0 = never)
  stepvel <1-127>    re-articulation velocity when stepping (lower = softer)
  sweep [note]       hold a note and sweep the wheel, to measure the real range
  oor <mode>         out-of-range note:
                       reanchor  play it without moving the wheel (default)
                       step      glide all the way, moving the base note when
                                 the wheel runs out — unlimited bend range
                       retrigger play it and recentre the wheel
                       ignore    drop it (Nord6.py's behaviour today)
                       clamp     bend as far as the range allows
  vel <1-127>        velocity for triggered notes
  mon <on|off>       print incoming notes
  trace <on|off>     print the decision taken on every note event (on by default)
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
            elif cmd == "stepvel":
                self.step_velocity = int(clamp(num(0, int), 1, 127))
                print(f"[MB] step velocity = {self.step_velocity}")
            elif cmd == "sweep":
                self._sweep(int(num(0, int)) if args else 60)
            elif cmd == "recentre":
                self.recentre_delay = clamp(num(0), 0.0, 30.0)
                print(f"[MB] recentre after {self.recentre_delay}s of silence"
                      if self.recentre_delay else "[MB] wheel never auto-recentres")
            elif cmd == "oor":
                if args and args[0] in ("reanchor", "step", "ignore", "retrigger", "clamp"):
                    self.out_of_range = args[0]
                    print(f"[MB] out-of-range = {self.out_of_range}")
                else:
                    print("usage: oor <reanchor|step|ignore|retrigger|clamp>")
            elif cmd == "vel":
                self.velocity = int(clamp(num(0, int), 1, 127))
                print(f"[MB] velocity = {self.velocity}")
            elif cmd == "mon":
                self.monitor = bool(args) and args[0].lower() == "on"
                print(f"[MON] {'on' if self.monitor else 'off'}")
            elif cmd == "trace":
                self.trace = bool(args) and args[0].lower() == "on"
                print(f"[TRACE] {'on' if self.trace else 'off'}")
            elif cmd == "status":
                print(f"range=+/-{self.semitone_range}  glide={self.glide}  "
                      f"speed={self.bend_speed}  ease={self.ease_factor}  "
                      f"ms={self.glide_ms}  oor={self.out_of_range}  "
                      f"recentre={self.recentre_delay}s  "
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
