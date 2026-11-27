#!/usr/bin/env node
// README screenshot tour — regenerates docs/img/readme/*.png from a RUNNING
// netzsim stack (backend + Vite dev UI) by driving the system Chrome.
//
// What you need running
//   backend : PYTHONPATH=src NETZSIM_PORT=8140 .venv/Scripts/python.exe -m netzsim.main
//   UI      : cd ui && NETZSIM_PORT=8140 NETZSIM_UI_PORT=5290 npm run dev
//   (any ports work — see the env vars below; the UI must proxy to THAT backend)
//
// How to run it
//   playwright-core is deliberately NOT a dependency of this repo. Install it
//   once in a scratch directory and run the script with that directory as CWD
//   (the script resolves playwright-core from the CWD, not from its own path):
//
//     mkdir C:\temp\readme-shots && cd C:\temp\readme-shots
//     npm init -y && npm i playwright-core
//     node <repo>\scripts\readme_screenshots.mjs            # all five shots
//     node <repo>\scripts\readme_screenshots.mjs --only 1,2 # a subset
//
//   No browser download: it launches the installed Chrome (channel "chrome").
//   A full run takes about one minute.
//
// Environment
//   NETZSIM_API    backend base URL         (default http://127.0.0.1:8140)
//   NETZSIM_UI     UI base URL              (default http://localhost:5290 —
//                  Vite binds IPv6, so "localhost", not 127.0.0.1)
//   NETZSIM_SHOTS  output directory         (default <repo>/docs/img/readme)
//   NETZSIM_STUDIO shot 3 variant: "library" (default — the suburban library
//                  grid with EV/PV configured, incl. the transformer check) or
//                  "reference" (IEEE 33-bus feeder, single-line diagram)
//
// What it does to the backend it talks to
//   It LOADS the reference scenarios 2, 3 and 4 (each replaces the running
//   grid), seeks/pauses the clock, and for the district shot places the MV
//   coordinator. Point it at a throw-away instance, not at a session you care
//   about. Nothing is written to disk on the server side. The simulation is
//   left PAUSED on the last frame.
//
// The shots (1280x760 CSS px @ deviceScaleFactor 1.5 -> 1920x1140 PNG, de-DE)
//   01-live-overload.png    scenario 2, 19:00, Lastfluss, popup on an overloaded line
//   02-three-views.png      same moment, Schätzung + pinned day graph of that line
//   03-netz-und-lasten.png  workspace "Netz & Lasten": grid picker, loads, trafo check
//   04-observability.png    scenario 3, 12:30, Gemessen, popup on the station meter
//   05-district.png         scenario 4, 18:11, MV district, the Netzampel cascade acting
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(path.join(process.cwd(), "x.js"));
const { chromium } = require("playwright-core");

const API = (process.env.NETZSIM_API ?? "http://127.0.0.1:8140").replace(/\/$/, "");
const UI = (process.env.NETZSIM_UI ?? "http://localhost:5290").replace(/\/$/, "");
const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(process.env.NETZSIM_SHOTS ?? path.join(HERE, "..", "docs", "img", "readme"));
const STUDIO = process.env.NETZSIM_STUDIO ?? "library";

