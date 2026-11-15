#!/usr/bin/env python3
"""CN60 screen blanker.

Presence mode (FRIGATE_WS + PRESENCE_CAMERA set and the feed healthy): blank once nobody has been
seen for ABSENT_AFTER s (default 60, so walking away does not blank instantly) AND there has been
no input/poke for that long; wake the moment a person is seen, on input, or on a poke.
Fallback (no presence feed): blank after BLANK_AFTER s (default 300) without input or poke.
Poke = touch ~/.cache/screen-poke (wakes the panel and restarts the timers)."""
import json, os, re, subprocess, threading, time
BLANK_AFTER = int(os.environ.get("BLANK_AFTER", "300"))
ABSENT_AFTER = int(os.environ.get("ABSENT_AFTER", "60"))
FRIGATE_WS = os.environ.get("FRIGATE_WS", "")      # e.g. ws://FRIGATE_HOST:PORT/ws
CAMERA = os.environ.get("PRESENCE_CAMERA", "")
FEED_STALE = 30                                    # s without any activity message = feed down
state = {"last_msg": 0.0, "present": False, "last_present": 0.0}


def presence_feed():
    import websocket  # websocket-client
    while True:
        try:
            ws = websocket.create_connection(FRIGATE_WS, timeout=30, suppress_origin=True)
            ws.send(json.dumps({"topic": "onConnect", "message": ""}))
            while True:
                m = json.loads(ws.recv())
                if m.get("topic") != "camera_activity":
                    continue
                a = m.get("payload")
                a = json.loads(a) if isinstance(a, str) else a
                cam = (a or {}).get(CAMERA)
                if cam is None:
                    continue
                now = time.time()
                present = any(o.get("label") in ("person", "person-verified") for o in cam.get("objects", []))
                state.update(last_msg=now, present=present)
                if present:
                    state["last_present"] = now
        except Exception as e:
            print("presence feed:", e, flush=True)
            state["last_msg"] = 0.0
            time.sleep(5)


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
if FRIGATE_WS and CAMERA:
    threading.Thread(target=presence_feed, daemon=True).start()
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
        else:
            now = time.time()
            feed_ok = bool(FRIGATE_WS and CAMERA) and now - state["last_msg"] < FEED_STALE
            if feed_ok:
                if cur != 0 and state["present"]:
                    psm(0)                          # someone appeared -> wake
                elif cur == 0 and not state["present"] and now - state["last_present"] >= ABSENT_AFTER and eff >= ABSENT_AFTER:
                    psm(3)                          # absent long enough and no input
            elif eff > BLANK_AFTER and cur == 0:
                psm(3)
    except Exception as e:
        print("err", e, flush=True)
    time.sleep(0.5)
