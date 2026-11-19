/* Flybit Composer - front end (no dependencies). */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) node.append(kid);
  return node;
};

const STORE_KEY = "flybit-composer-project-v1";
let LIB = { shapes: [], effects: [], palettes: [], profile: {}, drone_counts: [] };
let shapesById = {};
let effectsById = {};
let state = null;        // the project
let selected = null;     // uid of the selected scene, "takeoff", "landing", or "t:<uid>" (transition into that scene)
let sourceFilter = "All";
let lastJob = null;      // last finished export

/* ---------------------------------------------------------------- state */

const uid = () => Math.random().toString(36).slice(2, 9);
const clone = (o) => JSON.parse(JSON.stringify(o));

function defaultParams(effectId) {
  const out = {};
  for (const p of effectsById[effectId].params) out[p.key] = p.default;
  return out;
}

function newLook(effect, colors, params = {}) {
  return { effect, params: { ...defaultParams(effect), ...params }, colors: colors.slice(), fade_in: 1.5, fade_out: 1.5, enabled: true, blend: "NORMAL" };
}

function newProject(count) {
  return {
    name: "New drone show",
    drone_count: count,
    crossfade: 1.5,
    takeoff: { altitude: LIB.profile.takeoff_altitude || 6 },
    limits: { ...(LIB.profile.limits || {}) },
    takeoff_layers: [newLook("twinkle", ["#000000", "#fff1d6"])],
    transition_look: newLook("sparkle", ["#1a0a00", "#ffb400"]),
    landing_layers: [newLook("breathe", ["#000000", "#fff1d6"])],
    scenes: [],
  };
}

function newScene(shape) {
  const palette = LIB.palettes[state.scenes.length % LIB.palettes.length];
  return {
    uid: uid(), shape_id: shape.id, name: shape.name,
    hold: Math.round(shape.hold_seconds || 12), extra_transition: 0,
    layers: [newLook("gradient", palette.colors, { direction: "b2t" })],
    reveal: null,
    transition: clone((state.scenes[state.scenes.length - 1] || {}).transition || state.transition_look),
  };
}

function save() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* storage may be unavailable */ }
}

const scene = () => state.scenes.find((s) => s.uid === selected) || null;
const SEGMENTS = { takeoff: "Takeoff", landing: "Return + Land" };
/** Scene whose incoming transition is selected, if any. */
const transitionScene = () => (typeof selected === "string" && selected.startsWith("t:") ? state.scenes.find((s) => "t:" + s.uid === selected) || null : null);
const isOn = (look) => !!look && look.enabled !== false;
const onLooks = (list) => (list || []).filter(isOn);

/** Brings projects saved by older versions up to the current shape. */
function migrate(p) {
  for (const [list, single] of [["takeoff_layers", "takeoff_look"], ["landing_layers", "landing_look"]]) {
    if (!Array.isArray(p[list])) p[list] = p[single] ? [p[single]] : [];
    delete p[single];
  }
  if (!p.transition_look) p.transition_look = { ...newLook("sparkle", ["#1a0a00", "#ffb400"]), enabled: false };
  for (const sc of p.scenes || []) if (!sc.transition) sc.transition = clone(p.transition_look);
  p.takeoff = p.takeoff || { altitude: LIB.profile.takeoff_altitude || 6 };
  p.limits = p.limits || { ...(LIB.profile.limits || {}) };
  return p;
}

function select(key) {
  // look down at the takeoff grid a little, otherwise it is seen edge-on
  if (!!SEGMENTS[selected] !== !!SEGMENTS[key]) { cam.yaw = 0; cam.pitch = SEGMENTS[key] ? 0.75 : 0; }
  selected = key;
  exitFlight(); refresh();
}

/* ---------------------------------------------------------------- colour */

const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const toLin = (c) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
const toSrgb = (c) => (c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055);

/** Blender colour ramps interpolate in linear light; do the same so the preview matches. */
function makeRamp(colors) {
  const stops = (colors.length === 1 ? [colors[0], colors[0]] : colors).map((c) => hexToRgb(c).map(toLin));
  const table = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = (i / 255) * (stops.length - 1);
    const a = Math.min(Math.floor(x), stops.length - 2);
    const t = x - a;
    for (let k = 0; k < 3; k++) {
      table[i * 3 + k] = Math.round(255 * toSrgb(stops[a][k] + (stops[a + 1][k] - stops[a][k]) * t));
    }
  }
  return table;
}

const rampCss = (colors) => `linear-gradient(90deg, ${(colors.length > 1 ? colors : [colors[0], colors[0]]).join(",")})`;

/* ---------------------------------------------------------------- api */

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  } catch (e) {
    throw new Error("The Flybit server is not running. Start it with: python3 server.py (in the flybit-composer folder), then try again.");
  }
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || "Request failed");
  return data;
}

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 6000);
}

async function loadLibrary(count) {
  LIB = await api("/api/library" + (count ? "?count=" + count : ""));
  shapesById = Object.fromEntries(LIB.shapes.map((s) => [s.id, s]));
  effectsById = Object.fromEntries(LIB.effects.map((e) => [e.id, e]));
}

/* ---------------------------------------------------------------- thumbnails */

function drawThumb(canvas, points, color = "#ffc247") {
  const size = 150;
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#07080c";
  ctx.fillRect(0, 0, size, size);
  let lo = [1e9, 1e9], hi = [-1e9, -1e9];
  for (const p of points) {
    lo = [Math.min(lo[0], -p[1]), Math.min(lo[1], p[2])];
    hi = [Math.max(hi[0], -p[1]), Math.max(hi[1], p[2])];
  }
  const span = Math.max(hi[0] - lo[0], hi[1] - lo[1], 1);
  const k = (size - 24) / span;
  const ox = (size - (hi[0] - lo[0]) * k) / 2, oy = (size - (hi[1] - lo[1]) * k) / 2;
  ctx.fillStyle = color;
  ctx.shadowColor = color;
  ctx.shadowBlur = 4;
  for (const p of points) {
    ctx.beginPath();
    ctx.arc(ox + (-p[1] - lo[0]) * k, size - oy - (p[2] - lo[1]) * k, 1.7, 0, 6.283);
    ctx.fill();
  }
}

/* ---------------------------------------------------------------- library panel */

