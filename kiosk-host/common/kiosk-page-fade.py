#!/usr/bin/env python3
"""kiosk-page-fade: animate the switch between dashboards.

Home Assistant switches dashboards inside one page: history.pushState(path) then a window
'location-changed' event. A script (injected over Chrome's loopback DevTools port, and re-injected on
every page load) intercepts that event. Only a change of dashboard (first path segment) animates;
moving between views of one dashboard does not.

Setting (JSON, re-read live every few seconds, no restart): ~/.config/kiosk/page-fade.json
  {"mode": "fade", "seconds": 0.6, "hold_seconds": 0.5}
  mode          "none"       switch instantly
                "fade"       fade to black, switch while dark, fade back in
                "crossfade"  keep the old dashboard on screen and blend into the new one
                             (Chrome View Transitions)
  seconds       length of each fade (fade: out and in; crossfade: the blend)
  hold_seconds  time allowed for the new dashboard to render after the switch
Env: CDP_PORT (9222), KPF_CONFIG (config path).
"""
import json, os, threading, time, urllib.request
import websocket  # websocket-client

PORT = int(os.environ.get("CDP_PORT", "9222"))
CONFIG = os.path.expanduser(os.environ.get("KPF_CONFIG", "~/.config/kiosk/page-fade.json"))
DEFAULT = {"mode": "fade", "seconds": 0.6, "hold_seconds": 0.5}


def load_config():
    try:
        c = dict(DEFAULT, **json.load(open(CONFIG)))
        if c["mode"] not in ("none", "fade", "crossfade"):
            raise ValueError("mode")
        c["seconds"] = max(0.0, float(c["seconds"])); c["hold_seconds"] = max(0.0, float(c["hold_seconds"]))
        return c
    except FileNotFoundError:
        return dict(DEFAULT)
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
                mt = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0
                if mt != mtime:
                    mtime, cfg = mt, load_config()
                    if script_id[0]:
                        send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id[0]})
                    add_id = send("Page.addScriptToEvaluateOnNewDocument", {"source": make_js(cfg)})
                    send("Runtime.evaluate", {"expression": make_js(cfg)})
                    print("config changed:", cfg, flush=True)
    finally:
        ws.close()


def main():
    active = {}
    while True:
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