const VIEWPORT = { width: 1280, height: 760 };
const SCEN2 = "2-feierabend-laden-strang-berlast-nh-sicherung-l-st-aus";
const SCEN3 = "3-mittags-berlast-unsichtbar-f-r-die-nh-sicherung";
const SCEN4 = "4-feierabend-im-bezirk-engpass-unsichtbar-f-r-jede-zelle";
const STEP_EVENING = 19 * 60;        // scenario 2: all twelve EVs charge
const STEP_NOON = 12 * 60 + 30;      // scenario 3: PV peak + midday charging
const STEP_DISTRICT = 18 * 60 + 10;  // scenario 4: the whole wallbox wave is on

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---------------------------------------------------------------- backend --
async function api(method, p, body) {
  const r = await fetch(API + p, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${method} ${p} -> ${r.status} ${(await r.text()).slice(0, 300)}`);
  return r.json();
}
const getState = () => fetch(API + "/state").then((r) => (r.ok ? r.json() : null));

/** Pause and wait until the in-flight step has been published (the engine
 *  finishes a running solve before it honors the pause). `quietMs` must be
 *  longer than one step takes — on the district a step with a fresh WLS
 *  estimate needs about two seconds. */
async function drain(quietMs = 900) {
  await api("POST", "/control/pause");
  let last = NaN;
  for (;;) {
    const s = await getState();
    const ts = s?.timestamp ?? -1;
    if (ts === last) return s;
    last = ts;
    await sleep(quietMs);
  }
}

/** Let the paused simulation advance by exactly one step. */
async function stepOnce(quietMs = 900) {
  const ts0 = (await getState())?.timestamp ?? 0;
  await api("POST", "/control/start");
  for (;;) {
    const s = await getState();
    if (s && s.timestamp > ts0) break;
    await sleep(40);
  }
  return drain(quietMs);                     // lands inside the 1-s tick
}

/** Freeze the simulation on exactly `step`. Seeking while the loop runs races
 *  the engine's own step increment, and a paused seek does not re-solve — so:
 *  pause, drain, seek, then advance one step. `needEst` additionally demands
 *  that the WLS estimate on that frame is the one computed FOR that step. */
async function pin(step, { needEst = false, quietMs = 900 } = {}) {
  for (let attempt = 1; attempt <= 6; attempt++) {
    await drain(quietMs);
    await api("POST", "/control/interval?seconds=1");
    await api("POST", `/control/seek?step=${step}`);
    const s = await stepOnce(quietMs);
    if (s.step === step && s.converged && (!needEst || s.estimated?.step === step)) return s;
    log(`  pin(${step}) attempt ${attempt}: got step ${s.step}, est ${s.estimated?.step} — retrying`);
  }
  throw new Error(`could not pin step ${step}`);
}

async function loadScenario(id) {
  log(`loading scenario ${id.slice(0, 40)}…`);
  await api("POST", `/scenarios/${id}/load`);
  for (let i = 0; i < 600; i++) {            // wait for the first solve
    if (await getState()) return;
    await sleep(500);
  }
  throw new Error("scenario loaded but no step was solved");
}

const ranked = (state) => [...state.lines].sort((a, b) => b.loading_percent - a.loading_percent);
const describe = (net, l) =>
  `${net.lines.find((x) => x.id === l.index).name} ${l.loading_percent.toFixed(1)} % / ${(l.i_ka * 1000).toFixed(0)} A`;
const geoOf = (net, buses) => [...new Set(buses)].map((b) => net.buses.find((x) => x.id === b)?.geo).filter(Boolean);

// ----------------------------------------------------------------- browser --
async function waitTiles(page) {
  await page.waitForFunction(() => {
    const t = [...document.querySelectorAll(".leaflet-tile-pane img.leaflet-tile")];
    return t.length > 0 && t.every((i) => i.classList.contains("leaflet-tile-loaded") && i.complete && i.naturalWidth > 0);
  }, null, { timeout: 45000 });
  await page.waitForLoadState("networkidle").catch(() => {});
  await sleep(1000);                         // tile fade-in + canvas restyle
}

/** Fresh page on the Live workspace, showing the frame the backend is frozen on. */
async function openLive(ctx, state) {
  const page = await ctx.newPage();
  await page.goto(UI, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".side .clock");
  await page.waitForFunction(
    (t) => document.querySelector(".side .clock")?.textContent?.includes(t),
    state.time_of_day, { timeout: 30000 });
  await page.waitForSelector(".leaflet-container");
  await waitTiles(page);
  return page;
}

const segButton = (page, label) => page.locator(".mbar-seg button", { hasText: label }).first();

/** Viewport pixels of WGS84 points. The Leaflet map instance is not reachable
 *  from outside React, but every loaded tile knows its z/x/y and its on-screen
 *  rectangle — that is enough for an exact Web-Mercator projection (fractional
 *  zoom included, because the rectangle carries the CSS scale). */
async function project(page, lonlats) {
  const out = await page.evaluate((pts) => {
    const tile = [...document.querySelectorAll(".leaflet-tile-pane img.leaflet-tile-loaded")]
      .map((el) => ({ m: el.src.match(/\/(\d+)\/(\d+)\/(\d+)(?:@\dx)?\.png/), r: el.getBoundingClientRect() }))
      .filter((t) => t.m && t.r.width > 0).pop();
    if (!tile) return null;
    const [z, tx, ty] = tile.m.slice(1).map(Number);
    const n = 2 ** z;
    return pts.map(([lon, lat]) => {
      const wx = ((lon + 180) / 360) * n;
      const wy = ((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n;
      return { x: tile.r.left + (wx - tx) * tile.r.width, y: tile.r.top + (wy - ty) * tile.r.height };
    });
  }, lonlats);
  if (!out) throw new Error("no map tile to project against");
  return out;
}

const mapBox = (page) => page.locator(".leaflet-container").boundingBox();
const parkMouse = async (page) => { await page.mouse.move(40, 20); await sleep(350); };

/** Zoom and pan the map so the given points fill about `fill` of the map area
 *  and sit centered at (fx, fy) of it. Wheel-zooming keeps the point under the
 *  cursor fixed (the toolbar buttons zoom to the map center instead), the pan
 *  is a slow drag that ends at rest, so Leaflet adds no inertia. */
async function frame(page, lonlats, { fill = 0.62, fx = 0.46, fy = 0.52 } = {}) {
  const box = await mapBox(page);
  const bbox = async () => {
    const ps = await project(page, lonlats);
    const xs = ps.map((p) => p.x), ys = ps.map((p) => p.y);
    return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
  };
  let stepGain = Math.SQRT2;                 // one wheel notch = +0.5 zoom here
  for (let i = 0; i < 10; i++) {
    const b = await bbox();
    const room = Math.min((fill * box.width) / (b.x1 - b.x0 || 1), (fill * box.height) / (b.y1 - b.y0 || 1));
    if (room < stepGain * 0.97) break;       // another notch would overshoot
    const cx = Math.min(box.x + box.width - 30, Math.max(box.x + 30, (b.x0 + b.x1) / 2));
    const cy = Math.min(box.y + box.height - 30, Math.max(box.y + 30, (b.y0 + b.y1) / 2));
    await page.mouse.move(cx, cy);
    await page.mouse.wheel(0, -100);
    await sleep(800);                        // debounce + zoom animation
    const a = await bbox();
    const gain = (a.x1 - a.x0 + a.y1 - a.y0) / (b.x1 - b.x0 + b.y1 - b.y0 || 1);
    if (gain > 1.05) stepGain = gain; else break;
  }
  const b = await bbox();
  const dx = box.x + fx * box.width - (b.x0 + b.x1) / 2;
  const dy = box.y + fy * box.height - (b.y0 + b.y1) / 2;
  if (Math.hypot(dx, dy) > 3) {
    const sx = box.x + box.width / 2 - dx / 2, sy = box.y + box.height / 2 - dy / 2;
    await page.mouse.move(sx, sy);
    await page.mouse.down();
    await page.mouse.move(sx + dx, sy + dy, { steps: 12 });
    await sleep(250);
    await page.mouse.up();
  }
  await parkMouse(page);
  await waitTiles(page);
}

/** A click point ON a line's drawn polyline that keeps `clearPx` distance to
 *  every bus marker (markers sit on top of the lines and would take the
 *  click). Tries the lines in the given order; null if none qualifies. */
async function pickLine(page, net, lineIds, clearPx = 14) {
  const box = await mapBox(page);
  const busPx = await project(page, net.buses.filter((b) => b.geo).map((b) => b.geo));
  for (const id of lineIds) {
    const ln = net.lines.find((l) => l.id === id);
    const geo = (bus) => net.buses.find((b) => b.id === bus)?.geo;
    const pts = ln.geometry?.length >= 2 ? ln.geometry : [geo(ln.from_bus), geo(ln.to_bus)];
    if (pts.some((p) => !p)) continue;
    const samples = [];
    for (let i = 1; i < pts.length; i++) {
      for (const f of [0.2, 0.35, 0.5, 0.65, 0.8]) {
        samples.push([pts[i - 1][0] + f * (pts[i][0] - pts[i - 1][0]), pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1])]);
      }
    }
    const px = await project(page, samples);
    let best = null;
    px.forEach((p, i) => {
      // inside the map, with head-room for the popup that opens above the point
      if (p.x < box.x + 40 || p.x > box.x + box.width - 40 || p.y < box.y + 140 || p.y > box.y + box.height - 40) return;
      const clear = Math.min(...busPx.map((q) => Math.hypot(q.x - p.x, q.y - p.y)));
      if (!best || clear > best.clear) best = { clear, lonlat: samples[i] };
    });
    if (best && best.clear >= clearPx) return { id, name: ln.name ?? String(id), lonlat: best.lonlat };
  }
  return null;
}

/** Map click grammar: plain click = live-value popup, ctrl-click = pin the
 *  element's day-graph section. Afterwards the pointer is parked on the brand
 *  label so no hover tooltip ends up in the picture. */
async function mapClick(page, lonlat, { ctrl = false } = {}) {
  const [p] = await project(page, [lonlat]);
  if (ctrl) await page.keyboard.down("Control");
  await page.mouse.click(p.x, p.y);
  if (ctrl) await page.keyboard.up("Control");
  await parkMouse(page);
}

const popupTitle = (page) => page.locator(".leaflet-popup-content b").first()
  .textContent({ timeout: 4000 }).catch(() => null);
const closePopup = (page) => page.locator(".leaflet-popup-close-button").click({ timeout: 2000 }).catch(() => {});

/** Plain-click the first of the given lines that can be hit; a popup that
 *  turns out to belong to another element is closed and the next line tried. */
async function linePopup(page, net, lineIds) {
  let rest = lineIds;
  for (;;) {
    const target = await pickLine(page, net, rest);
    if (!target) { log("  no clickable line — shooting without popup"); return null; }
    await mapClick(page, target.lonlat);
    if ((await popupTitle(page)) === target.name) { log(`  popup: line ${target.name}`); return target; }
    await closePopup(page);
    rest = rest.slice(rest.indexOf(target.id) + 1);
  }
}

/** Ctrl-click a line to pin its day-graph section; same fallback as above. */
async function pinLine(page, net, lineIds) {
  let rest = lineIds;
  for (;;) {
    const target = await pickLine(page, net, rest);
    if (!target) throw new Error("no line could be pinned");
    await mapClick(page, target.lonlat, { ctrl: true });
    const pinned = await page.locator(".side .sec-title", { hasText: `Leitung ${target.name}` })
      .waitFor({ timeout: 3000 }).then(() => true, () => false);
    if (pinned) { log(`  pinned: line ${target.name}`); return target; }
    for (let i = 0; i < 5 && (await page.locator(".side .sec-close").count()); i++) {
      await page.locator(".side .sec-close").first().click();
    }
    rest = rest.slice(rest.indexOf(target.id) + 1);
  }
}

/** Plain-click a metered transformer (its meter ring sits at the LV busbar and
 *  forwards the click) — the popup then shows the station meter's reading. */
async function trafoPopup(page, net, trafoId) {
  const tr = net.trafos.find((t) => t.id === trafoId);
  await mapClick(page, net.buses.find((b) => b.id === tr.lv_bus).geo);
  if ((await popupTitle(page)) === tr.name) { log(`  popup: transformer ${tr.name}`); return; }
  await closePopup(page);
  log("  transformer popup did not open — shooting without popup");
}

/** When the side panel overflows, scroll it as little as possible so that BOTH
 *  visible edges fall between two blocks (sections, paragraphs) instead of
 *  cutting through one. Returns the scroll offset, 0 if nothing had to move. */
async function cropSidePanel(page) {
  return page.locator(".side").evaluate((el) => {
    el.scrollTop = 0;
    const h = el.clientHeight;
    if (el.scrollHeight <= h + 1) return 0;
    const base = el.getBoundingClientRect().top;
    const blocks = [...el.children].filter((c) => !c.classList.contains("side-resizer"))
      .map((c) => c.getBoundingClientRect()).filter((r) => r.height > 0)
      .map((r) => [r.top - base, r.bottom - base]);
    const gaps = [[0, blocks[0][0]]];
    for (let i = 1; i < blocks.length; i++) gaps.push([blocks[i - 1][1], blocks[i][0]]);
    gaps.push([blocks[blocks.length - 1][1], el.scrollHeight]);
    const m = 1;                             // stay this far inside a gap
    let best = null;
    for (const [t0, t1] of gaps) {
      for (const [b0, b1] of gaps) {
        const lo = Math.max(t0 + m, b0 + m - h, 0), hi = Math.min(t1 - m, b1 - m - h, el.scrollHeight - h);
        if (lo <= hi && (best === null || lo < best)) best = lo;
      }
    }
    if (best !== null) el.scrollTop = best;
    return el.scrollTop;
  });
}

/** Day graphs are whole-day sweeps (tens of seconds on a district): wait until
 *  every open section shows its graph and nothing says "lädt" anymore. */
async function waitGraphs(page, n, timeout = 240000) {
  await page.waitForFunction((want) => {
    const side = document.querySelector(".side");
    if (!side) return false;
    const txt = side.textContent ?? "";
    if (txt.includes("lädt") || txt.includes("wird gerechnet")) return false;
    return side.querySelectorAll(".sec-body svg").length >= want;
  }, n, { timeout });
  await sleep(500);
}

/** React range inputs ignore a plain value assignment — go through the native
 *  setter and fire a bubbling input event. */
async function setRange(page, fieldLabel, value) {
  await page.locator(".ns-config .field", { hasText: fieldLabel }).first()
    .locator("input[type=range]").evaluate((el, v) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, String(v));
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }, value);
}

async function shoot(page, name) {
  fs.mkdirSync(OUT, { recursive: true });
  const file = path.join(OUT, name);
  await page.screenshot({ path: file, type: "png" });
  log(`  -> ${file} (${Math.round(fs.statSync(file).size / 1024)} KB)`);
}

// ------------------------------------------------------------------- shots --
/** Scenario 2 frozen at 19:00 with a fresh estimate. Returns the frame, the
 *  topology, the lines by descending loading and the points worth framing
 *  (station + the twelve wallboxes of the overloaded feeder). */
async function setupEvening() {
  await loadScenario(SCEN2);
  const state = await pin(STEP_EVENING, { needEst: true });
  const net = await api("GET", "/network");
  const worst = ranked(state);
  log(`  ${state.time_of_day}: ` + worst.slice(0, 3).map((l) => describe(net, l)).join(" · "));
  const focus = geoOf(net, [...net.ev_buses, ...net.trafos.map((t) => t.lv_bus)]);
  return { state, net, worst: worst.slice(0, 4).map((l) => l.index), focus };
}

// 01 — the hero: true power flow, an overloaded feeder, live values on click
async function shot1(ctx, evening) {
  const { state, net, worst, focus } = evening;
  const page = await openLive(ctx, state);
  await segButton(page, "Lastfluss").click();
  await frame(page, focus);
  // popup on the worst-loaded line that is drawn long enough to be clicked
  // (the very worst one is a 6-m stub hidden under the station marker)
  await linePopup(page, net, worst);
  await shoot(page, "01-live-overload.png");
  await page.close();
}

// 02 — same moment through the operator's eyes: WLS estimate + day graph
async function shot2(ctx, evening) {
  const { state, net, worst, focus } = evening;
  const page = await openLive(ctx, state);
  await segButton(page, "Schätzung").click();
  await frame(page, focus);
  await pinLine(page, net, worst);
  await waitGraphs(page, 1);
  await cropSidePanel(page);
  await shoot(page, "02-three-views.png");
  await page.close();
}

// 03 — the configuration workspace (previews only; nothing is applied)
async function shot3(ctx) {
  const page = await ctx.newPage();
  await page.goto(UI, { waitUntil: "domcontentloaded" });
  await segButton(page, "Netz & Lasten").click();
  await page.waitForSelector(".netzstudio .ns-list .section");
  const open = async (group) => {
    const sec = page.locator(".ns-list .section", { hasText: group });
    if (!(await sec.locator(".sec-body").count())) await sec.locator(".sec-head").click();
  };
  if (STUDIO === "reference") {
    await open("Referenznetze");
    await page.locator(".ns-row", { hasText: "IEEE 33-Bus Test Feeder" }).click();
    await page.waitForSelector(".ns-chart svg", { timeout: 60000 });
  } else {
    await open("Bibliothek");
    await page.locator(".ns-row", { hasText: "265991" }).click();
    await page.waitForSelector(".ns-chart svg", { timeout: 60000 });
    // 40 % EVs + 50 % PV: the preview is debounced, so wait for THE answer
    const answered = page.waitForResponse((r) => {
      const body = r.request().postData() ?? "";
      return r.url().includes("/loadgen/assign") && r.ok()
        && body.includes('"ev_penetration":0.4') && body.includes('"pv_penetration":0.5');
    }, { timeout: 60000 });
    await setRange(page, "E-Auto-Anteil", 0.4);
    await setRange(page, "PV-Anteil", 0.5);
    await answered;
    await page.locator(".ns-chartrow .kpi", { hasText: "Trafo-Auslastung" }).waitFor();
  }
  await page.waitForSelector(".ns-preview svg");
  await parkMouse(page);
  await sleep(800);
  await shoot(page, "03-netz-und-lasten.png");
  await page.close();
}

// 04 — strict observability: only what the placed meters deliver
async function shot4(ctx) {
  await loadScenario(SCEN3);
  const state = await pin(STEP_NOON, { needEst: true });
  const net = await api("GET", "/network");
  const meters = await api("GET", "/measurements");
  const worst = ranked(state);
  log(`  ${state.time_of_day}: ${describe(net, worst[0])} (truth) · `
    + `${meters.coverage.n_node_meter}/${meters.coverage.n_bus} nodes metered`);
  const page = await openLive(ctx, state);
  await segButton(page, "Gemessen").click();
  await frame(page, geoOf(net, [...meters.node_buses, ...net.trafos.map((t) => t.lv_bus)]));
  // what the operator really has: the station meter's reading — harmless,
  // while a mid-feeder segment is overloaded and stays grey like every line
  await trafoPopup(page, net, meters.trafo_idxs[0]);
  await shoot(page, "04-observability.png");
  await page.close();
}

// 05 — vertical integration: the MV district with the Netzampel cascade
async function shot5(ctx) {
  await loadScenario(SCEN4);
  const quiet = 3000;                        // district step + WLS ≈ 2 s
  let state = await pin(STEP_DISTRICT, { needEst: true, quietMs: quiet });
  const net = await api("GET", "/network");
  log(`  ${state.time_of_day}: ${describe(net, ranked(state)[0])}`);
  // Place the MV coordinator. It acts on the estimate it is handed, and its
  // signal throttles the NEXT step — so the very next frame carries both the
  // still-overloaded ring segment and the Steuerboxen that just started dimming.
  await api("POST", "/controller", { scope: "mv" }).catch((e) => log(`  coordinator: ${e.message}`));
  for (let i = 0; i < 6; i++) {
    state = await stepOnce(quiet);
    if ((state.controllers ?? []).some((c) => c.scope === "mv" && c.ev_factor < 1)) break;
  }
  const mv = (state.controllers ?? []).find((c) => c.scope === "mv");
  const dimming = (state.controllers ?? []).filter((c) => c.scope === "cell" && c.ev_factor < 1).length;
  log(`  ${state.time_of_day}: ${describe(net, ranked(state)[0])} · coordinator sees `
    + `${mv?.seen_pct?.toFixed(1)} % (${mv?.seen_src}), EV signal ${mv?.ev_factor} · ${dimming} boxes dimming`);
  const page = await openLive(ctx, state);
  await segButton(page, "Lastfluss").click();
  // every Steuerbox leaves a collapsed "Transformator …" section behind —
  // close them so the panel shows the overview and the Netzampel block
  for (let i = 0; i < 80 && (await page.locator(".side .sec-close").count()); i++) {
    await page.locator(".side .sec-close").first().click();
  }
  const scrolled = await cropSidePanel(page);
  if (scrolled) log(`  side panel scrolled by ${scrolled.toFixed(1)} px to end between blocks`);
  await parkMouse(page);
  await sleep(800);
  await shoot(page, "05-district.png");
  await page.close();
}

// -------------------------------------------------------------------- main --
const only = (() => {
  const i = process.argv.indexOf("--only");
  return i < 0 ? null : new Set((process.argv[i + 1] ?? "").split(",").map(Number));
})();
const want = (n) => !only || only.has(n);

const health = await fetch(API + "/health").then((r) => r.json()).catch(() => null);
if (health?.app !== "netzsim") {
  console.error(`No netzsim backend at ${API} (GET /health said: ${JSON.stringify(health)})`);
  process.exit(1);
}
log(`backend ${API} · UI ${UI} · out ${OUT}`);

const browser = await chromium.launch({ channel: "chrome", headless: true });
const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1.5, locale: "de-DE" });
try {
  if (want(1) || want(2)) {
    const evening = await setupEvening();
    if (want(1)) await shot1(ctx, evening);
    if (want(2)) await shot2(ctx, evening);
  }
  if (want(3)) {
    // the header chip shows the ACTIVE grid — keep it the same in every run
    if (!(want(1) || want(2))) { await loadScenario(SCEN2); await drain(); }
    await shot3(ctx);
  }
  if (want(4)) await shot4(ctx);
  if (want(5)) await shot5(ctx);
} finally {
  await browser.close();
}
log("done");
