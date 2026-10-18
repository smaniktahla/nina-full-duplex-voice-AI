/* Kitchen kiosk route viewer. Standalone: reads one JSON document (?src=URL, default route.json), draws it, and polls it so a new route
 * replaces the old one and an empty/missing document shows "No active route". It holds no credentials: routing happens server side and
 * the document carries only geometry, text and two opaque action URLs. See README.md for the document shape. */
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var SRC = q.get("src") || "route.json";
  var POLL_MS = Math.max(1000, (parseInt(q.get("poll"), 10) || 3) * 1000);
  // OpenStreetMap's own tiles need no key (CARTO's free basemaps now stamp "API KEY REQUIRED" over every tile). They are light, so a CSS filter on the tile
  // pane makes them dark (css class dark-tiles); ?tiles=<url template> overrides the source and ?light=1 turns the filter off.
  var TILES = q.get("tiles") || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  var ATTR = "&copy; OpenStreetMap contributors";
  var $ = function (id) { return document.getElementById(id); };

  var map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer(TILES, { maxZoom: 19, attribution: ATTR, className: q.get("light") ? "" : "dark-tiles" }).addTo(map);
  map.setView([39.5, -98.35], 4);
  var layers = L.layerGroup().addTo(map);

  var TRAFFIC_COLORS = { NORMAL: "#3ddc84", SLOW: "#ffb020", TRAFFIC_JAM: "#ff4d4f" };      // clear / slow / heavy
  var doc = null, selected = 0, lastKey = null, fitPoints = null, userMoved = false;

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

  // The selected route: a dark casing, the plain blue line, then coloured stretches on top where Google reported the speed (so a stretch it did not report stays blue,
  // never green). Intervals index the decoded polyline: [start inclusive, end exclusive), so each stretch is drawn through its end point to join the next.
  function drawSelected(pts, traffic) {
    L.polyline(pts, { color: "#0b0f14", weight: 12, opacity: .85 }).addTo(layers);
    L.polyline(pts, { color: "#3aa0ff", weight: 7, opacity: .95 }).addTo(layers);
    (traffic || []).forEach(function (t) {
      var seg = pts.slice(t[0], t[1] + 1);
      if (seg.length > 1 && TRAFFIC_COLORS[t[2]]) L.polyline(seg, { color: TRAFFIC_COLORS[t[2]], weight: 7, opacity: .98, lineCap: "butt" }).addTo(layers);
    });
  }
  function drawLegend(pts, traffic) {
    var el = $("legend"); el.replaceChildren();
    if (!traffic || !traffic.length) return;
    var m = { NORMAL: 0, SLOW: 0, TRAFFIC_JAM: 0 };
    traffic.forEach(function (t) { var seg = pts.slice(t[0], t[1] + 1); for (var i = 1; i < seg.length; i++) if (m[t[2]] != null) m[t[2]] += map.distance(seg[i - 1], seg[i]); });
    [["NORMAL", "clear"], ["SLOW", "slow"], ["TRAFFIC_JAM", "heavy"]].forEach(function (k) {
      var span = document.createElement("span"), sw = document.createElement("i"); sw.style.background = TRAFFIC_COLORS[k[0]]; span.appendChild(sw);
      span.appendChild(document.createTextNode(k[1] + (k[0] !== "NORMAL" && m[k[0]] >= 160 ? " " + miles(m[k[0]]) : ""))); el.appendChild(span);
    });
  }

  function renderEmpty(why) {
    layers.clearLayers();
    $("dest").textContent = "No active route";
    $("eta").textContent = ""; $("dist").textContent = ""; $("traffic").textContent = why || ""; $("legend").replaceChildren();
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
    if (sel.length) { drawSelected(sel, r.traffic); all = all.concat(sel); }
    drawLegend(sel, r.traffic);
    if (doc.origin) L.circleMarker([doc.origin.lat, doc.origin.lng], { radius: 9, color: "#fff", weight: 3, fillColor: "#2ecc71", fillOpacity: 1 }).addTo(layers).bindTooltip(doc.origin.label || "Start");
    if (doc.destination && doc.destination.lat != null) {
      L.circleMarker([doc.destination.lat, doc.destination.lng], { radius: 9, color: "#fff", weight: 3, fillColor: "#e74c3c", fillOpacity: 1 }).addTo(layers);
      all.push([doc.destination.lat, doc.destination.lng]);
    }
    fitPoints = all.length ? all : null; userMoved = false;
    refit();
    $("phone").disabled = !(doc.actions && doc.actions.phone);
    $("close").disabled = !(doc.actions && doc.actions.close);
  }

  // Fit the whole route in view. Inside an iframe (Home Assistant) the map's box often has not reached its final size when the route first arrives, and Leaflet does
  // not refit by itself when it grows, which left the map zoomed into one end of the route. So refit whenever the box changes size, until the user pans or zooms.
  function refit() {
    map.invalidateSize();
    if (fitPoints && !userMoved) map.fitBounds(fitPoints, { padding: [40, 40], animate: false });
  }
  ["mousedown", "touchstart", "wheel", "keydown"].forEach(function (ev) { $("map").addEventListener(ev, function () { userMoved = true; }, { passive: true }); });
  if (window.ResizeObserver) new ResizeObserver(function () { refit(); }).observe($("map")); else window.addEventListener("resize", refit);

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
  window.__routeViewer = { map: map, render: render, state: function () { return { doc: doc, selected: selected }; } };   // test hook
})();
