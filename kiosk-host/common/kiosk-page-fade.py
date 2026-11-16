#!/usr/bin/env python3
"""kiosk-page-fade: fade out/in when the dashboard rotation switches dashboards.

Home Assistant switches dashboards inside one page: history.pushState(path) then a window
'location-changed' event. A small script (injected over Chrome's loopback DevTools port, and
re-injected on every page load) fades a black overlay in, holds the event back until it is dark,
delivers it, then fades back in once the new dashboard has had a moment to render. Only a change of
dashboard (first path segment) fades; moving between views of one dashboard does not.

Env: CDP_PORT (9222), FADE_OUT_MS (450), HOLD_MS (500), FADE_IN_MS (800). FADE_OUT_MS=0 disables.
"""
import json, os, threading, time, urllib.request
import websocket  # websocket-client

PORT = int(os.environ.get("CDP_PORT", "9222"))
OUT, HOLD, IN = (int(os.environ.get(k, d)) for k, d in (("FADE_OUT_MS", "450"), ("HOLD_MS", "500"), ("FADE_IN_MS", "800")))

JS = """(() => {
  if (window.__kpf) return 'present';
  window.__kpf = true;
  const OUT = %d, HOLD = %d, IN = %d;
  let ov = null, pending = false;
  const overlay = () => {
    if (!ov || !ov.isConnected) {
      ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:#000;opacity:0;z-index:2147483646;pointer-events:none;transition:none';
      document.documentElement.appendChild(ov);
    }
    return ov;
  };
  const seg = p => (p.split('/')[1] || '');
  let current = seg(location.pathname);
  const dispatch = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (e) {
    if (this === window && e && e.type === 'location-changed') {
      const next = seg(location.pathname);
      if (next !== current && !pending) {
        const dashboardChanged = true;
        pending = true; current = next;
        const o = overlay(); o.getBoundingClientRect();
        o.style.transition = 'opacity ' + OUT + 'ms ease-in-out'; o.style.opacity = '1';
        setTimeout(() => {
          dispatch.call(window, e);                      // dark now: let HA switch dashboards
          setTimeout(() => {
            o.style.transition = 'opacity ' + IN + 'ms ease-in-out'; o.style.opacity = '0';
            setTimeout(() => { pending = false; }, IN);
          }, HOLD);
        }, OUT);
        return true;
      }
      current = next;
    }
    return dispatch.call(this, e);
  };
  return 'installed';
})()""" % (OUT, HOLD, IN)


def serve(target):
    """Hold a DevTools session on one page: inject now and on every new document."""
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    ws.send(json.dumps({"id": 2, "method": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": JS}}))
    ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": JS}}))
    print("injected into", target["url"], flush=True)
    try:
        while True:
            try:
                ws.recv()                       # drain events; the session (and the registration) lives while we do
            except websocket.WebSocketTimeoutException:
                pass
    finally:
        ws.close()


def main():
    if OUT <= 0:
        print("disabled (FADE_OUT_MS=0)", flush=True)
        while True:
            time.sleep(3600)
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
