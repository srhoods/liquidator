"use strict";

const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const compact = new Intl.NumberFormat("en", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 2 });
const full = new Intl.NumberFormat("en", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

document.addEventListener("alpine:init", () => {
  Alpine.data("dashboard", () => ({
    range: "24h",
    live: false,
    s: { totals: { count: 0, usd: 0, long_usd: 0, short_usd: 0, biggest: 0 }, largest: null, top_assets: [], series: [] },
    tape: [],
    exch: { binance: true, bybit: true, okx: true },
    minUsd: 0,
    charts: {},
    seq: 0,
    ws: null,
    timer: null,

    async init() {
      await this.load();
      const recent = await (await fetch("/api/recent")).json();
      this.tape = recent.map((e) => this.tag(e));
      this.connect();
      this.timer = setInterval(() => this.load(), 30000);
    },
    tag(e) { e.key = ++this.seq; return e; },
    get visible() {
      return this.tape.filter((e) => this.exch[e.exchange] && e.notional_usd >= (this.minUsd || 0)).slice(0, 150);
    },
    usd(v, exact) { return exact && v < 1e6 ? full.format(v) : compact.format(v); },
    pct(l) { const t = this.s.totals.usd; return t ? (l / t) * 100 : 50; },
    time(iso) { return new Date(iso).toLocaleTimeString([], { hour12: false }); },

    async setRange(r) { this.range = r; await this.load(); },
    async load() {
      const r = await fetch("/api/stats?range=" + this.range);
      if (r.status === 401) { location.href = "/login"; return; }
      this.s = await r.json();
      this.draw();
    },
    connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      this.ws = new WebSocket(`${proto}://${location.host}/api/live`);
      this.ws.onopen = () => { this.live = true; };
      this.ws.onmessage = (m) => {
        const batch = JSON.parse(m.data).map((e) => this.tag({ ...e, notional_usd: +e.notional_usd }));
        this.tape = batch.reverse().concat(this.tape).slice(0, 300);
      };
      this.ws.onclose = () => { this.live = false; setTimeout(() => this.connect(), 3000); };
    },
    draw() {
      const text = cssVar("--muted"), grid = cssVar("--border");
      const exchanges = ["binance", "bybit", "okx"];
      const bucket = this.s.bucket_seconds * 1000;
      const times = [...new Set(this.s.series.map((p) => p.t))].sort();
      const labels = times.map((t) => {
        const d = new Date(t);
        return this.range === "7d" || this.range === "24h"
          ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
          : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
      });
      const datasets = exchanges.map((ex) => ({
        label: ex, backgroundColor: cssVar("--" + ex), borderWidth: 0,
        data: times.map((t) => (this.s.series.find((p) => p.t === t && p.exchange === ex) || { usd: 0 }).usd),
      }));
      const opts = {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: text } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${compact.format(c.parsed.y ?? c.parsed.x)}` } } },
      };
      const axes = (stacked, horizontal) => ({
        x: { stacked, ticks: { color: text, maxRotation: 0, autoSkip: true, callback: horizontal ? (v) => compact.format(v) : undefined }, grid: { color: grid } },
        y: { stacked, ticks: { color: text, callback: horizontal ? undefined : (v) => compact.format(v) }, grid: { color: grid } },
      });
      this.chart("series", "seriesChart", { type: "bar", data: { labels, datasets },
        options: { ...opts, scales: axes(true, false) } });
      const top = this.s.top_assets;
      this.chart("assets", "assetChart", { type: "bar",
        data: { labels: top.map((a) => a.asset), datasets: [{ label: "USD", data: top.map((a) => a.usd), backgroundColor: cssVar("--accent"), borderWidth: 0 }] },
        options: { ...opts, indexAxis: "y", plugins: { ...opts.plugins, legend: { display: false } }, scales: axes(false, true) } });
    },
    chart(key, id, cfg) {
      if (this.charts[key]) { this.charts[key].destroy(); }
      this.charts[key] = new Chart(document.getElementById(id), cfg);
    },
  }));
});

// CSP forbids inline handlers, so confirmation prompts are declared with data-confirm.
document.addEventListener("submit", (ev) => {
  const msg = ev.target.dataset && ev.target.dataset.confirm;
  if (msg && !confirm(msg)) { ev.preventDefault(); }
});
