#!/usr/bin/env python3
"""CN60 screen blanker: blank after BLANK_AFTER s without input or poke; wake on poke.
Poke = touch ~/.cache/screen-poke (wakes the panel and restarts the timer)."""
import os, re, subprocess, time
BLANK_AFTER = int(os.environ.get("BLANK_AFTER", "300"))
POKE = os.path.expanduser("~/.cache/screen-poke")
D = ["gdbus", "call", "--session", "--dest"]
def idle_s():
    o = subprocess.run(D + ["org.gnome.Mutter.IdleMonitor", "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
        "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"], capture_output=True, text=True).stdout
    return int(re.search(r"uint64 (\d+)", o).group(1)) / 1000
def psm(set_to=None):
    base = D + ["org.gnome.Mutter.DisplayConfig", "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method"]
    if set_to is None:
        o = subprocess.run(base + ["org.freedesktop.DBus.Properties.Get", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode"], capture_output=True, text=True).stdout
        return int(re.search(r"<(\d+)>", o).group(1))
    subprocess.run(base + ["org.freedesktop.DBus.Properties.Set", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode", f"<{set_to}>"], capture_output=True)
os.makedirs(os.path.dirname(POKE), exist_ok=True)
last_poke = os.path.getmtime(POKE) if os.path.exists(POKE) else 0
while True:
    try:
        pm = os.path.getmtime(POKE) if os.path.exists(POKE) else 0
        poked = pm > last_poke
        if poked:
            last_poke = pm
            if psm() != 0: psm(0)
        idle = idle_s()
        eff = min(idle, time.time() - last_poke)
        cur = psm()
        if cur != 0 and idle < 3:      # real input while blanked -> wake
            psm(0)
        elif eff > BLANK_AFTER and cur == 0:
            psm(3)
    except Exception as e:
        print("err", e, flush=True)
    time.sleep(0.5)