function renderLibrary() {
  const sources = ["All", ...new Set(LIB.shapes.map((s) => s.source))];
  if (!sources.includes(sourceFilter)) sourceFilter = "All";
  $("shapeSources").replaceChildren(...sources.map((src) =>
    el("button", { class: "chip" + (src === sourceFilter ? " active" : ""), onclick: () => { sourceFilter = src; renderLibrary(); } }, src)));
  const query = $("shapeSearch").value.trim().toLowerCase();
  const limit = (state.limits && state.limits.min_distance) || 2;
  const grid = $("shapeGrid");
  grid.replaceChildren();
  for (const shape of LIB.shapes) {
    if (sourceFilter !== "All" && shape.source !== sourceFilter) continue;
    if (query && !shape.name.toLowerCase().includes(query)) continue;
    const canvas = el("canvas");
    const tight = shape.min_spacing < limit;
    const card = el("button", { class: "shape", title: `${shape.name} - ${shape.drone_count} drones, closest ${shape.min_spacing} m apart`, onclick: () => addScene(shape) },
      canvas,

      el("span", { class: "badge" + (tight ? " warn" : "") }, `${shape.min_spacing} m`),
      el("div", { class: "label" }, el("span", { class: "nm" }, shape.name), el("span", { class: "cnt" }, `${shape.drone_count}`)));
    grid.append(card);
    drawThumb(canvas, shape.points);
  }
  if (!grid.children.length) grid.append(el("div", { class: "empty", style: "grid-column:1/-1" }, "No shapes match."));
}

function addScene(shape) {
  const sc = newScene(shape);
  state.scenes.push(sc);
  selected = sc.uid;
  refresh();
}

/* ---------------------------------------------------------------- timeline */

let dragUid = null;

function renderTimeline() {
  const tl = $("timeline");
  tl.replaceChildren();
  const fixed = (key, title, layers) => {
    const on = onLooks(layers);
    return el("div", { class: "tl-fixed" + (selected === key ? " active" : ""), title: "Flight planned by Skybrush. Click to edit the lights.", onclick: () => select(key) },
      el("b", {}, title), on.length ? `${on.length} look${on.length > 1 ? "s" : ""}` : "lights off",
      el("div", { class: "tl-ramp", style: `width:70%;background:${on.length ? rampCss(on[on.length - 1].colors) : "#222"}` }));
  };
  // gap before scene `sc`: the first one belongs to the takeoff, the one after the last shape to the landing
  const gap = (text, key, look) => el("div", { class: "tl-gap" + (selected === key ? " active" : "") + (!look || isOn(look) ? "" : " off"),
    title: "Flight planned automatically by Skybrush. Click to edit the lights of this part.", onclick: () => select(key) },
    look && isOn(look) ? el("div", { class: "tl-ramp", style: `width:30px;background:${rampCss(look.colors)}` }) : null, text);
  tl.append(fixed("takeoff", "Takeoff", state.takeoff_layers));
  state.scenes.forEach((sc, i) => {
    tl.append(gap(sc.extra_transition ? `auto +${sc.extra_transition}s` : "auto", i ? "t:" + sc.uid : "takeoff", i ? sc.transition : null));
    const shape = shapesById[sc.shape_id];
    const canvas = el("canvas");
    const on = onLooks(sc.layers);
    const block = el("div", {
      class: "tl-scene" + (sc.uid === selected ? " active" : ""), draggable: "true", "data-uid": sc.uid,
      style: `width:${Math.max(220, 150 + sc.hold * 6)}px`,
      onclick: () => select(sc.uid),
      ondragstart: () => { dragUid = sc.uid; },
      ondragover: (e) => { e.preventDefault(); block.classList.add("dragover"); },
      ondragleave: () => block.classList.remove("dragover"),
      ondrop: (e) => {
        e.preventDefault();
        const from = state.scenes.findIndex((s) => s.uid === dragUid);
        if (from < 0 || from === i) return;
        const [moved] = state.scenes.splice(from, 1);
        state.scenes.splice(i, 0, moved);
        refresh();
      },
    },
      canvas,
      el("div", { class: "tl-info" },
        el("div", { class: "name" }, `${i + 1}. ${sc.name}`),
        el("div", { class: "meta" }, `${sc.hold}s hold` + (isOn(sc.reveal) ? " · reveal" : "") + (on.length > 1 ? ` · ${on.length} layers` : on.length ? "" : " · lights off") + (on.some((l) => Array.isArray(l.mask)) ? " · regions" : "")),
        el("div", { class: "tl-ramp", style: `background:${on[0] ? rampCss(on[0].colors) : "#222"}` })),
      el("button", { class: "tl-x", title: "Remove", onclick: (e) => { e.stopPropagation(); removeScene(sc.uid); } }, "×"),
      el("button", { class: "tl-play", title: "Play this scene: the flight into it and its lights", onclick: (e) => { e.stopPropagation(); selected = sc.uid; renderTimeline(); renderSceneTab(); playTrack(sc); } }, "▶"));
    tl.append(block);
    if (shape) drawThumb(canvas, shape.points, on[0] ? on[0].colors[on[0].colors.length - 1] : "#555");
  });
  tl.append(gap("auto", "landing", null), fixed("landing", "Return + Land", state.landing_layers));
}

function removeScene(id) {
  const i = state.scenes.findIndex((s) => s.uid === id);
  state.scenes.splice(i, 1);
  if (selected === id || selected === "t:" + id) selected = (state.scenes[Math.min(i, state.scenes.length - 1)] || {}).uid || null;
  refresh();
}

/* ---------------------------------------------------------------- inspector widgets */

function numberRow(label, value, min, max, step, onChange, unit = "") {
  const val = el("span", { class: "val" }, value + unit);
  const input = el("input", { type: "range", min, max, step, value,
    oninput: () => { val.textContent = input.value + unit; onChange(parseFloat(input.value), false); },
    onchange: () => onChange(parseFloat(input.value), true) });
  return el("div", { class: "row" }, el("label", {}, label), input, val);
}

function selectRow(label, value, options, onChange) {
  const select = el("select", { onchange: () => onChange(select.value) });
  for (const opt of options) {
    if (opt.group) {
      const group = el("optgroup", { label: opt.group });
      for (const o of opt.items) group.append(el("option", { value: o[0], selected: o[0] === value }, o[1]));
      select.append(group);
    } else select.append(el("option", { value: opt[0], selected: opt[0] === value }, opt[1]));
  }
  return el("div", { class: "row two" }, el("label", {}, label), select);
}

