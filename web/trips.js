/* Trips section: Leaflet map + trip list. Driven by app.js via window.Trips. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const nf1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

  // Standard OpenStreetMap tiles (no key needed); darkened with a CSS filter in dark mode.
  const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const state = {
    map: null,
    tiles: null,
    theme: null,
    layers: null,     // all-trip routes
    focus: null,      // selected trip layers
    carMarker: null,
    trips: [],
    location: null,
    selected: null,
    fittedKey: null,
  };

  /* ---------------- formatting ---------------- */
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  function duration(s) {
    if (!s || s <= 0) return null;
    const m = Math.round(s / 60);
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  }
  function dayLabel(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const yest = new Date(); yest.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === yest.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  }
  function ago(iso) {
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  /* ---------------- map ---------------- */
  function currentTheme() {
    const forced = document.documentElement.dataset.theme;
    if (forced) return forced;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function ensureMap() {
    if (state.map || !window.L) return state.map;
    state.map = L.map("map", { zoomControl: true, attributionControl: true, scrollWheelZoom: false })
      .setView([57.7, 11.97], 11);
    state.map.on("focus", () => state.map.scrollWheelZoom.enable());
    state.map.on("blur", () => state.map.scrollWheelZoom.disable());
    state.map.on("click", () => select(null));
    applyTiles();
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyTiles(); draw(); });
    state.layers = L.layerGroup().addTo(state.map);
    state.focus = L.layerGroup().addTo(state.map);
    new ResizeObserver(() => state.map.invalidateSize()).observe($("map"));
    return state.map;
  }

  function applyTiles() {
    const theme = currentTheme();
    if (theme === state.theme) return;
    state.theme = theme;
    if (!state.tiles) {
      state.tiles = L.tileLayer(TILE_URL, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(state.map);
    }
    $("map").classList.toggle("map-dark", theme === "dark");
  }

  function draw() {
    const map = ensureMap();
    if (!map) return;
    state.layers.clearLayers();
    state.focus.clearLayers();
    const routeColor = css("--series-1");
    const accent = css("--accent");
    const bounds = L.latLngBounds([]);

    for (const t of state.trips) {
      const selected = state.selected === t.id;
      // Start and end only (no route from the car, none inferred yet): draw dotted
      const line = L.polyline(t.route, {
        color: routeColor,
        weight: selected ? 0 : 3,
        opacity: state.selected ? 0.25 : 0.6,
        lineJoin: "round",
        dashArray: straightOnly(t) ? "2 8" : null,
        lineCap: "round",
      });
      // Wide invisible line = easy hit target
      const hit = L.polyline(t.route, { color: "#000", opacity: 0, weight: 16 });
      hit.on("click", (e) => { L.DomEvent.stopPropagation(e); select(t.id, true); });
      hit.bindTooltip(tripLabel(t), { sticky: true, className: "trip-tip" });
      state.layers.addLayer(line).addLayer(hit);
      t.route.forEach((p) => bounds.extend(p));
    }

    const sel = state.trips.find((t) => t.id === state.selected);
    if (sel) {
      const dash = straightOnly(sel) ? "1 10" : null;
      L.polyline(sel.route, { color: css("--surface"), weight: 8, opacity: 0.9, lineJoin: "round", lineCap: "round", dashArray: dash }).addTo(state.focus);
      L.polyline(sel.route, { color: accent, weight: 5, opacity: 1, lineJoin: "round", lineCap: "round", dashArray: dash }).addTo(state.focus);
      L.circleMarker(sel.route[0], { radius: 6, color: "#fff", weight: 2, fillColor: css("--good"), fillOpacity: 1 })
        .bindTooltip("Start").addTo(state.focus);
      if (!sel.ongoing) {
        L.circleMarker(sel.route[sel.route.length - 1], { radius: 6, color: "#fff", weight: 2, fillColor: css("--text"), fillOpacity: 1 })
          .bindTooltip("End").addTo(state.focus);
      }
    }

    // Car
    const cur = state.location?.current;
    if (state.carMarker) { state.carMarker.remove(); state.carMarker = null; }
    if (cur) {
      const driving = state.location.state === "driving";
      state.carMarker = L.marker([cur.lat, cur.lon], {
        icon: L.divIcon({ className: "", html: `<div class="car-marker${driving ? " driving" : ""}"></div>`, iconSize: [18, 18], iconAnchor: [9, 9] }),
        keyboard: false,
        zIndexOffset: 1000,
      }).bindTooltip(driving ? "Driving" : "Parked here").addTo(map);
      bounds.extend([cur.lat, cur.lon]);
    }

    // Fit: on selection, or the first time a given data set appears.
    if (sel) {
      map.fitBounds(L.latLngBounds(sel.route), { padding: [40, 40], maxZoom: 16 });
    } else {
      const key = `${state.trips.length}:${state.trips[0]?.id || ""}`;
      if (bounds.isValid() && key !== state.fittedKey) {
        state.fittedKey = key;
        map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
      }
    }
  }

  /** True when all we can draw is a straight line between start and end. */
  function straightOnly(t) {
    return t.route_source !== "inferred" && t.route.length <= 2;
  }

  function tripLabel(t) {
    const when = t.start_ts ? `${dayLabel(t.start_ts)} ${clock(t.start_ts)}` : dayLabel(t.last_ts);
    return `${when} · ${nf1.format(Units.dist(t.distance_km))} ${Units.distLabel}`;
  }

  /* ---------------- list ---------------- */
  function select(id, fromMap) {
    state.selected = state.selected === id && !fromMap ? null : id;
    renderList();
    draw();
    if (fromMap && id) {
      document.querySelector(`.trip-item[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }

  function renderChip() {
    const chip = $("map-chip");
    const loc = state.location;
    chip.hidden = false;
    if (!loc || loc.available === false) {
      chip.textContent = "Location isn't available for this car";
      return;
    }
    if (!loc.current) {
      chip.textContent = "Waiting for the car's position…";
      return;
    }
    if (loc.state === "driving") {
      chip.textContent = `Driving · updated ${ago(loc.current.ts)}`;
    } else {
      const since = loc.parked?.ts || loc.current.ts;
      const d = new Date(since);
      const sameDay = d.toDateString() === new Date().toDateString();
      chip.textContent = `Parked since ${sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}`;
    }
  }

  function renderSummary(days) {
    const done = state.trips.filter((t) => !t.ongoing);
    const km = done.reduce((s, t) => s + (t.distance_km || 0), 0);
    const secs = done.reduce((s, t) => s + (t.duration_s || 0), 0);
    const el = $("trip-summary");
    el.replaceChildren();
    const stat = (value, label) => {
      const span = document.createElement("span");
      const b = document.createElement("b");
      b.textContent = value;
      span.append(b, document.createTextNode(label));
      el.appendChild(span);
    };
    stat(nf0.format(done.length), done.length === 1 ? "trip" : "trips");
    stat(nf0.format(Units.dist(km)), Units.distLabel);
    stat(secs >= 3600 ? `${Math.floor(secs / 3600)}h ${Math.round((secs % 3600) / 60)}m` : `${Math.round(secs / 60)}`, secs >= 3600 ? "driving" : "min driving");
    el.title = `Last ${days} days`;
  }

  function renderList() {
    const list = $("trip-list");
    list.replaceChildren();
    if (!state.trips.length) {
      const li = document.createElement("li");
      li.className = "trip-empty";
      li.textContent = state.location?.available === false
        ? "Trips need location data, which this car doesn't provide."
        : "No trips recorded in this period yet. Trips appear here after your next drive while the dashboard is running.";
      list.appendChild(li);
      return;
    }
    let lastDay = null;
    for (const t of state.trips) {
      const day = dayLabel(t.start_ts || t.last_ts);
      if (day !== lastDay) {
        const h = document.createElement("li");
        h.className = "trip-day";
        h.textContent = day;
        list.appendChild(h);
        lastDay = day;
      }
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "trip-item";
      btn.dataset.id = t.id;
      btn.setAttribute("aria-pressed", String(state.selected === t.id));

      const time = document.createElement("span");
      time.className = "trip-time";
      if (t.ongoing) {
        time.classList.add("trip-live");
        time.textContent = `${t.start_ts ? clock(t.start_ts) : "…"} – now`;
      } else {
        time.textContent = `${t.start_ts ? clock(t.start_ts) : "?"} – ${clock(t.end_ts)}`;
      }
      const dist = document.createElement("span");
      dist.className = "trip-dist";
      dist.textContent = `${t.distance_source === "gps" ? "≈" : ""}${nf1.format(Units.dist(t.distance_km))} ${Units.distLabel}`;

      const meta = document.createElement("span");
      meta.className = "trip-meta";
      const bits = [];
      const dur = duration(t.duration_s);
      if (dur) bits.push(dur);
      if (t.avg_speed_kmh) bits.push(`${nf0.format(Units.speed(t.avg_speed_kmh))} ${Units.speedLabel} avg`);
      if (t.soc_used != null && t.soc_start != null) {
        bits.push(`battery ${nf0.format(t.soc_start)}% → ${nf0.format(t.soc_end)}%`);
      }
      if (t.ongoing) bits.unshift("In progress");
      if (!t.ongoing) {
        if (t.route_source === "inferred") {
          const m = t.route_match;
          bits.push(m && (m < 0.85 || m > 1.15) ? "approx. route" : "route inferred from mileage");
        } else if (t.route_source === "pending") {
          bits.push("finding route…");
        } else if (straightOnly(t)) {
          bits.push("start & end only");
        }
      }
      meta.textContent = bits.join(" · ");

      btn.append(time, dist, meta);
      btn.addEventListener("click", () => select(t.id));
      li.appendChild(btn);
      list.appendChild(li);
    }
  }

  /* ---------------- public ---------------- */
  function update(data, days) {
    state.days = days;
    state.trips = data.trips || [];
    state.location = data.location || null;
    if (state.selected && !state.trips.some((t) => t.id === state.selected)) state.selected = null;
    renderChip();
    renderSummary(days);
    renderList();
    draw();
  }

  function rerender() {
    renderSummary(state.days);
    renderList();
    draw();
  }

  function reset() {
    state.selected = null;
    state.fittedKey = null;
  }

  window.Trips = { update, reset, rerender };
})();
