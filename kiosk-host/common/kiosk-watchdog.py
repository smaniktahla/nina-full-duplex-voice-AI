#!/usr/bin/env python3
"""Kiosk Chrome crash watchdog + flight recorder (ubuntu-HA-touch).

Why: Chrome's renderer on the kitchen kiosk occasionally dies with "Aw, Snap! SIGILL" and the
dead tab stays on screen until someone reloads it. This service
  1. recovers: reloads the crashed tab within seconds, and
  2. records evidence: keeps a rolling buffer of what the page was doing (URL changes, media
     player events/errors, console/log errors, system load) and writes it to a JSON file the
     moment a crash is detected, so the next crash can actually be diagnosed.

Talks to Chrome over the DevTools protocol on 127.0.0.1 only (needs --remote-debugging-port).
Config via env: KIOSK_WD_PORT (9222), KIOSK_WD_OUT (~/kiosk-watchdog), KIOSK_WD_URL (HA url),
KIOSK_WD_NOTIFY (1 = file a low-priority ATQ task after a crash, at most once per 6h),
"""
import collections
import json
import os
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

PORT = int(os.environ.get("KIOSK_WD_PORT", "9222"))
OUT = Path(os.environ.get("KIOSK_WD_OUT", str(Path.home() / "kiosk-watchdog")))
HA_URL = os.environ["KIOSK_WD_URL"]  # required: dashboard URL a fresh tab opens (set in the unit)
NOTIFY = os.environ.get("KIOSK_WD_NOTIFY", "1") == "1" and bool(os.environ.get("KIOSK_WD_ATQ"))
ATQ = os.environ.get("KIOSK_WD_ATQ", "")  # task-queue base URL; empty disables notifications
HOSTNAME = socket.gethostname()
RING_SECONDS = 120
NOISY = ("Media.playerPropertiesChanged", "Media.playerEventsAdded")
LOG_EVERY = 600


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class Recorder:
    """Rolling evidence buffers (events per target, and system samples)."""

    def __init__(self):
        self.events = collections.deque(maxlen=1500)
        self.system = collections.deque(maxlen=40)
        self.players = collections.defaultdict(dict)
        self._cpu = self._cpu_read()

    @staticmethod
    def _cpu_read():
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return sum(v), v[3] + v[4]

    def add(self, target_id, kind, detail):
        self.events.append((time.time(), target_id, kind, str(detail)[:300]))

    def sample_system(self):
        t, idle = self._cpu_read()
        busy = 100 * (1 - (idle - self._cpu[1]) / max(1, t - self._cpu[0]))
        self._cpu = (t, idle)
        mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith(("MemAvailable", "MemTotal"))}
        procs = collections.Counter()
        rss = collections.Counter()
        for line in subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True).stdout.splitlines():
            if "/opt/google/chrome/chrome" in line:
                kind = next((w.split("=")[1] for w in line.split() if w.startswith("--type=")), "browser")
                procs[kind] += 1
                rss[kind] += int(line.split()[0]) // 1024
        self.system.append({"t": time.strftime("%H:%M:%S"), "cpu_pct": round(busy, 1), "load1": open("/proc/loadavg").read().split()[0],
                            "mem_avail_mb": mem.get("MemAvailable"), "chrome_procs": dict(procs), "chrome_rss_mb": dict(rss)})

    def window(self, target_id, seconds=RING_SECONDS):
        cutoff = time.time() - seconds
        return [{"t": time.strftime("%H:%M:%S", time.localtime(ts)), "kind": k, "detail": d}
                for ts, tid, k, d in self.events if ts >= cutoff and tid in (target_id, "*")]


class CDP:
    def __init__(self, url, on_event):
        self.ws = websocket.create_connection(url, timeout=10, suppress_origin=True)  # Chrome 155 rejects an Origin header; avoids needing --remote-allow-origins=*
        self.ws.settimeout(1.0)
        self.n = 0
        self.on_event = on_event

    def send(self, method, params=None, session=None):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        return self.n

    def call(self, method, params=None, session=None, timeout=15):
        want = self.send(method, params, session)
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self.on_event(msg)
        raise TimeoutError(method)

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            self.on_event(msg)