function effectOptions(kind, allowNone) {
  const groups = {};
  for (const e of LIB.effects) if (e.kind === kind) (groups[e.group] = groups[e.group] || []).push([e.id, e.label]);
  const opts = Object.entries(groups).map(([group, items]) => ({ group, items }));
  if (allowNone) opts.unshift(["", allowNone]);
  return opts;
}

/** Editor for one look (effect + params + colours). `commit(structural)` re-renders when needed. */
function lookEditor(look, { kind = "look", colors = true, fades = false, blend = false, commit }) {
  const box = el("div", { style: "display:flex;flex-direction:column;gap:8px" });
  box.append(selectRow("Effect", look.effect, effectOptions(kind), (v) => {
    look.effect = v;
    look.params = defaultParams(v);
    commit(true);
  }));
  const def = effectsById[look.effect];
  if (def.description) box.append(el("div", { class: "note" }, def.description));
  for (const p of def.params) {
    const value = look.params[p.key] ?? p.default;
    if (p.type === "number") box.append(numberRow(p.label, value, p.min, p.max, p.step, (v) => { look.params[p.key] = v; commit(false); }));
    else if (p.type === "choice") box.append(selectRow(p.label, value, p.options, (v) => { look.params[p.key] = v; commit(false); }));
    else if (p.type === "bool") {
      const cb = el("input", { type: "checkbox", checked: !!value, onchange: () => { look.params[p.key] = cb.checked; commit(false); } });
      box.append(el("div", { class: "row two" }, el("label", {}, p.label), el("div", {}, cb)));
    }
  }
  if (colors) {
    const ramp = el("div", { class: "ramp", style: `background:${rampCss(look.colors)}` });
    const wrap = el("div", { class: "colors" });
    look.colors.forEach((c, i) => {
      const input = el("input", { type: "color", value: c, title: i === 0 ? "Colour at 0 (dark end of the effect)" : "Colour stop",
        oninput: () => { look.colors[i] = input.value; ramp.style.background = rampCss(look.colors); commit(false); } });
      wrap.append(input);
    });
    if (look.colors.length < 6) wrap.append(el("button", { class: "btn tiny", title: "Add colour stop", onclick: () => { look.colors.push("#ffffff"); commit(true); } }, "+"));
    if (look.colors.length > 1) wrap.append(el("button", { class: "btn tiny", title: "Remove last colour", onclick: () => { look.colors.pop(); commit(true); } }, "−"));
    const pal = el("select", { title: "Palette", onchange: () => { if (pal.value !== "") { look.colors = LIB.palettes[pal.value].colors.slice(); commit(true); } } },
      el("option", { value: "" }, "Palette…"), LIB.palettes.map((p, i) => el("option", { value: i }, p.name)));
    wrap.append(pal);
    box.append(el("div", { class: "row two" }, el("label", {}, "Colours"), wrap), ramp);
  }
  if (Array.isArray(look.mask)) {
    box.append(el("div", { class: "row two" }, el("label", {}, "Applies to"),
      el("div", { class: "mask-row" }, el("span", {}, `${look.mask.length} drones` + (look.mask_label ? ` (part “${look.mask_label}”)` : "")),
        el("button", { class: "btn tiny", title: "Apply this look to every drone of the shape", onclick: () => { delete look.mask; delete look.mask_label; commit(true); } }, "Whole shape"))));
  }
  if (blend) box.append(selectRow("Blend", look.blend || "NORMAL",
    [["NORMAL", "Normal (covers layers below)"], ["SCREEN", "Add glow (screen)"], ["LIGHTEN", "Lighten"], ["MULTIPLY", "Multiply (tint / mask)"]],
    (v) => { look.blend = v; commit(false); }));
  if (fades === true || fades === "in") box.append(numberRow("Fade in", look.fade_in ?? 1.5, 0, 6, 0.25, (v) => { look.fade_in = v; commit(false); }, "s"));
  if (fades === true || fades === "out") box.append(numberRow("Fade out", look.fade_out ?? 1.5, 0, 6, 0.25, (v) => { look.fade_out = v; commit(false); }, "s"));
  return box;
}

const switchBox = (checked, onChange) => {
  const sw = el("input", { type: "checkbox", class: "switch", checked, title: "Turn this light effect on or off", onchange: () => onChange(sw.checked) });
  return sw;
};

/** Cards for a stack of looks (bottom layer first), each with an on/off switch. */
function layerCards(list, { title = "Look", fades = true, commit }) {
  const cards = list.map((layer, i) => {
    const on = isOn(layer);
    return el("div", { class: "card" + (on ? "" : " off") },
      el("div", { class: "card-head" },
        el("h3", {}, list.length > 1 ? `${title} · layer ${i + 1}${i === list.length - 1 ? " (top)" : ""}` : title),
        el("div", { class: "card-tools" },
          i > 0 ? el("button", { class: "btn tiny", title: "Move down", onclick: () => { list.splice(i - 1, 0, list.splice(i, 1)[0]); commit(true); } }, "↓") : null,
          el("button", { class: "btn tiny danger", title: "Remove this layer", onclick: () => { list.splice(i, 1); commit(true); } }, "Remove"),
          switchBox(on, (v) => { layer.enabled = v; commit(true); }))),
      on ? lookEditor(layer, { fades, blend: i > 0, commit }) : el("div", { class: "note" }, "Off: left out of the show. Its settings are kept."));
  });
  if (!onLooks(list).length) cards.push(el("div", { class: "note" }, "No light effect is on here, so the drones stay dark."));
  cards.push(el("button", { class: "btn", onclick: () => { list.push(newLook("twinkle", ["#000000", "#ffffff"])); commit(true); } }, "+ Add look layer"));
  return cards;
}

function renderTransitionTab(body, sc, soft) {
  const i = state.scenes.indexOf(sc), prev = state.scenes[i - 1];
  if (!prev) { selected = "takeoff"; return renderSegmentTab(body, "takeoff", soft); }
  const look = sc.transition, on = isOn(look);
  body.append(el("div", { class: "card" + (on ? "" : " off") },
    el("div", { class: "card-head" }, el("h3", {}, `Transition ${i}: ${prev.name} → ${sc.name}`), switchBox(on, (v) => { look.enabled = v; soft(true); })),
    on ? lookEditor(look, { commit: soft }) : el("div", { class: "note" }, "Off: drones travel dark in this transition. Its settings are kept."),
    el("div", { class: "note" }, "Lights while the drones fly from one shape to the next. Every transition has its own look. The flight itself is planned only by Skybrush inside Blender. The stage shows the lights on the shape the drones leave, without any movement; watch the real transition in the flight preview after an export.")));
  body.append(el("button", { class: "btn", title: "Copy this look, and its on/off state, to every other transition", onclick: () => {
    for (const other of state.scenes) if (other !== sc) other.transition = clone(look);
    state.transition_look = clone(look);
    soft(true);
  } }, "Use this look for all transitions"));
}

