"""Flybit Composer - local server.

    python server.py            -> http://localhost:8765

Serves the web composer, the shape / effect library, exact light effect
previews (evaluated with the same Python code that goes into the show) and runs
export jobs by driving Blender + Skybrush Studio headless.

Only the Python standard library is used.
"""

import glob
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
SHAPES = os.path.join(ROOT, "library", "shapes")
EXPORTS = os.path.join(ROOT, "exports")
IMPORTS = os.path.join(ROOT, "imports")
FX_LIBRARY = os.path.join(ROOT, "fx", "effects_library.py")
CONFIG = os.path.join(ROOT, "config.json")

DEFAULT_CONFIG = {
    "port": 8765,
    "blender": "",
    # "server": ask Skybrush Studio Server to plan movement; "local": Skybrush's built-in planner
    "planner": "server",
    "profile": {
        "fps": 24,
        "grid_spacing": 1.0,
        "takeoff_altitude": 6.0,
        # Takeoff and return-to-home fly at these fixed average speeds for every
        # show and drone count; they do not follow the safety-limit sliders.
        "takeoff_velocity": 1.05,
        "rth_velocity": 1.18,
        "limits": {
            "min_distance": 2.0,
            "max_velocity_xy": 10.0,
            "max_velocity_z": 2.0,
            "max_acceleration": 4.0,
            "max_altitude": 150.0,
            "min_nav_altitude": 2.5,
        },
    },
}

