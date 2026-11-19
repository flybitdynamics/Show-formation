"""Read a Skybrush Studio show straight from its .blend, without Blender.

    show = SkybrushShow("show.blend")
    show.shapes()              # storyboard formations -> library shapes
    show.profile()             # fps, grid, safety limits
    show.preview(fps=4)        # sampled flight + lights for the composer stage

Movement is exact: drones in Skybrush are moved by a stack of Copy Location
constraints (one per storyboard entry) whose influence is keyframed, and the
formations themselves can be keyframed. Both are evaluated here the same way
Blender does.

Lights follow the show's Skybrush light effects: colour ramps, fades,
influence, "inside mesh" masks and the built-in outputs (first / last colour,
gradients, indexed, temporal) are exact. Custom-expression outputs need their
.py file; when that file is not available a look-alike stand-in is used and
reported in ``light_notes``.
"""

import bisect
import importlib.util
import inspect
import json
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blendfile import BlendFile  # noqa: E402


# --------------------------------------------------------------------------
# small math helpers (4x4 row-major matrices)
# --------------------------------------------------------------------------

def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def mat_vec(m, v):
    x, y, z = v
    return [m[i][0] * x + m[i][1] * y + m[i][2] * z + m[i][3] for i in range(3)]


def mat_from_blender(m):
    """Blender float[4][4] is column-major (m[col][row])."""
    return [[m[c][r] for c in range(4)] for r in range(4)]


def mat_inv(m):
    """Inverse of an affine 4x4."""
    a = [row[:3] for row in m[:3]]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
           - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    if abs(det) < 1e-12:
        det = 1e-12
    inv = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            r = [x for x in range(3) if x != j]
            c = [x for x in range(3) if x != i]
            minor = a[r[0]][c[0]] * a[r[1]][c[1]] - a[r[0]][c[1]] * a[r[1]][c[0]]
            inv[i][j] = ((-1) ** (i + j)) * minor / det
    t = [m[0][3], m[1][3], m[2][3]]
    out = [inv[i] + [-sum(inv[i][k] * t[k] for k in range(3))] for i in range(3)]
    return out + [[0.0, 0.0, 0.0, 1.0]]


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _m3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


# Blender rotmode -> order the axes are applied in
_EULER = {1: "XYZ", 2: "XZY", 3: "YXZ", 4: "YZX", 5: "ZXY", 6: "ZYX"}


def rot_matrix(rotmode, eul, quat, axis, angle):
    if rotmode in _EULER:
        r = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        for ax in _EULER[rotmode]:  # first axis applied first
            m = {"X": _rx, "Y": _ry, "Z": _rz}[ax]("XYZ".index(ax) is not None and eul["XYZ".index(ax)])
            r = _m3(m, r)
        return r
    if rotmode == -1:  # axis angle
        x, y, z = axis
        n = math.sqrt(x * x + y * y + z * z) or 1.0
        h = angle / 2
        quat = [math.cos(h)] + [c / n * math.sin(h) for c in (x, y, z)]
    w, x, y, z = quat
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


# --------------------------------------------------------------------------
# F-Curves
# --------------------------------------------------------------------------

class FCurve:
    def __init__(self, keys, extend):
        # keys: [(x, y, ipo, (hlx, hly), (hrx, hry))]
        self.keys = sorted(keys, key=lambda k: k[0])
        self.xs = [k[0] for k in self.keys]
        self.extend = extend
        self.const = all(abs(k[1] - self.keys[0][1]) < 1e-9 for k in self.keys) if self.keys else True

    def __call__(self, frame):
        k = self.keys
        if not k:
            return 0.0
        if self.const or frame <= k[0][0]:
            return k[0][1]
        if frame >= k[-1][0]:
            return k[-1][1]
        i = bisect.bisect_right(self.xs, frame) - 1
        a, b = k[i], k[i + 1]
        ipo = a[2]
        if ipo == 0:  # constant
            return a[1]
        if ipo == 1 or abs(a[1] - b[1]) < 1e-12 and abs(a[4][1] - a[1]) < 1e-12 and abs(b[3][1] - b[1]) < 1e-12:
            t = (frame - a[0]) / (b[0] - a[0])
            return a[1] + (b[1] - a[1]) * t
        # cubic bezier: P0=a, P1=a right handle, P2=b left handle, P3=b
        x0, y0 = a[0], a[1]
        x1, y1 = a[4]
        x2, y2 = b[3]
        x3, y3 = b[0], b[1]
        # keep handles inside the segment like Blender does
        span = x3 - x0
        h1, h2 = x1 - x0, x3 - x2
        if h1 + h2 > span and h1 + h2 > 0:
            f = span / (h1 + h2)
            x1 = x0 + h1 * f
            y1 = y0 + (y1 - y0) * f
            x2 = x3 - h2 * f
            y2 = y3 + (y2 - y3) * f
        lo, hi = 0.0, 1.0
        t = (frame - x0) / span
        for _ in range(30):
            mt = 1 - t
            x = mt * mt * mt * x0 + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t * t * t * x3
            if abs(x - frame) < 1e-4:
                break
            if x < frame:
                lo = t
            else:
                hi = t
            t = (lo + hi) / 2
        mt = 1 - t
        return mt * mt * mt * y0 + 3 * mt * mt * t * y1 + 3 * mt * t * t * y2 + t * t * t * y3


