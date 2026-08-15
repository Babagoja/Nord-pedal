"""
Latching-switch watcher for the Nord6 pedalboard.

Watches BCM 12 (the master on/off switch). On a flip, starts or stops
nord6.service via systemd, giving the user a clean "stop and start"
recovery option (re-enumerates MIDI, re-finds the Nord, etc.).

Runs as nord6-switch.service.
"""

import subprocess
from signal import pause
from gpiozero import Button

SWITCH_PIN = 12
SERVICE_NAME = "nord6.service"


def start_nord6():
    print(f"[switch] flipped ON  -> starting {SERVICE_NAME}")
    subprocess.run(["systemctl", "start", SERVICE_NAME], check=False)


def stop_nord6():
    print(f"[switch] flipped OFF -> stopping {SERVICE_NAME}")
    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)


switch = Button(SWITCH_PIN, pull_up=True, bounce_time=0.05)
switch.when_pressed = start_nord6
switch.when_released = stop_nord6

# Sync state to current switch position at startup
if switch.is_pressed:
    start_nord6()
else:
    stop_nord6()

print(f"[switch] watcher running on BCM {SWITCH_PIN}")
pause()
