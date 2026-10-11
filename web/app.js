/* Polestar dashboard front-end. Talks only to the local server's /api/*. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const LIVE_POLL_MS = 15_000;
  const HISTORY_POLL_MS = 60_000;

  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const state = {
    status: null,
    cars: [],
    vin: store.get("polestar.vin", null),
    hours: Number(store.get("polestar.hours", 24)) || 24,
    angle: Number(store.get("polestar.angle", 0)) || 0,
    lastHistoryFetch: 0,
    tripDays: Number(store.get("polestar.tripDays", 7)) || 7,
    lastTripsFetch: 0,
  };

  /* ---------------- formatting ---------------- */
  const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nfAxis = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const isNum = (v) => typeof v === "number" && !isNaN(v);
  const dash = "—";
  const n0 = (v) => (isNum(v) ? nf0.format(v) : dash);
  const n1 = (v) => (isNum(v) ? nf1.format(v) : dash);
  const parseDate = (s) => (s ? new Date(s) : null);

  function minutes(m) {
    if (!isNum(m) || m <= 0) return dash;
    const h = Math.floor(m / 60), mm = Math.round(m % 60);
    return h ? `${h} h ${mm} min` : `${mm} min`;
  }
  function ago(d) {
    if (!d) return dash;
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function dateTime(d) {
    if (!d) return dash;
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function clock(d) {
    if (!d) return dash;
    const sameDay = d.toDateString() === new Date().toDateString();
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return sameDay ? t : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
  }
  const first = (...vals) => vals.find((v) => v !== null && v !== undefined);

  /* ---------------- data access ---------------- */
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }

  function currentCar() {
    return state.cars.find((c) => c.vin === state.vin) || state.cars[0] || null;
  }

  /* ---------------- status / chrome ---------------- */
  function renderStatus() {
    const st = state.status;
    const pill = $("status-pill");
    const banner = $("banner");
    banner.hidden = true;

    if (!st) {
      pill.dataset.state = "error";
      pill.textContent = "Server offline";
      banner.hidden = false;
      banner.dataset.level = "critical";
      banner.textContent = "Can't reach the local dashboard server. Is it still running?";
      return;
    }
    if (st.state === "auth_failed") {
      pill.dataset.state = "auth_failed";
      pill.textContent = "Sign-in failed";
      banner.hidden = false;
      banner.dataset.level = "critical";
      banner.textContent = "Polestar rejected the username or password. Update POLESTAR_USERNAME / POLESTAR_PASSWORD in .env and restart the server.";
    } else if (st.state === "error") {
      pill.dataset.state = "error";
      pill.textContent = "Update failed";
      banner.hidden = false;
      banner.dataset.level = "warning";
      banner.textContent = `Last update failed — showing the most recent data. ${st.message || ""}`;
    } else if (st.state === "starting") {
      pill.dataset.state = "";
      pill.textContent = "Connecting…";
    } else {
      pill.dataset.state = st.demo ? "demo" : "ok";
      pill.textContent = st.demo ? "Demo data" : "Live";
    }

    const car = currentCar();
    const fetched = car && parseDate(car.fetched_at);
    $("updated").textContent = fetched ? `Updated ${ago(fetched)}` : "";
  }

  function renderCarSelect() {
    const sel = $("car-select");
    sel.hidden = state.cars.length < 2;
    const want = state.cars.map((c) => c.vin).join(",");
    if (sel.dataset.vins !== want) {
      sel.replaceChildren(
        ...state.cars.map((c) => {
          const o = document.createElement("option");
          o.value = c.vin;
          o.textContent = `${c.info?.model_name || "Polestar"} · ${c.info?.registration_no || c.vin.slice(-4)}`;
          return o;
        }),
      );
      sel.dataset.vins = want;
    }
    const car = currentCar();
    if (car) sel.value = car.vin;
  }

  /* ---------------- hero ---------------- */
  const preloaded = new Set();

  function renderHero(car) {
    const info = car.info || {};
    $("model-name").textContent = info.model_name || "Polestar";
    $("model-year").textContent = info.model_year && info.model_year !== "0000" ? `Model year ${info.model_year}` : "";
    $("reg").textContent = info.registration_no || "";

    const imgs = car.images?.transparent?.length ? car.images.transparent : car.images?.opaque || [];
    const picker = $("angle-picker");
    if (picker.childElementCount !== imgs.length) {
      picker.replaceChildren(
        ...imgs.map((im, i) => {
          const b = document.createElement("button");
          b.type = "button";
          b.setAttribute("aria-label", `View ${i + 1}`);
          b.addEventListener("click", () => setAngle(i));
          return b;
        }),
      );
    }
    state.angle = Math.min(state.angle, Math.max(0, imgs.length - 1));
    const img = $("car-image");
    const url = imgs[state.angle]?.url || "";
    if (img.getAttribute("src") !== url) img.setAttribute("src", url);
    img.alt = `${info.model_name || "Polestar"} exterior`;
    [...picker.children].forEach((b, i) => b.setAttribute("aria-pressed", String(i === state.angle)));
    // Preload other angles so drag-to-rotate is smooth.
    for (const im of imgs) {
      if (preloaded.has(im.url)) continue;
      preloaded.add(im.url);
      new Image().src = im.url;
    }
  }

  function setAngle(i) {
    const car = currentCar();
    const n = (car?.images?.transparent?.length || car?.images?.opaque?.length || 1);
    state.angle = ((i % n) + n) % n;
    store.set("polestar.angle", state.angle);
    if (car) renderHero(car);
  }

  function setupDragRotate() {
    const stage = document.querySelector(".car-stage");
    let startX = null, startAngle = 0;
    stage.addEventListener("pointerdown", (e) => { startX = e.clientX; startAngle = state.angle; stage.setPointerCapture(e.pointerId); });
    stage.addEventListener("pointermove", (e) => {
      if (startX == null) return;
      const steps = Math.round((startX - e.clientX) / 60);
      if (startAngle + steps !== state.angle) setAngle(startAngle + steps);
    });
    const end = () => { startX = null; };
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);
  }

  /* ---------------- battery & charging ---------------- */
  const CHARGE_TONES = {
    Charging: "active", "Smart Charging": "active",
    Done: "good",
    Scheduled: "neutral", "Smart Charging Paused": "neutral", Idle: "neutral", Discharging: "neutral",
    Fault: "critical", Error: "critical",
  };

  function renderBattery(car) {
    const b = car.battery || {};
    const g = car.grpc_battery || {};
    const tgt = car.grpc_target_soc || {};

    const soc = first(b.battery_charge_level_percentage, g.battery_charge_level_percentage);
    const range = first(b.estimated_distance_to_empty_km, g.estimated_distance_to_empty_km);
    const status = first(g.charging_status, b.charging_status);
    const connection = g.charger_connection_status;
    const charging = status === "Charging" || status === "Smart Charging";
    const target = tgt.battery_charge_target_level;

    $("soc").textContent = n0(soc);
    $("range").textContent = n0(Units.dist(range));

    const fill = $("battery-fill");
    fill.style.width = isNum(soc) ? `calc(${Math.max(0, Math.min(100, soc))}% - 4px)` : "0";
    fill.classList.toggle("charging", charging);
    $("battery-bar").setAttribute("aria-label", `Battery ${n0(soc)}%${isNum(target) ? `, charge limit ${target}%` : ""}`);

    const tmark = $("battery-target");
    tmark.hidden = !isNum(target) || target <= 0;
    if (isNum(target)) {
      tmark.style.left = `${target}%`;
      $("battery-target-label").textContent = `Limit ${target}%`;
    }

    // Headline status
    let text, tone;
    if (charging) {
      const kw = isNum(g.charging_power_watts) && g.charging_power_watts > 0 ? ` · ${n1(g.charging_power_watts / 1000)} kW` : "";
      text = `${status}${kw}`;
      tone = "active";
    } else if (connection === "Connected") {
      text = `Plugged in${status && status !== "Unspecified" ? ` · ${status}` : ""}`;
      tone = CHARGE_TONES[status] || "neutral";
    } else if (connection === "Disconnected") {
      text = "Not plugged in";
      tone = "neutral";
    } else if (connection === "Fault") {
      text = "Charger fault";
      tone = "critical";
    } else {
      text = status && status !== "Unspecified" ? status : "Charging status unavailable";
      tone = CHARGE_TONES[status] || "neutral";
    }
    $("charge-text").textContent = text;
    $("charge-dot").dataset.tone = tone;

    const toFull = first(g.estimated_charging_time_to_full_minutes, b.estimated_charging_time_to_full_minutes);
    const readyAt = parseDate(b.estimated_fully_charged);
    const hasGrpc = !!car.grpc_battery;
    const live = (v, f) => (charging && isNum(v) && v > 0 ? f(v) : dash);

    const rows = [
      ["Connection", connection || dash],
      ["Type", hasGrpc && g.charging_type && g.charging_type !== "Unspecified" ? g.charging_type : dash],
      ["Power", live(g.charging_power_watts, (v) => `${n1(v / 1000)} kW`)],
      ["Current", live(g.charging_current_amps, (v) => `${n0(v)} A`)],
      ["Voltage", live(g.charging_voltage_volts, (v) => `${n0(v)} V`)],
      ["Time to full", charging ? minutes(toFull) : dash],
      ["Ready at", charging ? clock(readyAt) : dash],
      ["Charge limit", isNum(target) && target > 0 ? `${target}%` : dash],
      ["Limit mode", tgt.charge_target_level_setting_type && tgt.charge_target_level_setting_type !== "Unspecified" ? tgt.charge_target_level_setting_type : dash],
    ];
    const dl = $("charge-details");
    dl.replaceChildren(
      ...rows.map(([k, v]) => {
        const d = document.createElement("div");
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = k;
        dd.textContent = v;
        dd.title = v;
        d.append(dt, dd);
        return d;
      }),
    );
  }

  /* ---------------- tiles ---------------- */
  function renderTiles(car) {
    const b = car.battery || {};
    const g = car.grpc_battery || {};
    const h = car.health || {};
    const odo = car.odometer?.odometer_meters;
    $("odometer").textContent = isNum(odo) ? n0(Units.dist(odo / 1000)) : dash;
    const cons = first(
      isNum(g.average_energy_consumption_kwh_per_100km) && g.average_energy_consumption_kwh_per_100km > 0 ? g.average_energy_consumption_kwh_per_100km : null,
      b.average_energy_consumption_kwh_per_100km,
    );
    $("consumption").textContent = n1(Units.consumption(cons));
    $("full-range").textContent = n0(Units.dist(b.estimated_full_charge_range_km));
    $("service-days").textContent = n0(h.days_to_service);
    $("service-km").textContent = isNum(h.distance_to_service_km) ? `or ${n0(Units.dist(h.distance_to_service_km))} ${Units.distLabel}` : "";
  }

  /* ---------------- health ---------------- */
  const ICONS = {
    good: '<path d="M20 6 9 17l-5-5"/>',
    warning: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    critical: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
    neutral: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  };
  function healthTone(v) {
    if (!v || v === "Unspecified") return ["neutral", "Unknown"];
    if (v === "No Warning") return ["good", "OK"];
    if (/Overdue|Too Low|Too High|Service Required/.test(v)) return ["critical", v];
    return ["warning", v];
  }
  function renderHealth(car) {
    const h = car.health;
    const list = $("health-list");
    const items = [
      ["Service", h?.service_warning],
      ["Brake fluid", h?.brake_fluid_level_warning],
      ["Coolant level", h?.engine_coolant_level_warning],
      ["Oil level", h?.oil_level_warning],
    ];
    list.replaceChildren(
      ...items.map(([name, v]) => {
        const [tone, label] = h ? healthTone(v) : ["neutral", "Not reported"];
        const li = document.createElement("li");
        const n = document.createElement("span");
        n.className = "h-name";
        n.textContent = name;
        const val = document.createElement("span");
        val.className = "h-val";
        val.insertAdjacentHTML(
          "afterbegin",
          `<svg class="health-icon" data-tone="${tone}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[tone]}</svg>`,
        );
        val.append(document.createTextNode(label));
        li.append(n, val);
        return li;
      }),
    );
  }

  /* ---------------- diagnostics ---------------- */
  function renderDiagnostics(car) {
    const a = car.api || {};
    const rows = [
      ["VIN", car.vin],
      ["API connected", a.api_connected ? "Yes" : "No"],
      ["Data API status", a.api_status_code_data ?? dash],
      ["Auth API status", a.api_status_code_auth ?? dash],
      ["Token expires", dateTime(parseDate(a.api_token_expires_at))],
      ["Battery data from car", ago(parseDate(car.battery?.event_updated_timestamp))],
      ["Odometer data from car", ago(parseDate(car.odometer?.event_updated_timestamp))],
      ["Health data from car", ago(parseDate(car.health?.event_updated_timestamp))],
      ["Charging data (gRPC)", car.grpc_battery ? "Available" : "Not available for this car"],
      ["Charge limit (gRPC)", car.grpc_target_soc ? "Available" : "Not available for this car"],
      ["Location (gRPC)", car.location?.current ? `Last fix ${ago(parseDate(car.location.current.ts))}`
        : car.location?.available === false ? `Not available${car.location.reason ? ` (${car.location.reason})` : ""}` : "No fix yet"],
      ["Live location stream", (() => {
        const st = car.location?.stream;
        if (!st || !st.state) return dash;
        const got = st.fixes ? ` · ${st.fixes} positions, last ${ago(parseDate(st.last_fix))}` : "";
        if (st.state === "off") return `Off${st.note ? ` (${st.note})` : ""}${got}`;
        return `${st.state === "connected" ? "Connected" : "Reconnecting"}${got}`;
      })()],
      ["Last fetched", dateTime(parseDate(car.fetched_at))],
    ];
    $("diagnostics").replaceChildren(
      ...rows.flatMap(([k, v]) => {
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = k;
        dd.textContent = String(v);
        return [dt, dd];
      }),
    );
  }

  /* ---------------- history charts ---------------- */
  async function loadHistory(force) {
    const car = currentCar();
    if (!car) return;
    if (!force && Date.now() - state.lastHistoryFetch < HISTORY_POLL_MS) return;
    state.lastHistoryFetch = Date.now();

    const figs = ["chart-soc", "chart-range", "chart-power", "chart-distance"].map($);
    figs.forEach((f) => f.classList.add("loading"));
    try {
      const days = state.hours <= 24 ? 14 : Math.round(state.hours / 24);
      const [rows, daily] = await Promise.all([
        api(`/api/history?vin=${encodeURIComponent(car.vin)}&hours=${state.hours}`),
        api(`/api/daily?vin=${encodeURIComponent(car.vin)}&days=${days}`),
      ]);
      renderCharts(car, rows, daily, days);
    } catch (e) {
      console.warn(e);
    } finally {
      figs.forEach((f) => f.classList.remove("loading"));
    }
  }

  function renderCharts(car, rows, daily, days) {
    const pts = (key, scale = 1, conv = (v) => v) =>
      rows.map((r) => ({ t: new Date(r.ts * 1000), v: r[key] == null ? null : conv(r[key] * scale) }));
    const to = new Date();
    const from = new Date(to.getTime() - state.hours * 3600e3);
    const target = car.grpc_target_soc?.battery_charge_target_level;
    const span = state.hours <= 24 ? "Last 24 hours" : `Last ${Math.round(state.hours / 24)} days`;

    Charts.line($("chart-soc"), {
      title: "Battery level", sub: span, valueLabel: "Battery",
      points: pts("soc"), from, to,
      color: "var(--series-1)", area: true,
      yMin: 0, yMax: 100, clampMin: 0, clampMax: 100,
      ref: isNum(target) && target > 0 ? { value: target, label: `Limit ${target}%` } : null,
      format: (v) => `${n0(v)}%`,
    });
    Charts.line($("chart-range"), {
      title: "Estimated range", sub: span, valueLabel: "Range",
      points: pts("range_km", 1, Units.dist), from, to,
      color: "var(--series-1)", yMin: 0, clampMin: 0,
      format: (v) => `${n0(v)} ${Units.distLabel}`,
    });
    const power = pts("charging_power_w", 1 / 1000);
    const anyPower = power.some((p) => isNum(p.v) && p.v > 0);
    Charts.line($("chart-power"), {
      title: "Charging power", sub: span, valueLabel: "Power",
      points: anyPower ? power : [], from, to,
      color: "var(--series-2)", area: true, step: true, yMin: 0, clampMin: 0, areaBase: 0,
      format: (v, axis) => (axis ? nfAxis.format(v) : `${n1(v)} kW`),
      emptyText: car.grpc_battery ? "No charging sessions in this period" : "Charging power isn't available for this car",
    });

    // Daily distance: fill in missing days with zero so gaps read as "didn't drive".
    const byDay = new Map(daily.map((d) => [d.day, d.km]));
    const items = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date();
      d.setHours(12, 0, 0, 0);
      d.setDate(d.getDate() - i);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      items.push({
        v: Units.dist(byDay.get(key) ?? 0),
        label: d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }),
        short: days <= 14 ? d.toLocaleDateString([], { weekday: "narrow" }) + d.getDate() : d.toLocaleDateString([], { month: "numeric", day: "numeric" }),
      });
    }
    const total = items.reduce((s, i) => s + i.v, 0);
    Charts.bars($("chart-distance"), {
      title: "Distance per day", sub: `${n0(total)} ${Units.distLabel} in the last ${days} days`,
      xLabel: "Day", valueLabel: "Distance",
      items: daily.length ? items : [], color: "var(--series-3)",
      format: (v, axis) => (axis ? n0(v) : `${n1(v)} ${Units.distLabel}`),
      emptyText: "Distance appears once the odometer has been recorded",
    });
  }

  /* ---------------- trips ---------------- */
  async function loadTrips(force) {
    const car = currentCar();
    if (!car || !window.Trips) return;
    if (!force && Date.now() - state.lastTripsFetch < HISTORY_POLL_MS) return;
    state.lastTripsFetch = Date.now();
    try {
      const data = await api(`/api/trips?vin=${encodeURIComponent(car.vin)}&days=${state.tripDays}`);
      Trips.update(data, state.tripDays);
      // Routes are inferred in the background (1 request/s); check back soon
      if (data.trips?.some((t) => t.route_source === "pending") && !state.tripsRetry) {
        state.tripsRetry = setTimeout(() => { state.tripsRetry = null; loadTrips(true); }, 3000);
      }
    } catch (e) {
      console.warn(e);
    }
  }

  function setupTripRange() {
    const buttons = [...$("trip-range").querySelectorAll("button")];
    if (!buttons.some((b) => Number(b.dataset.days) === state.tripDays)) state.tripDays = 7;
    const sync = () => buttons.forEach((b) => b.setAttribute("aria-checked", String(Number(b.dataset.days) === state.tripDays)));
    buttons.forEach((b) =>
      b.addEventListener("click", () => {
        state.tripDays = Number(b.dataset.days);
        store.set("polestar.tripDays", state.tripDays);
        sync();
        Trips.reset();
        loadTrips(true);
      }),
    );
    sync();
  }

  function setupRangePicker() {
    const group = $("range-picker");
    const buttons = [...group.querySelectorAll("button")];
    const sync = () => buttons.forEach((b) => b.setAttribute("aria-checked", String(Number(b.dataset.hours) === state.hours)));
    buttons.forEach((b) =>
      b.addEventListener("click", () => {
        state.hours = Number(b.dataset.hours);
        store.set("polestar.hours", state.hours);
        sync();
        loadHistory(true);
      }),
    );
    if (!buttons.some((b) => Number(b.dataset.hours) === state.hours)) state.hours = 24;
    sync();
  }

  /* ---------------- main loop ---------------- */
  function render() {
    renderStatus();
    renderCarSelect();
    const car = currentCar();
    const empty = $("empty");
    const dash_ = $("dashboard");
    if (!car) {
      dash_.hidden = true;
      empty.hidden = false;
      const st = state.status?.state;
      empty.querySelector(".spinner").hidden = st === "auth_failed";
      $("empty-text").textContent =
        st === "auth_failed" ? "Couldn't sign in to Polestar."
        : st === "error" ? "Still trying to reach Polestar…"
        : "Signing in to Polestar and fetching your car…";
      return;
    }
    empty.hidden = true;
    dash_.hidden = false;
    renderHero(car);
    renderBattery(car);
    renderTiles(car);
    renderHealth(car);
    renderDiagnostics(car);
  }

  async function tick() {
    try {
      const [status, cars] = await Promise.all([api("/api/status"), api("/api/cars")]);
      state.status = status;
      state.cars = cars;
      if (!state.cars.some((c) => c.vin === state.vin) && state.cars[0]) state.vin = state.cars[0].vin;
    } catch (e) {
      state.status = null;
    }
    render();
    loadHistory(false);
    loadTrips(false);
  }

  function renderUnitLabels() {
    document.querySelectorAll(".u-dist").forEach((el) => { el.textContent = Units.distLabel; });
    document.querySelectorAll(".u-consumption").forEach((el) => { el.textContent = Units.consumptionLabel; });
    document.querySelectorAll("#units-picker button").forEach((b) =>
      b.setAttribute("aria-checked", String(b.dataset.units === Units.system)));
  }

  function setupUnits() {
    document.querySelectorAll("#units-picker button").forEach((b) =>
      b.addEventListener("click", () => Units.set(b.dataset.units)));
    document.addEventListener("unitschange", () => {
      renderUnitLabels();
      render();
      loadHistory(true);
      if (window.Trips) Trips.rerender();
    });
    renderUnitLabels();
  }

  function init() {
    setupUnits();
    Collapse.bindAll();
    setupRangePicker();
    setupTripRange();
    setupDragRotate();
    $("car-select").addEventListener("change", (e) => {
      state.vin = e.target.value;
      store.set("polestar.vin", state.vin);
      render();
      loadHistory(true);
      if (window.Trips) Trips.reset();
      loadTrips(true);
    });
    $("refresh-btn").addEventListener("click", async () => {
      const btn = $("refresh-btn");
      btn.classList.add("spinning");
      try {
        await api("/api/refresh", { method: "POST" });
        await new Promise((r) => setTimeout(r, 2500));
        await tick();
        await loadHistory(true);
        await loadTrips(true);
      } finally {
        btn.classList.remove("spinning");
      }
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
    tick();
    setInterval(() => { if (!document.hidden) tick(); }, LIVE_POLL_MS);
  }

  init();
})();
