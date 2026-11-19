"""Extract every SHOW formation of a Skybrush .blend into the shape library.

Run by the composer server:
    blender -b <show.blend> --python extract_shapes.py -- <library_dir> [source label]

The .blend is only read, never saved. One JSON file per formation is written to
<library_dir>/<drone count>/<slug>.json, plus the show's house profile
(fps, safety limits, takeoff grid) to <library_dir>/_profile_<slug>.json.
"""

import json
import os
import re
import sys

import bpy

from sbstudio.plugin.model.formation import (
    get_world_coordinates_of_markers_from_formation,
)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "shape"


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    library_dir = args[0]
    source = args[1] if len(args) > 1 else os.path.basename(bpy.data.filepath)

    scene = bpy.context.scene
    sb = scene.skybrush
    fps = scene.render.fps
    written = []
    takeoff = {}

    for entry in sb.storyboard.entries:
        formation = entry.formation
        if formation is None:
            continue
        pts = get_world_coordinates_of_markers_from_formation(
            formation, frame=entry.frame_start
        )
        pts = [[round(float(c), 3) for c in p] for p in pts]
        if not pts:
            continue
        if entry.purpose != "SHOW":
            takeoff[entry.name] = {
                "purpose": entry.purpose,
                "points": pts,
                "frame_start": entry.frame_start,
            }
            continue
        if entry.name in ("Takeoff grid", "Takeoff", "Return to home", "Landing"):
            takeoff[entry.name] = {"points": pts, "frame_start": entry.frame_start}
            continue

        count = len(pts)
        out_dir = os.path.join(library_dir, str(count))
        os.makedirs(out_dir, exist_ok=True)
        slug = slugify(entry.name)
        shape = {
            "id": "%d/%s" % (count, slug),
            "name": entry.name.replace("_", " "),
            "drone_count": count,
            "source": source,
            "hold_seconds": round(entry.duration / fps, 1),
            "points": pts,
        }
        with open(os.path.join(out_dir, slug + ".json"), "w", encoding="utf-8") as f:
            json.dump(shape, f)
        written.append(shape["id"])

    sc = sb.safety_check
    grid = takeoff.get("Takeoff grid", {}).get("points", [])
    hover = takeoff.get("Takeoff", {}).get("points", [])
    xs = sorted({p[0] for p in grid})
    ys = sorted({p[1] for p in grid})
    profile = {
        "source": source,
        "fps": fps,
        "drone_count": len(grid),
        "grid_rows": len(ys),
        "grid_columns": len(xs),
        "grid_spacing": round(xs[1] - xs[0], 3) if len(xs) > 1 else 3.0,
        "takeoff_altitude": round(min((p[2] for p in hover), default=6.0), 2),
        "takeoff_layers": sorted({round(p[2], 2) for p in hover}),
        "max_velocity_xy": sc.velocity_xy_warning_threshold,
        "max_velocity_z": sc.velocity_z_warning_threshold,
        "max_acceleration": sb.settings.max_acceleration,
        "min_distance": sc.proximity_warning_threshold,
        "max_altitude": sc.altitude_warning_threshold,
        "min_nav_altitude": sc.min_navigation_altitude,
        "drone_radius": sb.settings.drone_radius,
        "light_effects": len(sb.light_effects.entries),
        "shapes": written,
    }
    with open(
        os.path.join(library_dir, "_profile_%s.json" % slugify(source)),
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(profile, f, indent=1)
    print("EXTRACT_RESULT " + json.dumps({"shapes": written, "profile": profile}))


main()
