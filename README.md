# Flybit Composer (first draft)

Web composer that builds complete Skybrush Studio drone shows by driving
Blender headless.

## Run

Double-click `Start Flybit Composer.bat` (or `python server.py`) and open
http://localhost:8765. Needs Python 3 and Blender with the Skybrush Studio
add-on enabled. Blender is found automatically; to override, create
`config.json` with `{"blender": "C:\path\to\blender.exe"}`.

Keep this folder on a short path (e.g. `C:\FlybitComposer`): Windows limits
paths to 260 characters.

## Layout

- `server.py` - local web server, shape library, effect preview, export jobs
- `web/` - the composer UI
- `fx/effects_library.py` - all light effects (add new ones here + REGISTRY)
- `blender/build_show.py` - builds the show inside Blender from a spec
- `blender/extract_shapes.py` - imports formations of a .blend into the library
- `library/shapes/<drone count>/*.json` - shape library
- `exports/<job>/` - .blend, .skyc, fx .py, flight report, preview data


## The one strict rule: Skybrush plans every movement

The composer never calculates or simulates drone movement. Takeoff, the flight into
the first shape, every transition between formations, the return to home and the
landing are all planned by Skybrush Studio inside Blender, using the same operators an
artist clicks by hand. The composer only supplies the formation points, the hold times
and the lights (colours and effects). The stage shows lights on still drones; the only
moving picture is the flight preview, which is sampled from the finished Blender file.
# Show-formation
