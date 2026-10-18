#!/usr/bin/env python3
"""Headless check of the route viewer against route.example.json (needs playwright + chromium; map tiles are blocked, so no internet is used).
   python3 test_viewer.py [--shot out.png]"""
import http.server, json, os, shutil, sys, tempfile, threading, functools
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
for f in ("index.html", "route.css", "route.js"):
    shutil.copy(os.path.join(HERE, f), tmp)
shutil.copytree(os.path.join(HERE, "vendor"), os.path.join(tmp, "vendor"))
posts = []

class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        posts.append(self.path); self.send_response(204); self.end_headers()

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=tmp))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d/" % srv.server_address[1]
doc = json.load(open(os.path.join(HERE, "route.example.json")))
put = lambda d: open(os.path.join(tmp, "route.json"), "w").write(json.dumps(d))
fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 900})
    pg.route("**/tile.openstreetmap.org/**", lambda r: r.abort() if os.environ.get("NO_TILES", "1") == "1" else r.continue_())
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    # 1. no document yet -> empty state, buttons disabled
    pg.goto(base + "index.html?poll=1")
    pg.wait_for_selector("#dest")
    check("empty state", pg.inner_text("#dest") == "No active route" and pg.is_disabled("#close"))
    # 2. a route appears
    put(doc); pg.wait_for_function("document.getElementById('dest').textContent==='Example Museum'", timeout=5000)
    check("destination and ETA", pg.inner_text("#eta") == "21 min" and "3.2 mi" in pg.inner_text("#dist"))
    check("traffic note", "+7 min from traffic" in pg.inner_text("#traffic"))
    check("ordered steps", pg.locator("#steps li").count() == 5 and pg.locator("#steps li").first.inner_text().startswith("Head north"))
    check("polylines drawn (selected + alternate)", pg.locator("path.leaflet-interactive").count() >= 4)   # 2 lines + 2 markers
    check("two route tabs", pg.locator("#tabs button").count() == 2)
    pg.screenshot(path=sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else os.path.join(tmp, "a.png"))
    # 3. alternative route tab
    pg.locator("#tabs button").nth(1).click()
    check("alternate selected", pg.inner_text("#eta") == "25 min" and pg.locator("#steps li").count() == 3)
    # 4. same document re-polled keeps the user's tab; a new route id replaces it
    pg.wait_for_timeout(2200)
    check("poll does not reset tab", pg.inner_text("#eta") == "25 min")
    new = dict(doc, id="example02", destination={"lat": 40.75, "lng": -73.99, "label": "Second Place"}, selected=0)
    put(new); pg.wait_for_function("document.getElementById('dest').textContent==='Second Place'", timeout=5000)
    check("new route replaces old", pg.inner_text("#eta") == "21 min")
    # 5. actions
    pg.click("#close"); pg.wait_for_timeout(400)
    pg.click("#phone"); pg.wait_for_timeout(400)
    check("close/phone POST their action URLs", posts == ["/action/close", "/action/phone"])
    # 6. route removed -> empty again
    os.remove(os.path.join(tmp, "route.json")); pg.wait_for_function("document.getElementById('dest').textContent==='No active route'", timeout=5000)
    check("route removed -> empty state", pg.is_disabled("#close"))
    # 6a. the live bug: Home Assistant loads the viewer in an iframe that is hidden or zero-sized, and shows it later. The map used to fit the route to a box with no size
    #     and stay zoomed into one spot (zoom 19, neither endpoint in view). The whole route must be in view once the iframe appears.
    put(doc); pg.wait_for_function("document.getElementById('dest').textContent==='Example Museum'", timeout=5000)
    host = b.new_page(viewport={"width": 1300, "height": 800}); host.route("**/tile.openstreetmap.org/**", lambda r: r.abort())
    host.set_content('<body style="margin:0"><iframe id="f" src="%sindex.html" style="width:1100px;height:0;border:0;display:none"></iframe></body>' % base); host.wait_for_timeout(1500)
    host.evaluate("var f = document.getElementById('f'); f.style.display = 'block'; f.style.height = '620px'"); host.wait_for_timeout(1500)
    probe = """() => { const v = window.__routeViewer, m = v.map, b = m.getBounds(), d = v.state().doc;
                      return { zoom: m.getZoom(), o: b.contains([d.origin.lat, d.origin.lng]), t: b.contains([d.destination.lat, d.destination.lng]) }; }"""
    r = host.frames[1].evaluate(probe)
    check("route fully in view when the iframe is shown after loading hidden (zoom %s)" % r["zoom"], r["o"] and r["t"] and r["zoom"] < 15)
    host.frames[1].evaluate("document.getElementById('map').dispatchEvent(new Event('wheel'))")      # the user touches the map ...
    z = host.frames[1].evaluate("window.__routeViewer.map.getZoom()")
    host.evaluate("document.getElementById('f').style.height = '500px'"); host.wait_for_timeout(600)
    check("after the user touches the map, a resize does not refit it", host.frames[1].evaluate("window.__routeViewer.map.getZoom()") == z)
    host.close()
    # 6b. an expired signed link (401) says so, and keeps the route
    put(doc); pg.wait_for_function("document.getElementById('dest').textContent==='Example Museum'", timeout=5000)
    pg.route("**/route.json*", lambda r: r.fulfill(status=401, body="expired")); pg.wait_for_function("document.getElementById('msg').textContent.includes('expired')", timeout=6000)
    check("expired link is reported and the route stays", pg.inner_text("#dest") == "Example Museum"); pg.unroute("**/route.json*")
    # 7. server error keeps last route and says so
    put(doc); pg.wait_for_function("document.getElementById('dest').textContent==='Example Museum'", timeout=5000)
    srv.shutdown(); srv.server_close(); pg.wait_for_function("document.getElementById('msg').textContent.includes('Showing the last route')", timeout=6000)
    check("outage keeps last route and reports it", pg.inner_text("#dest") == "Example Museum")
    check("no JS errors", not errs)
    b.close()
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
