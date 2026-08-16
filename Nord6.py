import mido
import json
import os
import sys
import threading
import time
import math
import signal
import atexit
from collections import deque
from statistics import mean


class Nord6:
    # ----------------------------
    # ARP CONSTANTS (CEFFECT_4)
    # ----------------------------
    ARP_BPM_MIN = 40.0
    ARP_BPM_MAX = 300.0
    ARP_TAP_RESET_GAP = 2.0
    ARP_GATE_RATIO = 0.80
    ARP_IDLE_SLEEP = 0.005
    ARP_MIN_STEP = 0.020
    ARP_PATTERN_PRESETS = {0: 0, 26: 1, 51: 2, 77: 3, 102: 4, 127: 5}
    ARP_PATTERN_NAMES = ["Up", "Down", "UpDown", "UpDownNoEdges", "Mimic", "Custom"]
    ARP_SUBDIV_NAMES = ["Fit x1/bar", "1/4", "1/8", "1/16", "1/32"]
    ARP_SUBDIV_MULT = {0: 1.0, 1: 1.0, 2: 2.0, 3: 4.0, 4: 8.0}
    # Custom mode reads CC 16..24 as pattern steps 1..9 rather than as one
    # slot per chord tone: each drawbar's detent picks which tone of the held
    # chord that step plays, so one dialed-in pattern follows any chord.
    # Fully out (127) = tone 1 (lowest), each detent down = one tone higher,
    # fully in (0) = rest.
    ARP_CUSTOM_DETENTS = 8                # drawbar has 9 positions, 0..8
    # Live-retrigger coalescing window. Wide enough that a rolled chord is
    # one restart rather than one per finger, far below the gap between
    # actual chord changes even at fast tempo.
    ARP_RETRIGGER_DEBOUNCE = 0.12

    # ----------------------------
    # SIDECHAIN CONSTANTS (CEFFECT_3)
    # ----------------------------
    # Pedal-triggered volume duck. One duck per sustain press, so the player is
    # the clock and it can never drift against a live drummer.
    SC_DT = 0.01                          # 100 Hz output loop
    SC_VOLUME_CC = 7                      # channel volume; nothing reads CC7 as input
    SC_FLOOR_DEFAULT = 30                 # tuned on hardware at 120bpm
    SC_LENGTH_DEFAULT = 0.3
    SC_CURVE_DEFAULT = 4.0
    SC_LENGTH_MIN = 0.03
    SC_LENGTH_MAX = 1.5
    SC_CURVE_MIN = 1.0
    SC_CURVE_MAX = 6.0
    # The value the duck returns to, and the most we ever send. Measured as 127
    # on this Nord with sidechain_test.py's `ref`; kept configurable because the
    # General MIDI default for channel volume is 100, and restoring above the
    # resting value would leave a quiet patch louder than the player set it.
    SC_CEILING_DEFAULT = 127
    # One pedal press can arrive as several CC64 messages — the Nord transmits
    # per active section, and _midi_loop acts on every channel. Anything this
    # close together is a duplicate, not a deliberate re-tap: 50ms is 1200bpm
    # sixteenths, far faster than a foot can move.
    SC_TRIGGER_DEBOUNCE = 0.05
    # The sustain pedal is continuous, not a switch: it streams CC64 values as
    # it travels. Firing on `value >= 64` re-triggers on every message above
    # the threshold, so a slow press and a slow release both fire repeatedly.
    # Trigger on the crossing instead, with hysteresis so a pedal resting near
    # the threshold cannot chatter.
    SC_PEDAL_ON = 64
    SC_PEDAL_OFF = 32
    # The pedal's contact springs back closed ~10ms after release, for ~25ms,
    # producing a phantom press. Measured on hardware: bounces land 8-11ms
    # after a release, real re-taps 183ms or more. Ignore presses inside this
    # window. Note this is release-to-press; SC_TRIGGER_DEBOUNCE is
    # press-to-press and cannot see a bounce that follows a real release.
    SC_PEDAL_REARM = 0.08
    # Logs every CC64 with its gap from the previous one. Set True to re-measure
    # pedal behaviour if bounce handling ever needs revisiting.
    SC_DEBUG_PEDAL = False
    # Knobs claimed only while shift is held — see _handle_cc. Unmodified, these
    # keep their existing jobs (102 pan, 103 mod amplitude, 107 mod frequency).
    SC_KNOB_FLOOR = 102
    SC_KNOB_LENGTH = 103
    SC_KNOB_CURVE = 107

    # ----------------------------
    # PRESET SYSTEM
    # ----------------------------
    # Slot 1 is the read-only base; mirrors the __init__ defaults below.
    # Slots 2-5 are saveable and persisted to PRESETS_PATH. Pointer starts
    # at 3 every startup (not persisted).
    BASE_PRESET = {
        "effects": {"CEFFECT_1": False, "CEFFECT_2": False, "CEFFECT_3": False,
                    "CEFFECT_4": False, "CEFFECT_5": False, "CEFFECT_6": False},
        "pan_value": None,
        "sc_floor": SC_FLOOR_DEFAULT,
        "sc_length": SC_LENGTH_DEFAULT,
        "sc_curve": SC_CURVE_DEFAULT,
        "sc_ceiling": SC_CEILING_DEFAULT,
        "mod_frequency": 7.5,
        "mod_amplitude": 1200,
        "harmonizer_interval": 0,
        "bend_speed": 2000,
        "arp_bpm": 120.0,
        "arp_subdiv_idx": 1,
        "arp_pattern_mode": 0,
        "arp_cc27_on": False,
        "arp_cc30_state": 0,
        # Custom-mode steps: a plain 4-note ascending run (tones 1 2 3 4,
        # then rests). [64]*9 meant something under the old rank-based
        # custom mode; under step mode it was nine copies of tone 5.
        "arp_custom_cc": [127, 111, 95, 79, 0, 0, 0, 0, 0],
        "arp_pedal_to_speed": False,
        "arp_pedal_full_range": False,
        "arp_live_retrigger": False,
        "pedal_targets": [],
    }
    PRESETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")
    # Seconds to confirm the erase-everything button with a second press.
    CLEAR_CONFIRM_WINDOW = 5.0

    @staticmethod
    def _open_nord_ports():
        # Cross-platform port lookup. Windows enumerates as
        # "Nord Electro 6 MIDI 0" / "...MIDI 1"; Linux/ALSA uses similar
        # substrings. Match by "Nord Electro" + the MIDI 0/1 suffix, with
        # a fallback to the first match if the suffix differs.
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()

        in_port = next((n for n in inputs if "Nord Electro" in n and "MIDI 0" in n), None)
        out_port = next((n for n in outputs if "Nord Electro" in n and "MIDI 1" in n), None)
        if in_port is None:
            in_port = next((n for n in inputs if "Nord Electro" in n), None)
        if out_port is None:
            out_port = next((n for n in outputs if "Nord Electro" in n), None)

        if in_port is None or out_port is None:
            raise RuntimeError(
                "Nord Electro MIDI ports not found.\n"
                f"Inputs:  {inputs}\nOutputs: {outputs}"
            )

        print(f"Using input:  {in_port}")
        print(f"Using output: {out_port}")
        return mido.open_input(in_port), mido.open_output(out_port)

    def __init__(self):
        print("Inputs:", mido.get_input_names())
        print("Outputs:", mido.get_output_names())

        self.inp, self.out = self._open_nord_ports()
        self.CHANNEL = 15

        # ----------------------------
        # SHUTDOWN STATE
        # ----------------------------
        self.stop_event = threading.Event()
        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()

        # ----------------------------
        # ACTIVE / PAUSED STATE (button 10)
        # ----------------------------
        self.active = True
        self._gpio_buttons = []          # Pi: keep button refs alive

        # ----------------------------
        # LED STATE
        # ----------------------------
        self._leds = {}                  # populated by _setup_inputs_gpio; empty on Windows

        # ----------------------------
        # PER-PATCH EFFECT SETTINGS
        # ----------------------------
        # Keyed "bank:program". Every program change applies the matching entry,
        # or the defaults when there isn't one — deliberately mirroring the way
        # the Nord itself resets its effects on a patch change, so there is one
        # mental model rather than two.
        self._patch_presets = {}         # "bank:program" -> captured dict
        self._clear_armed_at = 0.0       # two-press confirm for the wipe
        self._program_echo_until = 0.0   # ignore our own program change coming back
        self._preset_load_disk()

        # ----------------------------
        # SUSTAIN STATE
        # ----------------------------
        self.sustain_on = False
        self.sustain_held_notes = set()

        # ----------------------------
        # PAN STATE (CEFFECT_1)
        # ----------------------------
        self.pan_value = None          # latest knob/pedal input (0-127)
        self.last_sent_pan = None      # last CC 10 value emitted
        self.PAN_DT = 0.02             # 50 Hz output loop

        # ----------------------------
        # MODULATION (LFO → pitchwheel)
        # ----------------------------
        self.mod_on = False
        self.mod_frequency = 7.5
        self.mod_amplitude = 1200
        self.MOD_DT = 0.01

        # ----------------------------
        # ARP STATE (CEFFECT_4) — guarded by self.arp_lock
        # ----------------------------
        self.arp_on = False
        self.arp_lock = threading.Lock()
        self.arp_held_notes = {}              # note -> velocity
        self.arp_press_order = []             # notes in press order (Mimic)
        self.arp_bpm = 120.0
        self.arp_tap_times = deque(maxlen=4)
        self.arp_pattern_mode = 0
        self.arp_subdiv_idx = 1               # default 1/4
        # CC 16..24 last-seen values; mirrors BASE_PRESET (tones 1 2 3 4).
        self.arp_custom_cc = [127, 111, 95, 79, 0, 0, 0, 0, 0]
        self.arp_sustained = False
        self.arp_frozen_held = {}
        self.arp_frozen_press = []
        self.arp_pedal_to_speed = False
        self.arp_pedal_full_range = False
        self.arp_live_retrigger = False
        self.arp_cc27_on = False
        self.arp_cc30_state = 0
        self.arp_retrigger_event = threading.Event()
        self.arp_last_retrigger = 0.0

        # ----------------------------
        # SIDECHAIN STATE (CEFFECT_3) — guarded by self.sc_lock
        # ----------------------------
        self.sc_lock = threading.Lock()
        self.sc_floor = self.SC_FLOOR_DEFAULT
        self.sc_length = self.SC_LENGTH_DEFAULT
        self.sc_curve = self.SC_CURVE_DEFAULT
        self.sc_ceiling = self.SC_CEILING_DEFAULT
        self.sc_trigger_time = None      # None = idle, sitting at the ceiling
        self.sc_last_sent = self.SC_CEILING_DEFAULT
        self.sc_last_trigger = 0.0       # for SC_TRIGGER_DEBOUNCE
        self.sc_pedal_down = False       # edge state for the pedal
        self._sc_last_pedal_msg = 0.0    # for SC_DEBUG_PEDAL gap timing
        self.sc_pedal_up_time = 0.0      # last release, for SC_PEDAL_REARM

        # ----------------------------
        # EFFECT REGISTRY
        # ----------------------------
        self.effects = {
            "CEFFECT_1": False,   # pan
            "CEFFECT_2": False,   # modulation
            "CEFFECT_3": False,   # sidechain
            "CEFFECT_4": False,   # arpeggiator
            "CEFFECT_5": False,   # monobend
            "CEFFECT_6": False,   # harmonizer
        }

        # ----------------------------
        # TOGGLE INPUTS (shift-modified CC press)
        # ----------------------------
        self.cc_to_ceffect_toggle = {
            9:   "CEFFECT_4",   # arpeggiator
            82:  "CEFFECT_1",   # pan
            91:  "CEFFECT_2",   # modulation
            97:  "CEFFECT_6",   # harmonizer
            116: "CEFFECT_5",   # monobend
            118: "CEFFECT_3",   # sidechain (ctrl+118 stays the mod_amplitude
                                # pedal-target bind — different modifier)
        }

        # ----------------------------
        # KNOB INPUTS (continuous control)
        # ----------------------------
        self.cc_to_ceffect_knob = {
            102: "CEFFECT_1",   # controls pan value
            107: "CEFFECT_2",   # controls modulation frequency
        }

        self.SUSTAIN_CC = 64
        self.EXPRESSION_CC = 11

        # ----------------------------
        # MODIFIER KEYS (keyboard for now, pedalboard buttons later)
        # ----------------------------
        self.shift_held = False
        self.ctrl_held = False

        # CCs the Nord emits from its assignable buttons.
        # While shift/ctrl is held, any of these get bounced back with
        # the opposite value so the Nord's own effect is suppressed.
        self.ACTIVATION_CCS = {9, 82, 91, 97, 116, 118}

        # Echo protection: the Nord reflects our bounce back to our input.
        # Ignore any incoming message for the same CC within this window.
        self.BOUNCE_COOLDOWN = 0.2
        self.last_bounce_time = {}

        # ----------------------------
        # CONTROL PEDAL ROUTING (ctrl-modified CC press assigns target)
        # ----------------------------
        self.cc_to_pedal_target = {
            82:  "pan",
            91:  "mod_frequency",
            118: "mod_amplitude",
            116: "bend_speed",
        }
        # Multiple targets can ride the pedal at once. Ctrl+CC toggles membership.
        self.pedal_targets = set()

        # Default for monobend pitch-lerp speed (overridable via expression pedal)
        self.bend_speed = 2000

        # ----------------------------
        # MONOBEND STATE (CEFFECT_5)
        # ----------------------------
        self.mb_held_notes = []
        self.mb_sounding_note = None
        self.mb_pitch = 0
        self.mb_target = 0
        self.mb_last_sent = None      # last pitchwheel emitted; None forces a send
        self.MB_MAX_BEND = 8191
        # The Nord's own response to pitchwheel tops out at a whole tone, so
        # anything wider has to be reached by re-triggering rather than bending.
        self.MB_SEMITONE_RANGE = 2
        self.MB_DT = 0.01
        # After the last key comes up the wheel is left where it is, so the
        # release tail keeps its pitch. Once nothing has sounded for this long
        # the tail is gone and the wheel can safely snap back to centre.
        self.MB_RECENTRE_DELAY = 1.5
        self.mb_silent_since = None

        # ----------------------------
        # HARMONIZER STATE (CEFFECT_6)
        # ----------------------------
        self.harmonizer_interval = 0          # semitones, -12..+12
        self.harmonizer_active = {}           # played_note -> harmony_note currently sounding

        # ----------------------------
        # PROGRAM / OCTAVE / ENGINE / FX STATE
        # ----------------------------
        self.state = {"bank": 0, "program": 0, "octave": 0}

        self.octave_memory = {}

        self.engine_touch_memory = {}
        self.engine_toggle_state = {}

        self.fx_touch_memory = {}
        self.fx_toggle_state = {}

        self.OCTAVE_CCS = [12, 35, 44]

        self.ENGINE_CCS = {
            "organ": 9,
            "piano": 33,
            "synth": 42,
        }

        self.EFFECT_CCS = {
            "fx1": 82,
            "fx2": 91,
            "fx3": 118,
        }

    # ----------------------------
    # START
    # ----------------------------
    def start(self):
        threading.Thread(target=self._midi_loop, daemon=True).start()
        threading.Thread(target=self._pan_loop, daemon=True).start()
        threading.Thread(target=self._lfo_loop, daemon=True).start()
        threading.Thread(target=self._mb_loop, daemon=True).start()
        threading.Thread(target=self._arp_loop, daemon=True).start()
        threading.Thread(target=self._sidechain_loop, daemon=True).start()

        self._setup_inputs()

        print("[Nord6] Running")

    # ----------------------------
    # INPUT BINDINGS (cross-platform)
    # ----------------------------
    def _setup_inputs(self):
        if sys.platform == "win32":
            self._setup_inputs_keyboard()
        elif sys.platform.startswith("linux"):
            self._setup_inputs_gpio()
        else:
            print(f"[Nord6] No input setup for platform {sys.platform}; running headless.")

    def _setup_inputs_keyboard(self):
        # Windows dev/test bindings. Pi uses GPIO instead.
        import keyboard

        # wasd: octave/program, matching Pi buttons 4/5/6/7. Shift+w erases all
        # stored patch settings, shift+s saves the current patch.
        keyboard.on_press_key("w", lambda _: self._shift_alt(self.octave_up,   self._patch_clear_all))
        keyboard.on_press_key("a", lambda _: self._if_active(self.prev_program))
        keyboard.on_press_key("s", lambda _: self._shift_alt(self.octave_down, self._patch_save_current))
        keyboard.on_press_key("d", lambda _: self._if_active(self.next_program))

        keyboard.on_press_key("1", lambda _: self._if_active(lambda: self.toggle_engine("organ")))
        keyboard.on_press_key("2", lambda _: self._if_active(lambda: self.toggle_engine("piano")))
        keyboard.on_press_key("3", lambda _: self._if_active(lambda: self.toggle_engine("synth")))

        keyboard.on_press_key("4", lambda _: self._if_active(lambda: self.toggle_effect("fx1")))
        keyboard.on_press_key("5", lambda _: self._if_active(lambda: self.toggle_effect("fx2")))
        keyboard.on_press_key("6", lambda _: self._if_active(lambda: self.toggle_effect("fx3")))

        # Modifier keys
        keyboard.on_press_key("n",   lambda _: self._set_shift(True))
        keyboard.on_release_key("n", lambda _: self._set_shift(False))
        keyboard.on_press_key("m",   lambda _: self._set_ctrl(True))
        keyboard.on_release_key("m", lambda _: self._set_ctrl(False))

        # Active toggle (mirrors Pi button 10's short-press)
        keyboard.on_press_key("p", lambda _: self._toggle_active())

        keyboard.on_press_key("esc", lambda _: self._request_shutdown())

    def _setup_inputs_gpio(self):
        # 10-button pedalboard on the Pi. Each button is wired to GND
        # with the internal pull-up enabled. BCM pin numbers below.
        try:
            from gpiozero import Button, LED
        except ImportError:
            print("[Nord6] gpiozero not installed. Run: pip install gpiozero")
            return

        PINS = {
            "engine_organ": 17,   # button 1
            "engine_piano": 27,   # button 2
            "engine_synth": 22,   # button 3
            "octave_up":     5,   # button 4
            "program_prev":  6,   # button 5
            "octave_down":  13,   # button 6
            "program_next": 19,   # button 7
            "ctrl":         26,   # button 8
            "shift":        16,   # button 9
            # BCM 12 (button 10 / latching power switch) is owned by
            # nord6-switch.service — see nord6_switch_watcher.py.
        }
        BOUNCE = 0.05

        # Buttons 1/2/3: shift swaps engine toggle for a Nord FX toggle.
        b1 = Button(PINS["engine_organ"], pull_up=True, bounce_time=BOUNCE)
        b1.when_pressed = lambda: self._shift_alt(
            lambda: self.toggle_engine("organ"),
            lambda: self.toggle_effect("fx2"))
        self._gpio_buttons.append(b1)

        b2 = Button(PINS["engine_piano"], pull_up=True, bounce_time=BOUNCE)
        b2.when_pressed = lambda: self._shift_alt(
            lambda: self.toggle_engine("piano"),
            lambda: self.toggle_effect("fx3"))
        self._gpio_buttons.append(b2)

        b3 = Button(PINS["engine_synth"], pull_up=True, bounce_time=BOUNCE)
        b3.when_pressed = lambda: self._shift_alt(
            lambda: self.toggle_engine("synth"),
            lambda: self.toggle_effect("fx1"))
        self._gpio_buttons.append(b3)

        # Buttons 4-7: octave / program nav. Shift on the two octave buttons
        # saves this patch's effects, and erases every stored patch. Program
        # nav has no shift alternate — loading is automatic on patch change.
        b4 = Button(PINS["octave_up"], pull_up=True, bounce_time=BOUNCE)
        b4.when_pressed = lambda: self._shift_alt(self.octave_up, self._patch_clear_all)
        self._gpio_buttons.append(b4)

        b5 = Button(PINS["program_prev"], pull_up=True, bounce_time=BOUNCE)
        b5.when_pressed = lambda: self._if_active(self.prev_program)
        self._gpio_buttons.append(b5)

        b6 = Button(PINS["octave_down"], pull_up=True, bounce_time=BOUNCE)
        b6.when_pressed = lambda: self._shift_alt(self.octave_down, self._patch_save_current)
        self._gpio_buttons.append(b6)

        b7 = Button(PINS["program_next"], pull_up=True, bounce_time=BOUNCE)
        b7.when_pressed = lambda: self._if_active(self.next_program)
        self._gpio_buttons.append(b7)

        # Buttons 8/9: ctrl/shift modifiers. Always tracked, even when
        # paused, so the held state is correct on resume.
        b8 = Button(PINS["ctrl"], pull_up=True, bounce_time=BOUNCE)
        b8.when_pressed  = lambda: self._set_ctrl(True)
        b8.when_released = lambda: self._set_ctrl(False)
        self._gpio_buttons.append(b8)

        b9 = Button(PINS["shift"], pull_up=True, bounce_time=BOUNCE)
        b9.when_pressed  = lambda: self._set_shift(True)
        b9.when_released = lambda: self._set_shift(False)
        self._gpio_buttons.append(b9)

        print(f"[Nord6] GPIO buttons configured: {sorted(PINS.values())}")

        # LED setup
        # Only six LEDs are physically wired. Sidechain gets the one that used
        # to show modulation, that being the least-used effect; "mod" now
        # points at BCM 23, which has nothing attached. Wire an LED there and
        # modulation lights up again with no code change — _update_effect_leds
        # already drives every entry, and _leds.get() makes an absent pin a
        # no-op rather than an error.
        LED_PINS = {
            "pan":   4,
            "sc":    18,   # was mod
            "arp":   24,
            "mb":    20,
            "harm":  21,
            "mod":   23,   # unwired spare
            "power": 25,
        }
        for name, pin in LED_PINS.items():
            led = LED(pin)
            led.off()
            self._leds[name] = led

        # Power LED on immediately. If Nord6 is running at all, the
        # latching switch must be in the "on" position (the watcher
        # service only starts us when it's flipped on).
        self._leds["power"].on()
        print(f"[Nord6] LEDs configured on pins: {LED_PINS}")

    def _if_active(self, fn):
        if self.active:
            fn()

    # ----------------------------
    # MIDI INPUT
    # ----------------------------
    def _midi_loop(self):
        try:
            for msg in self.inp:

                # When paused, drain incoming MIDI silently — output threads
                # also idle, so the synth and the program both stay quiet.
                if not self.active:
                    continue

                if msg.type == "control_change" and msg.control == 32:
                    self.state["bank"] = msg.value
                    continue

                if msg.type == "program_change":
                    self.state["program"] = msg.program
                    print(f"⬅ SYNC -> Bank {self.state['bank']} | Program {self.state['program']}")
                    # _send_program already applied settings for a change we
                    # initiated; this is that message coming back to us.
                    if time.time() >= self._program_echo_until:
                        self._patch_apply_current()
                    continue

                if msg.type == "control_change":
                    if msg.control == self.SUSTAIN_CC:
                        self._handle_sustain(msg.value, getattr(msg, "channel", None))
                    elif msg.control == self.EXPRESSION_CC:
                        self._handle_expression(msg.value)
                    else:
                        self._handle_cc(msg.control, msg.value)

                elif msg.type == "note_on" and msg.velocity > 0:
                    # When monobend is on it drives the harmony itself
                    # (one sounding voice that bends or switches), so the
                    # per-keypress harmony hook must not also fire.
                    if self.effects["CEFFECT_5"]:
                        self._mb_note_on(msg.note)
                    else:
                        self._arp_note_on(msg.note, msg.velocity)
                        if self.effects["CEFFECT_6"]:
                            self._harmonizer_note_on(msg.note, msg.velocity)

                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    if self.effects["CEFFECT_5"]:
                        self._mb_note_off(msg.note)
                    else:
                        self._arp_note_off(msg.note)
                        if self.effects["CEFFECT_6"]:
                            self._harmonizer_note_off(msg.note)
        except Exception as e:
            if not self.stop_event.is_set():
                print(f"[MIDI] loop error: {e}")

    # ----------------------------
    # SUSTAIN PEDAL
    # ----------------------------
    def _handle_sustain(self, value, channel=None):
        # Sidechain claims the pedal outright: a press fires one duck, and the
        # release does nothing. No note sustain, no arp chord-freeze — holding
        # the pedal down must not leave notes or a frozen chord stranded.
        if self.effects["CEFFECT_3"]:
            if self.SC_DEBUG_PEDAL:
                now = time.time()
                gap = (now - self._sc_last_pedal_msg) * 1000 if self._sc_last_pedal_msg else 0.0
                self._sc_last_pedal_msg = now
                print(f"[PEDAL] CC64={value:3d} ch={channel} +{gap:6.0f}ms "
                      f"down={self.sc_pedal_down}")
            if not self.sc_pedal_down and value >= self.SC_PEDAL_ON:
                # Track the position either way, so holding the pedal still
                # gates the knobs even when the duck itself is suppressed.
                self.sc_pedal_down = True
                since_release = time.time() - self.sc_pedal_up_time
                if since_release >= self.SC_PEDAL_REARM:
                    self._sc_trigger(channel)
                else:
                    print(f"[SIDECHAIN] contact bounce ignored "
                          f"({since_release * 1000:.0f}ms after release)")
            elif self.sc_pedal_down and value <= self.SC_PEDAL_OFF:
                self.sc_pedal_down = False
                self.sc_pedal_up_time = time.time()
            return

        self.sustain_on = value >= 64

        if not self.sustain_on:
            for n in self.sustain_held_notes:
                self.out.send(mido.Message(
                    "note_off", note=n, velocity=0, channel=self.CHANNEL
                ))
            self.sustain_held_notes.clear()

        # Also drive the arp's chord-freeze snapshot. Harmless when arp is off.
        self._arp_set_sustain(self.sustain_on)

    # ----------------------------
    # EXPRESSION PEDAL (routes to current pedal_target)
    # ----------------------------
    def _handle_expression(self, value):
        # Arp's pedal→bpm is its own flag, not in self.pedal_targets, so it
        # runs independently of the ctrl-bind mechanism.
        if self.arp_pedal_to_speed:
            with self.arp_lock:
                self.arp_bpm = self._arp_bpm_from_pedal(value)
                bpm = self.arp_bpm
            print(f"[PEDAL→BPM] {round(bpm, 1)}")

        if not self.pedal_targets:
            return

        if "pan" in self.pedal_targets:
            self.pan_value = value
            print(f"[PEDAL→PAN] {value}")

        if "mod_frequency" in self.pedal_targets:
            self.mod_frequency = 0.1 + (value / 127) * (15.0 - 0.1)
            print(f"[PEDAL→MOD FREQ] {round(self.mod_frequency, 2)} Hz")

        if "mod_amplitude" in self.pedal_targets:
            self.mod_amplitude = int((value / 127) * 8191)
            print(f"[PEDAL→MOD AMP] {self.mod_amplitude}")

        if "bend_speed" in self.pedal_targets:
            self.bend_speed = int(1000 + (value / 127) * (3000 - 1000))
            print(f"[PEDAL→BEND SPEED] {self.bend_speed}")

    # ----------------------------
    # CC HANDLER (knobs + shift/ctrl-gated toggles)
    # ----------------------------
    def _handle_cc(self, cc, value):

        # Harmonizer hijacks the shared speed dial (CC104) when active —
        # must run before _handle_arp_cc, which would otherwise eat CC104.
        if cc == 104 and self.effects["CEFFECT_6"]:
            semitones = round((value / 127.0) * 24) - 12
            self.harmonizer_interval = semitones
            print(f"[HARMONIZER] interval = {semitones:+d} semitones")
            return

        # Sidechain claims its three knobs ONLY while the sustain pedal is held
        # down, so pan (102), mod amplitude (103) and mod frequency (107) keep
        # working normally even with CEFFECT_3 on. Holding the pedal costs one
        # duck as it goes down and then sits still — edge detection means it
        # does not keep firing — which makes it a usable modifier. Must run
        # before _handle_arp_cc and before the CC103 mod-amplitude path below.
        if self.sc_pedal_down and self.effects["CEFFECT_3"]:
            if cc == self.SC_KNOB_FLOOR:
                with self.sc_lock:
                    self.sc_floor = min(value, self.sc_ceiling)
                    floor = self.sc_floor
                print(f"[SIDECHAIN] floor = {floor}")
                return
            if cc == self.SC_KNOB_LENGTH:
                with self.sc_lock:
                    self.sc_length = self.SC_LENGTH_MIN + (value / 127) * (
                        self.SC_LENGTH_MAX - self.SC_LENGTH_MIN)
                    length = self.sc_length
                print(f"[SIDECHAIN] length = {round(length, 3)}s")
                return
            if cc == self.SC_KNOB_CURVE:
                with self.sc_lock:
                    self.sc_curve = self.SC_CURVE_MIN + (value / 127) * (
                        self.SC_CURVE_MAX - self.SC_CURVE_MIN)
                    curve = self.sc_curve
                print(f"[SIDECHAIN] curve = {round(curve, 2)}")
                return

        # Arp-specific CCs (tempo knob, pattern, subdivision, tap, custom slots,
        # octavizer, etc.) are consumed before the generic Nord6 paths.
        if self._handle_arp_cc(cc, value):
            return

        # CC103 → modulation amplitude (0-127 → 0-8191), independent of any
        # effect slot since amplitude lives directly on self.mod_amplitude.
        if cc == 103:
            self.mod_amplitude = int((value / 127) * 8191)
            print(f"[MOD] AMP = {self.mod_amplitude}")
            return

        # Knob: real-time value update
        if cc in self.cc_to_ceffect_knob:
            effect = self.cc_to_ceffect_knob[cc]

            # CEFFECT_1 knob maps to pan value (inversion applied at output)
            if effect == "CEFFECT_1":
                self.pan_value = value
                print(f"[PAN] VALUE = {value}")

            # CEFFECT_2 knob maps to modulation frequency (0-127 → 0.1-15 Hz)
            elif effect == "CEFFECT_2":
                self.mod_frequency = 0.1 + (value / 127) * (15.0 - 0.1)
                print(f"[MOD] FREQ = {round(self.mod_frequency, 2)} Hz")

            return

        # Skip our own bounce echoed back through the Nord
        if cc in self.ACTIVATION_CCS:
            if time.time() - self.last_bounce_time.get(cc, 0) < self.BOUNCE_COOLDOWN:
                return

        # Shift-modified activation: bounce inverse, then toggle custom effect.
        # Nord sends one CC per press (alternating 127/0), so every press fires.
        if self.shift_held and cc in self.ACTIVATION_CCS:
            opposite = 0 if value == 127 else 127
            self.out.send(mido.Message(
                "control_change", channel=0, control=cc, value=opposite
            ))
            self.last_bounce_time[cc] = time.time()

            effect = self.cc_to_ceffect_toggle.get(cc)
            if effect is not None:
                self._toggle_effect(effect)
            return

        # Ctrl-modified activation: bounce inverse, assign control pedal target.
        if self.ctrl_held and cc in self.ACTIVATION_CCS:
            opposite = 0 if value == 127 else 127
            self.out.send(mido.Message(
                "control_change", channel=0, control=cc, value=opposite
            ))
            self.last_bounce_time[cc] = time.time()

            target = self.cc_to_pedal_target.get(cc)
            if target is not None:
                if target in self.pedal_targets:
                    self.pedal_targets.discard(target)
                    print(f"[PEDAL] unbound from {target}  (active: {sorted(self.pedal_targets) or 'none'})")
                else:
                    self.pedal_targets.add(target)
                    print(f"[PEDAL] bound to {target}  (active: {sorted(self.pedal_targets)})")
            return

        # Unmodified activation CCs: ignore (matches prior behavior)

    # ----------------------------
    # EFFECT TOGGLE (driven by shift+CC)
    # ----------------------------
    def _toggle_effect(self, effect):
        new_state = not self.effects[effect]
        self.effects[effect] = new_state

        print(f"[EFFECT] {effect} {'ENABLED' if new_state else 'DISABLED'}")

        # Arp and harmonizer cannot coexist — both intercept note events.
        if new_state and effect == "CEFFECT_4" and self.effects["CEFFECT_6"]:
            self.effects["CEFFECT_6"] = False
            self._set_harmonizer(False)
            print("[EFFECT] CEFFECT_6 DISABLED (arp took over)")
        elif new_state and effect == "CEFFECT_6" and self.effects["CEFFECT_4"]:
            self.effects["CEFFECT_4"] = False
            self._set_arp(False)
            print("[EFFECT] CEFFECT_4 DISABLED (harmonizer took over)")

        if effect == "CEFFECT_1":
            self._set_pan(new_state)
        elif effect == "CEFFECT_2":
            self._set_mod(new_state)
        elif effect == "CEFFECT_3":
            self._set_sidechain(new_state)
        elif effect == "CEFFECT_4":
            self._set_arp(new_state)
        elif effect == "CEFFECT_5":
            self._set_monobend(new_state)
        elif effect == "CEFFECT_6":
            self._set_harmonizer(new_state)

        self._update_effect_leds()

    # ----------------------------
    # EFFECT STATE SETTER (no-toggle variant — used by preset apply)
    # ----------------------------
    def _set_effect_state(self, effect, new_state):
        if self.effects[effect] == new_state:
            return
        self.effects[effect] = new_state
        if   effect == "CEFFECT_1": self._set_pan(new_state)
        elif effect == "CEFFECT_2": self._set_mod(new_state)
        elif effect == "CEFFECT_3": self._set_sidechain(new_state)
        elif effect == "CEFFECT_4": self._set_arp(new_state)
        elif effect == "CEFFECT_5": self._set_monobend(new_state)
        elif effect == "CEFFECT_6": self._set_harmonizer(new_state)
        self._update_effect_leds()

    # ----------------------------
    # SHIFT-ALTERNATE BUTTON DISPATCH
    # ----------------------------
    def _shift_alt(self, default_action, shift_action):
        # Gated on `active` so all preset/engine/program buttons go quiet
        # while the program is paused (button 10 OFF).
        if not self.active:
            return
        (shift_action if self.shift_held else default_action)()

    # ----------------------------
    # LED CONTROL
    # ----------------------------
    def _update_effect_leds(self):
        mapping = {
            "CEFFECT_1": "pan",
            "CEFFECT_2": "mod",
            "CEFFECT_3": "sc",
            "CEFFECT_4": "arp",
            "CEFFECT_5": "mb",
            "CEFFECT_6": "harm",
        }
        for effect, led_name in mapping.items():
            led = self._leds.get(led_name)
            if led:
                led.on() if self.effects[effect] else led.off()

    # ----------------------------
    # PRESETS — capture / apply
    # ----------------------------
    def _preset_capture(self):
        return {
            "effects": {ef: self.effects[ef] for ef in
                        ("CEFFECT_1", "CEFFECT_2", "CEFFECT_3",
                         "CEFFECT_4", "CEFFECT_5", "CEFFECT_6")},
            "pan_value": self.pan_value,
            "sc_floor": self.sc_floor,
            "sc_length": self.sc_length,
            "sc_curve": self.sc_curve,
            "sc_ceiling": self.sc_ceiling,
            "mod_frequency": self.mod_frequency,
            "mod_amplitude": self.mod_amplitude,
            "harmonizer_interval": self.harmonizer_interval,
            "bend_speed": self.bend_speed,
            "arp_bpm": self.arp_bpm,
            "arp_subdiv_idx": self.arp_subdiv_idx,
            "arp_pattern_mode": self.arp_pattern_mode,
            "arp_cc27_on": self.arp_cc27_on,
            "arp_cc30_state": self.arp_cc30_state,
            "arp_custom_cc": list(self.arp_custom_cc),
            "arp_pedal_to_speed": self.arp_pedal_to_speed,
            "arp_pedal_full_range": self.arp_pedal_full_range,
            "arp_live_retrigger": self.arp_live_retrigger,
            "pedal_targets": sorted(self.pedal_targets),
        }

    def _preset_apply(self, p):
        # Apply parameters first so transitions to "on" pick up correct values.
        self.pan_value = p["pan_value"]
        self.mod_frequency = p["mod_frequency"]
        self.mod_amplitude = p["mod_amplitude"]
        self.harmonizer_interval = p["harmonizer_interval"]
        self.bend_speed = p["bend_speed"]
        with self.arp_lock:
            self.arp_bpm = p["arp_bpm"]
            self.arp_subdiv_idx = p["arp_subdiv_idx"]
            self.arp_pattern_mode = p["arp_pattern_mode"]
            self.arp_cc30_state = p["arp_cc30_state"]
            self.arp_custom_cc = list(p["arp_custom_cc"])
        self.arp_cc27_on = p["arp_cc27_on"]
        self.arp_pedal_to_speed = p["arp_pedal_to_speed"]
        self.arp_pedal_full_range = p["arp_pedal_full_range"]
        self.arp_live_retrigger = p["arp_live_retrigger"]
        self.pedal_targets = set(p["pedal_targets"])

        # Sidechain keys read via .get: presets saved before CEFFECT_3 existed
        # do not carry them, and a bare p["key"] would KeyError and brick the
        # slot. Same reason target.get(...) is used for the effect below.
        with self.sc_lock:
            self.sc_floor = p.get("sc_floor", self.SC_FLOOR_DEFAULT)
            self.sc_length = p.get("sc_length", self.SC_LENGTH_DEFAULT)
            self.sc_curve = p.get("sc_curve", self.SC_CURVE_DEFAULT)
            self.sc_ceiling = p.get("sc_ceiling", self.SC_CEILING_DEFAULT)

        # Effect on/off transitions: force-off first then on, so cleanup
        # side-effects run before any new effect arms.
        target = p["effects"]
        for ef in ("CEFFECT_4", "CEFFECT_6", "CEFFECT_1", "CEFFECT_2",
                   "CEFFECT_3", "CEFFECT_5"):
            if self.effects[ef] and not target.get(ef, False):
                self._set_effect_state(ef, False)
        for ef in ("CEFFECT_1", "CEFFECT_2", "CEFFECT_3",
                   "CEFFECT_4", "CEFFECT_5", "CEFFECT_6"):
            if not self.effects[ef] and target.get(ef, False):
                self._set_effect_state(ef, True)

        # Defensive mutex: if a hand-edited preset has both arp and
        # harmonizer on, harmonizer wins.
        if self.effects["CEFFECT_4"] and self.effects["CEFFECT_6"]:
            self._set_effect_state("CEFFECT_4", False)
            print("[PRESET] mutex enforced: arp disabled (preset had both on)")

    # ----------------------------
    # PRESETS — disk I/O
    # ----------------------------
    def _preset_load_disk(self):
        try:
            with open(self.PRESETS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = {}
        except Exception as e:
            print(f"[PRESET] failed to read {self.PRESETS_PATH}: {e}")
            raw = {}

        if isinstance(raw, dict) and "patches" in raw:
            self._patch_presets = raw["patches"]
            print(f"[PRESET] loaded settings for {len(self._patch_presets)} patch(es)")
        else:
            # A file from the old numbered-slot system. Slots are gone; start
            # clean rather than guessing which patch a slot belonged to.
            if raw:
                print("[PRESET] ignoring old slot-based presets file")
            self._patch_presets = {}

    def _preset_write_disk(self):
        # Write to a temp file in the same directory, fsync, then os.replace —
        # which is atomic. The Pi loses power abruptly at a latching switch, and
        # a plain open(...,"w") truncates first, so a cut mid-write would leave
        # an empty file and take every saved setting with it.
        tmp = self.PRESETS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "patches": self._patch_presets}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.PRESETS_PATH)
        except Exception as e:
            print(f"[PRESET] failed to write {self.PRESETS_PATH}: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ----------------------------
    # PRESETS — user-facing actions
    # ----------------------------
    def _patch_key(self):
        return f"{self.state['bank']}:{self.state['program']}"

    def _patch_apply_current(self):
        """Apply the current patch's saved effects, or the defaults.

        Unsaved patches deliberately get the defaults — effects off — matching
        how the Nord resets its own effects on a patch change. Forgetting to
        save means redoing them; that is the accepted trade for consistency.
        """
        key = self._patch_key()
        p = self._patch_presets.get(key)
        if p is None:
            self._preset_apply(self.BASE_PRESET)
            print(f"[PATCH] {key} — no saved settings, defaults applied")
        else:
            self._preset_apply(p)
            print(f"[PATCH] {key} — settings applied")

    def _patch_save_current(self):
        key = self._patch_key()
        self._patch_presets[key] = self._preset_capture()
        self._preset_write_disk()
        print(f"[PATCH] {key} — settings saved ({len(self._patch_presets)} stored)")

    def _patch_clear_all(self):
        """Erase every stored patch setting. Two presses to confirm.

        One stray press on a foot controller would otherwise wipe every song's
        setup with no way back, so the first press only arms it, and the
        previous file is kept as .bak even after the second.
        """
        now = time.time()
        if now - self._clear_armed_at > self.CLEAR_CONFIRM_WINDOW:
            self._clear_armed_at = now
            print(f"[PATCH] press again within {self.CLEAR_CONFIRM_WINDOW:.0f}s "
                  f"to ERASE all {len(self._patch_presets)} stored patch settings")
            return

        self._clear_armed_at = 0.0
        try:
            if os.path.exists(self.PRESETS_PATH):
                os.replace(self.PRESETS_PATH, self.PRESETS_PATH + ".bak")
        except Exception as e:
            print(f"[PATCH] backup failed, clearing anyway: {e}")
        self._patch_presets = {}
        self._preset_write_disk()
        print("[PATCH] all settings erased (previous file kept as presets.json.bak)")

    # ----------------------------
    # PAN ON/OFF
    # ----------------------------
    def _set_pan(self, state):
        if not state:
            self.out.send(mido.Message(
                "control_change", control=10, value=64, channel=self.CHANNEL
            ))
            self.last_sent_pan = 64
        else:
            # Force the loop to re-emit the current knob value on next tick
            self.last_sent_pan = None

        print(f"[PAN] {'ON' if state else 'OFF'}")

    # ----------------------------
    # MODULATION ON/OFF
    # ----------------------------
    def _set_mod(self, state):
        self.mod_on = state

        if not self.mod_on:
            self.out.send(mido.Message(
                "pitchwheel", channel=self.CHANNEL, pitch=0
            ))

        print(f"[MOD] {'ON' if self.mod_on else 'OFF'}")

    # ----------------------------
    # MONOBEND ON/OFF
    # ----------------------------
    def _set_monobend(self, state):
        if not state:
            if self.mb_sounding_note is not None:
                self.out.send(mido.Message(
                    "note_off", note=self.mb_sounding_note, velocity=0, channel=self.CHANNEL
                ))
            self.mb_held_notes = []
            self.mb_sounding_note = None
            self.mb_pitch = 0
            self.mb_target = 0
            self.out.send(mido.Message(
                "pitchwheel", channel=self.CHANNEL, pitch=0
            ))
            self.mb_last_sent = 0
            self.mb_silent_since = None
        else:
            # The LFO writes the same pitchwheel while monobend is off, so what
            # we last sent tells us nothing about where the wheel now sits.
            # None forces the first emission rather than trusting a stale value.
            self.mb_last_sent = None

        print(f"[MONOBEND] {'ON' if state else 'OFF'}")

    # ----------------------------
    # HARMONIZER ON/OFF
    # ----------------------------
    def _set_harmonizer(self, state):
        if not state:
            for harmony in list(self.harmonizer_active.values()):
                self.out.send(mido.Message(
                    "note_off", note=harmony, velocity=0, channel=self.CHANNEL
                ))
            self.harmonizer_active.clear()
        print(f"[HARMONIZER] {'ON' if state else 'OFF'} (interval={self.harmonizer_interval:+d})")

    # ----------------------------
    # HARMONIZER NOTE HANDLERS
    # ----------------------------
    def _harmonizer_note_on(self, played_note, velocity):
        interval = self.harmonizer_interval
        if interval == 0:
            return

        harmony = played_note + interval
        if not (0 <= harmony <= 127):
            return

        # Mirror the sustain-emulator retrigger pattern from _arp_note_on:
        # if this exact pitch is being held by sustain, flush it first.
        if harmony in self.sustain_held_notes:
            self.out.send(mido.Message(
                "note_off", note=harmony, velocity=0, channel=self.CHANNEL
            ))
            self.sustain_held_notes.discard(harmony)

        self.out.send(mido.Message(
            "note_on", note=harmony, velocity=velocity, channel=self.CHANNEL
        ))
        self.harmonizer_active[played_note] = harmony

    def _harmonizer_note_off(self, played_note):
        harmony = self.harmonizer_active.pop(played_note, None)
        if harmony is None:
            return

        if self.sustain_on:
            self.sustain_held_notes.add(harmony)
        else:
            self.out.send(mido.Message(
                "note_off", note=harmony, velocity=0, channel=self.CHANNEL
            ))

    # ----------------------------
    # MONOBEND-DRIVEN HARMONY (one voice that bends with the mb voice;
    # sustain is bypassed because monobend itself ignores sustain)
    # ----------------------------
    def _harm_attach_voice(self, played_note):
        if not self.effects["CEFFECT_6"]:
            return
        interval = self.harmonizer_interval
        if interval == 0:
            return
        harmony = played_note + interval
        if not (0 <= harmony <= 127):
            return
        self.out.send(mido.Message(
            "note_on", note=harmony, velocity=100, channel=self.CHANNEL
        ))
        self.harmonizer_active[played_note] = harmony

    def _harm_release_voice(self, played_note):
        harmony = self.harmonizer_active.pop(played_note, None)
        if harmony is None:
            return
        self.out.send(mido.Message(
            "note_off", note=harmony, velocity=0, channel=self.CHANNEL
        ))

    # ----------------------------
    # MONOBEND NOTE HANDLERS
    # ----------------------------
    def _mb_note_on(self, note):
        if note not in self.mb_held_notes:
            self.mb_held_notes.append(note)

        if self.mb_sounding_note is None:
            self.mb_silent_since = None
            if int(self.mb_pitch) != 0:
                # The previous phrase left the wheel deflected and its release
                # tail may still be ringing. Reanchor so the wheel stays put.
                self._mb_reanchor(note)
                return
            self.out.send(mido.Message(
                "note_on", note=note, velocity=100, channel=self.CHANNEL
            ))
            self.mb_sounding_note = note
            self.mb_target = 0
            self.mb_pitch = 0
            self._harm_attach_voice(note)
            return

        diff = note - self.mb_sounding_note
        if abs(diff) <= self.MB_SEMITONE_RANGE:
            self.mb_target = int((diff / self.MB_SEMITONE_RANGE) * self.MB_MAX_BEND)
        else:
            self._mb_reanchor(note)

    def _mb_note_off(self, note):
        if note in self.mb_held_notes:
            self.mb_held_notes.remove(note)

        if not self.mb_held_notes:
            if self.mb_sounding_note is not None:
                self._harm_release_voice(self.mb_sounding_note)
                self.out.send(mido.Message(
                    "note_off", note=self.mb_sounding_note, velocity=0, channel=self.CHANNEL
                ))
            self.mb_sounding_note = None
            # Freeze the wheel rather than gliding it back to centre. The note
            # just released is still in its release tail, and the wheel is
            # channel-wide, so returning to 0 would drag that dying voice down
            # with it — heard as the pitch sagging after the key comes up.
            self.mb_target = int(self.mb_pitch)
            self.mb_silent_since = time.time()
            return

        newest = self.mb_held_notes[-1]
        diff = newest - self.mb_sounding_note

        if abs(diff) <= self.MB_SEMITONE_RANGE:
            self.mb_target = int((diff / self.MB_SEMITONE_RANGE) * self.MB_MAX_BEND)
        else:
            self._mb_reanchor(newest)

    # ----------------------------
    # MONOBEND REANCHOR
    # ----------------------------
    def _mb_reanchor(self, want):
        """Move the voice to `want` without moving the pitchwheel.

        The wheel is channel-wide, so recentring it drags every voice still
        sounding — including the one we just released, which is still in its
        release tail. That made a run like C-D-E pull the dying C voice back
        down from D to C, heard as the original note retriggering.

        Instead the wheel stays put and we trigger a note offset by whatever
        bend is currently applied: wheel at +2 semitones and E wanted, so play
        D. Nothing already sounding moves. The reachable window then follows
        the music rather than staying anchored to the first note, so an
        ascending run keeps climbing and the way back down is pure bending.
        """
        bend_semis = (self.mb_pitch / self.MB_MAX_BEND) * self.MB_SEMITONE_RANGE
        base = int(round(want - bend_semis))
        base = max(0, min(127, base))
        residual = want - base
        new_pitch = max(-self.MB_MAX_BEND, min(self.MB_MAX_BEND,
                        residual / self.MB_SEMITONE_RANGE * self.MB_MAX_BEND))

        if self.mb_sounding_note is not None:
            self._harm_release_voice(self.mb_sounding_note)
            self.out.send(mido.Message(
                "note_off", note=self.mb_sounding_note, velocity=0, channel=self.CHANNEL
            ))

        # Usually a no-op: with the wheel parked at full deflection the offset
        # is a whole number of semitones and nothing has to move at all.
        if int(new_pitch) != self.mb_last_sent:
            self.out.send(mido.Message(
                "pitchwheel", channel=self.CHANNEL, pitch=int(new_pitch)
            ))
            self.mb_last_sent = int(new_pitch)

        self.mb_pitch = float(new_pitch)
        self.mb_target = int(new_pitch)
        self.out.send(mido.Message(
            "note_on", note=base, velocity=100, channel=self.CHANNEL
        ))
        self.mb_sounding_note = base
        self._harm_attach_voice(base)

    # ----------------------------
    # MONOBEND PITCH LERP LOOP
    # ----------------------------
    def _mb_loop(self):
        while not self.stop_event.is_set():
            if self.active and self.effects["CEFFECT_5"]:
                if (self.mb_sounding_note is None
                        and self.mb_silent_since is not None
                        and time.time() - self.mb_silent_since >= self.MB_RECENTRE_DELAY):
                    # Nothing has sounded for long enough that the release tail
                    # is gone; snap rather than glide, so the next phrase starts
                    # with the full bend range in both directions.
                    self.mb_pitch = 0.0
                    self.mb_target = 0
                    self.mb_silent_since = None
                elif self.mb_pitch < self.mb_target:
                    self.mb_pitch += self.bend_speed
                    if self.mb_pitch > self.mb_target:
                        self.mb_pitch = self.mb_target
                elif self.mb_pitch > self.mb_target:
                    self.mb_pitch -= self.bend_speed
                    if self.mb_pitch < self.mb_target:
                        self.mb_pitch = self.mb_target

                # Emit only on change. This used to send unconditionally —
                # 100 messages a second down a shared port even while the
                # pitch sat still at its target.
                value = int(self.mb_pitch)
                if value != self.mb_last_sent:
                    self.out.send(mido.Message(
                        "pitchwheel", channel=self.CHANNEL, pitch=value
                    ))
                    self.mb_last_sent = value

            time.sleep(self.MB_DT)

    # ----------------------------
    # LFO LOOP (pitchwheel modulation)
    # ----------------------------
    def _lfo_loop(self):
        phase = 0.0

        while not self.stop_event.is_set():
            if self.active and self.mod_on and not self.effects["CEFFECT_5"]:
                phase += 2 * math.pi * self.mod_frequency * self.MOD_DT

                if phase > 2 * math.pi:
                    phase -= 2 * math.pi

                value = int(math.sin(phase) * self.mod_amplitude)

                self.out.send(mido.Message(
                    "pitchwheel", channel=self.CHANNEL, pitch=value
                ))

            time.sleep(self.MOD_DT)

    # ----------------------------
    # PAN OUTPUT LOOP (50 Hz)
    # ----------------------------
    def _pan_loop(self):
        while not self.stop_event.is_set():
            if self.active and self.effects["CEFFECT_1"] and self.pan_value is not None:
                pan_out = 127 - self.pan_value

                if pan_out != self.last_sent_pan:
                    self.out.send(mido.Message(
                        "control_change", control=10, value=pan_out, channel=self.CHANNEL
                    ))
                    self.last_sent_pan = pan_out

            time.sleep(self.PAN_DT)

    # ----------------------------
    # SIDECHAIN (CEFFECT_3) — pedal-triggered volume duck
    # ----------------------------
    def _sc_value(self, elapsed, floor, length, curve, ceiling):
        """Instant drop to floor, then an accelerating power-curve climb back.

        Hangs low and rushes up at the end — the drawn sidechain shape rather
        than a real compressor's release. Never returns above the ceiling, so
        the duck can only attenuate; it cannot leave a patch louder than the
        player set it.
        """
        floor = min(floor, ceiling)
        if elapsed >= length:
            return ceiling
        frac = elapsed / length
        return int(round(floor + (ceiling - floor) * (frac ** curve)))

    def _sc_send(self, value):
        try:
            self.out.send(mido.Message(
                "control_change", control=self.SC_VOLUME_CC,
                value=value, channel=self.CHANNEL
            ))
        except Exception as e:
            print(f"[SIDECHAIN] send failed: {e}")

    def _sc_trigger(self, channel=None):
        now = time.time()
        with self.sc_lock:
            if now - self.sc_last_trigger < self.SC_TRIGGER_DEBOUNCE:
                print(f"[SIDECHAIN] duplicate pedal message ignored (channel {channel})")
                return
            self.sc_last_trigger = now
            self.sc_trigger_time = now
            floor = min(self.sc_floor, self.sc_ceiling)
            send = floor != self.sc_last_sent
            if send:
                self.sc_last_sent = floor
        # Drop on the pedal's own thread rather than waiting up to SC_DT for
        # the output loop's next tick: a piano or synth attack peaks within a
        # few ms, and a 10ms late duck lets the transient through at full level.
        if send:
            self._sc_send(floor)

    def _sc_restore(self):
        """Volume back to the ceiling, duck cancelled.

        A stuck duck leaves the instrument quiet with no obvious cause, so
        every path that stops sidechaining must land here.
        """
        with self.sc_lock:
            self.sc_trigger_time = None
            self.sc_last_sent = self.sc_ceiling
            ceiling = self.sc_ceiling
        self._sc_send(ceiling)

    def _sidechain_loop(self):
        while not self.stop_event.is_set():
            if self.active and self.effects["CEFFECT_3"]:
                with self.sc_lock:
                    t = self.sc_trigger_time
                    floor, length = self.sc_floor, self.sc_length
                    curve, ceiling = self.sc_curve, self.sc_ceiling

                if t is None:
                    value = ceiling
                else:
                    elapsed = time.time() - t
                    value = self._sc_value(elapsed, floor, length, curve, ceiling)
                    if elapsed >= length:
                        with self.sc_lock:
                            # Only clear if no re-trigger landed meanwhile.
                            if self.sc_trigger_time == t:
                                self.sc_trigger_time = None

                # Emit only on change, mirroring _pan_loop's last_sent_pan idiom.
                with self.sc_lock:
                    send = value != self.sc_last_sent
                    if send:
                        self.sc_last_sent = value
                if send:
                    self._sc_send(value)

            time.sleep(self.SC_DT)

    def _set_sidechain(self, state):
        # Either transition starts from a known pedal position: a stale "down"
        # would swallow the next press, a stale "up" would fire a spurious duck.
        self.sc_pedal_down = False
        if state:
            # Enabling mid-song: flush any sustain the pedal is already holding,
            # or those notes and the frozen arp chord are stranded — the pedal
            # stops reporting releases the moment sidechain owns it.
            for n in self.sustain_held_notes:
                try:
                    self.out.send(mido.Message(
                        "note_off", note=n, velocity=0, channel=self.CHANNEL
                    ))
                except Exception as e:
                    print(f"[SIDECHAIN] flush note_off failed: {e}")
            self.sustain_held_notes.clear()
            self._arp_set_sustain(False)
            self.sustain_on = False
        else:
            self._sc_restore()
        print(f"[SIDECHAIN] {'ON' if state else 'OFF'}")

    # ----------------------------
    # ARP ON/OFF
    # ----------------------------
    def _set_arp(self, state):
        # Kill in-flight sound on either transition: on→off cleans up the
        # arp's own note, off→on cleans up pass-through notes still ringing.
        with self.arp_lock:
            self.arp_on = state
        self._arp_all_notes_off()
        print(f"[ARP] {'ON' if state else 'OFF'}")

    # ----------------------------
    # ARP SUSTAIN (CC 64 — freezes the arp chord while held)
    # ----------------------------
    def _arp_set_sustain(self, on):
        with self.arp_lock:
            if on and not self.arp_sustained:
                self.arp_frozen_held = dict(self.arp_held_notes)
                self.arp_frozen_press = list(self.arp_press_order)
                self.arp_sustained = True
            elif not on and self.arp_sustained:
                self.arp_frozen_held = {}
                self.arp_frozen_press = []
                self.arp_sustained = False

    # ----------------------------
    # ARP BPM CURVES
    # ----------------------------
    def _arp_bpm_from_value(self, value):
        # Exponential curve: constant ratio per pedal/knob step. Tempo
        # perception is logarithmic, so a linear BPM map feels oversensitive
        # at low tempo.
        return self.ARP_BPM_MIN * (self.ARP_BPM_MAX / self.ARP_BPM_MIN) ** (value / 127.0)

    def _arp_bpm_from_pedal(self, value):
        # In pedal_full_range mode the pedal maps to step-rate (notes/sec)
        # directly across the union of all subdivisions' achievable rates.
        if self.arp_pedal_full_range:
            max_mult = max(self.ARP_SUBDIV_MULT.values())     # 8.0 (1/32)
            rate_min = self.ARP_BPM_MIN / 60.0                # ~0.67 Hz
            rate_max = self.ARP_BPM_MAX * max_mult / 60.0     # ~40 Hz
            step_rate = rate_min * (rate_max / rate_min) ** (value / 127.0)
            mult = self.ARP_SUBDIV_MULT.get(self.arp_subdiv_idx, 1.0)
            return step_rate * 60.0 / mult
        return self._arp_bpm_from_value(value)

    # ----------------------------
    # ARP ECHO / BOUNCE HELPERS (drive Nord lamps, suppress reflections)
    # ----------------------------
    def _arp_echo_value(self, cc, value):
        self.out.send(mido.Message(
            "control_change", channel=0, control=cc, value=value
        ))
        self.last_bounce_time[cc] = time.time()

    def _arp_bounce_inverse(self, cc, value):
        opposite = 0 if value == 127 else 127
        self.out.send(mido.Message(
            "control_change", channel=0, control=cc, value=opposite
        ))
        self.last_bounce_time[cc] = time.time()

    def _arp_all_notes_off(self):
        self.out.send(mido.Message(
            "control_change", control=123, value=0, channel=self.CHANNEL
        ))

    # ----------------------------
    # ARP CC HANDLER — returns True if it consumed the CC
    # ----------------------------
    def _handle_arp_cc(self, cc, value):
        # Echo suppression for any CC we've just bounced/echoed back.
        if cc in (9, 12, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
                  25, 26, 27, 28, 30, 104):
            if time.time() - self.last_bounce_time.get(cc, 0) < self.BOUNCE_COOLDOWN:
                return True

        # Shift+CC15 → toggle live-retrigger (must precede the CC15 tap
        # branch so tapping with shift down doesn't also tap).
        if cc == 15 and self.shift_held:
            self._arp_bounce_inverse(cc, value)
            with self.arp_lock:
                self.arp_live_retrigger = not self.arp_live_retrigger
                on = self.arp_live_retrigger
            print(f"[LIVE RETRIGGER] {'ON' if on else 'OFF'}")
            return True

        # Ctrl+CC28 → toggle pedal_full_range (step-rate mode)
        if cc == 28 and self.ctrl_held:
            self._arp_bounce_inverse(cc, value)
            self.arp_pedal_full_range = not self.arp_pedal_full_range
            print(f"[PEDAL FULL-RANGE] {'ON' if self.arp_pedal_full_range else 'OFF'}")
            return True

        # CC28 (no modifier) → toggle pedal→speed routing for the arp
        if cc == 28:
            self._arp_bounce_inverse(cc, value)
            self.arp_pedal_to_speed = not self.arp_pedal_to_speed
            print(f"[PEDAL→SPEED] {'bound' if self.arp_pedal_to_speed else 'unbound'}")
            return True

        # CC25 → reset all arp settings and sync Nord knob lamps
        if cc == 25:
            with self.arp_lock:
                self.arp_bpm = 120.0
                self.arp_subdiv_idx = 1
                self.arp_pattern_mode = 0
                self.arp_cc27_on = False
                self.arp_cc30_state = 0
                self.arp_custom_cc[:] = [127] * 9
            # Value 69 on CC104 ≈ 120 BPM on the exponential curve
            # (127 * ln(120/40) / ln(300/40) ≈ 69).
            self._arp_echo_value(25, 0)
            self._arp_echo_value(104, 69)
            # No CC12 echo: the subdiv control is a spring-return rocker with
            # no lamp state to sync, and CC12 is also OCTAVE_CCS[0] — echoing
            # 64 there recentered the Nord's octave as a side effect.
            self._arp_echo_value(26, 0)
            self._arp_echo_value(27, 0)
            self._arp_echo_value(30, 0)
            for k in range(16, 25):
                self._arp_echo_value(k, 127)
            print("[ARP RESET] all settings restored to defaults")
            return True

        if cc == 104:
            with self.arp_lock:
                self.arp_bpm = self._arp_bpm_from_value(value)
            return True

        if cc == 27:
            self.arp_cc27_on = value >= 64
            print(f"[ARP CC27] root-octave-double {'ON' if self.arp_cc27_on else 'OFF'}")
            return True

        if cc == 30:
            with self.arp_lock:
                self.arp_cc30_state = (self.arp_cc30_state + 1) % 4
                state = self.arp_cc30_state
            print(f"[ARP CC30] octavizer state {state}")
            return True

        if cc == 15:
            now = time.time()
            with self.arp_lock:
                if self.arp_tap_times and (now - self.arp_tap_times[-1]) > self.ARP_TAP_RESET_GAP:
                    self.arp_tap_times.clear()
                self.arp_tap_times.append(now)
                if len(self.arp_tap_times) >= 2:
                    ts = list(self.arp_tap_times)
                    diffs = [b - a for a, b in zip(ts, ts[1:])]
                    avg = mean(diffs)
                    if avg > 0:
                        self.arp_bpm = max(self.ARP_BPM_MIN, min(self.ARP_BPM_MAX, 60.0 / avg))
            return True

        if cc == 26:
            nearest = min(self.ARP_PATTERN_PRESETS, key=lambda v: abs(v - value))
            with self.arp_lock:
                self.arp_pattern_mode = self.ARP_PATTERN_PRESETS[nearest]
                mode = self.arp_pattern_mode
            print(f"[ARP PATTERN] {self.ARP_PATTERN_NAMES[mode]}")
            return True

        if 16 <= cc <= 24:
            with self.arp_lock:
                self.arp_custom_cc[cc - 16] = value
                steps = self._arp_custom_steps() if self.arp_pattern_mode == 5 else None
            if steps is not None:
                # Dialing a step pattern blind is hopeless — echo it back.
                print("[ARP STEPS] " + (" ".join("." if d == 0 else str(d)
                                                 for d in steps) or "(empty)"))
            return True

        if cc == 12:
            with self.arp_lock:
                if value == 75:
                    self.arp_subdiv_idx = min(4, self.arp_subdiv_idx + 1)
                elif value == 53:
                    self.arp_subdiv_idx = max(0, self.arp_subdiv_idx - 1)
                # 64 is spring-return-to-center; ignore.
                idx = self.arp_subdiv_idx
            print(f"[ARP SUBDIV] {self.ARP_SUBDIV_NAMES[idx]}")
            return True

        return False

    # ----------------------------
    # ARP NOTE HANDLERS — maintain held-chord, gate pass-through
    # ----------------------------
    def _arp_note_on(self, note, velocity):
        # Caller (_midi_loop) gates on velocity > 0, so we never see note-offs.
        with self.arp_lock:
            is_new_note = note not in self.arp_held_notes
            self.arp_held_notes[note] = velocity
            if note not in self.arp_press_order:
                self.arp_press_order.append(note)
            arp_active = self.arp_on
            # Re-sync on any chord change, not just chords struck from
            # silence — playing legato (hold two notes, move one) is the
            # normal case live, and that never empties the held set.
            now = time.time()
            fire_retrigger = (
                is_new_note and arp_active and self.arp_live_retrigger
                and (now - self.arp_last_retrigger) > self.ARP_RETRIGGER_DEBOUNCE
            )
            if fire_retrigger:
                self.arp_last_retrigger = now

        if not arp_active:
            # If this note is being held by the sustain emulator, the synth
            # still has the prior voice ringing and will ignore a duplicate
            # note_on. Flush an explicit note_off first so it retriggers.
            if note in self.sustain_held_notes:
                self.out.send(mido.Message(
                    "note_off", note=note, velocity=0, channel=self.CHANNEL
                ))
                self.sustain_held_notes.discard(note)
            self.out.send(mido.Message(
                "note_on", note=note, velocity=velocity, channel=self.CHANNEL
            ))
        if fire_retrigger:
            self.arp_retrigger_event.set()

    def _arp_note_off(self, note):
        with self.arp_lock:
            self.arp_held_notes.pop(note, None)
            if note in self.arp_press_order:
                self.arp_press_order.remove(note)
            arp_active = self.arp_on

        if not arp_active:
            if self.sustain_on:
                self.sustain_held_notes.add(note)
            else:
                self.out.send(mido.Message(
                    "note_off", note=note, velocity=0, channel=self.CHANNEL
                ))

    # ----------------------------
    # ARP PATTERN GENERATION — callers must hold self.arp_lock
    # ----------------------------
    def _arp_active_chord(self):
        # While sustain is down, the arp plays the snapshot taken at
        # sustain-down; newly-pressed notes are not added to the cycle.
        if self.arp_sustained:
            return self.arp_frozen_held, self.arp_frozen_press
        return self.arp_held_notes, self.arp_press_order

    def _arp_custom_degree(self, value):
        # Drawbar detent -> chord-tone number. Inverted so the fully-out
        # position is tone 1, matching the "bigger value comes first" feel
        # the old custom mode had. 0 means the step is a rest.
        pos = round((value / 127.0) * self.ARP_CUSTOM_DETENTS)
        return 0 if pos == 0 else self.ARP_CUSTOM_DETENTS + 1 - pos

    def _arp_custom_steps(self):
        # Trailing rests shorten the pattern, so a drawbar pushed fully in
        # is how you set the cycle length; interior rests stay as silence.
        steps = [self._arp_custom_degree(v) for v in self.arp_custom_cc]
        while steps and steps[-1] == 0:
            steps.pop()
        return steps

    def _arp_apply_pattern(self, layer_asc, layer_order, layer_notes_set):
        layer_desc = list(reversed(layer_asc))
        mode = self.arp_pattern_mode
        if   mode == 0: return layer_asc
        elif mode == 1: return layer_desc
        elif mode == 2: return layer_asc + layer_desc
        elif mode == 3:
            return layer_asc + layer_desc[1:-1] if len(layer_asc) > 2 else layer_asc
        elif mode == 4:
            mimic = [n for n in layer_order if n in layer_notes_set]
            extras = [n for n in layer_asc if n not in layer_order]
            return mimic + extras
        elif mode == 5:
            n = len(layer_asc)
            cycle = []
            for degree in self._arp_custom_steps():
                if degree == 0:
                    cycle.append(None)
                    continue
                # Degrees past the top of the chord wrap round an octave up,
                # so a pattern written for a 7th chord still climbs on a triad.
                note = layer_asc[(degree - 1) % n] + 12 * ((degree - 1) // n)
                cycle.append(note if note <= 127 else None)
            return cycle
        return []

    def _arp_build_cycle(self):
        if not self.arp_on:
            return []
        notes, order = self._arp_active_chord()
        if not notes:
            return []
        asc_base = sorted(notes.keys())

        # UpDown / UpDownNoEdges treat the octavizer stack as one long ladder
        # so CC27's note lands at the very top of the sweep.
        if self.arp_pattern_mode in (2, 3):
            full_asc = [
                n + 12 * k
                for k in range(self.arp_cc30_state + 1)
                for n in asc_base
                if n + 12 * k <= 127
            ]
            if self.arp_cc27_on and full_asc:
                root_pc = asc_base[0] % 12
                root_max = max(
                    (n for n in full_asc if n % 12 == root_pc),
                    default=full_asc[0],
                )
                extra = root_max + 12
                if extra <= 127:
                    full_asc.append(extra)
            full_order = [
                n + 12 * k
                for k in range(self.arp_cc30_state + 1)
                for n in order
            ]
            return self._arp_apply_pattern(full_asc, full_order, set(full_asc))

        # Other patterns stay layer-by-layer; CC27 slots into the top layer
        # as its new peak, non-top layers are plain octave shifts.
        cycle = []
        for k in range(0, self.arp_cc30_state + 1):
            layer_asc = [n + 12 * k for n in asc_base if n + 12 * k <= 127]
            if not layer_asc:
                continue

            if self.arp_cc27_on and k == self.arp_cc30_state:
                root_pc = layer_asc[0] % 12
                layer_root_max = max(
                    (n for n in layer_asc if n % 12 == root_pc),
                    default=layer_asc[0],
                )
                extra = layer_root_max + 12
                if extra <= 127:
                    layer_asc = layer_asc + [extra]

            layer_order = [n + 12 * k for n in order]
            cycle.extend(self._arp_apply_pattern(layer_asc, layer_order, set(layer_asc)))

        return cycle

    def _arp_step_duration(self, cycle_len):
        beat = 60.0 / self.arp_bpm
        # Fit x1/bar fits the entire cycle into one 4-beat bar.
        n = max(1, cycle_len)
        idx = self.arp_subdiv_idx
        if   idx == 4: dur = beat / 8.0
        elif idx == 3: dur = beat / 4.0
        elif idx == 2: dur = beat / 2.0
        elif idx == 1: dur = beat
        elif idx == 0: dur = (4.0 * beat) / n
        else:          dur = beat
        return max(self.ARP_MIN_STEP, dur)

    # ----------------------------
    # ARP ENGINE
    # ----------------------------
    def _arp_loop(self):
        step_idx = 0
        while not self.stop_event.is_set():
            if not self.active:
                step_idx = 0
                self.arp_retrigger_event.clear()
                time.sleep(self.ARP_IDLE_SLEEP)
                continue

            with self.arp_lock:
                cycle = self._arp_build_cycle()
                dur = self._arp_step_duration(len(cycle))
                vel_map = dict(self._arp_active_chord()[0])

            if not cycle:
                step_idx = 0
                # Drain any pending retrigger so the next real cycle starts clean.
                self.arp_retrigger_event.clear()
                time.sleep(self.ARP_IDLE_SLEEP)
                continue

            # None is a rest (custom mode): the step still eats its time slot
            # so the pattern keeps its rhythm, it just stays silent.
            note = cycle[step_idx % len(cycle)]
            on_time = dur * self.ARP_GATE_RATIO
            off_time = dur - on_time

            if note is not None:
                self.out.send(mido.Message(
                    "note_on", note=note, velocity=vel_map.get(note, 100),
                    channel=self.CHANNEL
                ))

            retriggered = self.arp_retrigger_event.wait(on_time)
            if note is not None:
                self.out.send(mido.Message(
                    "note_off", note=note, velocity=0, channel=self.CHANNEL
                ))
            if retriggered:
                self.arp_retrigger_event.clear()
                step_idx = 0
                continue

            if self.arp_retrigger_event.wait(off_time):
                self.arp_retrigger_event.clear()
                step_idx = 0
                continue
            step_idx += 1

    # ----------------------------
    # MODIFIER STATE
    # ----------------------------
    def _set_shift(self, state):
        self.shift_held = state

    def _set_ctrl(self, state):
        self.ctrl_held = state

    # ----------------------------
    # ACTIVE / PAUSED (button 10 short-press)
    # ----------------------------
    def _toggle_active(self):
        if self.active:
            self._go_inactive()
        else:
            self._go_active()

    def _go_active(self):
        self.active = True
        # Force the pan loop to re-emit on next tick (the synth was set to
        # 64 by the panic; last_sent_pan is still the pre-pause value).
        self.last_sent_pan = None
        if "power" in self._leds:
            self._leds["power"].on()
        print("[Nord6] ACTIVE")

    def _go_inactive(self):
        # Panic: silence the synth. Same sequence as _shutdown's exit panic.
        for m in (
            mido.Message("control_change", control=self.SUSTAIN_CC, value=0, channel=self.CHANNEL),
            mido.Message("control_change", control=123, value=0, channel=self.CHANNEL),
            mido.Message("pitchwheel", pitch=0, channel=self.CHANNEL),
            mido.Message("control_change", control=10, value=64, channel=self.CHANNEL),
        ):
            try:
                self.out.send(m)
            except Exception as e:
                print(f"[Nord6] pause-panic send failed ({m}): {e}")

        # Clear in-memory note state so resume starts from a clean slate.
        with self.arp_lock:
            self.arp_held_notes.clear()
            self.arp_press_order.clear()
            self.arp_frozen_held = {}
            self.arp_frozen_press = []
            self.arp_sustained = False
        self.mb_held_notes = []
        self.mb_sounding_note = None
        self.mb_pitch = 0
        self.mb_target = 0
        self.harmonizer_active.clear()
        self.sustain_held_notes.clear()
        self.sustain_on = False
        self.sc_pedal_down = False
        # Pausing mid-duck must not leave the instrument quiet.
        self._sc_restore()
        self.active = False
        if "power" in self._leds:
            self._leds["power"].off()
        print("[Nord6] PAUSED")

    # ----------------------------
    # GRACEFUL SHUTDOWN
    # ----------------------------
    def _request_shutdown(self, *_):
        # Signal handlers, keyboard callbacks, and atexit all land here.
        self.stop_event.set()

    def _shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        print("\n[Nord6] Shutting down...")

        # Panic sequence — each send in its own try so one failure doesn't
        # skip the rest. Release sustain first so held notes flush; then
        # all-notes-off; then center pitchwheel (mod/monobend); then center pan.
        for msg in (
            mido.Message("control_change", control=self.SUSTAIN_CC, value=0, channel=self.CHANNEL),
            mido.Message("control_change", control=123, value=0, channel=self.CHANNEL),
            mido.Message("pitchwheel", pitch=0, channel=self.CHANNEL),
            mido.Message("control_change", control=10, value=64, channel=self.CHANNEL),
            # Restore channel volume: quitting mid-duck must not leave the Nord
            # silent with no obvious cause.
            mido.Message("control_change", control=self.SC_VOLUME_CC,
                         value=self.sc_ceiling, channel=self.CHANNEL),
        ):
            try:
                self.out.send(msg)
            except Exception as e:
                print(f"[Nord6] panic send failed ({msg}): {e}")

        # Close GPIO buttons (no-op on non-Pi).
        for b in self._gpio_buttons:
            try:
                b.close()
            except Exception as e:
                print(f"[Nord6] gpio close failed: {e}")
        self._gpio_buttons = []

        # Close LEDs (no-op on non-Pi).
        for led in self._leds.values():
            try:
                led.off()
                led.close()
            except Exception as e:
                print(f"[Nord6] led close failed: {e}")
        self._leds = {}

        # Give the output loops a tick to notice stop_event and stop sending.
        # Closing the ports out from under a mid-flight send() throws, and on
        # a pedal that gets power-cycled every session that noise is constant.
        time.sleep(0.05)

        # Close ports explicitly so WinMM/ALSA release the handles.
        for name, port in (("input", self.inp), ("output", self.out)):
            try:
                port.close()
            except Exception as e:
                print(f"[Nord6] {name} port close failed: {e}")

        print("[Nord6] Clean exit")

    def run(self):
        signal.signal(signal.SIGINT, self._request_shutdown)
        for sig_name in ("SIGBREAK", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self._request_shutdown)
                except (ValueError, OSError):
                    pass

        # Belt-and-suspenders: if main crashes before the finally runs,
        # atexit still closes the ports. _shutdown is idempotent.
        atexit.register(self._shutdown)

        try:
            # Short timeout so Ctrl+C gets processed promptly even on Windows,
            # where long blocking waits can delay SIGINT delivery.
            while not self.stop_event.is_set():
                self.stop_event.wait(0.5)
        finally:
            self._shutdown()

    # ----------------------------
    # PROGRAM KEY
    # ----------------------------
    def _get_program_key(self):
        return (self.state["bank"], self.state["program"])

    # ----------------------------
    # PROGRAM CHANGE
    # ----------------------------
    def _send_program(self):
        self.out.send(mido.Message("control_change", control=0, value=0))
        self.out.send(mido.Message("control_change", control=32, value=self.state["bank"]))

        time.sleep(0.01)

        self.out.send(mido.Message("program_change", program=self.state["program"]))

        # Only override the patch's own octave if the user explicitly
        # stored a custom one for this program this session. Otherwise leave
        # the program's stored octave untouched (no "E"/Edited flag, no
        # unwanted octave change).
        key = self._get_program_key()
        if key in self.octave_memory:
            self.state["octave"] = self.octave_memory[key]
            self._send_octave()
        else:
            self.state["octave"] = 0  # display only; don't touch the patch

        print(f"▶ Bank {self.state['bank']} | Program {self.state['program']} | Octave {self.state['octave']}")

        # Apply this patch's effects here rather than waiting for the Nord to
        # echo the program change back, and suppress that echo so it does not
        # apply twice.
        self._program_echo_until = time.time() + 0.3
        self._patch_apply_current()

    # ----------------------------
    # OCTAVE
    # ----------------------------
    def _send_octave(self):
        # Clamped rather than chained ifs: an out-of-range octave used to
        # leave `value` unbound, which kills the calling thread outright —
        # and on the Pi that thread is a GPIO button callback.
        octave = max(-1, min(1, self.state["octave"]))
        value = {-1: 30, 0: 64, 1: 100}[octave]

        for cc in self.OCTAVE_CCS:
            self.out.send(mido.Message(
                "control_change", control=cc, value=value
            ))
            # CC12 doubles as the arp's subdivision rocker, so record the
            # send or the Nord's echo returns as a phantom subdiv change.
            self.last_bounce_time[cc] = time.time()

        print(f"🎹 Octave: {self.state['octave']}")

    # ----------------------------
    # ENGINE TOGGLE
    # ----------------------------
    def toggle_engine(self, engine):
        key = (self._get_program_key(), engine)

        if key not in self.engine_touch_memory:
            self.engine_touch_memory[key] = True
            self.engine_toggle_state[key] = False
        else:
            self.engine_toggle_state[key] = not self.engine_toggle_state[key]

        value = 64 if self.engine_toggle_state[key] else 0

        self.out.send(mido.Message(
            "control_change", control=self.ENGINE_CCS[engine], value=value
        ))

        print(f"🎛 {engine.upper()} -> {'ON' if self.engine_toggle_state[key] else 'OFF'}")

    # ----------------------------
    # EFFECT TOGGLE
    # ----------------------------
    def toggle_effect(self, effect):
        key = (self._get_program_key(), effect)

        if key not in self.fx_touch_memory:
            self.fx_touch_memory[key] = True
            self.fx_toggle_state[key] = False
        else:
            self.fx_toggle_state[key] = not self.fx_toggle_state[key]

        value = 64 if self.fx_toggle_state[key] else 0

        self.out.send(mido.Message(
            "control_change", control=self.EFFECT_CCS[effect], value=value
        ))

        print(f"✨ {effect.upper()} -> {'ON' if self.fx_toggle_state[key] else 'OFF'}")

    # ----------------------------
    # PROGRAM NAVIGATION
    # ----------------------------
    def next_program(self):
        self.state["program"] += 1

        if self.state["program"] > 127:
            self.state["program"] = 0
            self.state["bank"] += 1

        self._send_program()

    def prev_program(self):
        self.state["program"] -= 1

        if self.state["program"] < 0:
            self.state["program"] = 127
            self.state["bank"] -= 1
            if self.state["bank"] < 0:
                self.state["bank"] = 0
                self.state["program"] = 0

        self._send_program()

    # ----------------------------
    # OCTAVE CONTROL
    # ----------------------------
    def octave_up(self):
        key = self._get_program_key()
        current = self.octave_memory.get(key, 0)

        if current < 1:
            current += 1
            self.octave_memory[key] = current
            self.state["octave"] = current
            self._send_octave()

    def octave_down(self):
        key = self._get_program_key()
        current = self.octave_memory.get(key, 0)

        if current > -1:
            current -= 1
            self.octave_memory[key] = current
            self.state["octave"] = current
            self._send_octave()


# ----------------------------
# RUN
# ----------------------------
if __name__ == "__main__":
    system = Nord6()
    system.start()
    system.run()