function renderSegmentTab(body, key, soft) {
  const lightsOnly = "The flight is planned by Skybrush exactly as before and is not changed here: only the lights are. The stage shows the lights on the takeoff grid; watch the real flight in the flight preview after an export.";
  if (key === "takeoff") {
    body.append(el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Takeoff lights")),
      el("div", { class: "note" }, "These looks play from lift-off until the first shape. " + lightsOnly)));
    body.append(...layerCards(state.takeoff_layers, { title: "Takeoff look", fades: "in", commit: soft }));
  } else {
    body.append(el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Return + Land lights")),
      el("div", { class: "note" }, "These looks play from the last shape until touchdown. " + lightsOnly)));
    body.append(...layerCards(state.landing_layers, { title: "Landing look", fades: "out", commit: soft }));
  }
  if (!state.scenes.length) body.append(el("div", { class: "note" }, "Add a shape to the timeline to see this previewed on the stage."));
}

/* ---------------------------------------------------------------- inspector: scene */

function renderSceneTab() {
  const body = $("tab-scene");
  body.replaceChildren();
  const sc = scene();
  const soft = (structural) => { save(); if (structural) refresh(); else { renderTimelineSoon(); requestPreview(); } };
  if (!sc && transitionScene()) return renderTransitionTab(body, transitionScene(), soft);
  if (!sc && SEGMENTS[selected]) return renderSegmentTab(body, selected, soft);
  if (!sc) {
    body.append(el("div", { class: "empty" }, "Click a shape in the library to add it to the timeline, then shape its look here. Click Takeoff, a transition or Return + Land in the timeline to set their lights."));
    return;
  }
  const shape = shapesById[sc.shape_id];

  const name = el("input", { type: "text", value: sc.name, oninput: () => { sc.name = name.value; save(); renderTimelineSoon(); updateStageTitle(); } });
  const general = el("div", { class: "card" },
    el("div", { class: "row two" }, el("label", {}, "Name"), name),
    numberRow("Hold in the sky", sc.hold, 2, 60, 1, (v, done) => { sc.hold = v; save(); if (done) refresh(); }, "s"),
    numberRow("Extra travel time", sc.extra_transition, 0, 20, 1, (v, done) => { sc.extra_transition = v; save(); if (done) renderTimeline(); }, "s"),
    el("div", { class: "note" }, `Skybrush works out the fastest safe transition into this shape; extra travel time slows it down for a calmer move.` +
      (shape && shape.min_spacing < state.limits.min_distance ? ` Note: the closest drones in this shape are ${shape.min_spacing} m apart, under your ${state.limits.min_distance} m limit.` : "")));
  body.append(general);

  // reveal
  const revealOn = isOn(sc.reveal);
  const revealCard = el("div", { class: "card" });
  revealCard.append(el("div", { class: "card-head" }, el("h3", {}, "Reveal"), switchBox(revealOn, (v) => {
    if (sc.reveal) sc.reveal.enabled = v;
    else sc.reveal = { effect: "reveal_wipe", params: defaultParams("reveal_wipe"), duration: 2.5, enabled: true };
    soft(true);
  })));
  if (revealOn) {
    revealCard.append(lookEditor(sc.reveal, { kind: "reveal", colors: false, commit: soft }),
      numberRow("Duration", sc.reveal.duration, 0.5, 10, 0.25, (v) => { sc.reveal.duration = v; soft(false); }, "s"));
  } else revealCard.append(el("div", { class: "note" }, "Off: the look fades in as the drones arrive. On: drones arrive dark and the shape is revealed."));
  body.append(revealCard);

  body.append(...layerCards(sc.layers, { commit: soft }),
    el("button", { class: "btn", onclick: () => {
      const copy = clone(sc); copy.uid = uid();
      state.scenes.splice(state.scenes.indexOf(sc) + 1, 0, copy); selected = copy.uid; refresh();
    } }, "Duplicate scene"));
}

/* ---------------------------------------------------------------- inspector: show settings */

function renderShowTab() {
  const body = $("tab-show");
  body.replaceChildren();
  body.append(
    el("div", { class: "note" }, "Lights for takeoff, each transition and return + land: click those parts in the timeline. Takeoff and landing always fly the same way, whatever the drone count."),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Timing")),
      numberRow("Crossfade", state.crossfade, 0, 5, 0.25, (v) => { state.crossfade = v; save(); }, "s"),
      el("div", { class: "note" }, "How long one look blends into the next around each transition."),
      numberRow("Takeoff altitude", state.takeoff.altitude, 3, 30, 1, (v) => { state.takeoff.altitude = v; save(); }, "m")),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Safety limits")),
      numberRow("Min distance", state.limits.min_distance, 1, 5, 0.1, (v, done) => { state.limits.min_distance = v; save(); if (done) renderLibrary(); }, "m"),
      numberRow("Max speed (horiz.)", state.limits.max_velocity_xy, 2, 15, 0.5, (v) => { state.limits.max_velocity_xy = v; save(); }, ""),
      numberRow("Max speed (vert.)", state.limits.max_velocity_z, 0.5, 6, 0.25, (v) => { state.limits.max_velocity_z = v; save(); }, ""),
      numberRow("Max acceleration", state.limits.max_acceleration, 1, 8, 0.5, (v) => { state.limits.max_acceleration = v; save(); }, ""),
      numberRow("Max altitude", state.limits.max_altitude, 30, 300, 5, (v) => { state.limits.max_altitude = v; save(); }, "m"),
      el("div", { class: "note" }, "Defaults come from the Dingucha Diwali show. Skybrush plans transitions inside these limits and every export is checked against them.")));
}

/* ---------------------------------------------------------------- stage (canvas preview) */

