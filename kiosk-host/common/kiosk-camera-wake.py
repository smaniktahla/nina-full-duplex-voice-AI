#!/usr/bin/env python3
"""kiosk-camera-wake: local camera motion wakes the kiosk screen, a quiet spell blanks it.
Camera-agnostic: any kiosk points MOTION_URL at its own camera (USB webcam, Kinect, ...) in its unit.

No Frigate, no cloud: one ffmpeg reads the local camera stream, scales it to 96x54 gray at 2 fps,
and frames are compared. Motion rules:
  * screen off: wake after MOTION_CONFIRM (2) consecutive motion frames (one noisy frame is ignored)
  * screen on:  every motion frame restarts the quiet timer, so someone who keeps moving stays lit
  * blank when there has been no motion AND no input/poke for ABSENT_AFTER s (default 60)
  * camera stream down for 30 s: fall back to blanking after BLANK_AFTER s (default 300) of no input
Input, `touch ~/.cache/screen-poke`, or a GET to http://127.0.0.1:WAKE_PORT/wake (default 8777; the Nina client
and the page script call it) always wakes and restarts the timers.
Backends (SCREEN_BACKEND): gnome (Mutter PowerSaveMode, Wayland) or x11 (xset/xprintidle).
Blank and wake fade the dashboard to/from black through Chrome's debug port (CDP_PORT, default 9222;
FADE_MS 1500 = blank fade; WAKE_DELAY_MS 1400 = black while the panel lights up; WAKE_FADE_MS 1000 = wake fade-in;
0 disables a fade; all three can be overridden live in ~/.config/kiosk/camera-wake.json as fade_ms/wake_delay_ms/wake_fade_ms) so the change is smooth; without Chrome it is instant.
Fullscreen: Chrome can drop fullscreen when the panel powers back up. If it was fullscreen when the screen blanked, wake puts it
back (CDP); a window someone deliberately left fullscreen is never touched, so Esc / swipe-down still works.
Env: MOTION_URL (camera MJPEG URL), SCREEN_BACKEND, ABSENT_AFTER, BLANK_AFTER, MOTION_PIXEL (22),
MOTION_AREA (0.025), MOTION_CONFIRM (2).
"""
import http.server, json, os, re, subprocess, threading, time, urllib.request
from statistics import median

BACKEND = os.environ.get("SCREEN_BACKEND", "gnome")
URL = os.environ.get("MOTION_URL", "")
ABSENT_AFTER = int(os.environ.get("ABSENT_AFTER", "60"))
BLANK_AFTER = int(os.environ.get("BLANK_AFTER", "300"))
PIXEL = int(os.environ.get("MOTION_PIXEL", "22"))
AREA = float(os.environ.get("MOTION_AREA", "0.025"))
CONFIRM = int(os.environ.get("MOTION_CONFIRM", "2"))
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
FADE_MS = int(os.environ.get("FADE_MS", "1500"))               # blank: fade to black
WAKE_DELAY_MS = int(os.environ.get("WAKE_DELAY_MS", "1400"))   # wake: black while the panel/HDMI lights up
WAKE_FADE_MS = int(os.environ.get("WAKE_FADE_MS", "1000"))     # wake: fade-in length
TUNE = os.path.expanduser(os.environ.get("KCW_CONFIG", "~/.config/kiosk/camera-wake.json"))

EFFECTS_URL = os.environ.get("KIOSK_EFFECTS_URL", "")   # Dashboard Manager file: http://<ha>/local/kiosk_effects.json
KIOSK_ID = os.environ.get("KIOSK_ID", "")               # this kiosk's Browser Mod id, the key in that file
_fx = {"t": 0.0, "data": {}}


def ha_effects():
    """This kiosk's settings from the Dashboard Manager UI; last good copy if Home Assistant is unreachable."""
    if EFFECTS_URL and KIOSK_ID and time.time() - _fx["t"] > 5:
        _fx["t"] = time.time()
        try:
            _fx["data"] = json.load(urllib.request.urlopen(EFFECTS_URL, timeout=3)).get(KIOSK_ID) or _fx["data"]
        except Exception:
            pass
    return _fx["data"]



def tune():
    """Optional live overrides (no restart): {"fade_ms":1500,"wake_delay_ms":1400,"wake_fade_ms":1000}."""
    try:
        d = json.load(open(TUNE))
    except Exception:
        d = {}
    base = (int(d.get("fade_ms", FADE_MS)), int(d.get("wake_delay_ms", WAKE_DELAY_MS)), int(d.get("wake_fade_ms", WAKE_FADE_MS)))
    fx = ha_effects()                                   # Dashboard Manager UI (seconds) wins over the local file
    if fx:
        return (int(float(fx.get("blank_fade", base[0] / 1000)) * 1000), int(float(fx.get("wake_delay", base[1] / 1000)) * 1000),
                int(float(fx.get("wake_fade", base[2] / 1000)) * 1000))
    return base