class Watchdog:
    def __init__(self):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "crashes").mkdir(exist_ok=True)
        self.rec = Recorder()
        self.targets = {}      # targetId -> targetInfo (pages only)
        self.sessions = {}     # sessionId -> targetId
        self.crashed = {}      # targetId -> crash time
        self.crash_times = collections.deque(maxlen=20)
        self.last_notify = 0.0
        self.cdp = None
        self.want_attach = set()

    def process_attach(self):
        for tid in list(self.want_attach):
            self.want_attach.discard(tid)
            if tid in self.targets and tid not in self.crashed and tid not in self.sessions.values():
                self.attach(tid)

    # ---- event handling -------------------------------------------------
    def attach(self, tid):
        if tid in self.sessions.values():
            return
        try:
            sid = self.cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        except Exception as e:
            log(f"attach {tid[:8]} failed: {e}")
            return
        self.sessions[sid] = tid
        for m in ("Media.enable", "Log.enable", "Runtime.enable"):
            self.cdp.send(m, session=sid)

    def on_event(self, msg):
        m, p, sid = msg.get("method"), msg.get("params", {}), msg.get("sessionId")
        if m in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p["targetInfo"]
            if info.get("type") != "page":
                return
            tid, old = info["targetId"], self.targets.get(info["targetId"])
            self.targets[tid] = info
            if old is None or old.get("url") != info.get("url"):
                self.rec.add(tid, "navigate", info.get("url"))
            if tid not in self.crashed and tid not in self.sessions.values():
                self.want_attach.add(tid)  # never call() from inside the handler (re-entrancy swallows replies)
        elif m == "Target.targetDestroyed":
            tid = p["targetId"]
            self.targets.pop(tid, None)
            self.crashed.pop(tid, None)
        elif m == "Target.detachedFromTarget":
            self.sessions.pop(p.get("sessionId"), None)
        elif m == "Target.targetCrashed":
            self.handle_crash(p["targetId"], p.get("status"), p.get("errorCode"))
        elif sid and sid in self.sessions:
            tid = self.sessions[sid]
            if m == "Media.playersCreated":
                self.rec.add(tid, "media.playersCreated", len(p.get("players", [])))
            elif m == "Media.playerPropertiesChanged":
                for pr in p.get("properties", []):
                    if pr["name"] in ("kVideoDecoderName", "kIsPlatformVideoDecoder", "kVideoCodec", "kResolution", "kIsMSE", "kFrameUrl"):
                        self.rec.add(tid, "media.prop", f"{p['playerId']} {pr['name']}={pr.get('value')}")
            elif m == "Media.playerErrorsRaised":
                for e in p.get("errors", []):
                    self.rec.add(tid, "media.ERROR", json.dumps(e))
            elif m == "Media.playerMessagesLogged":
                for x in p.get("messages", []):
                    if x.get("level") in ("error", "warning"):
                        self.rec.add(tid, "media.msg." + x["level"], x.get("message"))
            elif m == "Log.entryAdded":
                e = p["entry"]
                if e.get("level") in ("error", "warning"):
                    self.rec.add(tid, "log." + e["level"], f"{e.get('source')}: {e.get('text')} {e.get('url', '')}")
            elif m == "Runtime.exceptionThrown":
                d = p["exceptionDetails"]
                self.rec.add(tid, "js.exception", f"{d.get('text')} {(d.get('exception') or {}).get('description', '')[:200]}")
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning"):
                self.rec.add(tid, "console." + p["type"], " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))

    # ---- crash handling -------------------------------------------------
    def kernel_lines(self):
        r = subprocess.run("journalctl -k --since '-60 sec' --no-pager -o short-iso | grep -iE 'invalid opcode|segfault|general protection|oom|killed process' | tail -5",
                           shell=True, capture_output=True, text=True)
        return r.stdout.strip().splitlines()

    def handle_crash(self, tid, status, code):
        now = time.time()
        info = self.targets.get(tid, {})
        self.crashed[tid] = now
        self.crash_times.append(now)
        self.rec.sample_system()
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "targetId": tid, "status": status, "errorCode": code,
                  "url": info.get("url"), "title": info.get("title"),
                  "kernel": self.kernel_lines(), "system_last_samples": list(self.rec.system)[-12:],
                  "last_events": self.rec.window(tid)[-250:]}
        path = OUT / "crashes" / f"crash-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(record, indent=1))
        log(f"CRASH target={tid[:8]} status={status} code={code} url={info.get('url')} -> {path.name} ({len(record['last_events'])} events saved)")
        self.notify(record, path)
        self.recover(tid)

    def recover(self, tid):
        """Open a fresh tab and close the dead one. Never wait on the dead tab: its renderer is gone,
        so commands sent to it (Page.reload, closeTarget...) may never be answered."""
        recent = [t for t in self.crash_times if time.time() - t < 600]
        if len(recent) >= 3:
            log(f"{len(recent)} crashes in 10 min: backing off 60s before reloading")
            self.cdp.pump(60)
        else:
            self.cdp.pump(2)
        self.sessions = {s: t for s, t in self.sessions.items() if t != tid}
        try:
            new = self.cdp.call("Target.createTarget", {"url": HA_URL}, timeout=10)["targetId"]
        except Exception as e:
            log(f"RECOVERY FAILED (could not open a new tab): {e}")
            return
        self.cdp.send("Target.closeTarget", {"targetId": tid})  # fire and forget
        self.crashed.pop(tid, None)
        self.cdp.pump(8)  # let the page load
        self.process_attach()  # attach the recorder to the new tab
        sid = next((s for s, t in self.sessions.items() if t == new), None)
        state = "no recorder session yet"
        if sid:
            try:
                r = self.cdp.call("Runtime.evaluate", {"expression": "document.readyState+' '+location.pathname", "returnByValue": True}, session=sid, timeout=8)
                state = r.get("result", {}).get("value")
            except Exception as e:
                state = f"eval failed: {e}"
        log(f"recovered: opened new tab {new[:8]} and closed dead {tid[:8]} -> {state}")

    def notify(self, record, path):
        if not NOTIFY or time.time() - self.last_notify < 6 * 3600:
            return
        self.last_notify = time.time()
        last = [e for e in record["last_events"] if "ERROR" in e["kind"] or "error" in e["kind"]][-3:]
        text = (f"Kiosk Chrome tab crashed {record['time']} on {record['url']} (status={record['status']}). Auto-reloaded by kiosk-watchdog. "
                f"Evidence JSON on kiosk {HOSTNAME}: {path} (last {RING_SECONDS}s of page events + system samples). Recent errors: {last}. "
                f"Context: DocMost '10/11/2026 - Kitchen kiosk Chrome SIGILL crashes...' + memory project-kiosk-bi-pending-reboot-incident.")
        body = {"type": "agent_task", "assigned_to": "claude", "priority": 2, "created_by": "kiosk-watchdog",
                "instructions": text, "context": {"tier": "P3", "title": "Kiosk Chrome crash captured by watchdog"}}
        try:
            req = urllib.request.Request(f"{ATQ}/tasks", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
            log("ATQ task filed (P3)")
        except Exception as e:
            log(f"ATQ notify failed: {e}")

    # ---- main loop ------------------------------------------------------
    def connect(self):
        ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5))
        self.cdp = CDP(ver["webSocketDebuggerUrl"], self.on_event)
        self.targets.clear(); self.sessions.clear(); self.crashed.clear()
        self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        self.process_attach()
        log(f"connected to {ver.get('Browser')} on port {PORT}; tracking {len(self.targets)} page(s)")

    def run(self):
        last_sample = last_log = 0.0
        while True:
            try:
                self.connect()
                while True:
                    self.cdp.pump(1)
                    self.process_attach()
                    now = time.time()
                    if now - last_sample >= 5:
                        self.rec.sample_system(); last_sample = now
                    if now - last_log >= LOG_EVERY:
                        log(f"alive: pages={len(self.targets)} sessions={len(self.sessions)} crashes_total={len(self.crash_times)}"); last_log = now
            except Exception as e:
                log(f"connection lost/unavailable ({type(e).__name__}: {str(e)[:100]}); retrying in 5s")
                time.sleep(5)


if __name__ == "__main__":
    try:
        Watchdog().run()
    except KeyboardInterrupt:
        sys.exit(0)