const stage = $("stage");
const ctx = stage.getContext("2d");
const cam = { yaw: 0, pitch: 0 };
let playing = true;
let preview = null;       // { fps, layers:[frames], reveal:frames|null, ramps, points, geo }
let flight = null;        // { fps, count, frames, positions, colors, geo, t, duration }
let clock = 0, lastTick = performance.now();

function geoOf(points) {
  const lo = [1e9, 1e9, 1e9], hi = [-1e9, -1e9, -1e9];
  for (const p of points) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], p[k]); hi[k] = Math.max(hi[k], p[k]); }
  const c = lo.map((v, k) => (v + hi[k]) / 2);
  let r = 1;
  for (const p of points) r = Math.max(r, Math.hypot(p[0] - c[0], p[1] - c[1], p[2] - c[2]));
  return { c, r };
}

function project(x, y, z, geo, w, h) {
  // audience looks along +X: right = -Y, up = +Z, depth = +X
  let right = -(y - geo.c[1]), up = z - geo.c[2], depth = x - geo.c[0];
  const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
  [right, depth] = [right * cy + depth * sy, -right * sy + depth * cy];
  const cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
  [up, depth] = [up * cp - depth * sp, up * sp + depth * cp];
  const f = geo.r * 4.5;
  const k = (f / (f + depth)) * (Math.min(w, h) * 0.42 / geo.r);
  return [w / 2 + right * k, h / 2 - up * k, k];
}

function drawDot(x, y, radius, r, g, b) {
  if (r + g + b < 6) {
    ctx.fillStyle = "rgba(120,130,160,0.16)";
    ctx.beginPath(); ctx.arc(x, y, radius * 0.45, 0, 6.283); ctx.fill();
    return;
  }
  const glow = ctx.createRadialGradient(x, y, 0, x, y, radius * 3.2);
  glow.addColorStop(0, `rgba(${r},${g},${b},0.95)`);
  glow.addColorStop(0.25, `rgba(${r},${g},${b},0.55)`);
  glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
  ctx.fillStyle = glow;
  ctx.beginPath(); ctx.arc(x, y, radius * 3.2, 0, 6.283); ctx.fill();
  ctx.fillStyle = `rgba(${Math.min(255, r + 90)},${Math.min(255, g + 90)},${Math.min(255, b + 90)},0.9)`;
  ctx.beginPath(); ctx.arc(x, y, radius * 0.5, 0, 6.283); ctx.fill();
}

function tick(now) {
  const dt = Math.min(0.1, (now - lastTick) / 1000);
  lastTick = now;
  const dpr = window.devicePixelRatio || 1;
  const w = stage.clientWidth, h = stage.clientHeight;
  if (stage.width !== Math.round(w * dpr) || stage.height !== Math.round(h * dpr)) { stage.width = Math.round(w * dpr); stage.height = Math.round(h * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.globalCompositeOperation = "lighter";

  if (flight) {
    if (playing) flight.t = (flight.t + dt) % flight.duration;
    // interpolate between samples so the flight moves smoothly
    const ft = Math.min(flight.frames - 1, flight.t * flight.fps);
    const f0 = Math.floor(ft), f1 = Math.min(flight.frames - 1, f0 + 1), a = ft - f0;
    const n = flight.count, b0 = f0 * n * 3, b1 = f1 * n * 3;
    const P = flight.positions, C = flight.colors;
    for (let i = 0; i < n; i++) {
      const k0 = b0 + i * 3, k1 = b1 + i * 3;
      const x = (P[k0] + (P[k1] - P[k0]) * a) / 100, y = (P[k0 + 1] + (P[k1 + 1] - P[k0 + 1]) * a) / 100, z = (P[k0 + 2] + (P[k1 + 2] - P[k0 + 2]) * a) / 100;
      const [sx, sy, s] = project(x, y, z, flight.geo, w, h);
      const ck = a < 0.5 ? k0 : k1;
      drawDot(sx, sy, Math.max(1.2, s * 0.5), C[ck], C[ck + 1], C[ck + 2]);
    }
    $("scrubRange").value = Math.round((flight.t / flight.duration) * 1000);
    const label = (flight.timeline || []).filter((e) => e.start <= flight.t).pop();
    const puid = label && label.uid && !label.name.startsWith("Transition") ? label.uid : null;
    if (puid !== flight.playingUid) { flight.playingUid = puid; markPlaying(puid); }
    $("scrubTime").textContent = `${fmt(flight.t)} / ${fmt(flight.duration)}` + (label ? ` · ${label.name}` : "");
  } else if (preview) {
    if (playing) clock += dt;
    const total = preview.layers.length ? preview.layers[0].length : (preview.reveal ? preview.reveal.length : 1);
    const f = Math.floor(clock * preview.fps) % total;
    const pts = preview.points, end = preview.end;
    let tt = end ? f / Math.max(1, total - 1) : 0;
    tt = tt * tt * (3 - 2 * tt);
    for (let i = 0; i < pts.length; i++) {
      const c = [0, 0, 0];
      for (let l = 0; l < preview.layers.length; l++) {
        if (preview.masks && preview.masks[l] && !preview.masks[l].has(i)) continue;
        const idx = Math.round(preview.layers[l][f][i] * 255) * 3, ramp = preview.ramps[l], mode = preview.blends[l];
        for (let k = 0; k < 3; k++) {
          const v = ramp[idx + k];
          c[k] = mode === "SCREEN" ? 255 - (255 - c[k]) * (255 - v) / 255 : mode === "MULTIPLY" ? c[k] * v / 255 : mode === "LIGHTEN" ? Math.max(c[k], v) : v;
        }
      }
      let [r, g, b] = c;
      if (preview.reveal) { const m = preview.reveal[f][i]; r *= m; g *= m; b *= m; }
      const p = end ? [0, 1, 2].map((k) => pts[i][k] + (end[i][k] - pts[i][k]) * tt) : pts[i];
      const [x, y, s] = project(p[0], p[1], p[2], preview.geo, w, h);
      drawDot(x, y, Math.min(4.5, Math.max(1.6, s * 0.55)), Math.round(r), Math.round(g), Math.round(b));
    }
  }
  ctx.globalCompositeOperation = "source-over";
  requestAnimationFrame(tick);
}

const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

let previewTimer = null, previewSeq = 0;
function requestPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const sc = scene(), key = selected, n = state.scenes.length;
    let layers, body;
    if (sc) {
      layers = onLooks(sc.layers);
      body = { shape_id: sc.shape_id, reveal: isOn(sc.reveal) ? sc.reveal : null, seconds: Math.min(Math.max(sc.hold, 4), 12) };
    } else if (transitionScene()) {
      const to = transitionScene(), from = state.scenes[state.scenes.indexOf(to) - 1];
      if (!from) { preview = null; return; }
      layers = onLooks([to.transition]);
      // lights only, shown on the shape the drones leave: no movement is simulated here
      body = { seconds: 8, still: true, path: [from.shape_id, to.shape_id] };
    } else if (SEGMENTS[key] && n) {
      // lights only, on the takeoff grid: the flight itself is not simulated here
      layers = onLooks(key === "takeoff" ? state.takeoff_layers : state.landing_layers);
      body = { seconds: 8, still: true, path: ["grid", key === "takeoff" ? state.scenes[0].shape_id : state.scenes[n - 1].shape_id] };
    } else { preview = null; return; }
    const seq = ++previewSeq;
    try {
      const data = await api("/api/preview", { drone_count: state.drone_count, layers, fps: 12, ...body });
      if (seq !== previewSeq) return;
      const points = data.start || shapesById[sc.shape_id].points;
      const keepClock = preview && preview.uid === key;
      preview = { uid: key, fps: data.fps, layers: data.layers, reveal: data.reveal, ramps: layers.map((l) => makeRamp(l.colors)),
        blends: layers.map((l) => l.blend || "NORMAL"), masks: layers.map((l) => (Array.isArray(l.mask) ? new Set(l.mask) : null)), points, end: data.end || null, geo: geoOf(data.end ? points.concat(data.end) : points) };
      if (!keepClock) clock = 0;
    } catch (e) { toast(e.message); }
  }, 120);
}