SETTLE = 4          # s to ignore motion after we change the screen (backlight shifts the picture)
W, H = 96, 54
POKE = os.path.expanduser("~/.cache/screen-poke")
cam = {"last_frame": time.time(), "last_motion": time.time(), "consec": 0, "ignore_until": 0.0, "log": os.environ.get("MOTION_LOG") == "1"}


WAKE_PORT = int(os.environ.get("WAKE_PORT", "8777"))   # 0 disables the loopback endpoint


class WakeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/wake":
            with open(POKE, "a"):
                os.utime(POKE, None)                    # the main loop sees the new mtime: wake + restart the timers
            print("wake request", flush=True)
            self.send_response(204)
        else:
            self.send_response(404)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


def serve_wake():
    try:
        http.server.ThreadingHTTPServer(("127.0.0.1", WAKE_PORT), WakeHandler).serve_forever()
    except OSError as e:
        print("wake endpoint unavailable:", e, flush=True)


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


def cdp_eval(js):
    """Run JS in the first Chrome page via the loopback DevTools port; False if Chrome is unreachable."""
    try:
        import websocket  # websocket-client
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2))
        page = next(t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=3, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js}}))
            while json.loads(ws.recv()).get("id") != 1:
                pass
        finally:
            ws.close()
        return True
    except Exception as e:
        print("fade unavailable:", e, flush=True)
        return False


def window_state(want=None):
    """Return Chrome's window state via the debug port; if want is given, set it when different. None if unreachable."""
    try:
        import websocket
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2))
        page = next(t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=3, suppress_origin=True)
        try:
            def call(i, m, p=None):
                ws.send(json.dumps({"id": i, "method": m, "params": p or {}}))
                while True:
                    r = json.loads(ws.recv())
                    if r.get("id") == i:
                        return r.get("result", {})
            w = call(1, "Browser.getWindowForTarget")
            st = w.get("bounds", {}).get("windowState")
            if want and st != want:
                call(2, "Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"windowState": want}})
                print("restored", want, flush=True)
            return st
        finally:
            ws.close()
    except Exception as e:
        print("window state unavailable:", e, flush=True)
        return None


OVERLAY = ("(()=>{let d=document.getElementById('kcw-fade');if(!d){d=document.createElement('div');d.id='kcw-fade';"
           "d.style.cssText='position:fixed;inset:0;background:#000;opacity:%s;z-index:2147483647;pointer-events:none;transition:none';"
           "document.documentElement.appendChild(d);}return d})()")


def fade_out():
    """Fade the page to black; leave the overlay in place so a later power-on starts dark."""
    FADE_MS = tune()[0]
    if FADE_MS <= 0:
        return
    js = (OVERLAY % "0") + f";var d=document.getElementById('kcw-fade');d.getBoundingClientRect();"
    js += f"d.style.transition='opacity {FADE_MS}ms ease-in-out';d.style.opacity='1';1"
    if cdp_eval(js):
        time.sleep(FADE_MS / 1000 + 0.1)


def fade_in():
    """Panel is already powered: start from black, fade the page back in, then drop the overlay."""
    FADE_MS = tune()[2]
    if FADE_MS <= 0:
        return
    js = (OVERLAY % "1") + ";var d=document.getElementById('kcw-fade');d.style.transition='none';d.style.opacity='1';d.getBoundingClientRect();"
    js += f"d.style.transition='opacity {FADE_MS}ms ease-in-out';d.style.opacity='0';setTimeout(()=>d.remove(),{FADE_MS}+100);1"
    cdp_eval(js)


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
    cam["ignore_until"] = time.time() + SETTLE + (tune()[0] + tune()[1] + tune()[2]) / 1000   # light shifts during the fade are not motion
    if on:
        set_screen(True)
        time.sleep(tune()[1] / 1000)   # let the panel light up while the page is still black
        if cam.get("was_fs"):
            window_state("fullscreen")   # Chrome drops fullscreen when the panel powers up; only undo that, never a deliberate exit
        fade_in()
    else:
        cam["was_fs"] = window_state() == "fullscreen"
        fade_out()
        set_screen(False)
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
    if WAKE_PORT:
        threading.Thread(target=serve_wake, daemon=True).start()
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
