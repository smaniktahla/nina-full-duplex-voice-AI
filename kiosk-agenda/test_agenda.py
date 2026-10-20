#!/usr/bin/env python3
"""Headless check of the agenda viewer (playwright + chromium, no network).   python3 test_agenda.py [--shot out.png]"""
import datetime as dt, functools, http.server, json, os, shutil, sys, tempfile, threading
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
for f in ("index.html", "agenda.css", "agenda.js"):
    shutil.copy(os.path.join(HERE, f), tmp)

class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=tmp))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d/" % srv.server_address[1]
doc = json.load(open(os.path.join(HERE, "agenda.example.json")))
put = lambda d: open(os.path.join(tmp, "agenda.json"), "w").write(json.dumps(d))
fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(base + "index.html?poll=1"); pg.wait_for_selector("#title")
    check("no document: says so", "No agenda requested" in pg.inner_text("#empty"))
    put(doc); pg.wait_for_function("document.getElementById('title').textContent==='Tomorrow'", timeout=5000)
    check("date line", pg.inner_text("#date") == "Monday, October 19")
    rows = pg.locator(".ev"); check("six timed events in order", rows.count() == 6 and "Morning standup" in rows.nth(0).inner_text() and "Dinner" in rows.nth(5).inner_text())
    check("times shown with minutes and end time", "9:00 AM" in rows.nth(0).inner_text() and "to 9:30 AM" in rows.nth(0).inner_text())
    check("all-day strip", pg.locator(".allday span").count() == 1 and pg.inner_text(".allday span") == "School holiday")
    check("location shown (one line, no country)", "12 Example St, Town, ST 11111" in rows.nth(1).inner_text() and "USA" not in rows.nth(1).inner_text())
    check("hold-time bar is shown", pg.is_visible("#timer"))
    pg.screenshot(path=sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else os.path.join(tmp, "a.png"))
    # today: past dimmed, current highlighted, overlap flagged
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    mk = lambda t, a, z: {"title": t, "all_day": False, "start": (now + dt.timedelta(minutes=a)).isoformat(), "end": (now + dt.timedelta(minutes=z)).isoformat(), "time": "x", "end_time": "", "location": ""}
    today = dict(doc, id="t1", title="Today", days=[{"date": "d", "label": "Today", "relative": "Today", "today": True, "events": [mk("Past", -180, -120), mk("Current", -10, 30), mk("Overlap", 10, 50), mk("Later", 120, 180)]}])
    put(today); pg.wait_for_function("document.getElementById('title').textContent==='Today'", timeout=5000)
    check("today: past dimmed, current highlighted, overlap flagged", pg.locator(".ev.past").count() == 1 and pg.locator(".ev.now").count() == 1 and pg.locator(".ev.clash").count() == 1 and "Current" in pg.inner_text(".ev.now"))
    # several days: compact sections, empty day says so
    week = dict(doc, id="w1", title="This week", days=[dict(doc["days"][0], relative="Tomorrow"), {"date": "2026-10-20", "label": "Tuesday, October 20", "relative": "", "today": False, "events": []}])
    put(week); pg.wait_for_function("document.getElementById('title').textContent==='This week'", timeout=5000)
    check("week: one heading per day, empty day says nothing scheduled", pg.locator(".day h2").count() == 2 and "Nothing scheduled" in pg.inner_text(".day:nth-child(2)"))
    # an empty single day
    put(dict(doc, id="e1", title="Friday", days=[{"date": "2026-10-23", "label": "Friday, October 23", "relative": "", "today": False, "events": []}]))
    pg.wait_for_function("document.getElementById('title').textContent==='Friday'", timeout=5000)
    check("empty day: 'Nothing scheduled.'", pg.is_visible("#empty") and "Nothing scheduled" in pg.inner_text("#empty"))
    # titles are text, never HTML
    put(dict(doc, id="x1", title="X", days=[{"date": "d", "label": "L", "today": False, "events": [{"title": "<img src=x onerror=window.__pwned=1>", "all_day": False, "start": now.isoformat(), "end": now.isoformat(), "time": "1:00 PM", "location": "<b>b</b>"}]}]))
    pg.wait_for_function("document.getElementById('title').textContent==='X'", timeout=5000)
    check("event text is not interpreted as HTML", pg.evaluate("window.__pwned") is None and pg.locator(".ev img").count() == 0 and "<img" in pg.inner_text(".ev"))
    # countdown continues locally, labels stay text, and returning to agenda clears it
    put({"kind": "timer", "id": "timer1", "updated": "1", "title": "Turkey <b>oven</b>", "remaining_s": 2, "hold_s": 60})
    pg.wait_for_selector(".countdown", timeout=5000)
    check("timer label stays text", pg.inner_text("#title") == "Turkey <b>oven</b>" and pg.locator("#title b").count() == 0)
    pg.wait_for_function("document.querySelector('.countdown').textContent === 'Time is up!'", timeout=4000)
    check("timer expires locally", True)
    put(dict(doc, id="back-to-agenda"))
    pg.wait_for_function("document.getElementById('title').textContent==='Tomorrow'", timeout=5000)
    check("agenda replaces countdown", pg.locator(".countdown").count() == 0 and pg.locator(".ev").count() == 6)
    # signed dismissal action: retry failures and clear only on successful POST
    put({"kind": "timer", "id": "dismiss-me", "updated": "1", "title": "Timer", "remaining_s": 0, "stop_action": "../action/timer-stop"})
    pg.wait_for_selector(".timer-stop", timeout=5000)
    pg.route("**/action/timer-stop", lambda r: r.fulfill(status=502, content_type="application/json", body='{"ok":false}'))
    pg.click(".timer-stop")
    pg.wait_for_function("document.getElementById('msg').textContent.includes('Try again')")
    check("failed dismiss allows retry", pg.is_enabled(".timer-stop"))
    pg.unroute("**/action/timer-stop")
    dismissals = []
    def dismiss(route):
        dismissals.append((route.request.method, route.request.post_data_json))
        put(None)
        route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
    pg.route("**/action/timer-stop", dismiss)
    pg.click(".timer-stop")
    pg.wait_for_function("document.getElementById('empty').textContent.includes('No agenda requested')")
    check("dismiss sends timer id and clears popup", dismissals == [("POST", {"id": "dismiss-me"})] and pg.locator(".timer-stop").count() == 0)
    # expired link
    pg.route("**/agenda.json*", lambda r: r.fulfill(status=401, body="expired")); pg.wait_for_function("document.getElementById('msg').textContent.includes('expired')", timeout=6000)
    check("expired link is reported", True); pg.unroute("**/agenda.json*")
    check("no JS errors", not errs)
    b.close()
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