# --------------------------------------------------------------------------
# the show
# --------------------------------------------------------------------------

OUTPUT_NAMES = {1: "FIRST_COLOR", 2: "LAST_COLOR", 3: "INDEXED_BY_DRONES", 4: "GRADIENT_XYZ",
                5: "GRADIENT_XZY", 6: "GRADIENT_YXZ", 7: "GRADIENT_YZX", 8: "GRADIENT_ZXY",
                9: "GRADIENT_ZYX", 10: "DISTANCE", 11: "TEMPORAL", 12: "CUSTOM", 13: "INDEXED_BY_FORMATION"}
GRADIENT_AXES = {4: (0, 1, 2), 5: (0, 2, 1), 6: (1, 0, 2), 7: (1, 2, 0), 8: (2, 0, 1), 9: (2, 1, 0)}


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "shape"


class SkybrushShow:
    def __init__(self, path, fx_dirs=()):
        self.path = path
        self.bf = bf = BlendFile(path)
        self.fx_dirs = list(fx_dirs)
        scenes = list(bf.ids("SC"))
        if not scenes:
            raise ValueError("No scene in this .blend")
        self.scene = sc = scenes[0]
        props = bf.idprops(sc)
        if "skybrush" not in props:
            raise ValueError("This .blend has no Skybrush Studio data.")
        self.sb = props["skybrush"]
        r = sc.sub("r")
        self.fps = r["frs_sec"] / (r["frs_sec_base"] or 1.0)
        self.frame_start, self.frame_end = r["sfra"], r["efra"]
        self.objects = {o.name: o for o in bf.ids("OB")}
        self.collections = {c.name: c for c in bf.ids("GR")}
        self._fc_cache = {}
        self._mesh_cache = {}
        self._world_cache = {}
        self.light_notes = []

    # ---------------------------------------------------------- low level
    def _cstr(self, addr, ctx=None):
        b = self.bf.block_at(addr, ctx)
        if not b:
            return ""
        o = b["off"] + addr - b["old"]
        return self.bf.buf[o:b["off"] + b["len"]].split(b"\0")[0].decode("utf-8", "replace")

    def _ptr_array(self, addr, n, ctx=None):
        if not addr or n <= 0:
            return []
        b = self.bf.block_at(addr, ctx)
        o = b["off"] + addr - b["old"]
        return list(struct.unpack_from("<%dQ" % n, self.bf.buf, o))

    def collection_objects(self, name):
        c = self.collections.get(name)
        if not c:
            return []
        return [co.deref("ob", "Object") for co in self.bf.listbase(c, "gobject", "CollectionObject")]

    def fcurves(self, ob):
        """{(rna_path, index): FCurve} of an ID's action."""
        key = ob.addr
        if key in self._fc_cache:
            return self._fc_cache[key]
        out = {}
        if ob.ptr("adt"):
            adt = ob.deref("adt", "AnimData")
            act = adt.deref("action", "bAction") if adt and adt.ptr("action") else None
            if act is not None:
                curves = []
                if act.has("strip_keyframe_data_array"):
                    slot = adt.get("slot_handle")
                    ctx = act.block
                    for kd in self._ptr_array(act.ptr("strip_keyframe_data_array"), act["strip_keyframe_data_array_num"], ctx):
                        kd = self.bf.ref_at(kd, "ActionStripKeyframeData", ctx=ctx)
                        for cb in self._ptr_array(kd.ptr("channelbag_array"), kd["channelbag_array_num"], ctx):
                            cb = self.bf.ref_at(cb, "ActionChannelBag", ctx=ctx)
                            if slot and cb["slot_handle"] != slot:
                                continue
                            curves += [self.bf.ref_at(f, "FCurve", ctx=ctx) for f in self._ptr_array(cb.ptr("fcurve_array"), cb["fcurve_array_num"], ctx)]
                curves += self.bf.listbase(act, "curves", "FCurve")
                for fc in curves:
                    path = self._cstr(fc.ptr("rna_path"), fc.block)
                    keys = []
                    for b in self.bf.array_at(fc.ptr("bezt"), "BezTriple", fc["totvert"], fc.block):
                        v = b["vec"]
                        keys.append((v[1][0], v[1][1], b["ipo"], (v[0][0], v[0][1]), (v[2][0], v[2][1])))
                    out[(path, fc["array_index"])] = FCurve(keys, fc["extend"])
        self._fc_cache[key] = out
        return out

    def _channel(self, ob, fcs, path, index, static, frame):
        fc = fcs.get((path, index))
        return fc(frame) if fc else static

    def local_matrix(self, ob, frame):
        fcs = self.fcurves(ob)
        loc = [self._channel(ob, fcs, "location", i, ob["loc"][i], frame) + ob["dloc"][i] for i in range(3)]
        size = [self._channel(ob, fcs, "scale", i, ob["size"][i], frame) * (ob["dscale"][i] or 1.0) for i in range(3)]
        eul = [self._channel(ob, fcs, "rotation_euler", i, ob["rot"][i], frame) + ob["drot"][i] for i in range(3)]
        quat = [self._channel(ob, fcs, "rotation_quaternion", i, ob["quat"][i], frame) for i in range(4)]
        axis = [self._channel(ob, fcs, "rotation_axis_angle", i + 1, ob["rotAxis"][i], frame) for i in range(3)]
        angle = self._channel(ob, fcs, "rotation_axis_angle", 0, ob["rotAngle"], frame)
        r = rot_matrix(ob["rotmode"], eul, quat, axis, angle)
        return [[r[i][0] * size[0], r[i][1] * size[1], r[i][2] * size[2], loc[i]] for i in range(3)] + [[0, 0, 0, 1]]

    def world_matrix(self, ob, frame):
        key = (ob.addr, frame)
        m = self._world_cache.get(key)
        if m is None:
            m = self.local_matrix(ob, frame)
            if ob.ptr("parent"):
                parent = ob.deref("parent", "Object")
                m = mat_mul(mat_mul(self.world_matrix(parent, frame), mat_from_blender(ob["parentinv"])), m)
            if len(self._world_cache) > 200000:
                self._world_cache.clear()
            self._world_cache[key] = m
        return m

    def mesh(self, ob):
        """(vertices, {vertex group name: [(index, weight)]}) of a mesh object."""
        key = ob.addr
        if key in self._mesh_cache:
            return self._mesh_cache[key]
        me = ob.deref("data", "Mesh")
        ctx = me.block
        n = me["totvert"]
        verts = []
        if me.has("attribute_storage"):
            st = me.sub("attribute_storage")
            for a in self.bf.array_at(st.ptr("dna_attributes"), "Attribute", st["dna_attributes_num"], ctx):
                if self._cstr(a.ptr("name"), ctx) == "position":
                    arr = self.bf.ref_at(a.ptr("data"), "AttributeArray", ctx=ctx)
                    b = self.bf.block_at(arr.ptr("data"), ctx)
                    o = b["off"] + arr.ptr("data") - b["old"]
                    flat = struct.unpack_from("<%df" % (3 * n), self.bf.buf, o)
                    verts = [list(flat[i * 3:i * 3 + 3]) for i in range(n)]
        vd = me.sub("vdata")
        layers = self.bf.array_at(vd.ptr("layers"), "CustomDataLayer", vd["totlayer"], ctx)
        if not verts:
            for l in layers:
                if l["name"] == "position" or l["type"] == 0:
                    b = self.bf.block_at(l.ptr("data"), ctx)
                    o = b["off"] + l.ptr("data") - b["old"]
                    flat = struct.unpack_from("<%df" % (3 * n), self.bf.buf, o)
                    verts = [list(flat[i * 3:i * 3 + 3]) for i in range(n)]
                    break
        names = [g["name"] for g in self.bf.listbase(me, "vertex_group_names", "bDeformGroup")]
        groups = {}
        dv_addr = me.ptr("dvert")
        if not dv_addr:
            for l in layers:
                if l["type"] == 2:  # CD_MDEFORMVERT
                    dv_addr = l.ptr("data")
        if dv_addr and names:
            for vi, dv in enumerate(self.bf.array_at(dv_addr, "MDeformVert", n, ctx)):
                for w in self.bf.array_at(dv.ptr("dw"), "MDeformWeight", dv["totweight"], ctx):
                    if 0 <= w["def_nr"] < len(names):
                        groups.setdefault(names[w["def_nr"]], []).append((vi, w["weight"]))
        lo = [min(v[i] for v in verts) for i in range(3)] if verts else [0, 0, 0]
        hi = [max(v[i] for v in verts) for i in range(3)] if verts else [0, 0, 0]
        self._mesh_cache[key] = (verts, groups, lo, hi)
        return self._mesh_cache[key]

    def _hooks(self, ob):
        key = ("hooks", ob.addr)
        if key not in self._mesh_cache:
            hooks = []
            for m in self.bf.listbase(ob, "modifiers", "ModifierData"):
                if m["type"] != 9 or not (m["mode"] & 1):  # Hook, enabled in the viewport
                    continue
                h = self.bf.ref_at(m.addr, "HookModifierData", ctx=ob.block)
                hob = h.deref("object", "Object")
                if hob is None:
                    continue
                idx = []
                if h.ptr("indexar") and h["totindex"]:
                    b = self.bf.block_at(h.ptr("indexar"), ob.block)
                    o = b["off"] + h.ptr("indexar") - b["old"]
                    idx = [(i, 1.0) for i in struct.unpack_from("<%di" % h["totindex"], self.bf.buf, o)]
                if h["name"]:
                    idx = self.mesh(ob)[1].get(h["name"], idx)
                hooks.append({"ob": hob, "parentinv": mat_from_blender(h["parentinv"]), "force": h["force"], "verts": idx})
            self._mesh_cache[key] = hooks
        return self._mesh_cache[key]

    def world_verts(self, ob, frame):
        """World positions of a mesh object's vertices, hooks applied."""
        key = ("wv", ob.addr, frame)
        out = self._world_cache.get(key)
        if out is not None:
            return out
        verts = [list(v) for v in self.mesh(ob)[0]]
        m = self.world_matrix(ob, frame)
        hooks = self._hooks(ob)
        if hooks:
            inv = mat_inv(m)
            for h in hooks:
                hm = mat_mul(inv, mat_mul(self.world_matrix(h["ob"], frame), h["parentinv"]))
                for i, w in h["verts"]:
                    if 0 <= i < len(verts):
                        fac = h["force"] * w
                        co = verts[i]
                        nco = mat_vec(hm, co)
                        verts[i] = [co[k] + (nco[k] - co[k]) * fac for k in range(3)]
        out = [mat_vec(m, v) for v in verts]
        self._world_cache[key] = out
        return out

    def target_point(self, ob, subtarget, frame):
        if ob["type"] == 1 and subtarget:
            verts = self.world_verts(ob, frame)
            g = self.mesh(ob)[1].get(subtarget)
            if not g:
                mm = re.search(r"Vertex (\d+)", subtarget)
                g = [(int(mm.group(1)), 1.0)] if mm and int(mm.group(1)) < len(verts) else []
            if g:
                tw = sum(w for _i, w in g) or 1.0
                return [sum(verts[i][k] * w for i, w in g) / tw for k in range(3)]
        m = self.world_matrix(ob, frame)
        return [m[0][3], m[1][3], m[2][3]]

    # --------------------------------------------------------- formations
    def formation_points(self, name, frame):
        pts = []
        for ob in self.collection_objects(name):
            if ob is None:
                continue
            if ob["type"] == 1:
                pts += self.world_verts(ob, frame)
            elif ob["type"] == 0:
                m = self.world_matrix(ob, frame)
                pts.append([m[0][3], m[1][3], m[2][3]])
        return pts

    def storyboard(self):
        return list(self.sb.get("storyboard", {}).get("entries", []))

    def shapes(self, source):
        out = []
        seen = set()
        for e in self.storyboard():
            name = e.get("formation") or e.get("name")
            if e.get("purpose", 2) != 2 or name in ("Takeoff grid", "Takeoff", "Return to home", "Landing"):
                continue
            pts = self.formation_points(name, e.get("frame_start", 1))
            if not pts:
                continue
            slug = _slug(e.get("name") or name)
            if slug in seen:
                continue
            seen.add(slug)
            out.append({
                "id": "%d/%s" % (len(pts), slug),
                "name": (e.get("name") or name).replace("_", " "),
                "drone_count": len(pts),
                "source": source,
                "hold_seconds": round(e.get("duration", 0) / self.fps, 1),
                "points": [[round(c, 3) for c in p] for p in pts],
            })
        if not out:
            out = self._loose_shapes(source)
        return out

    def _loose_shapes(self, source):
        """Shapes of a file without a storyboard: Skybrush formations first,
        otherwise every point-cloud mesh (vertices only, one per drone)."""
        found = []
        skip = {"Takeoff grid", "Takeoff", "Return to home", "Landing"}
        forms = self.collections.get("Formations")
        if forms is not None:
            for ch in self.bf.listbase(forms, "children", "CollectionChild"):
                col = ch.deref("collection", "Collection")
                if col is not None and col.name not in skip:
                    found.append((col.name, self.formation_points(col.name, self.frame_start)))
        if not found:
            helpers = set()
            for name in ("Drones",) + tuple(skip):
                helpers |= {o.addr for o in self.collection_objects(name) if o is not None}
            for ob in self.bf.ids("OB"):
                if ob["type"] != 1 or ob.addr in helpers:
                    continue
                me = ob.deref("data", "Mesh")
                if me is None or me["totpoly"] > 0 or me["totvert"] < 4:
                    continue  # solid meshes are helpers (masks), not formations
                found.append((ob.name, self.world_verts(ob, self.frame_start)))
        out, seen = [], set()
        for name, pts in found:
            label = re.sub(r"^C_", "", name).replace("_", " ").strip()
            slug = _slug(label)
            if not pts or slug in seen:
                continue
            seen.add(slug)
            out.append({"id": "%d/%s" % (len(pts), slug), "name": label, "drone_count": len(pts),
                        "source": source, "hold_seconds": 12.0,
                        "points": [[round(c, 3) for c in p] for p in pts]})
        return out

    def profile(self, source, shape_ids):
        entries = {e.get("name"): e for e in self.storyboard()}
        grid = self.formation_points("Takeoff grid", 1) if "Takeoff grid" in self.collections else []
        hover = self.formation_points("Takeoff", entries.get("Takeoff", {}).get("frame_start", 1))
        xs = sorted({round(p[0], 3) for p in grid})
        ys = sorted({round(p[1], 3) for p in grid})
        sc = self.sb.get("safety_check", {})
        st = self.sb.get("settings", {})
        return {
            "source": source, "fps": round(self.fps, 3), "drone_count": len(grid) or len(self.drones()),
            "grid_rows": len(ys), "grid_columns": len(xs),
            "grid_spacing": round(xs[1] - xs[0], 3) if len(xs) > 1 else 3.0,
            "takeoff_altitude": round(min((p[2] for p in hover), default=6.0), 2),
            "takeoff_layers": sorted({round(p[2], 2) for p in hover}),
            "max_velocity_xy": sc.get("velocity_xy_warning_threshold", 8.0),
            "max_velocity_z": sc.get("velocity_z_warning_threshold", 3.0),
            "max_acceleration": st.get("max_acceleration", 4.0),
            "min_distance": sc.get("proximity_warning_threshold", 3.0),
            "max_altitude": sc.get("altitude_warning_threshold", 150.0),
            "min_nav_altitude": sc.get("min_navigation_altitude", 2.5),
            "drone_radius": st.get("drone_radius", 0.5),
            "light_effects": len(self.sb.get("light_effects", {}).get("entries", [])),
            "shapes": shape_ids,
        }

    # ------------------------------------------------------------- drones
    def drones(self):
        drones = [o for o in self.collection_objects("Drones") if o is not None]
        # natural sort: Drone 2 before Drone 10
        return sorted(drones, key=lambda o: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", o.name)])

    def _drone_rig(self, ob):
        cons = []
        fcs = self.fcurves(ob)
        for c in self.bf.listbase(ob, "constraints", "bConstraint"):
            if c["type"] != 9 or c["flag"] & 2:  # Copy Location, not disabled
                continue
            data = c.deref("data", "bLocateLikeConstraint")
            if data is None or not data.ptr("tar"):
                continue
            name = c["name"]
            fc = fcs.get(('constraints["%s"].influence' % name, 0))
            cons.append({
                "target": data.deref("tar", "Object"), "sub": data["subtarget"],
                "axes": data["flag"] & 7, "influence": fc or (lambda f, v=c["enforce"]: v),
            })
        return cons

    def drone_position(self, ob, rig, frame):
        m = self.world_matrix(ob, frame)
        pos = [m[0][3], m[1][3], m[2][3]]
        # the last fully-on constraint wins; only evaluate from there on
        active = []
        for c in reversed(rig):
            w = c["influence"](frame)
            if w <= 1e-6:
                continue
            active.append((c, min(1.0, w)))
            if w >= 1 - 1e-6:
                break
        for c, w in reversed(active):
            t = self.target_point(c["target"], c["sub"], frame)
            for k in range(3):
                if c["axes"] & (1 << k):
                    pos[k] += (t[k] - pos[k]) * w
        return pos

    # ------------------------------------------------------------- lights
    def _ramp(self, effect):
        tex_name = effect.get("texture")
        tex = self.objects_by_code("TE").get(tex_name.split("TE", 1)[-1] if tex_name else "")
        if tex is None:
            for t in self.bf.ids("TE"):
                if t.name == (tex_name or "")[2:] or t.name == tex_name:
                    tex = t
                    break
        stops, ipo = [[0.0, (0, 0, 0, 1)], [1.0, (1, 1, 1, 1)]], 0
        if tex is not None and tex.ptr("coba"):
            cb = tex.deref("coba", "ColorBand")
            ipo = cb["ipotype"]
            stops = []
            for d in cb["data"][:cb["tot"]]:
                stops.append([d["pos"], (d["r"], d["g"], d["b"], d["a"])])
            stops.sort(key=lambda s: s[0])
        return stops, ipo

    def objects_by_code(self, code):
        if not hasattr(self, "_by_code"):
            self._by_code = {}
        if code not in self._by_code:
            self._by_code[code] = {o.name: o for o in self.bf.ids(code)}
        return self._by_code[code]

    @staticmethod
    def _eval_ramp(stops, ipo, v):
        if v <= stops[0][0]:
            return stops[0][1]
        if v >= stops[-1][0]:
            return stops[-1][1]
        for i in range(len(stops) - 1):
            a, b = stops[i], stops[i + 1]
            if a[0] <= v <= b[0]:
                if ipo == 4:  # constant
                    return a[1]
                t = (v - a[0]) / ((b[0] - a[0]) or 1e-9)
                if ipo == 1:  # ease
                    t = t * t * (3 - 2 * t)
                return tuple(a[1][k] + (b[1][k] - a[1][k]) * t for k in range(4))
        return stops[-1][1]

    def _load_custom(self, path, index):
        """The real custom function when its .py file is available."""
        base = os.path.basename(path.replace("\\", "/"))
        for d in self.fx_dirs + [os.path.dirname(self.path)]:
            cand = os.path.join(d, base)
            if os.path.isfile(cand):
                try:
                    spec = importlib.util.spec_from_file_location("sbfx_" + _slug(base).replace("-", "_"), cand)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    fns = [f for _n, f in inspect.getmembers(mod, inspect.isfunction)
                           if f.__module__ == mod.__name__ and not f.__name__.startswith("_")]
                    fns.sort(key=lambda f: f.__name__)
                    # Skybrush lists the module's functions alphabetically and
                    # stores the chosen one's 1-based position.
                    if 1 <= index <= len(fns):
                        return fns[index - 1]
                except Exception:  # noqa: BLE001
                    return None
        return None

    @staticmethod
    def _standin(base, index):
        """Look-alike for a custom expression whose .py file is missing."""
        b = base.lower()

        def h(i):
            x = math.sin(i * 12.9898 + 78.233) * 43758.5453
            return x - math.floor(x)

        if "reveal" in b and "corner" in b:
            def fn(frame, tf, i, fi, pos, n, geo):
                d = min(math.dist((geo["u"](pos), geo["v"](pos)), c) for c in ((0, 0), (0, 1), (1, 0), (1, 1))) / 1.42
                return max(0.0, min(1.0, (tf * 1.4 - d) / 0.35))
            return fn
        if "reveal" in b:
            def fn(frame, tf, i, fi, pos, n, geo):
                u, v = geo["u"](pos), geo["v"](pos)
                d = 1 - 2 * max(abs(u - 0.5), abs(v - 0.5))  # 0 at the edges, 1 in the middle
                return max(0.0, min(1.0, (tf * 1.4 - d) / 0.35))
            return fn
        if "gradient" in b:
            def fn(frame, tf, i, fi, pos, n, geo):
                u, v = geo["u"](pos), geo["v"](pos)
                return 1 - 2 * max(abs(u - 0.5), abs(v - 0.5))
            return fn
        if "transition" in b:
            # ramps here are colour near 0 and black above ~0.13: a sparkle
            def fn(frame, tf, i, fi, pos, n, geo):
                return (h(i) + frame / 24.0 * (0.6 + h(i + 7))) % 1.0
            return fn
        if "swipe" in b:
            def fn(frame, tf, i, fi, pos, n, geo):
                return (geo["u"](pos) + frame / 24.0 * 0.5) % 1.0
            return fn

        def fn(frame, tf, i, fi, pos, n, geo):
            return 0.5 + 0.5 * math.sin(2 * math.pi * (geo["u"](pos) * 0.8 + geo["v"](pos) * 0.4 - frame / 24.0 * 0.35))
        return fn

    def _effects(self):
        effects = []
        missing = {}
        for e in self.sb.get("light_effects", {}).get("entries", []):
            if not e.get("enabled", 1):
                continue
            out = e.get("output", 2)
            fx = {
                "name": e.get("name", ""), "start": e.get("frame_start", 1), "duration": e.get("duration", 1),
                "fade_in": e.get("fade_in_duration", 0), "fade_out": e.get("fade_out_duration", 0),
                "influence": e.get("influence", 1.0), "output": out,
                "ordered": e.get("output_mapping_mode", 1) == 1,
                "mesh": None, "invert": e.get("invert_target", 0),
            }
            fx["stops"], fx["ipo"] = self._ramp(e)
            if e.get("target", 1) == 2 and e.get("mesh"):
                fx["mesh"] = self.objects.get(e["mesh"])
            if out == 12:
                of = e.get("output_function") or {}
                path, idx = of.get("path", ""), of.get("name", 1)
                fn = self._load_custom(path, idx) if path else None
                if fn is not None:
                    fx["custom"], fx["custom_real"] = fn, True
                else:
                    base = os.path.basename(path.replace("\\", "/")) or "?"
                    fx["custom"], fx["custom_real"] = self._standin(base, idx), False
                    missing.setdefault(base, set()).add(idx)
            effects.append(fx)
        self.light_notes = ["%s (functions %s) not found; a look-alike was used." % (k, ", ".join(map(str, sorted(v))))
                            for k, v in sorted(missing.items())]
        return effects

    def _inside(self, mesh_ob, pos, frame):
        inv = mat_inv(self.world_matrix(mesh_ob, frame))
        p = mat_vec(inv, pos)
        _v, _g, lo, hi = self.mesh(mesh_ob)
        return all(lo[k] - 1e-4 <= p[k] <= hi[k] + 1e-4 for k in range(3))

    def colors_at(self, effects, positions, frame):
        n = len(positions)
        cols = [[0.0, 0.0, 0.0] for _ in range(n)]
        lo = [min(p[k] for p in positions) for k in range(3)]
        hi = [max(p[k] for p in positions) for k in range(3)]

        def norm(v, k):
            return 0.5 if hi[k] - lo[k] < 1e-6 else (v - lo[k]) / (hi[k] - lo[k])

        geo = {"u": lambda p: 1 - norm(p[1], 1), "v": lambda p: norm(p[2], 2)}
        for fx in effects:
            end = fx["start"] + fx["duration"]
            if frame < fx["start"] or frame >= end:
                continue
            alpha = fx["influence"]
            if fx["fade_in"] and frame < fx["start"] + fx["fade_in"]:
                alpha *= (frame - fx["start"]) / fx["fade_in"]
            if fx["fade_out"] and frame > end - fx["fade_out"]:
                alpha *= (end - frame) / fx["fade_out"]
            if alpha <= 0:
                continue
            out = fx["output"]
            tf = (frame - fx["start"]) / max(1, fx["duration"])
            if out in GRADIENT_AXES:
                axes = GRADIENT_AXES[out]
                if fx["ordered"]:
                    order = sorted(range(n), key=lambda i: tuple(positions[i][a] for a in axes))
                    vals = [0.0] * n
                    for rank, i in enumerate(order):
                        vals[i] = rank / max(1, n - 1)
                else:
                    vals = [norm(p[axes[0]], axes[0]) for p in positions]
            for i in range(n):
                if fx["mesh"] is not None:
                    inside = self._inside(fx["mesh"], positions[i], frame)
                    if inside == bool(fx["invert"]):
                        continue
                if out == 1:
                    v = 0.0
                elif out == 2:
                    v = 1.0
                elif out == 3:
                    v = i / max(1, n - 1)
                elif out in GRADIENT_AXES:
                    v = vals[i]
                elif out == 11:
                    v = tf
                elif out == 12:
                    try:
                        if fx["custom_real"]:
                            v = fx["custom"](frame, tf, i, i, tuple(positions[i]), n)
                        else:
                            v = fx["custom"](frame, tf, i, i, positions[i], n, geo)
                    except Exception:  # noqa: BLE001
                        v = 0.0
                else:
                    v = 1.0
                v = 0.0 if v is None else max(0.0, min(1.0, float(v)))
                c = self._eval_ramp(fx["stops"], fx["ipo"], v)
                a = alpha * max(0.0, min(1.0, c[3]))
                col = cols[i]
                for k in range(3):
                    col[k] += (c[k] - col[k]) * a
        return cols

    # ------------------------------------------------------------ project
    @staticmethod
    def _hex(rgb):
        def enc(c):
            c = max(0.0, min(1.0, c))
            return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return "#%02x%02x%02x" % tuple(int(round(enc(c) * 255)) for c in rgb[:3])

    def _ramp_colors(self, fx, n=5):
        """The effect's colour ramp as evenly spaced composer colours."""
        cols = []
        for k in range(n):
            c = self._hex(self._eval_ramp(fx["stops"], fx["ipo"], k / (n - 1)))
            if not cols or cols[-1] != c:
                cols.append(c)
        return cols

    def _custom_kind(self, e):
        path = ((e.get("output_function") or {}).get("path") or "").replace("\\", "/").lower()
        base = os.path.basename(path)
        if "reveal" in base:
            return "corner_reveal" if "corner" in base else "reveal"
        if "gradient" in base:
            return "gradient"
        if "transition" in base:
            return "transition"
        if "swipe" in base:
            return "swipe"
        return "fx"

    def _look(self, fx, e):
        """A composer look that matches a Skybrush light effect."""
        out = fx["output"]
        cols = self._ramp_colors(fx)
        look = {"enabled": True, "blend": "NORMAL",
                "fade_in": round(fx["fade_in"] / self.fps, 2), "fade_out": round(fx["fade_out"] / self.fps, 2),
                "source": fx["name"]}
        if out == 1:
            look.update(effect="solid", params={"level": 1.0}, colors=[cols[0]])
        elif out == 2 or out not in GRADIENT_AXES and out != 12:
            look.update(effect="solid", params={"level": 1.0}, colors=[cols[-1]])
        elif out in GRADIENT_AXES:
            axis = GRADIENT_AXES[out][0]
            look.update(effect="gradient", params={"direction": {0: "f2b", 1: "r2l", 2: "b2t"}[axis]}, colors=cols)
        else:
            kind = self._custom_kind(e)
            if kind == "gradient":
                look.update(effect="gradient_radial", params={}, colors=cols[::-1])
            elif kind == "transition":
                look.update(effect="sparkle", params={}, colors=["#000000", cols[0]])
            elif kind == "swipe":
                look.update(effect="gradient_flow", params={"direction": "l2r", "speed": 0.5}, colors=cols)
            elif kind in ("reveal", "corner_reveal"):
                look.update(effect="solid", params={"level": 1.0}, colors=[cols[-1]])
            else:
                look.update(effect="wave", params={"direction": "l2r"}, colors=cols)
        return look

    PALETTES = [["#ff6a00", "#ffffff"], ["#ff2d95", "#ffb400", "#18d6c0"], ["#0b2a8f", "#00b7c2", "#38e07b"],
                ["#4a0010", "#e0002a", "#ffb000"], ["#2a0a6b", "#8a2be2", "#ffd700"], ["#001a40", "#3fa9f5", "#ffffff"]]

    def _simple_project(self, label, shapes):
        """A starting timeline for a file that only holds shapes."""
        count = max((s["drone_count"] for s in shapes), key=[s["drone_count"] for s in shapes].count) if shapes else 0
        def look(effect, colors, params=None):
            return {"effect": effect, "params": params or {}, "colors": colors, "fade_in": 1.5, "fade_out": 1.5,
                    "enabled": True, "blend": "NORMAL"}
        scenes = []
        for k, sh in enumerate([s for s in shapes if s["drone_count"] == count]):
            scenes.append({"uid": "imp%03d" % k, "shape_id": sh["id"], "name": sh["name"], "hold": 12, "extra_transition": 0,
                           "layers": [look("gradient", self.PALETTES[k % len(self.PALETTES)], {"direction": "b2t"})],
                           "reveal": None, "transition": look("sparkle", ["#1a0a00", "#ffb400"])})
        return {"name": label, "drone_count": count, "crossfade": 1.5, "scenes": scenes,
                "takeoff_layers": [look("twinkle", ["#000000", "#fff1d6"])],
                "landing_layers": [look("breathe", ["#000000", "#fff1d6"])],
                "transition_look": look("sparkle", ["#1a0a00", "#ffb400"]),
                "imported_from": os.path.basename(self.path)}

    def project(self, label, shapes):
        """The show as a Flybit Composer project: one timeline block per
        formation, with looks, reveals and transition lights taken from the
        Skybrush light effects."""
        if not any(e.get("purpose", 2) == 2 for e in self.storyboard()):
            return self._simple_project(label, shapes)
        by_name = {s["name"]: s for s in shapes}
        entries = [e for e in self.storyboard() if e.get("purpose", 2) == 2
                   and (e.get("name") or "").replace("_", " ") in by_name]
        effects = self._effects()
        raw = [e for e in self.sb.get("light_effects", {}).get("entries", []) if e.get("enabled", 1)]
        scenes = []
        for e in entries:
            shape = by_name[(e.get("name") or "").replace("_", " ")]
            scenes.append({"entry": e, "shape": shape, "start": e["frame_start"],
                           "end": e["frame_start"] + e.get("duration", 0), "layers": [], "reveal": None, "transition": None})
        takeoff, landing = [], []

        def scene_for(frame):
            for sc in scenes:
                if sc["start"] - 2 * self.fps <= frame < sc["end"]:
                    return sc
            return None

        for fx, e in zip(effects, raw):
            if fx["duration"] >= (self.frame_end - self.frame_start) * 0.9:
                continue  # the all-black base
            mid = fx["start"] + fx["duration"] / 2
            kind = self._custom_kind(e) if fx["output"] == 12 else None
            if kind == "transition":
                look = self._look(fx, e)
                nxt = next((sc for sc in scenes if sc["start"] >= mid), None)
                if not scenes or mid < scenes[0]["start"] and nxt is scenes[0] and fx["start"] <= self.frame_start + self.fps:
                    takeoff.append(look)
                elif nxt is None:
                    landing.append(look)
                elif nxt["transition"] is None:
                    nxt["transition"] = look
                continue
            sc = scene_for(mid)
            if sc is None:
                continue
            is_reveal = kind in ("reveal", "corner_reveal") or "reveal" in fx["name"].lower()
            if is_reveal and fx["start"] < sc["start"] + self.fps:
                if sc["reveal"] is None:
                    eff = "reveal_corners_in" if kind == "corner_reveal" else "reveal_edges_in"
                    sc["reveal"] = {"effect": eff, "params": {"softness": 0.25}, "duration": 2.5, "enabled": True,
                                    "source": fx["name"]}
                # the colours come from the scene's own looks, unless this
                # reveal is the only effect on its part of the shape
                twin = any(o is not fx and o["mesh"] is fx["mesh"] and "reveal" not in o["name"].lower()
                           and sc["start"] - 2 * self.fps <= o["start"] + o["duration"] / 2 < sc["end"] for o in effects)
                if fx["mesh"] is None or twin:
                    continue
            look = self._look(fx, e)
            if fx["mesh"] is not None:
                frame = int((sc["start"] + sc["end"]) / 2)
                pts = self.formation_points(sc["entry"].get("formation") or sc["entry"]["name"], frame)
                mask = [i for i, p in enumerate(pts) if self._inside(fx["mesh"], p, frame) != bool(fx["invert"])]
                if not mask:
                    continue
                if len(mask) < len(pts):
                    look["mask"] = mask
                    look["mask_label"] = fx["mesh"].name
            sc["layers"].append(look)

        out_scenes = []
        for k, sc in enumerate(scenes):
            out_scenes.append({
                "uid": "imp%03d" % k, "shape_id": sc["shape"]["id"], "name": sc["shape"]["name"],
                "hold": round(sc["entry"].get("duration", 0) / self.fps, 1), "extra_transition": 0,
                "layers": sc["layers"] or [{"effect": "solid", "params": {"level": 1.0}, "colors": ["#ffffff"],
                                            "fade_in": 1.5, "fade_out": 1.5, "enabled": True, "blend": "NORMAL"}],
                "reveal": sc["reveal"],
                "transition": sc["transition"] or {"effect": "sparkle", "params": {}, "colors": ["#000000", "#ffb400"],
                                                   "fade_in": 1.5, "fade_out": 1.5, "enabled": False, "blend": "NORMAL"},
                "source_time": round((sc["start"] - self.frame_start) / self.fps, 2),
            })
        return {
            "name": label, "drone_count": scenes[0]["shape"]["drone_count"] if scenes else 0,
            "crossfade": 1.5, "scenes": out_scenes,
            "takeoff_layers": takeoff, "landing_layers": landing,
            "transition_look": out_scenes[1]["transition"] if len(out_scenes) > 1 else None,
            "imported_from": os.path.basename(self.path),
        }

    # ------------------------------------------------------------ preview
    def preview(self, fps=4, progress=None):
        drones = self.drones()
        if not drones:
            raise ValueError("No drones in the 'Drones' collection.")
        rigs = [self._drone_rig(d) for d in drones]
        effects = self._effects()
        step = self.fps / fps
        frames = []
        f = float(self.frame_start)
        while f <= self.frame_end:
            frames.append(round(f))
            f += step
        positions, colors = [], []
        for fi, frame in enumerate(frames):
            pts = [self.drone_position(d, rig, frame) for d, rig in zip(drones, rigs)]
            cols = self.colors_at(effects, pts, frame)
            for p in pts:
                positions += [int(round(c * 100)) for c in p]
            for c in cols:
                colors += [max(0, min(255, int(round(v * 255)))) for v in c]
            self._world_cache.clear()
            if progress and fi % 50 == 0:
                progress(fi / len(frames))
        timeline = []
        for e in self.storyboard():
            timeline.append({"name": e.get("name"), "frame_start": e.get("frame_start"),
                             "start": round((e.get("frame_start", 1) - self.frame_start) / self.fps, 2)})
        return {
            "fps": fps, "count": len(drones), "frames": len(frames),
            "positions": positions, "colors": colors, "timeline": timeline,
            "light_notes": self.light_notes,
        }


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    show = SkybrushShow(sys.argv[1])
    print("fps", show.fps, "frames", show.frame_start, show.frame_end)
    shapes = show.shapes("test")
    print("shapes", [(s["id"], len(s["points"])) for s in shapes])
    if len(sys.argv) > 2:
        pv = show.preview(fps=float(sys.argv[2]) if len(sys.argv) > 3 else 2)
        with open(sys.argv[-1] if len(sys.argv) > 3 else sys.argv[2], "w") as f:
            json.dump(pv, f, separators=(",", ":"))
        print("preview", pv["frames"], "frames", pv["light_notes"])
    print("%.1fs" % (time.time() - t0))
