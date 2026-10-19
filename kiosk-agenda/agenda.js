/* Kitchen kiosk agenda viewer. Standalone: reads one JSON document (?src=URL, default agenda.json), draws the day(s), and polls it so a new request replaces what is
 * shown. It holds no credentials and no calendar access: the voice server builds the document from the household calendars. Every string is set with textContent. */
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var SRC = q.get("src") || "agenda.json";
  var POLL_MS = Math.max(1000, (parseInt(q.get("poll"), 10) || 5) * 1000);
  var $ = function (id) { return document.getElementById(id); };
  var lastKey = null, tick = null;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function clock(iso) { var d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }

  function render(doc) {
    var days = doc.days || [], multi = days.length > 1;
    $("title").textContent = doc.title || "Agenda";
    $("date").textContent = multi ? "" : (days[0] && days[0].label) || "";
    var host = $("days"); host.replaceChildren(); host.className = multi ? "multi" : "";
    var total = 0, now = Date.now();
    days.forEach(function (day) {
      var sec = el("div", "day");
      if (multi) sec.appendChild(el("h2", "", (day.relative ? day.relative + " · " : "") + day.label));
      var all = (day.events || []).filter(function (e) { return e.all_day; });
      var timed = (day.events || []).filter(function (e) { return !e.all_day; });
      if (all.length) { var strip = el("div", "allday"); all.forEach(function (e) { strip.appendChild(el("span", "", e.title)); }); sec.appendChild(strip); }
      var prevEnd = 0;
      timed.forEach(function (e) {
        var s = Date.parse(e.start), f = Date.parse(e.end || e.start);
        var row = el("div", "ev"), t = el("div", "t", e.time || clock(e.start));
        if (e.end_time) t.appendChild(el("small", "", "to " + e.end_time));
        var body = el("div", "b"); body.appendChild(el("div", "n", e.title));
        if (e.location) body.appendChild(el("div", "l", e.location));
        row.appendChild(t); row.appendChild(body);
        if (day.today && f && f < now) row.classList.add("past");
        else if (day.today && s <= now && now < f) row.classList.add("now");
        if (prevEnd && s && s < prevEnd) row.classList.add("clash");
        prevEnd = Math.max(prevEnd, f || 0);
        sec.appendChild(row);
      });
      if (multi && !all.length && !timed.length) sec.appendChild(el("div", "ev", "")).appendChild(el("div", "n", "Nothing scheduled"));
      total += all.length + timed.length;
      host.appendChild(sec);
    });
    $("empty").textContent = "Nothing scheduled."; $("empty").hidden = !(total === 0 && !multi);
    // a thin bar that drains over the hold time, so the screen's return to the dashboards is not a surprise
    var tm = $("timer"); clearTimeout(tick);
    if (doc.hold_s) { tm.hidden = false; var i = tm.firstElementChild; i.style.transition = "none"; i.style.transform = "scaleX(1)";
      requestAnimationFrame(function () { requestAnimationFrame(function () { i.style.transition = "transform " + doc.hold_s + "s linear"; i.style.transform = "scaleX(0)"; }); }); }
    else tm.hidden = true;
  }

  function poll() {
    fetch(SRC, { cache: "no-store" }).then(function (r) {
      if (r.status === 401 || r.status === 403) { var e = new Error("expired"); e.expired = true; throw e; }
      if (r.status === 404 || r.status === 204) return null;
      if (!r.ok) throw new Error("http " + r.status);
      return r.text().then(function (t) { return t.trim() ? JSON.parse(t) : null; });
    }).then(function (d) {
      var key = d ? (d.id || "") + "|" + (d.updated || "") : "none";
      if (key === lastKey) return;
      lastKey = key; $("msg").textContent = "";
      if (d) render(d); else { $("days").replaceChildren(); $("title").textContent = "Agenda"; $("date").textContent = ""; $("empty").hidden = false; $("empty").textContent = "No agenda requested."; $("timer").hidden = true; }
    }).catch(function (e) {
      $("msg").textContent = e && e.expired ? "This link has expired. Ask Nina again." : "Can't reach the agenda service. Showing the last agenda.";
    });
  }
  poll(); setInterval(poll, POLL_MS);
  window.__agendaViewer = { render: render };            // test hook
})();