async function openImportedProject(file) {
  const project = await api("/api/imports/" + file);
  if (state.scenes.length && state.imported_from !== project.imported_from &&
      !confirm(`Load “${project.name}” into the timeline? The current timeline will be replaced (use Save first to keep it).`)) return false;
  await loadLibrary(project.drone_count);
  state = migrate(project);
  state.scenes = state.scenes.filter((s) => shapesById[s.shape_id]);
  selected = (state.scenes[0] || {}).uid || null;
  $("showName").value = state.name; fillCounts(); renderLibrary(); renderShowTab(); refresh();
  return true;
}

let trackSeq = 0;
async function playTrack(sc) {
  if (!state.scenes.length) return toast("Add at least one shape to the timeline first.");
  const seq = ++trackSeq;
  $("stageTitle").textContent = sc ? `Building the animation of “${sc.name}”…` : "Building the animation of the whole show…";
  try {
    const only = sc ? state.scenes.indexOf(sc) : null;
    const data = await api("/api/track-preview", { show: state, only });
    if (seq !== trackSeq) return;
    await playAnimation(data, sc ? `▶ ${sc.name} — draft animation` : "▶ Whole show — draft animation");
  } catch (e) { toast(e.message); updateStageTitle(); }
}

function markPlaying(uid) {
  for (const b of document.querySelectorAll(".tl-scene")) {
    const on = !!uid && b.dataset.uid === uid;
    b.classList.toggle("playing", on);
    if (on) {
      const tl = $("timeline"), r = b.getBoundingClientRect(), box = tl.getBoundingClientRect();
      tl.scrollTo({ left: tl.scrollLeft + r.left - box.left - (tl.clientWidth - r.width) / 2, behavior: "smooth" });
    }
  }
}

function playOriginal() {
  if (state.source_preview) playAnimation("/api/imports/" + state.source_preview, `Original show — ${state.name}`);
}

function updateStageTitle() {
  $("btnOriginal").hidden = !state.source_preview;
  const sc = scene();
  $("stageTitle").textContent = flight ? flight.title : sc ? sc.name : SEGMENTS[selected] ? SEGMENTS[selected] + " lights (on the takeoff grid)" : transitionScene() ? "Transition lights (no movement shown)" : "Pick a shape to start";
}

function exitFlight() {
  flight = null;
  markPlaying(null);
  $("scrub").hidden = true;
  updateStageTitle();
}

async function enterFlight() {
  if (!lastJob) return;
  playAnimation(`/api/files/${lastJob.id}/${lastJob.report.files.preview}`, "Flight preview — the exported show");
}

async function playAnimation(url, title) {
  try {
    const data = typeof url === "string" ? await (await fetch(url)).json() : url;
    const pts = [];
    for (let i = 0; i < data.positions.length; i += 30) pts.push([data.positions[i] / 100, data.positions[i + 1] / 100, data.positions[i + 2] / 100]);
    flight = { ...data, title, geo: geoOf(pts), t: 0, duration: data.frames / data.fps, playingUid: null };
    playing = true; $("btnPlay").textContent = "Pause";
    $("scrub").hidden = false;
    updateStageTitle();
  } catch (e) { toast("Could not load the animation."); }
}

/* ---------------------------------------------------------------- export */

function showModal(...content) { $("modalCard").replaceChildren(...content); $("modal").hidden = false; }
function hideModal() { $("modal").hidden = true; }

async function pollJob(id, onUpdate) {
  for (;;) {
    const job = await api("/api/jobs/" + id);
    onUpdate(job);
    if (job.status !== "running") return job;
    await new Promise((r) => setTimeout(r, 900));
  }
}

async function exportShow() {
  if (!state.scenes.length) return toast("Add at least one shape to the timeline first.");
  const bar = el("div"), msg = el("div", { class: "muted" }, "Starting Blender…");
  showModal(el("h1", {}, `Exporting “${state.name}”`), el("div", { class: "progress" }, bar), msg,
    el("div", { class: "note" }, "Blender and Skybrush Studio are building the show in the background: takeoff, transitions, return to home, landing, light effects, flight check and the .skyc export. A few minutes for a long show."));
  try {
    const { job } = await api("/api/export", state);
    const done = await pollJob(job, (j) => { bar.style.width = j.progress + "%"; msg.textContent = j.message; });
    if (done.status !== "done") throw new Error(done.message);
    lastJob = done;
    $("btnFlight").hidden = false;
    showResult(done);
  } catch (e) {
    showModal(el("h1", {}, "Export failed"), el("div", {}, e.message),
      el("div", { class: "actions" }, el("button", { class: "btn", onclick: hideModal }, "Close")));
  }
}