PALETTES = [
    {"name": "Diya gold", "colors": ["#3a1200", "#ff7a00", "#ffd24a"]},
    {"name": "Saffron to white", "colors": ["#ff6a00", "#ffffff"]},
    {"name": "Tiranga", "colors": ["#ff9933", "#ffffff", "#138808"]},
    {"name": "Rangoli", "colors": ["#ff2d95", "#ffb400", "#18d6c0"]},
    {"name": "Peacock", "colors": ["#0b2a8f", "#00b7c2", "#38e07b"]},
    {"name": "Kumkum", "colors": ["#4a0010", "#e0002a", "#ffb000"]},
    {"name": "Royal", "colors": ["#2a0a6b", "#8a2be2", "#ffd700"]},
    {"name": "Ice", "colors": ["#001a40", "#3fa9f5", "#ffffff"]},
    {"name": "Warm white", "colors": ["#000000", "#fff1d6"]},
    {"name": "Pure white", "colors": ["#000000", "#ffffff"]},
    {"name": "Fire", "colors": ["#000000", "#ff2a00", "#ffd000"]},
    {"name": "Rainbow", "colors": ["#ff0040", "#ffd000", "#00e070", "#00a0ff", "#a040ff"]},
]


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.isfile(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            user = json.load(f)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    if not cfg["blender"]:
        found = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"))
        found += sorted(glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender"))
        found += sorted(glob.glob(os.path.expanduser("~/Applications/Blender*.app/Contents/MacOS/Blender")))
        cfg["blender"] = found[-1] if found else "blender"
    return cfg


def load_fx():
    spec = importlib.util.spec_from_file_location("flybit_fx", FX_LIBRARY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CFG = load_config()
FX = load_fx()
JOBS = {}


# --------------------------------------------------------------------------
# shape library
# --------------------------------------------------------------------------

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "show"


def resample(outline, n, closed=True):
    """n points spread evenly along a polyline (list of (u, v))."""
    pts = list(outline) + ([outline[0]] if closed else [])
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(seg)
    out = []
    slots = n if closed else max(n - 1, 1)
    for k in range(n):
        target = total * k / slots
        i = 0
        while i < len(seg) - 1 and target > seg[i]:
            target -= seg[i]
            i += 1
        t = target / seg[i] if seg[i] else 0.0
        out.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t))
    return out, total


def place(uv, spacing_now, spacing=3.0, depth=None):
    """Scale a flat (u, v) point set to the wanted drone spacing and stand it
    up in the Y/Z plane facing the audience, at a safe altitude."""
    k = spacing / spacing_now if spacing_now else 1.0
    vmin = min(v for _u, v in uv)
    base = 40.0
    pts = []
    for i, (u, v) in enumerate(uv):
        x = depth[i] * k if depth else 0.0
        pts.append([round(x, 3), round(-u * k, 3), round(base + (v - vmin) * k, 3)])
    return pts


def basic_shapes(n):
    shapes = []

    def add(name, pts):
        # Only offer generated shapes that are safe as they stand: drones at
        # least 2.5 m apart and the whole shape under 140 m.
        if len(pts) <= 600 and min_spacing(pts) < 2.5:
            return
        if max(p[2] for p in pts) > 140:
            return
        shapes.append({
            "id": "basic/%d/%s" % (n, slugify(name)), "name": name, "drone_count": n,
            "source": "Basic shapes", "hold_seconds": 12.0, "points": pts,
        })

    def nested(outline):
        """Outline drawn as 1-4 concentric copies so large fleets stay compact."""
        rings = 1 if n <= 60 else 2 if n <= 140 else 3 if n <= 260 else 4
        scales = [1 - 0.27 * i for i in range(rings)]
        left, uv, step = n, [], None
        for i, k in enumerate(scales):
            share = left if i == rings - 1 else max(3, round(n * k / sum(scales)))
            share = min(share, left)
            left -= share
            pts, total = resample(outline, share)
            if step is None:
                step = total / share
            uv += [(u * k, v * k) for u, v in pts]
        return place(uv, step)

    circle = [(math.cos(2 * math.pi * i / 180), math.sin(2 * math.pi * i / 180)) for i in range(180)]
    add("Circle", nested(circle))

    heart = []
    for i in range(240):
        t = 2 * math.pi * i / 240
        heart.append((16 * math.sin(t) ** 3, 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)))
    add("Heart", nested(heart))

    star = []
    for i in range(10):
        r = 1.0 if i % 2 == 0 else 0.42
        a = math.pi / 2 + i * math.pi / 5
        star.append((r * math.cos(a), r * math.sin(a)))
    add("Star", nested(star))

    cols = max(2, math.ceil(math.sqrt(n * 1.6)))
    uv = [(i % cols - (cols - 1) / 2, -(i // cols)) for i in range(n)]
    add("Wall", place(uv, 1.0))

    inner = max(3, int(n * 0.38))
    a, ta = resample(circle, n - inner)
    b, tb = resample(circle, inner)
    ratio = (inner / (n - inner))
    uv = a + [(u * ratio, v * ratio) for u, v in b]
    add("Double ring", place(uv, ta / (n - inner)))

    # Fibonacci sphere: true 3D shape
    golden = math.pi * (3 - math.sqrt(5))
    uv, depth = [], []
    for i in range(n):
        z = 1 - 2 * (i + 0.5) / n
        r = math.sqrt(1 - z * z)
        uv.append((r * math.cos(golden * i), z))
        depth.append(r * math.sin(golden * i))
    add("Sphere", place(uv, math.sqrt(4 * math.pi / n) * 0.9, depth=depth))

    if n >= 12:
        turns = 3
        uv, depth = [], []
        for i in range(n):
            t = i / (n - 1)
            uv.append((math.cos(turns * 2 * math.pi * t), t * 3.2))
            depth.append(math.sin(turns * 2 * math.pi * t))
        step = math.dist((uv[0][0], uv[0][1], depth[0]), (uv[1][0], uv[1][1], depth[1]))
        add("Helix", place(uv, step, depth=depth))
    return shapes


def min_spacing(points):
    best = 1e9
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = math.dist(points[i], points[j])
            if d < best:
                best = d
    return round(best, 2)


_SPACING_CACHE = {}


def with_spacing(shape):
    key = shape["id"]
    if key not in _SPACING_CACHE:
        _SPACING_CACHE[key] = min_spacing(shape["points"])
    shape["min_spacing"] = _SPACING_CACHE[key]
    return shape


def list_shapes(count=None):
    shapes = []
    for path in sorted(glob.glob(os.path.join(SHAPES, "*", "*.json"))):
        with open(path, encoding="utf-8") as f:
            shape = json.load(f)
        if count is None or shape["drone_count"] == count:
            shapes.append(shape)
    if count:
        shapes.extend(basic_shapes(count))
    return [with_spacing(s) for s in shapes]


def drone_counts():
    counts = set()
    for d in glob.glob(os.path.join(SHAPES, "*")):
        if os.path.isdir(d) and os.path.basename(d).isdigit() and glob.glob(os.path.join(d, "*.json")):
            counts.add(int(os.path.basename(d)))
    return sorted(counts)


def find_shape(shape_id, count):
    for shape in list_shapes(count):
        if shape["id"] == shape_id:
            return shape
    raise ValueError("Shape not found in the library: %s" % shape_id)


# --------------------------------------------------------------------------
# effect preview: the real Python effect, evaluated on the shape's points
# --------------------------------------------------------------------------

def geometry(points):
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    c = [(lo[i] + hi[i]) / 2 for i in range(3)]
    r = max(math.dist(p, c) for p in points)
    return {"lo": lo, "hi": hi, "c": c, "r": max(r, 0.5)}


def grid_points(n, spacing):
    """Takeoff grid on the ground, centred on the origin (preview only)."""
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return [[(i // cols - (rows - 1) / 2) * spacing, (i % cols - (cols - 1) / 2) * spacing, 0.0] for i in range(n)]


def active_looks(looks):
    """Looks that are switched on; switched-off ones are left out of the show."""
    return [l for l in (looks or []) if l and l.get("effect") and l.get("enabled", True)]


def evaluate_effect(points, layer, seconds, fps, one_shot_seconds=None, end_points=None, geo_points=None):
    """Values 0..1 per frame and drone. With end_points the drones travel from
    points to end_points during the clip, like the client-side preview does."""
    fn = getattr(FX, layer["effect"], None)
    if fn is None or layer["effect"] not in {e[0] for e in FX.REGISTRY}:
        raise ValueError("Unknown effect: %s" % layer["effect"])
    P = FX.default_params(layer["effect"])
    P.update({k: v for k, v in (layer.get("params") or {}).items() if k in P})
    P.update(geometry(geo_points or (points + end_points if end_points else points)))
    P["f0"], P["fps"] = 0, fps
    n = len(points)
    total = max(1, int(seconds * fps))
    span = max(1, int((one_shot_seconds or seconds) * fps))
    frames = []
    for frame in range(total):
        tf = min(1.0, frame / span)
        pts = points
        if end_points:
            t = frame / max(1, total - 1)
            t = t * t * (3 - 2 * t)
            pts = [[a[k] + (b[k] - a[k]) * t for k in range(3)] for a, b in zip(points, end_points)]
        frames.append([
            round(max(0.0, min(1.0, fn(frame, tf, i, i, pts[i], n, P))), 3) for i in range(n)
        ])
    return frames


# --------------------------------------------------------------------------
# draft track preview: the timeline played on the stage before any export
# --------------------------------------------------------------------------
# Only for watching: drones move in straight, eased lines between the shapes.
# The show that is exported is still planned entirely by Skybrush in Blender.

def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(c):
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _ramp_table(colors):
    cols = colors or ["#000000", "#ffffff"]
    if len(cols) == 1:
        cols = [cols[0], cols[0]]
    stops = [[_lin(int(c[i:i + 2], 16) / 255) for i in (1, 3, 5)] for c in cols]
    table = []
    for i in range(256):
        x = i / 255 * (len(stops) - 1)
        a = min(int(x), len(stops) - 2)
        t = x - a
        table.append([round(255 * _srgb(stops[a][k] + (stops[a + 1][k] - stops[a][k]) * t)) for k in range(3)])
    return table


def _assign(cur, pts):
    """Greedy nearest assignment: returns for each drone the index of its point."""
    pairs = sorted((math.dist(c, p), d, j) for d, c in enumerate(cur) for j, p in enumerate(pts))
    out, used_d, used_p = [None] * len(cur), set(), set()
    for _dist, d, j in pairs:
        if d in used_d or j in used_p:
            continue
        out[d] = j
        used_d.add(d)
        used_p.add(j)
        if len(used_d) == len(cur):
            break
    return out


def _travel_time(a, b, extra=0.0):
    dxy = max(math.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(a, b))
    dz = max(abs(p[2] - q[2]) for p, q in zip(a, b))
    return max(4.0, dxy / 3.0, dz / 1.6) + float(extra or 0)


def track_preview(show, only=None, fps=4):
    count = int(show["drone_count"])
    profile = CFG["profile"]
    alt = float((show.get("takeoff") or {}).get("altitude") or profile["takeoff_altitude"])
    spacing = profile["grid_spacing"]
    xf = float(show.get("crossfade", 1.5))
    scenes = [sc for sc in show.get("scenes", [])]
    if not scenes:
        raise ValueError("Add at least one shape to the timeline.")
    shapes = [find_shape(sc["shape_id"], count)["points"] for sc in scenes]
    grid = grid_points(count, spacing)
    hover = [[p[0], p[1], alt] for p in grid]

    # ---- the path: list of segments (start positions, end positions, seconds, kind, index)
    segs = []
    pos = [list(p) for p in grid]
    form = list(range(count))  # formation index each drone shows

    def move_to(points, seconds, kind, idx, assign=True):
        nonlocal pos, form
        if assign:
            perm = _assign(pos, points)
            end = [points[j] for j in perm]
            form = perm
        else:
            end = [list(p) for p in points]
        segs.append({"a": pos, "b": end, "t": seconds, "kind": kind, "i": idx, "form": list(form)})
        pos = end

    lift = alt / profile.get("takeoff_velocity", 1.05)
    move_to(hover, lift, "takeoff", None, assign=False)
    for i, pts in enumerate(shapes):
        move_to(pts, _travel_time(pos, pts, scenes[i].get("extra_transition")), "takeoff" if i == 0 else "transition", i)
        segs.append({"a": pos, "b": pos, "t": float(scenes[i].get("hold", 10)), "kind": "hold", "i": i, "form": list(form)})
    home = [[p[0], p[1], alt] for p in grid]
    perm = _assign(pos, home)
    move_to([home[perm[d]] for d in range(count)], _travel_time(pos, home), "landing", None, assign=False)
    move_to([[p[0], p[1], 0.0] for p in pos], alt / profile.get("rth_velocity", 1.18), "landing", None, assign=False)

    # times
    t = 0.0
    for sg in segs:
        sg["t0"], t = t, t + sg["t"]
    total = t
    lo_t, hi_t = 0.0, total
    if only is not None:
        only = int(only)
        mine = [sg for sg in segs if sg["i"] == only]
        lo_t, hi_t = mine[0]["t0"], mine[-1]["t0"] + mine[-1]["t"]

    # ---- lights: one entry per look with its time window
    looks = []

    def add_look(look, t0, t1, geo_pts, fade_in, fade_out, mask=None, reveal=None):
        if not look or not look.get("effect") or look.get("enabled", True) is False or t1 <= t0:
            return
        P = FX.default_params(look["effect"])
        P.update({k: v for k, v in (look.get("params") or {}).items() if k in P})
        P.update(geometry(geo_pts))
        P["f0"], P["fps"] = int(round(t0 * 24)), 24
        half = (t1 - t0) / 2
        looks.append({"fn": getattr(FX, look["effect"]), "P": P, "t0": t0, "t1": t1,
                      "fi": min(fade_in, half), "fo": min(fade_out, half), "ramp": _ramp_table(look.get("colors")),
                      "blend": look.get("blend") or "NORMAL", "mask": set(look["mask"]) if isinstance(look.get("mask"), list) else None,
                      "reveal": reveal})

    holds = [sg for sg in segs if sg["kind"] == "hold"]
    trans = {sg["i"]: sg for sg in segs if sg["kind"] in ("takeoff", "transition") and sg["i"] is not None}
    first_t = trans[0]
    takeoff_end = first_t["t0"] + first_t["t"]
    for look in active_looks(show.get("takeoff_layers")):
        add_look(look, 0.0, takeoff_end, grid + shapes[0], float(look.get("fade_in", 1.0)), xf)
    for i in range(1, len(scenes)):
        sg = trans[i]
        add_look(scenes[i].get("transition"), sg["t0"] - xf, sg["t0"] + sg["t"], shapes[i - 1] + shapes[i], xf, xf)
    land_t0 = holds[-1]["t0"] + holds[-1]["t"]
    for look in active_looks(show.get("landing_layers")):
        add_look(look, land_t0 - xf, total, grid + shapes[-1], xf, float(look.get("fade_out", 2.0)))
    for i, sg in enumerate(holds):
        sc = scenes[i]
        rev = sc.get("reveal") if (sc.get("reveal") or {}).get("enabled", True) and (sc.get("reveal") or {}).get("effect") else None
        for look in active_looks(sc.get("layers")):
            add_look(look, sg["t0"], sg["t0"] + sg["t"], shapes[i], 0.0 if rev else float(look.get("fade_in", 1.5)),
                     float(look.get("fade_out", 1.5)))
        if rev:
            P = FX.default_params(rev["effect"])
            P.update({k: v for k, v in (rev.get("params") or {}).items() if k in P})
            P.update(geometry(shapes[i]))
            P["f0"], P["fps"] = int(round(sg["t0"] * 24)), 24
            looks.append({"reveal_of": i, "fn": getattr(FX, rev["effect"]), "P": P, "t0": sg["t0"],
                          "t1": sg["t0"] + max(0.5, float(rev.get("duration", 2.0)))})

    # ---- sample
    positions, colors, frames = [], [], 0
    step = 1.0 / fps
    si = 0
    tt = lo_t
    while tt <= hi_t + 1e-9:
        while si < len(segs) - 1 and tt >= segs[si]["t0"] + segs[si]["t"]:
            si += 1
        sg = segs[si]
        u = 0.0 if sg["t"] <= 0 else max(0.0, min(1.0, (tt - sg["t0"]) / sg["t"]))
        u = u * u * (3 - 2 * u)
        pts = [[a[k] + (b[k] - a[k]) * u for k in range(3)] for a, b in zip(sg["a"], sg["b"])]
        form = sg["form"]
        frame = int(round(tt * 24))
        cols = [[0.0, 0.0, 0.0] for _ in range(count)]
        reveal_mult = None
        for lk in looks:
            if not (lk["t0"] <= tt < lk["t1"]):
                continue
            tf = (tt - lk["t0"]) / (lk["t1"] - lk["t0"])
            if "reveal_of" in lk:
                reveal_mult = [max(0.0, min(1.0, lk["fn"](frame, tf, d, form[d], pts[d], count, lk["P"]))) for d in range(count)]
                continue
            a = 1.0
            if lk["fi"] > 0 and tt < lk["t0"] + lk["fi"]:
                a *= (tt - lk["t0"]) / lk["fi"]
            if lk["fo"] > 0 and tt > lk["t1"] - lk["fo"]:
                a *= (lk["t1"] - tt) / lk["fo"]
            for d in range(count):
                if lk["mask"] is not None and form[d] not in lk["mask"]:
                    continue
                v = max(0.0, min(1.0, lk["fn"](frame, tf, d, form[d], pts[d], count, lk["P"])))
                c = lk["ramp"][int(round(v * 255))]
                cur = cols[d]
                for k in range(3):
                    x = c[k]
                    if lk["blend"] == "SCREEN":
                        x = 255 - (255 - cur[k]) * (255 - x) / 255
                    elif lk["blend"] == "MULTIPLY":
                        x = cur[k] * x / 255
                    elif lk["blend"] == "LIGHTEN":
                        x = max(cur[k], x)
                    cur[k] += (x - cur[k]) * a
        if reveal_mult:
            for d in range(count):
                cols[d] = [c * reveal_mult[d] for c in cols[d]]
        for p in pts:
            positions += [int(round(c * 100)) for c in p]
        for c in cols:
            colors += [max(0, min(255, int(round(v)))) for v in c]
        frames += 1
        tt += step

    timeline = []
    for sg in segs:
        if lo_t - 1e-6 <= sg["t0"] < hi_t:
            name = {"takeoff": "Takeoff", "landing": "Return + Land"}.get(sg["kind"])
            if sg["kind"] == "hold":
                name = scenes[sg["i"]].get("name")
            elif sg["kind"] == "transition":
                name = "Transition → " + (scenes[sg["i"]].get("name") or "")
            timeline.append({"name": name, "start": round(sg["t0"] - lo_t, 2), "uid": scenes[sg["i"]].get("uid") if sg["i"] is not None else None})
    return {"fps": fps, "count": count, "frames": frames, "positions": positions, "colors": colors,
            "timeline": timeline, "duration": round(hi_t - lo_t, 1)}


# --------------------------------------------------------------------------
# export jobs
# --------------------------------------------------------------------------

def build_spec(show, job_dir):
    profile = CFG["profile"]
    count = int(show["drone_count"])
    scenes = []
    for sc in show.get("scenes", []):
        shape = find_shape(sc["shape_id"], count)
        scenes.append({
            "name": sc.get("name") or shape["name"],
            "points": shape["points"],
            "hold": float(sc.get("hold", 10)),
            "extra_transition": float(sc.get("extra_transition", 0)),
            "layers": active_looks(sc.get("layers")),
            "reveal": (active_looks([sc.get("reveal")]) or [None])[0],
            # look while flying into this shape; older projects have one look for all
            "transition": (active_looks([sc["transition"] if "transition" in sc else show.get("transition_look")]) or [None])[0],
        })
    if not scenes:
        raise ValueError("Add at least one shape to the timeline.")
    limits = dict(profile["limits"])
    limits.update(show.get("limits") or {})
    takeoff = {"altitude": profile["takeoff_altitude"], "grid_spacing": profile["grid_spacing"]}
    takeoff.update(show.get("takeoff") or {})
    takeoff["velocity"] = profile.get("takeoff_velocity", 1.05)
    landing = dict(show.get("landing") or {})
    landing["velocity"] = profile.get("rth_velocity", 1.18)
    return {
        "name": show.get("name") or "Untitled show",
        "file_base": slugify(show.get("name") or "show")[:40].strip("-") or "show",
        "out_dir": job_dir,
        "drone_count": count,
        "fps": int(show.get("fps") or profile["fps"]),
        "limits": limits,
        "takeoff": takeoff,
        "landing": landing,
        "crossfade": float(show.get("crossfade", 1.5)),
        "planner": show.get("planner") or CFG.get("planner", "server"),
        "scenes": scenes,
        # older projects have a single takeoff_look / landing_look
        "takeoff_layers": active_looks(show["takeoff_layers"] if "takeoff_layers" in show else [show.get("takeoff_look")]),
        "landing_layers": active_looks(show["landing_layers"] if "landing_layers" in show else [show.get("landing_look")]),
        "export": show.get("export") or {},
        "fx_library": FX_LIBRARY,
        "fx_registry": FX.registry_as_json(),
    }


def run_blender(job, args, on_line=None):
    cmd = [CFG["blender"], "-b"] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    job["pid"] = proc.pid
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        del tail[:-400]
        if on_line:
            on_line(line)
    proc.wait()
    return proc.returncode, tail


def export_job(job, show):
    try:
        job_dir = os.path.join(EXPORTS, job["id"])
        os.makedirs(job_dir, exist_ok=True)
        spec = build_spec(show, job_dir)
        spec_path = os.path.join(job_dir, spec["file_base"] + "_spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        with open(os.path.join(job_dir, spec["file_base"] + "_composer.json"), "w", encoding="utf-8") as f:
            json.dump(show, f, indent=1)

        def on_line(line):
            if line.startswith("PROGRESS "):
                _tag, pct, message = line.split(" ", 2)
                job["progress"], job["message"] = int(pct), message

        code, tail = run_blender(job, ["--python", os.path.join(ROOT, "blender", "build_show.py"), "--", spec_path], on_line)
        with open(os.path.join(job_dir, "blender_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(tail))
        report_path = os.path.join(job_dir, spec["file_base"] + "_report.json")
        if not os.path.isfile(report_path):
            raise RuntimeError("Blender exited (code %s) without a report. See blender_log.txt." % code)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        job["report"] = report
        job["folder"] = job_dir
        if not report.get("ok"):
            raise RuntimeError(report.get("error") or "Build failed")
        job["status"] = "done"
        job["progress"] = 100
        job["message"] = "Show exported"
    except Exception as ex:  # noqa: BLE001 - shown to the user
        job["status"] = "error"
        job["message"] = str(ex)


def import_job(job, blend_path, label):
    """Read a Skybrush .blend directly (no Blender needed): formations go to
    the shape library and the whole show (flight + lights) is sampled so the
    stage can play it."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "blender"))
        from blend_import import SkybrushShow

        job["message"] = "Reading the .blend"
        show = SkybrushShow(blend_path, fx_dirs=[os.path.join(ROOT, "library", "fx_sources")])
        shapes = show.shapes(label)
        if not shapes:
            raise RuntimeError("No Skybrush formations could be read from that file.")
        written = []
        for shape in shapes:
            out_dir = os.path.join(SHAPES, str(shape["drone_count"]))
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, shape["id"].split("/", 1)[1] + ".json"), "w", encoding="utf-8") as f:
                json.dump(shape, f)
            written.append(shape["id"])
        _SPACING_CACHE.clear()
        profile = show.profile(label, written)
        with open(os.path.join(SHAPES, "_profile_%s.json" % slugify(label)), "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=1)
        job["progress"] = 10
        name, preview = None, None
        if show.drones() and show.storyboard():
            job["message"] = "Imported %d shapes. Sampling the show's animation" % len(written)

            def on_progress(frac):
                job["progress"] = 10 + int(88 * frac)
                job["message"] = "Sampling the show's animation: %d%%" % int(100 * frac)

            preview = show.preview(fps=4, progress=on_progress)
            preview["name"] = label
            name = slugify(label) + "_preview.json"
            with open(os.path.join(IMPORTS, name), "w", encoding="utf-8") as f:
                json.dump(preview, f, separators=(",", ":"))

        # the show as a timeline project, saved next to its animation
        project = show.project(label, shapes)
        for sc in project["scenes"]:
            for look in sc["layers"] + [sc["transition"]] + ([sc["reveal"]] if sc["reveal"] else []):
                look["params"] = {**FX.default_params(look["effect"]), **(look.get("params") or {})}
        for look in project["takeoff_layers"] + project["landing_layers"]:
            look["params"] = {**FX.default_params(look["effect"]), **(look.get("params") or {})}
        project["takeoff"] = {"altitude": profile["takeoff_altitude"] if preview else CFG["profile"]["takeoff_altitude"]}
        project["limits"] = ({k: profile[k] for k in ("min_distance", "max_velocity_xy", "max_velocity_z",
                                                      "max_acceleration", "max_altitude", "min_nav_altitude")}
                             if preview else dict(CFG["profile"]["limits"]))
        project["source_preview"] = name
        project_name = slugify(label) + "_project.json"
        with open(os.path.join(IMPORTS, project_name), "w", encoding="utf-8") as f:
            json.dump(project, f, indent=1)
        counts = sorted({s["drone_count"] for s in shapes})
        shape_list = [{"id": s["id"], "name": s["name"], "drone_count": s["drone_count"]} for s in shapes]
        with open(os.path.join(IMPORTS, slugify(label) + "_info.json"), "w", encoding="utf-8") as f:
            json.dump({"name": label, "shapes": shape_list, "drone_counts": counts, "has_show": bool(preview)}, f, indent=1)
        job["report"] = {"shapes": written, "shape_list": shape_list, "profile": profile, "preview": name, "project": project_name,
                         "drone_counts": counts, "shows": bool(preview),
                         "duration": round(preview["frames"] / preview["fps"], 1) if preview else 0,
                         "light_notes": preview["light_notes"] if preview else []}
        job["status"], job["progress"] = "done", 100
        job["message"] = ("Imported %d shapes and the show's animation" % len(written) if preview
                          else "Imported %d shapes (%s drones)" % (len(written), ", ".join(map(str, counts))))
    except Exception as ex:  # noqa: BLE001
        job["status"] = "error"
        job["message"] = str(ex)


def list_imports():
    out = []
    stems = {p[:-len("_preview.json")] for p in glob.glob(os.path.join(IMPORTS, "*_preview.json"))}
    stems |= {p[:-len("_project.json")] for p in glob.glob(os.path.join(IMPORTS, "*_project.json"))}
    stems |= {p[:-len("_info.json")] for p in glob.glob(os.path.join(IMPORTS, "*_info.json"))}
    for stem in sorted(stems, key=lambda st: max(os.path.getmtime(f) for f in glob.glob(st + "_*.json")), reverse=True):
        preview, project = stem + "_preview.json", stem + "_project.json"
        info = {}
        if os.path.isfile(stem + "_info.json"):
            with open(stem + "_info.json", encoding="utf-8") as f:
                info = json.load(f)
        out.append({"name": info.get("name") or os.path.basename(stem).replace("-", " ").title(),
                    "shapes": len(info.get("shapes", [])), "drone_counts": info.get("drone_counts", []),
                    "preview": os.path.basename(preview) if os.path.isfile(preview) else None,
                    "has_blend": os.path.isfile(stem + ".blend"),
                    "project": os.path.basename(project) if os.path.isfile(project) else None})
    return out


def start_job(target, *args):
    # Short ids keep Windows paths under the 260 character limit Blender has
    job = {"id": time.strftime("%m%d-%H%M%S"), "status": "running",
           "progress": 0, "message": "Starting Blender", "report": None}
    JOBS[job["id"]] = job
    threading.Thread(target=target, args=(job,) + args, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "FlybitComposer/0.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download=False):
        if not os.path.isfile(path):
            return self.send_json({"error": "Not found"}, 404)
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(path))
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 16):
                self.wfile.write(chunk)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        try:
            if path == "/api/library":
                counts = drone_counts()
                count = int(query.get("count", [counts[0] if counts else 100])[0])
                return self.send_json({
                    "drone_counts": counts, "count": count, "shapes": list_shapes(count),
                    "effects": FX.registry_as_json(), "palettes": PALETTES, "profile": CFG["profile"],
                })
            if path.startswith("/api/jobs/"):
                job = JOBS.get(path.split("/")[3])
                if not job:
                    return self.send_json({"error": "Unknown job"}, 404)
                return self.send_json({k: v for k, v in job.items() if k != "pid"})
            if path == "/api/imports":
                return self.send_json({"imports": list_imports()})
            if path.startswith("/api/imports/"):
                return self.send_file(os.path.join(IMPORTS, os.path.basename(path)))
            if path.startswith("/api/files/"):
                _a, _b, _c, job_id, name = path.split("/", 4)
                folder = os.path.join(EXPORTS, os.path.basename(job_id))
                return self.send_file(os.path.join(folder, os.path.basename(name)),
                                      download=not name.endswith("_preview.json"))
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB, rel))
            if not full.startswith(WEB):
                return self.send_json({"error": "Not found"}, 404)
            return self.send_file(full)
        except Exception as ex:  # noqa: BLE001
            return self.send_json({"error": str(ex)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/upload-blend":
                # the file chosen in the browser, sent as the raw request body
                name = os.path.basename(unquote(self.headers.get("X-Filename") or "show.blend"))
                if not name.lower().endswith(".blend"):
                    raise ValueError("Please choose a .blend file.")
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 2_000_000_000:
                    raise ValueError("The file is empty or too large.")
                label = os.path.splitext(name)[0]
                os.makedirs(IMPORTS, exist_ok=True)
                dest = os.path.join(IMPORTS, slugify(label) + ".blend")
                with open(dest, "wb") as f:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                return self.send_json({"job": start_job(import_job, dest, label)["id"]})
            data = self.body()
            if path == "/api/preview":
                count = int(data["drone_count"])
                fps = int(data.get("fps", 12))
                seconds = min(float(data.get("seconds", 6)), 20)
                out = {"fps": fps, "layers": [], "reveal": None}
                end_points = None
                if data.get("path"):
                    # travelling preview: "grid" or a shape id at each end
                    ends = [grid_points(count, CFG["profile"]["grid_spacing"]) if e == "grid"
                            else find_shape(e, count)["points"] for e in data["path"]]
                    points, end_points = ends
                    out["start"] = points
                    if not data.get("still"):
                        out["end"] = end_points
                else:
                    points = find_shape(data["shape_id"], count)["points"]
                for layer in data.get("layers", []):
                    if data.get("still"):
                        # lights only, on the takeoff grid; no flight is simulated
                        out["layers"].append(evaluate_effect(points, layer, seconds, fps, geo_points=points + end_points))
                    else:
                        out["layers"].append(evaluate_effect(points, layer, seconds, fps, end_points=end_points))
                reveal = data.get("reveal")
                if reveal and reveal.get("effect"):
                    out["reveal"] = evaluate_effect(points, reveal, seconds, fps, float(reveal.get("duration", 2)))
                return self.send_json(out)
            if path == "/api/track-preview":
                return self.send_json(track_preview(data["show"], data.get("only")))
            if path == "/api/export":
                build_spec(data, EXPORTS)  # validate before starting Blender
                return self.send_json({"job": start_job(export_job, data)["id"]})
            if path == "/api/import-blend":
                blend = data.get("path", "").strip().strip('"')
                if not (blend.lower().endswith(".blend") and os.path.isfile(blend)):
                    raise ValueError("That .blend file was not found on this computer.")
                label = data.get("label") or os.path.splitext(os.path.basename(blend))[0]
                return self.send_json({"job": start_job(import_job, blend, label)["id"]})
            if path == "/api/open-folder":
                job = JOBS.get(data.get("job"))
                if job and job.get("folder") and sys.platform == "win32":
                    os.startfile(job["folder"])  # noqa: S606 - local convenience
                return self.send_json({"ok": True})
            return self.send_json({"error": "Not found"}, 404)
        except Exception as ex:  # noqa: BLE001
            return self.send_json({"error": str(ex)}, 400)


def main():
    os.makedirs(EXPORTS, exist_ok=True)
    os.makedirs(IMPORTS, exist_ok=True)
    longest = len(os.path.join(EXPORTS, "0000-000000", "x" * 40 + "_composer.json"))
    if sys.platform == "win32" and longest > 255:
        print("WARNING: this folder's path is very long (%d characters). Windows limits paths to 260; "
              "move Flybit Composer to a short path such as C:\\FlybitComposer or use short show names." % longest, flush=True)
    if len(sys.argv) > 2 and sys.argv[1] == "--build":
        # Command line build for testing: python server.py --build show.json
        with open(sys.argv[2], encoding="utf-8") as f:
            show = json.load(f)
        job = {"id": "cli-" + time.strftime("%H%M%S"), "status": "running", "progress": 0, "message": "", "report": None}
        last = [None]

        def watch():
            while job["status"] == "running":
                if job["message"] != last[0]:
                    last[0] = job["message"]
                    print("%3d%% %s" % (job["progress"], job["message"]), flush=True)
                time.sleep(0.5)

        threading.Thread(target=watch, daemon=True).start()
        export_job(job, show)
        print(json.dumps({k: job[k] for k in ("status", "message", "report", "id")}, indent=1)[:6000])
        return

    desired_port = int(CFG["port"])
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            try:
                desired_port = int(sys.argv[i + 1])
            except ValueError:
                pass

    ports_to_try = [desired_port] + [p for p in [8766, 8767, 8080, 8000] if p != desired_port]
    server = None
    actual_port = None
    for p in ports_to_try:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            actual_port = p
            break
        except OSError as ex:
            if p == desired_port and len(ports_to_try) > 1:
                print("Notice: Port %s could not be bound (%s). Trying fallback port..." % (p, ex), flush=True)
            elif p == ports_to_try[-1]:
                raise

    print("Flybit Composer running at http://localhost:%s  (Blender: %s)" % (actual_port, CFG["blender"]), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
