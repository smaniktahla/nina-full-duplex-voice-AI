#!/usr/bin/env python3
"""kiosk-camera-wake: local camera motion wakes the kiosk screen, a quiet spell blanks it.
Camera-agnostic: any kiosk points MOTION_URL at its own camera (USB webcam, Kinect, ...) in its unit.

No Frigate, no cloud: one ffmpeg reads the local camera stream, scales it to 96x54 gray at 2 fps,
and frames are compared. Motion rules:
  * screen off: wake after MOTION_CONFIRM (2) consecutive motion frames (one noisy frame is ignored)
  * screen on:  every motion frame restarts the quiet timer, so someone who keeps moving stays lit
  * blank when there has been no motion AND no input/poke for ABSENT_AFTER s (default 60)
  * camera stream down for 30 s: fall back to blanking after BLANK_AFTER s (default 300) of no input
Input or `touch ~/.cache/screen-poke` always wakes and restarts the timers.
Backends (SCREEN_BACKEND): gnome (Mutter PowerSaveMode, Wayland) or x11 (xset/xprintidle).
Env: MOTION_URL (camera MJPEG URL), SCREEN_BACKEND, ABSENT_AFTER, BLANK_AFTER, MOTION_PIXEL (22),
MOTION_AREA (0.025), MOTION_CONFIRM (2).
"""
import os, re, subprocess, threading, time
from statistics import median

BACKEND = os.environ.get("SCREEN_BACKEND", "gnome")
URL = os.environ.get("MOTION_URL", "")
ABSENT_AFTER = int(os.environ.get("ABSENT_AFTER", "60"))
BLANK_AFTER = int(os.environ.get("BLANK_AFTER", "300"))
PIXEL = int(os.environ.get("MOTION_PIXEL", "22"))
AREA = float(os.environ.get("MOTION_AREA", "0.025"))
CONFIRM = int(os.environ.get("MOTION_CONFIRM", "2"))
SETTLE = 4          # s to ignore motion after we change the screen (backlight shifts the picture)
W, H = 96, 54
POKE = os.path.expanduser("~/.cache/screen-poke")
cam = {"last_frame": time.time(), "last_motion": time.time(), "consec": 0, "ignore_until": 0.0, "log": os.environ.get("MOTION_LOG") == "1"}


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout


if BACKEND == "gnome":
    D = ["gdbus", "call", "--session", "--dest"]
    def idle_s():
        o = run(*D, "org.gnome.Mutter.IdleMonitor", "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime")
        return int(re.search(r"uint64 (\d+)", o).group(1)) / 1000
    def screen_on():
        o = run(*D, "org.gnome.Mutter.DisplayConfig", "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method",
                "org.freedesktop.DBus.Properties.Get", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode")
        return int(re.search(r"<(\d+)>", o).group(1)) == 0
    def set_screen(on):
        run(*D, "org.gnome.Mutter.DisplayConfig", "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method",
            "org.freedesktop.DBus.Properties.Set", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode", f"<{0 if on else 3}>")
    def setup():
        pass
else:
    def idle_s():
        return int(run("xprintidle").strip() or 0) / 1000
    def screen_on():
        return "Monitor is On" in run("xset", "q")
    def set_screen(on):
        if on:
            run("xset", "s", "reset")
        run("xset", "dpms", "force", "on" if on else "off")
    def setup():
        # we own blanking now: disable the X server's own idle timers, keep DPMS usable for force on/off
        run("xset", "s", "off"); run("xset", "+dpms"); run("xset", "dpms", "0", "0", "0")


def motion(prev, cur):
    diffs = [b - a for a, b in zip(prev, cur)]
    shift = median(diffs)                       # ignore camera-wide exposure changes
    return sum(abs(d - shift) >= PIXEL for d in diffs) >= len(diffs) * AREA


def camera():
    while True:
        cmd = ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-use_wallclock_as_timestamps", "1", "-i", URL,
               "-vf", f"fps=2,scale={W}:{H}:flags=area,format=gray", "-f", "rawvideo", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        prev = None
        while True:
            cur = p.stdout.read(W * H)
            if len(cur) < W * H:
                break
            now = time.time()
            cam["last_frame"] = now
            if prev is not None and now >= cam["ignore_until"] and motion(prev, cur):
                cam["last_motion"] = now
                cam["consec"] += 1
                if cam["log"]:
                    print("motion", cam["consec"], flush=True)
            else:
                cam["consec"] = 0
            prev = cur
        p.kill()
        cam["consec"] = 0
        time.sleep(5)


def change(on):
    set_screen(on)
    cam["ignore_until"] = time.time() + SETTLE
    cam["consec"] = 0
    cam["last_motion"] = time.time() if on else cam["last_motion"]
    print("screen", "on" if on else "off", flush=True)


def main():
    setup()
    os.makedirs(os.path.dirname(POKE), exist_ok=True)
    last_poke = os.path.getmtime(POKE) if os.path.exists(POKE) else 0
    if URL:
        threading.Thread(target=camera, daemon=True).start()
    while True:
        try:
            pm = os.path.getmtime(POKE) if os.path.exists(POKE) else 0
            poked = pm > last_poke
            last_poke = max(last_poke, pm)
            idle = idle_s()
            on = screen_on()
            now = time.time()
            quiet = min(idle, now - last_poke)
            if poked:
                cam["last_motion"] = now
                if not on:
                    change(True)
            elif not on and idle < 3:
                change(True)                                   # real input while blanked
            elif not on and cam["consec"] >= CONFIRM:
                change(True)                                   # confirmed motion
            elif on:
                if now - cam["last_frame"] < 10:               # camera healthy
                    if now - cam["last_motion"] >= ABSENT_AFTER and quiet >= ABSENT_AFTER:
                        change(False)
                elif quiet > BLANK_AFTER:
                    change(False)
        except Exception as e:
            print("err", e, flush=True)
        time.sleep(0.5)


main()