function showResult(job) {
  const r = job.report, c = r.check, L = c.limits;
  const stat = (k, v, bad) => el("div", { class: "stat " + (bad === undefined ? "" : bad ? "bad" : "ok") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v));
  const file = (name, label) => name ? el("a", { class: "btn", href: `/api/files/${job.id}/${name}` }, label) : null;
  showModal(
    el("h1", {}, r.warnings.length ? "Show exported — check the warnings" : "Show exported"),
    el("div", { class: "stats" },
      stat("Duration", fmt(c.duration)),
      stat("Drones", String(state.drone_count)),
      stat("Light effects", String(r.light_effects)),
      stat("Closest distance", c.min_distance === null ? "—" : c.min_distance + " m", c.min_distance !== null && c.min_distance < L.min_distance / 1.05),
      stat("Top speed (horiz.)", c.max_velocity_xy + " m/s", c.max_velocity_xy > L.max_velocity_xy * 1.05),
      stat("Top speed (vert.)", Math.max(c.max_velocity_up, c.max_velocity_down) + " m/s", Math.max(c.max_velocity_up, c.max_velocity_down) > L.max_velocity_z * 1.05),
      stat("Highest point", c.max_altitude + " m", c.max_altitude > L.max_altitude),
      stat("Build time", fmt(r.seconds)),
      stat("Scenes", String(state.scenes.length))),
    r.planner ? el("div", { class: "note" }, "Movement planned by Skybrush: " + (r.planner.used === "server" ? "Skybrush Studio Server." : r.planner.requested === "server"
      ? "inside Blender. The server was asked first and refused" + (r.planner.notes[0] ? " (" + r.planner.notes[0] + ")" : "") + "."
      : "inside Blender (local planner).")) : null,
    r.warnings.length ? el("ul", { class: "warnlist" }, r.warnings.map((w) => el("li", {}, w))) : el("div", { style: "color:var(--ok)" }, "Flight check passed: distance, speed and altitude are inside your limits."),
    el("div", { class: "note" }, "Open the .blend from the export folder (Open folder). Its light effects load their code from the _fx.py file there by full path, so if you move the folder, point the effects at the moved file again."),
    el("div", { class: "actions" },
      file(r.files.skyc, "Download .skyc"), file(r.files.blend, "Download .blend"), file(r.files.fx, "Download fx .py"),
      el("button", { class: "btn", onclick: () => api("/api/open-folder", { job: job.id }) }, "Open folder"),
      el("button", { class: "btn primary", onclick: () => { hideModal(); enterFlight(); } }, "Watch flight preview")));
}

function importBlend() {
  const file = el("input", { type: "file", accept: ".blend", hidden: true });
  const pick = el("button", { class: "dropzone", onclick: () => file.click() },
    el("b", {}, "Choose a .blend file…"), el("span", { class: "muted" }, "or drop it here"));
  const bar = el("div"), progress = el("div", { class: "progress", hidden: true }, bar);
  const msg = el("div", { class: "muted" });
  const result = el("div");
  const past = el("div", { class: "imports" });

  async function upload(f) {
    if (!f) return;
    if (!/\.blend$/i.test(f.name)) { msg.textContent = "Please choose a .blend file."; return; }
    pick.disabled = true; result.replaceChildren(); progress.hidden = false; bar.style.width = "0%";
    msg.textContent = `Uploading ${f.name}…`;
    try {
      const res = await fetch("/api/upload-blend", { method: "POST", headers: { "X-Filename": encodeURIComponent(f.name) }, body: f });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const done = await pollJob(data.job, (j) => { msg.textContent = j.message; bar.style.width = j.progress + "%"; });
      if (done.status !== "done") throw new Error(done.message);
      const r = done.report;
      await loadLibrary(state.drone_count);
      fillCounts(); renderLibrary();
      if (r.shows) {
        const count = r.profile.drone_count;
        msg.textContent = `${done.message}: ${r.shapes.length} shapes for ${count} drones, ${fmt(r.duration)} of animation.`;
        if (r.project && await openImportedProject(r.project)) msg.textContent += " The show is now in the timeline: drag blocks to reorder, click one to edit its lights.";
        result.replaceChildren(
          r.light_notes.length ? el("div", { class: "note" }, "Movement is exact. Some light effects use Python files that are not inside the .blend, so look-alikes are shown for them. Put those files in library/fx_sources and import again for the exact lights:",
            el("ul", { class: "warnlist" }, r.light_notes.map((n) => el("li", {}, n)))) : null,
          el("div", { class: "actions" }, el("button", { class: "btn primary", onclick: () => { hideModal(); playAnimation("/api/imports/" + r.preview, `Animation — ${r.profile.source}`); } }, "Play the show's animation")));
      } else {
        // a shapes-only file: add to the library; the drone count of each
        // shape is its number of points
        const counts = r.drone_counts, count = counts[0];
        const byCount = counts.map((c) => [c, r.shape_list.filter((x) => x.drone_count === c)]);
        msg.textContent = `Found ${r.shape_list.length} shape${r.shape_list.length > 1 ? "s" : ""} for ${counts.join(" and ")} drones. They are in the library under “${r.profile.source}”.`;
        const autoShow = state.drone_count === count || !state.scenes.length;
        if (autoShow) {
          if (state.drone_count !== count) await setCount(count);
          sourceFilter = r.profile.source; renderLibrary();
        }
        result.replaceChildren(
          ...byCount.map(([c, list]) => el("div", { class: "count-group" },
            el("div", { class: "count-head" }, el("b", {}, `${c} drones`), el("span", { class: "muted" }, `${list.length} shape${list.length > 1 ? "s" : ""}`)),
            el("ul", { class: "shape-list" }, list.map((x) => el("li", {}, x.name))))),
          autoShow ? el("div", { class: "note" }, `The library now shows these ${count}-drone shapes. Click one to add it to the timeline.`) : null,
          el("div", { class: "actions" },
            r.project ? el("button", { class: "btn", title: "Start a new timeline with all these shapes", onclick: async () => { if (await openImportedProject(r.project)) hideModal(); } }, "Open all in a new timeline") : null,
            autoShow ? el("button", { class: "btn primary", onclick: hideModal }, "Done") :
            el("button", { class: "btn primary", onclick: async () => {
              hideModal();
              await setCount(count);
              if (state.drone_count === count) { sourceFilter = r.profile.source; renderLibrary(); }
            } }, `Show these shapes (${count} drones)`)));
      }
      listPast();
    } catch (e) { msg.textContent = e.message; }
    pick.disabled = false;
  }

  async function listPast() {
    try {
      const { imports } = await api("/api/imports");
      past.replaceChildren(...(imports.length ? [el("h2", {}, "Imported shows")] : []),
        ...imports.map((im) => el("div", { class: "import-row" },
          el("span", {}, im.name, im.shapes ? el("span", { class: "muted" }, ` · ${im.shapes} shapes · ${im.drone_counts.join(" / ")} drones`) : null),
          el("div", { class: "card-tools" },
            im.project ? el("button", { class: "btn tiny", title: "Put this show's scenes in the timeline", onclick: async () => { if (await openImportedProject(im.project)) hideModal(); } }, "Open in timeline") : null,
            im.preview ? el("button", { class: "btn tiny", onclick: () => { hideModal(); playAnimation("/api/imports/" + im.preview, `Original show — ${im.name}`); } }, "Play animation") : el("span", { class: "muted" }, "shapes only")))));
    } catch (e) { /* list is optional */ }
  }

  file.onchange = () => upload(file.files[0]);
  pick.ondragover = (e) => { e.preventDefault(); pick.classList.add("over"); };
  pick.ondragleave = () => pick.classList.remove("over");
  pick.ondrop = (e) => { e.preventDefault(); pick.classList.remove("over"); upload(e.dataTransfer.files[0]); };
  showModal(el("h1", {}, "Import a Skybrush .blend"),
    el("div", { class: "note" }, "Every shape in the file becomes a library shape; its drone count is its number of points. If the file is a full Skybrush show, its whole animation (takeoff, every transition, landing and the lights) is read too, so you can watch it here. The file is only read, never changed. Blender is not needed."),
    file, pick, progress, msg, result, past,
    el("div", { class: "actions" }, el("button", { class: "btn", onclick: hideModal }, "Close")));
  listPast();
}

