#!/usr/bin/env python3
"""kiosk-page-fade: animate the switch between dashboards.

Home Assistant switches dashboards inside one page: history.pushState(path) then a window
'location-changed' event. A script (injected over Chrome's loopback DevTools port, and re-injected on
every page load) intercepts that event. Only a change of dashboard (first path segment) animates;
moving between views of one dashboard does not.

Setting (JSON, re-read live every few seconds, no restart): ~/.config/kiosk/page-fade.json
  {"mode": "fade", "seconds": 0.6, "hold_seconds": 0.5}
  When KIOSK_EFFECTS_URL and KIOSK_ID are set, the Dashboard Manager's "Kiosk Effects" card (served as
  http://<ha>/local/kiosk_effects.json) overrides this file live; the file is the fallback.
  mode          "none"       switch instantly
                "fade"       fade to black, switch while dark, fade back in
                "crossfade"  keep the old dashboard on screen and blend into the new one
                             (Chrome View Transitions)
  seconds       length of each fade (fade: out and in; crossfade: the blend)
  hold_seconds  time allowed for the new dashboard to render after the switch
Env: CDP_PORT (9222), KPF_CONFIG (config path), KIOSK_EFFECTS_URL, KIOSK_ID.
Also, for 2 minutes after each Chrome launch, closes hidden duplicate Home Assistant tabs (restarts leave
extra kiosk tabs that keep rendering dashboards and slow the switch animations); later tabs are left alone.
"""
import json, os, threading, time, urllib.request
import websocket  # websocket-client

PORT = int(os.environ.get("CDP_PORT", "9222"))
CONFIG = os.path.expanduser(os.environ.get("KPF_CONFIG", "~/.config/kiosk/page-fade.json"))
DEFAULT = {"mode": "fade", "seconds": 0.6, "hold_seconds": 0.5}

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



def load_config():
    try:
        c = dict(DEFAULT, **json.load(open(CONFIG)))
        fx = ha_effects()                                  # the Dashboard Manager UI wins over the local file
        if fx:
            c.update(mode=fx.get("transition_mode", c["mode"]), seconds=fx.get("transition_seconds", c["seconds"]),
                     hold_seconds=fx.get("transition_hold", c["hold_seconds"]))
        if c["mode"] not in ("none", "fade", "crossfade"):
            raise ValueError("mode")
        c["seconds"] = max(0.0, float(c["seconds"])); c["hold_seconds"] = max(0.0, float(c["hold_seconds"]))
        return c
    except FileNotFoundError:
        fx = ha_effects()
        return dict(DEFAULT, mode=fx.get("transition_mode", "fade"), seconds=fx.get("transition_seconds", 0.6),
                    hold_seconds=fx.get("transition_hold", 0.5)) if fx else dict(DEFAULT)
    except Exception as e:
        print("bad config, using defaults:", e, flush=True)
        return dict(DEFAULT)


JS_TEMPLATE = """(() => {
  window.__kpfCfg = %CFG%;
  if (window.__kpfV === 2) return 'config updated';       // already running this version: only the setting changed
  window.__kpfV = 2;
  let ov = null, pending = false;
  const seg = p => (p.split('/')[1] || '');
  let current = seg(location.pathname);
  const dispatch = window.__kpfBase || EventTarget.prototype.dispatchEvent;   // keep the page's own dispatchEvent
  window.__kpfBase = dispatch;
  const overlay = () => {
    if (!ov || !ov.isConnected) {
      ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:#000;opacity:0;z-index:2147483646;pointer-events:none;transition:none';
      document.documentElement.appendChild(ov);
    }
    return ov;
  };
  const vtStyle = sec => {
    let s = document.getElementById('kpf-style');
    if (!s) { s = document.createElement('style'); s.id = 'kpf-style'; document.head.appendChild(s); }
    s.textContent = '::view-transition-group(root){animation-duration:' + sec + 's}' +
      '::view-transition-old(root),::view-transition-new(root){animation-duration:' + sec + 's;animation-timing-function:ease-in-out}';
  };
  const watchFrames = (mode, ms) => {            // debug: worst frame gap during the transition
    const t0 = performance.now(); let last = t0, worst = 0, n = 0;
    const tick = t => { worst = Math.max(worst, t - last); last = t; n++;
      if (t - t0 < ms + 400) requestAnimationFrame(tick); else window.__kpfLast = {mode, frames: n, worstGapMs: Math.round(worst)}; };
    requestAnimationFrame(tick);
  };
  EventTarget.prototype.dispatchEvent = function (e) {
    if (this === window && e && e.type === 'location-changed') {
      const cfg = window.__kpfCfg, next = seg(location.pathname);
      if (next !== current && !pending && cfg.mode !== 'none' && cfg.seconds > 0) {
        current = next; pending = true;
        const sec = cfg.seconds, hold = cfg.hold_seconds * 1000;
        if (cfg.mode === 'crossfade' && document.startViewTransition) {
          vtStyle(sec);
          watchFrames('crossfade', sec * 1000 + hold);
          const vt = document.startViewTransition(() => new Promise(res => { dispatch.call(window, e); setTimeout(res, hold); }));
          vt.finished.finally(() => { pending = false; });
        } else {
          watchFrames('fade', 2 * sec * 1000 + hold);
          const o = overlay(); o.getBoundingClientRect();
          o.style.transition = 'opacity ' + sec + 's ease-in-out'; o.style.opacity = '1';
          setTimeout(() => {
            dispatch.call(window, e);                    // dark now: let HA switch dashboards
            setTimeout(() => {
              o.style.transition = 'opacity ' + sec + 's ease-in-out'; o.style.opacity = '0';
              setTimeout(() => { pending = false; }, sec * 1000);
            }, hold);
          }, sec * 1000);
        }
        return true;
      }
      current = next;
    }
    return dispatch.call(this, e);
  };
  return 'installed';
})()"""


