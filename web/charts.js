/* Minimal dependency-free SVG charts: a time-series line/area chart and a bar chart.
 * Both share a crosshair/hover tooltip and a "Table" toggle so values are
 * reachable without hovering. */
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const HEIGHT = 200;
  const PAD = { top: 12, right: 12, bottom: 24, left: 40 };
  const tooltip = () => document.getElementById("tooltip");

  /** Run `fn` at most once per animation frame, with the latest arguments. */
  function perFrame(fn) {
    let raf = 0, args = null;
    const run = (...a) => {
      args = a;
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; fn(...args); });
    };
    run.cancel = () => { if (raf) cancelAnimationFrame(raf); raf = 0; };
    return run;
  }

  function el(tag, attrs = {}, parent) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceTicks(min, max, count = 4) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo, hi, ticks };
  }

  function fmtTime(d, spanMs) {
    if (spanMs <= 36 * 3600e3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function fmtFull(d) {
    return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function showTooltip(x, y, time, rows) {
    const tt = tooltip();
    tt.replaceChildren();
    const t = document.createElement("div");
    t.className = "tt-time";
    t.textContent = time;
    tt.appendChild(t);
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "tt-row";
      const key = document.createElement("span");
      key.className = "tt-key";
      key.style.background = r.color;
      const val = document.createElement("span");
      val.className = "tt-val";
      val.textContent = r.value;
      const name = document.createElement("span");
      name.className = "tt-name";
      name.textContent = r.name;
      row.append(key, val, name);
      tt.appendChild(row);
    }
    tt.hidden = false;
    const w = tt.offsetWidth, h = tt.offsetHeight;
    let left = x + 14, top = y - h - 10;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 14;
    tt.style.left = `${left}px`;
    tt.style.top = `${top}px`;
  }
  function hideTooltip() { tooltip().hidden = true; }

  /* ---- shell shared by both chart kinds: header, table toggle, resize ---- */
  function shell(fig, opts, drawChart, tableRows) {
    fig._opts = opts;
    if (!fig._built) {
      fig._built = true;
      fig._mode = "chart";
      const head = document.createElement("div");
      head.className = "chart-head";
      const left = document.createElement("div");
      const h3 = document.createElement("h3");
      const titleText = document.createElement("span");
      h3.appendChild(titleText);
      const sub = document.createElement("div");
      sub.className = "chart-sub";
      left.append(h3, sub);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "chart-toggle collapse-body";
      toggle.textContent = "Table";
      toggle.addEventListener("click", () => {
        fig._mode = fig._mode === "chart" ? "table" : "chart";
        toggle.textContent = fig._mode === "chart" ? "Table" : "Chart";
        fig._render();
      });
      head.append(left, toggle);
      const body = document.createElement("div");
      body.className = "chart-body collapse-body";
      fig.append(head, body);
      fig._title = titleText; fig._sub = sub; fig._body = body;
      if (window.Collapse && fig.id) window.Collapse.bind(fig, h3, fig.id);
      // Only a width change needs a redraw (the chart's height is fixed)
      new ResizeObserver(() => {
        if (fig._render && body.clientWidth !== fig._drawnWidth) fig._render();
      }).observe(body);
    }
    fig._title.textContent = opts.title;
    fig._sub.textContent = opts.sub || "";
    fig._render = () => {
      const body = fig._body;
      body.replaceChildren();
      const width = body.clientWidth;
      fig._drawnWidth = width || null;
      if (fig._mode === "table") return renderTable(body, opts, tableRows());
      if (!width) return;
      drawChart(body, width);
    };
    fig._render();
  }

  function renderTable(body, opts, rows) {
    const wrap = document.createElement("div");
    wrap.className = "chart-table-wrap";
    const table = document.createElement("table");
    table.className = "chart-table";
    const thead = table.createTHead().insertRow();
    for (const h of [opts.xLabel || "Time", opts.valueLabel || opts.title]) {
      const th = document.createElement("th");
      th.textContent = h;
      thead.appendChild(th);
    }
    const tbody = table.createTBody();
    for (const [a, b] of rows) {
      const tr = tbody.insertRow();
      tr.insertCell().textContent = a;
      tr.insertCell().textContent = b;
    }
    wrap.appendChild(table);
    body.appendChild(wrap);
  }

  function emptyState(body, text) {
    const d = document.createElement("div");
    d.className = "chart-empty";
    d.textContent = text;
    body.appendChild(d);
  }

  /* ---------------- Line / area chart ---------------- */
  function line(fig, opts) {
    const pts = (opts.points || []).filter((p) => p.t instanceof Date && !isNaN(p.t));
    const fmt = opts.format || ((v) => String(v));

    shell(fig, opts, (body, width) => {
      const valid = pts.filter((p) => p.v != null && !isNaN(p.v));
      if (valid.length < 2) return emptyState(body, opts.emptyText || "Not enough data yet");

      const t0 = opts.from ? +opts.from : +valid[0].t;
      const t1 = opts.to ? +opts.to : +valid[valid.length - 1].t;
      let vmin = Math.min(...valid.map((p) => p.v));
      let vmax = Math.max(...valid.map((p) => p.v));
      if (opts.ref != null) { vmin = Math.min(vmin, opts.ref.value); vmax = Math.max(vmax, opts.ref.value); }
      if (opts.yMin != null) vmin = Math.min(vmin, opts.yMin);
      if (opts.yMax != null) vmax = Math.max(vmax, opts.yMax);
      const { lo, hi, ticks } = niceTicks(vmin, vmax);
      const yLo = opts.clampMin != null ? Math.max(lo, opts.clampMin) : lo;
      const yHi = opts.clampMax != null ? Math.min(hi, opts.clampMax) : hi;

      const iw = width - PAD.left - PAD.right;
      const ih = HEIGHT - PAD.top - PAD.bottom;
      const x = (t) => PAD.left + ((+t - t0) / Math.max(1, t1 - t0)) * iw;
      const y = (v) => PAD.top + ih - ((v - yLo) / Math.max(1e-9, yHi - yLo)) * ih;

      const svg = el("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, role: "img", "aria-label": opts.title });

      // grid + y labels
      for (const tv of ticks.filter((v) => v >= yLo && v <= yHi)) {
        el("line", { x1: PAD.left, x2: width - PAD.right, y1: y(tv), y2: y(tv), stroke: "var(--grid)", "stroke-width": 1 }, svg);
        el("text", { x: PAD.left - 8, y: y(tv) + 4, "text-anchor": "end" }, svg).textContent = fmt(tv, true);
      }
      // x labels
      const nX = Math.max(2, Math.min(6, Math.floor(iw / 90)));
      for (let i = 0; i <= nX; i++) {
        const t = new Date(t0 + ((t1 - t0) * i) / nX);
        const anchor = i === 0 ? "start" : i === nX ? "end" : "middle";
        el("text", { x: x(t), y: HEIGHT - 6, "text-anchor": anchor }, svg).textContent = fmtTime(t, t1 - t0);
      }

      // Split into segments at nulls and at gaps much larger than the usual sample spacing.
      const deltas = [];
      for (let i = 1; i < pts.length; i++) deltas.push(pts[i].t - pts[i - 1].t);
      deltas.sort((a, b) => a - b);
      const median = deltas[Math.floor(deltas.length / 2)] || 60e3;
      const gap = Math.max(median * 6, 20 * 60e3);
      const segments = [];
      let cur = [];
      pts.forEach((p, i) => {
        const broken = p.v == null || isNaN(p.v) || (i > 0 && p.t - pts[i - 1].t > gap);
        if (broken && cur.length) { segments.push(cur); cur = []; }
        if (p.v != null && !isNaN(p.v)) cur.push(p);
      });
      if (cur.length) segments.push(cur);

      const baseY = y(Math.max(yLo, Math.min(yHi, opts.areaBase ?? yLo)));
      for (const seg of segments) {
        let d = "";
        seg.forEach((p, i) => {
          const px = x(p.t), py = y(p.v);
          if (i === 0) d += `M${px},${py}`;
          else if (opts.step) d += `H${px}V${py}`;
          else d += `L${px},${py}`;
        });
        if (opts.area) {
          const first = x(seg[0].t), last = x(seg[seg.length - 1].t);
          el("path", { d: `${d}L${last},${baseY}L${first},${baseY}Z`, fill: opts.color, "fill-opacity": 0.14, stroke: "none" }, svg);
        }
        el("path", { d, fill: "none", stroke: opts.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      }

      // reference line (e.g. target SoC)
      if (opts.ref != null && opts.ref.value >= yLo && opts.ref.value <= yHi) {
        const ry = y(opts.ref.value);
        el("line", { x1: PAD.left, x2: width - PAD.right, y1: ry, y2: ry, stroke: "var(--text-2)", "stroke-width": 1, "stroke-dasharray": "4 4" }, svg);
        const label = el("text", { x: width - PAD.right, y: ry - 5, "text-anchor": "end" }, svg);
        label.textContent = opts.ref.label;
        label.style.fill = "var(--text-2)";
      }

      // last-value marker + direct label
      const last = valid[valid.length - 1];
      el("circle", { cx: x(last.t), cy: y(last.v), r: 4, fill: opts.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);

      // crosshair
      const hair = el("line", { y1: PAD.top, y2: PAD.top + ih, stroke: "var(--text-3)", "stroke-width": 1, visibility: "hidden" }, svg);
      const dot = el("circle", { r: 4.5, fill: opts.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg);
      const hit = el("rect", { x: PAD.left, y: 0, width: iw, height: HEIGHT, fill: "transparent", tabindex: 0 }, svg);

      const nearest = (tx) => {
        // valid is in time order: binary search, then pick the closer neighbour
        let lo = 0, hi = valid.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (+valid[mid].t < tx) lo = mid + 1; else hi = mid;
        }
        const prev = valid[lo - 1];
        return prev && Math.abs(+prev.t - tx) <= Math.abs(+valid[lo].t - tx) ? prev : valid[lo];
      };
      const show = (p, clientX, clientY) => {
        hair.setAttribute("x1", x(p.t)); hair.setAttribute("x2", x(p.t));
        hair.setAttribute("visibility", "visible");
        dot.setAttribute("cx", x(p.t)); dot.setAttribute("cy", y(p.v));
        dot.setAttribute("visibility", "visible");
        showTooltip(clientX, clientY, fmtFull(p.t), [{ color: opts.color, value: fmt(p.v), name: opts.valueLabel || opts.title }]);
      };
      const track = perFrame((clientX, clientY) => {
        const r = svg.getBoundingClientRect();
        const sx = ((clientX - r.left) / r.width) * width;
        const t = t0 + ((sx - PAD.left) / iw) * (t1 - t0);
        show(nearest(t), clientX, clientY);
      });
      const hide = () => { track.cancel(); hair.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTooltip(); };
      hit.addEventListener("pointermove", (e) => track(e.clientX, e.clientY));
      hit.addEventListener("pointerleave", hide);
      let kbIndex = valid.length - 1;
      hit.addEventListener("focus", () => {
        const r = svg.getBoundingClientRect();
        show(valid[kbIndex], r.left + (x(valid[kbIndex].t) / width) * r.width, r.top + 20);
      });
      hit.addEventListener("blur", hide);
      hit.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        kbIndex = Math.max(0, Math.min(valid.length - 1, kbIndex + (e.key === "ArrowRight" ? 1 : -1)));
        const r = svg.getBoundingClientRect();
        show(valid[kbIndex], r.left + (x(valid[kbIndex].t) / width) * r.width, r.top + 20);
      });

      body.appendChild(svg);
    }, () => pts.filter((p) => p.v != null).slice().reverse().map((p) => [fmtFull(p.t), fmt(p.v)]));
  }

  /* ---------------- Bar chart ---------------- */
  function bars(fig, opts) {
    const items = opts.items || [];
    const fmt = opts.format || ((v) => String(v));

    shell(fig, opts, (body, width) => {
      if (!items.length) return emptyState(body, opts.emptyText || "No data yet");
      const vmax = Math.max(1, ...items.map((i) => i.v || 0));
      const { hi, ticks } = niceTicks(0, vmax);
      const iw = width - PAD.left - PAD.right;
      const ih = HEIGHT - PAD.top - PAD.bottom;
      const y = (v) => PAD.top + ih - (v / hi) * ih;
      const slot = iw / items.length;
      const bw = Math.max(3, Math.min(36, slot - 2 - slot * 0.25));

      const svg = el("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, role: "img", "aria-label": opts.title });
      for (const tv of ticks) {
        el("line", { x1: PAD.left, x2: width - PAD.right, y1: y(tv), y2: y(tv), stroke: "var(--grid)", "stroke-width": 1 }, svg);
        el("text", { x: PAD.left - 8, y: y(tv) + 4, "text-anchor": "end" }, svg).textContent = fmt(tv, true);
      }
      const labelEvery = Math.ceil(items.length / Math.max(2, Math.floor(iw / 56)));
      items.forEach((it, i) => {
        const cx = PAD.left + slot * i + slot / 2;
        const v = it.v || 0;
        const top = y(v), bottom = y(0);
        const h = Math.max(0, bottom - top);
        const r = Math.min(4, h, bw / 2);
        // rounded top corners, square at baseline
        const xL = cx - bw / 2, xR = cx + bw / 2;
        const d = h > 0
          ? `M${xL},${bottom}V${top + r}Q${xL},${top} ${xL + r},${top}H${xR - r}Q${xR},${top} ${xR},${top + r}V${bottom}Z`
          : "";
        const bar = d ? el("path", { d, fill: opts.color }, svg) : null;
        if ((items.length - 1 - i) % labelEvery === 0) {
          el("text", { x: cx, y: HEIGHT - 6, "text-anchor": "middle" }, svg).textContent = it.short;
        }
        const hit = el("rect", { x: PAD.left + slot * i, y: PAD.top, width: slot, height: ih, fill: "transparent", tabindex: 0 }, svg);
        const on = perFrame((clientX, clientY) => {
          if (bar) bar.setAttribute("fill-opacity", 0.75);
          const rect = hit.getBoundingClientRect();
          showTooltip(clientX ?? rect.left + rect.width / 2, clientY ?? rect.top + 20, it.label, [{ color: opts.color, value: fmt(v), name: opts.valueLabel || "" }]);
        });
        const off = () => { on.cancel(); if (bar) bar.removeAttribute("fill-opacity"); hideTooltip(); };
        hit.addEventListener("pointermove", (e) => on(e.clientX, e.clientY));
        hit.addEventListener("pointerleave", off);
        hit.addEventListener("focus", () => on());
        hit.addEventListener("blur", off);
      });
      body.appendChild(svg);
    }, () => items.slice().reverse().map((i) => [i.label, fmt(i.v || 0)]));
  }

  window.Charts = { line, bars };
})();
