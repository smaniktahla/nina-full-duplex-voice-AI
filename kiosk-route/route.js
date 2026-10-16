/* Kitchen kiosk route viewer. Standalone: reads one JSON document (?src=URL, default route.json), draws it, and polls it so a new route
 * replaces the old one and an empty/missing document shows "No active route". It holds no credentials: routing happens server side and
 * the document carries only geometry, text and two opaque action URLs. See README.md for the document shape. */
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var SRC = q.get("src") || "route.json";
  var POLL_MS = Math.max(1000, (parseInt(q.get("poll"), 10) || 3) * 1000);
  var TILES = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
  var ATTR = "&copy; OpenStreetMap contributors &copy; CARTO";
  var $ = function (id) { return document.getElementById(id); };

  var map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer(TILES, { subdomains: "abcd", maxZoom: 19, attribution: ATTR }).addTo(map);
  map.setView([39.5, -98.35], 4);
  var layers = L.layerGroup().addTo(map);

  var doc = null, selected = 0, lastKey = null;

  function decode(str) {                       // Google encoded polyline, precision 1e5
    var i = 0, lat = 0, lng = 0, out = [];
    while (i < str.length) {
      for (var k = 0; k < 2; k++) {
        var b, shift = 0, res = 0;
        do { b = str.charCodeAt(i++) - 63; res |= (b & 31) << shift; shift += 5; } while (b >= 32);
        var d = (res & 1) ? ~(res >> 1) : (res >> 1);
        if (k === 0) lat += d; else lng += d;
      }
      out.push([lat / 1e5, lng / 1e5]);
    }
    return out;
  }
  function points(r) { return typeof r.polyline === "string" ? decode(r.polyline) : (r.polyline || []); }
  function mins(s) { var m = Math.max(1, Math.round(s / 60)); return m < 60 ? m + " min" : Math.floor(m / 60) + " h " + (m % 60 ? (m % 60) + " min" : ""); }
  function miles(m) { var v = m / 1609.344; return (v < 10 ? v.toFixed(1) : Math.round(v)) + " mi"; }
  function ft(m) { var f = m * 3.28084; return f < 1000 ? Math.round(f / 10) * 10 + " ft" : miles(m); }
  function msg(t) { $("msg").textContent = t || ""; }

  function renderEmpty(why) {
    layers.clearLayers();
    $("dest").textContent = "No active route";
    $("eta").textContent = ""; $("dist").textContent = ""; $("traffic").textContent = why || "";
    $("tabs").replaceChildren(); $("steps").replaceChildren();
    $("phone").disabled = true; $("close").disabled = true;
  }

  function render() {
    var routes = (doc && doc.routes) || [];
    if (!routes.length) return renderEmpty();
    if (selected >= routes.length) selected = 0;
    var r = routes[selected];
    $("dest").textContent = (doc.destination && doc.destination.label) || doc.title || "Destination";
    $("eta").textContent = mins(r.duration_s);
    $("dist").textContent = miles(r.distance_m);
    var extra = (r.duration_s || 0) - (r.static_duration_s || r.duration_s || 0);
    $("traffic").textContent = extra >= 300 ? "+" + mins(extra) + " from traffic" : (r.summary ? "via " + r.summary : "");

    var tabs = $("tabs"); tabs.replaceChildren();
    if (routes.length > 1) routes.forEach(function (rt, i) {
      var b = document.createElement("button"); b.type = "button";
      b.textContent = (i === 0 ? "Fastest" : "Alt " + i) + " · " + mins(rt.duration_s);
      b.setAttribute("aria-pressed", i === selected ? "true" : "false");
      b.onclick = function () { selected = i; render(); };
      tabs.appendChild(b);
    });

    var ol = $("steps"); ol.replaceChildren();
    (r.steps || []).forEach(function (s) {
      var li = document.createElement("li");
      li.textContent = s.instruction || "";
      if (s.distance_m) { var d = document.createElement("span"); d.className = "d"; d.textContent = ft(s.distance_m); li.appendChild(d); }
      ol.appendChild(li);
    });

    layers.clearLayers();
    var all = [];
    routes.forEach(function (rt, i) {                 // alternates first and thin so the selected one draws on top
      if (i === selected) return;
      var p = points(rt); if (p.length) { L.polyline(p, { color: "#6b7785", weight: 5, opacity: .8 }).addTo(layers); all = all.concat(p); }
    });
    var sel = points(r);
    if (sel.length) { L.polyline(sel, { color: "#3aa0ff", weight: 7, opacity: .95 }).addTo(layers); all = all.concat(sel); }
    if (doc.origin) L.circleMarker([doc.origin.lat, doc.origin.lng], { radius: 9, color: "#fff", weight: 3, fillColor: "#2ecc71", fillOpacity: 1 }).addTo(layers).bindTooltip(doc.origin.label || "Start");
    if (doc.destination && doc.destination.lat != null) {
      L.circleMarker([doc.destination.lat, doc.destination.lng], { radius: 9, color: "#fff", weight: 3, fillColor: "#e74c3c", fillOpacity: 1 }).addTo(layers);
      all.push([doc.destination.lat, doc.destination.lng]);
    }
    if (all.length) map.fitBounds(all, { padding: [40, 40] });
    $("phone").disabled = !(doc.actions && doc.actions.phone);
    $("close").disabled = !(doc.actions && doc.actions.close);
  }

  function act(name, btn) {
    var url = doc && doc.actions && doc.actions[name];
    if (!url) return;
    btn.disabled = true; msg(name === "close" ? "Closing…" : "Sending…");
    fetch(url, { method: "POST", keepalive: true })           // same origin as the page, so the real result is readable
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); msg(name === "close" ? "" : "Sent. Check your phone."); })
      .catch(function () { msg("That didn't go through."); })
      .then(function () { btn.disabled = false; });
  }
  $("phone").onclick = function () { act("phone", this); };
  $("close").onclick = function () { act("close", this); };

  function poll() {
    fetch(SRC, { cache: "no-store" }).then(function (r) {
      if (r.status === 401 || r.status === 403) { var e = new Error("expired"); e.expired = true; throw e; }
      if (r.status === 404 || r.status === 204) return null;
      if (!r.ok) throw new Error("http " + r.status);
      return r.text().then(function (t) { return t.trim() ? JSON.parse(t) : null; });
    }).then(function (d) {
      var key = d ? (d.id || "") + "|" + (d.updated || "") : "none";
      if (key === lastKey) return;                    // unchanged: leave the user's alt-route tab and map pan alone
      lastKey = key; doc = d;
      selected = d && typeof d.selected === "number" ? d.selected : 0;
      if (d) { msg(""); render(); } else renderEmpty();
    }).catch(function (e) {
      msg(e && e.expired ? "This link has expired. Ask Nina to show the route again." : "Can't reach the route service. Showing the last route.");
    });
  }
  poll(); setInterval(poll, POLL_MS);
  window.__routeViewer = { render: render, state: function () { return { doc: doc, selected: selected }; } };   // test hook
})();