def make_js(cfg):
    return JS_TEMPLATE.replace("%CFG%", json.dumps(cfg))


def serve(target):
    """Hold a DevTools session on one page: inject now and on every new document; follow config edits."""
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
    n = [10]
    script_id = [None]

    def send(method, params=None):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
        return n[0]

    cfg = load_config()
    mtime = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0
    send("Page.enable")
    add_id = send("Page.addScriptToEvaluateOnNewDocument", {"source": make_js(cfg)})
    send("Runtime.evaluate", {"expression": make_js(cfg)})
    print("injected into", target["url"], cfg, flush=True)
    last_check = time.time()
    try:
        while True:
            try:
                m = json.loads(ws.recv())
                if m.get("id") == add_id and "result" in m:
                    script_id[0] = m["result"].get("identifier")
            except websocket.WebSocketTimeoutException:
                pass
            if time.time() - last_check > 3:
                last_check = time.time()
                new = load_config()
                if new != cfg:
                    cfg = new
                    if script_id[0]:
                        send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id[0]})
                    add_id = send("Page.addScriptToEvaluateOnNewDocument", {"source": make_js(cfg)})
                    send("Runtime.evaluate", {"expression": make_js(cfg)})
                    print("config changed:", cfg, flush=True)
    finally:
        ws.close()


def hidden_ha_duplicates(pages):
    """Hidden Home Assistant tabs, when another tab is visible (Chrome restarts leave duplicate kiosk tabs)."""
    base = "/".join(EFFECTS_URL.split("/")[:3]) if EFFECTS_URL else ""
    if not base or len(pages) < 2:
        return []
    state = {}
    for t in pages:
        try:
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=3, suppress_origin=True)
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "document.visibilityState", "returnByValue": True}}))
            state[t["id"]] = json.loads(ws.recv())["result"]["result"].get("value")
            ws.close()
        except Exception:
            pass
    if "visible" not in state.values():
        return []
    return [t for t in pages if state.get(t["id"]) == "hidden" and t["url"].startswith(base)]


def main():
    active = {}
    browser, seen = None, 0.0
    while True:
        try:
            ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3))["webSocketDebuggerUrl"]
            if ver != browser:
                browser, seen = ver, time.time()               # a new Chrome launch
            if time.time() - seen < 120:                       # only the start-up burst; later tabs are yours (troubleshooting)
                pages = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3)) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                for t in hidden_ha_duplicates(pages):
                    urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{t['id']}", timeout=3).read()
                    print("closed duplicate hidden tab", t["url"], flush=True)
        except Exception:
            pass
        try:
            for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3)):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl") and (t["id"] not in active or not active[t["id"]].is_alive()):
                    th = threading.Thread(target=lambda t=t: serve(t), daemon=True)
                    active[t["id"]] = th
                    th.start()
        except Exception as e:
            print("waiting for Chrome:", e, flush=True)
        time.sleep(5)


main()