/* ---------------------------------------------------------------- wiring */

let tlTimer = null;
function renderTimelineSoon() { clearTimeout(tlTimer); tlTimer = setTimeout(renderTimeline, 250); }

function renderSummary() {
  const hold = state.scenes.reduce((a, s) => a + s.hold, 0);
  $("summary").innerHTML = `<b>${state.scenes.length}</b> scenes · <b>${fmt(hold)}</b> of shapes + auto transitions`;
}

function refresh() {
  save();
  renderTimeline();
  renderSceneTab();
  renderSummary();
  updateStageTitle();
  requestPreview();
}

function fillCounts() {
  const counts = [...new Set([...LIB.drone_counts, state.drone_count])].sort((a, b) => a - b);
  $("droneCount").replaceChildren(...counts.map((c) => el("option", { value: c, selected: c === state.drone_count }, c)),
    el("option", { value: "custom" }, "Other…"));
}

async function setCount(count) {
  if (state.scenes.length && !confirm(`Switch to ${count} drones? The timeline will be cleared because shapes are built for one drone count.`)) { fillCounts(); return; }
  await loadLibrary(count);
  state.drone_count = count; state.scenes = []; selected = null;
  fillCounts(); renderLibrary(); refresh();
}

async function init() {
  await loadLibrary();
  try { state = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (e) { state = null; }
  if (!state) state = newProject(LIB.count);
  await loadLibrary(state.drone_count);
  migrate(state);
  state.scenes = state.scenes.filter((s) => shapesById[s.shape_id]);
  selected = (state.scenes[0] || {}).uid || null;

  $("showName").value = state.name;
  $("showName").oninput = () => { state.name = $("showName").value; save(); };
  fillCounts();
  $("droneCount").onchange = () => {
    let v = $("droneCount").value;
    if (v === "custom") { v = prompt("How many drones?", "200"); if (!v || !(parseInt(v, 10) >= 4)) { fillCounts(); return; } }
    setCount(parseInt(v, 10));
  };
  $("shapeSearch").oninput = renderLibrary;
  $("btnExport").onclick = exportShow;
  $("btnImport").onclick = importBlend;
  $("btnFlight").onclick = enterFlight;
  $("btnOriginal").onclick = playOriginal;
  $("btnPlayAll").onclick = () => playTrack(null);
  $("btnExitFlight").onclick = exitFlight;
  $("btnPlay").onclick = () => { playing = !playing; $("btnPlay").textContent = playing ? "Pause" : "Play"; };
  $("btnView").onclick = () => { cam.yaw = 0; cam.pitch = 0; };
  $("scrubRange").oninput = () => { if (flight) flight.t = ($("scrubRange").value / 1000) * flight.duration; };
  $("btnSave").onclick = () => {
    const a = el("a", { href: URL.createObjectURL(new Blob([JSON.stringify(state, null, 1)], { type: "application/json" })), download: (state.name || "show").replace(/[^\w-]+/g, "_") + ".flybit.json" });
    a.click();
  };
  $("btnOpen").onclick = () => $("fileOpen").click();
  $("fileOpen").onchange = async () => {
    const file = $("fileOpen").files[0];
    if (!file) return;
    try {
      const project = JSON.parse(await file.text());
      if (!project.drone_count || !Array.isArray(project.scenes)) throw new Error("Not a composer project file.");
      await loadLibrary(project.drone_count);
      state = migrate(project); state.scenes = state.scenes.filter((s) => shapesById[s.shape_id]);
      selected = (state.scenes[0] || {}).uid || null;
      $("showName").value = state.name; fillCounts(); renderLibrary(); renderShowTab(); refresh();
    } catch (e) { toast(e.message); }
    $("fileOpen").value = "";
  };
  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("tab-scene").hidden = tab.dataset.tab !== "scene";
      $("tab-show").hidden = tab.dataset.tab !== "show";
    };
  }
  let drag = null;
  stage.onpointerdown = (e) => { drag = [e.clientX, e.clientY, cam.yaw, cam.pitch]; stage.setPointerCapture(e.pointerId); };
  stage.onpointermove = (e) => {
    if (!drag) return;
    cam.yaw = drag[2] + (e.clientX - drag[0]) * 0.008;
    cam.pitch = Math.max(-1.2, Math.min(1.2, drag[3] + (e.clientY - drag[1]) * 0.008));
  };
  stage.onpointerup = () => { drag = null; };

  renderLibrary();
  renderShowTab();
  refresh();
  requestAnimationFrame(tick);
}

init().catch((e) => toast("Could not start: " + e.message));
