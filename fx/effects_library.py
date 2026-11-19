"""Flybit Composer light effects library.

Every effect is a plain function

    effect(frame, time_fraction, drone_index, formation_index, position, drone_count, P)

returning a value in 0..1 that Skybrush passes through the colour ramp of the
light effect. ``P`` is a dict the composer fills in automatically:

    P["lo"], P["hi"]  bounding box of the formation (x, y, z)  -> no hand-tuned min/max
    P["c"]            centre of the formation
    P["r"]            largest distance of any drone from the centre
    P["f0"], P["fps"] first frame of the effect and the scene frame rate
    ...plus the effect's own parameters from REGISTRY.

Screen convention (matches the Flybit shows): the audience looks along +X, so
"left to right" is +Y -> -Y and "bottom to top" is -Z -> +Z.

At export time the composer copies this file next to the .blend and appends one
6-argument wrapper per used effect, because Skybrush only treats functions with
exactly six parameters as classic (V1) custom expressions.

To add an effect: write the function, then add an entry to REGISTRY.
"""

import math

TAU = 2.0 * math.pi


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clamp(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _secs(frame, P):
    return (frame - P["f0"]) / float(P["fps"])


def _norm(v, lo, hi):
    return 0.5 if hi - lo < 1e-6 else _clamp((v - lo) / (hi - lo))


def _u(P, pos):
    """0 = audience left, 1 = audience right."""
    return 1.0 - _norm(pos[1], P["lo"][1], P["hi"][1])


def _v(P, pos):
    """0 = bottom, 1 = top."""
    return _norm(pos[2], P["lo"][2], P["hi"][2])


def _d(P, pos):
    """0 = nearest the audience, 1 = farthest."""
    return _norm(pos[0], P["lo"][0], P["hi"][0])


def _along(P, pos, direction):
    """Normalised 0..1 coordinate that grows in the travel direction."""
    if direction == "l2r":
        return _u(P, pos)
    if direction == "r2l":
        return 1.0 - _u(P, pos)
    if direction == "b2t":
        return _v(P, pos)
    if direction == "t2b":
        return 1.0 - _v(P, pos)
    if direction == "f2b":
        return _d(P, pos)
    if direction == "b2f":
        return 1.0 - _d(P, pos)
    if direction == "diag_up":
        return (_u(P, pos) + _v(P, pos)) * 0.5
    if direction == "diag_down":
        return (_u(P, pos) + 1.0 - _v(P, pos)) * 0.5
    return _u(P, pos)


def _screen(P, pos):
    """Screen-plane offset from the centre in metres: (right, up)."""
    return P["c"][1] - pos[1], pos[2] - P["c"][2]


def _radius(P, pos):
    """0 at the centre, 1 at the outermost drone (full 3D distance)."""
    dx = pos[0] - P["c"][0]
    dy = pos[1] - P["c"][1]
    dz = pos[2] - P["c"][2]
    return _clamp(math.sqrt(dx * dx + dy * dy + dz * dz) / max(P["r"], 1e-6))


def _hash01(*values):
    """Deterministic pseudo-random number in [0, 1): same input, same output."""
    h = 2166136261
    for v in values:
        h ^= int(round(v * 1000)) & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489917) & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFF) / float(0x1000000)


def _index(drone_index, formation_index):
    """Formation index when Skybrush knows it, the drone index otherwise
    (during transitions Skybrush passes formation_index=None)."""
    return drone_index if formation_index is None else formation_index


def _smooth(edge0, edge1, x):
    if edge1 - edge0 < 1e-9:
        return 1.0 if x >= edge1 else 0.0
    t = _clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------
# static looks
# --------------------------------------------------------------------------

def solid(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return P["level"]


def gradient(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return _along(P, position, P["direction"])


def gradient_radial(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return _radius(P, position)


def gradient_flow(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """The colour ramp scrolls endlessly through the formation."""
    s = _along(P, position, P["direction"]) * P["repeats"] - _secs(frame, P) * P["speed"]
    s = s % 2.0
    return s if s <= 1.0 else 2.0 - s


# --------------------------------------------------------------------------
# whole-formation pulses
# --------------------------------------------------------------------------

def breathe(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    b = (math.sin(_secs(frame, P) * P["speed"] * TAU - math.pi / 2) + 1.0) / 2.0
    return P["floor"] + (1.0 - P["floor"]) * b


def heartbeat(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    phase = (_secs(frame, P) * P["speed"]) % 1.0
    a = math.exp(-((phase - 0.12) / 0.045) ** 2)
    b = math.exp(-((phase - 0.32) / 0.06) ** 2) * 0.75
    return _clamp(P["floor"] + (1.0 - P["floor"]) * max(a, b))


def strobe(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return 1.0 if (_secs(frame, P) * P["speed"]) % 1.0 < P["duty"] else 0.0


def odd_even(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    odd = _index(drone_index, formation_index) % 2
    return (math.sin(_secs(frame, P) * P["speed"] * TAU + odd * math.pi) + 1.0) / 2.0


# --------------------------------------------------------------------------
# travelling patterns
# --------------------------------------------------------------------------

def wave(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    s = _along(P, position, P["direction"])
    b = (math.sin((s * P["waves"] - _secs(frame, P) * P["speed"]) * TAU) + 1.0) / 2.0
    return b ** P["sharpness"]


def wipe(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Bright band with a fading tail that loops across the formation."""
    s = _along(P, position, P["direction"])
    span = 1.0 + P["trail"] + P["width"]
    head = (_secs(frame, P) * P["speed"]) % 1.0 * span
    behind = head - s
    if behind < 0.0:
        return 0.0
    if behind <= P["width"]:
        return 1.0
    return _clamp(1.0 - (behind - P["width"]) / max(P["trail"], 1e-6))


def ping_pong(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Band sweeps there and back with no dead time (ferrofluid / scanner)."""
    s = _along(P, position, P["direction"])
    phase = (_secs(frame, P) * P["speed"]) % 2.0
    head = phase if phase <= 1.0 else 2.0 - phase
    dist = abs(s - head)
    if dist >= P["width"]:
        return P["floor"]
    return max(P["floor"], (1.0 - dist / P["width"]) ** P["sharpness"])


def ripple(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    r = _radius(P, position)
    sign = -1.0 if P["inward"] else 1.0
    b = (math.sin((r * P["rings"] - sign * _secs(frame, P) * P["speed"]) * TAU) + 1.0) / 2.0
    return b ** P["sharpness"]


def spiral(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    right, up = _screen(P, position)
    angle = math.atan2(up, right)
    r = _radius(P, position)
    arm = 0.5 + 0.5 * math.cos(P["arms"] * angle - P["twist"] * r * TAU - _secs(frame, P) * P["speed"] * TAU)
    return arm ** P["sharpness"]


def lighthouse(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    right, up = _screen(P, position)
    angle = math.degrees(math.atan2(up, right)) % 360.0
    beam = (_secs(frame, P) * P["speed"] * 360.0) % 360.0
    spacing = 360.0 / P["beams"]
    diff = (angle - beam) % spacing
    diff = min(diff, spacing - diff)
    half = P["width_deg"] / 2.0
    return 1.0 - (diff / half) * 0.4 if diff <= half else 0.0


def comet(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Comet running through the drones in formation-index order."""
    i = _index(drone_index, formation_index)
    head = (_secs(frame, P) * P["speed"] * drone_count) % drone_count
    best = 0.0
    for k in range(int(P["comets"])):
        h = (head + k * drone_count / P["comets"]) % drone_count
        behind = (h - i) % drone_count
        tail = P["tail"] * drone_count
        if behind <= tail:
            best = max(best, math.exp(-4.0 * behind / max(tail, 1e-6)))
    return best


def chase(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Saw-tooth ramp chasing through the drones by index."""
    i = _index(drone_index, formation_index)
    length = max(2.0, P["length"] * drone_count)
    s = ((_secs(frame, P) * P["speed"] * length) + i) % length / length
    if P["shape"] == "triangle":
        return 1.0 - abs(2.0 * s - 1.0)
    if P["shape"] == "ramp_down":
        return 1.0 - s
    return s


# --------------------------------------------------------------------------
# organic / sparkle
# --------------------------------------------------------------------------

def twinkle(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    seed = _hash01(7, drone_index)
    if _hash01(9, drone_index) > P["density"]:
        return P["floor"]
    phase = _secs(frame, P) * P["speed"] * (0.6 + 0.8 * seed) * TAU + seed * TAU
    return max(P["floor"], ((math.sin(phase) + 1.0) / 2.0) ** P["sharpness"])


def sparkle(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Hard random glitter: each drone flashes at random moments."""
    tick = int(_secs(frame, P) * P["speed"] * 12.0)
    return 1.0 if _hash01(13, drone_index, tick) < P["density"] else P["floor"]


def firefly(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    s1 = _hash01(17, drone_index) * TAU
    s2 = _hash01(19, drone_index) * TAU
    t = _secs(frame, P) * P["speed"]
    f = math.sin(t * 4.8 + s1) * math.sin(t * 8.9 + s2)
    return max(P["floor"], (f + 1.0) / 2.0)


def plasma(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    t = _secs(frame, P) * P["speed"] * 1.2
    scale = max(P["r"] * P["scale"], 1e-3)
    right, up = _screen(P, position)
    v1 = math.sin(right / scale + t)
    v2 = math.sin(up / scale - t * 1.3)
    v3 = math.sin((right + up) / scale + t * 0.7)
    v4 = math.sin(math.sqrt(right * right + up * up) / scale - t * 1.6)
    return ((v1 + v2 + v3 + v4) / 4.0 + 1.0) / 2.0


def flames(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    t = _secs(frame, P) * P["speed"] * 1.9
    scale = max(P["r"] * P["scale"], 1e-3)
    x, y, z = position[1] - P["c"][1], position[0] - P["c"][0], position[2] - P["c"][2]
    zz = z / scale - t * 1.6
    n = math.sin(x / scale * 1.7 + math.sin(zz * 1.3 + y / scale) * 1.5 + t * 0.3)
    n += 0.5 * math.sin(y / scale * 2.3 + zz * 2.1 + math.sin(x / scale * 3.1 - t) * 1.2)
    n += 0.25 * math.sin((x + y) / scale * 4.1 + zz * 3.7 - t * 0.6)
    return _clamp(max(0.0, (n / 1.75) * 0.5 + 0.5) ** P["sharpness"])


def galaxy(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    right, up = _screen(P, position)
    radius = math.sqrt(right * right + up * up)
    angle = math.atan2(up, right)
    spin = _secs(frame, P) * P["speed"] * 1.5
    arm = 0.5 + 0.5 * math.cos(P["arms"] * (angle - P["twist"] * math.log(radius + 1.0)) - spin)
    core = math.exp(-(radius / max(P["r"] * 0.2, 1e-3)) ** 2)
    return _clamp(max(arm ** P["sharpness"] * 0.9, core))


def mandala(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    right, up = _screen(P, position)
    radius = math.sqrt(right * right + up * up) / max(P["r"], 1e-6)
    angle = math.atan2(up, right)
    t = _secs(frame, P) * P["speed"] * 1.2
    petal_a = 0.5 + 0.5 * math.cos(P["petals"] * angle + t)
    petal_b = 0.5 + 0.5 * math.cos(P["petals"] * angle - t * 1.3 + math.pi)
    ring = 0.5 + 0.5 * math.cos(TAU * radius * P["rings"] - t * 2.0)
    return _clamp(max(petal_a * ring, petal_b * (1.0 - ring)) ** P["sharpness"])


def fireworks(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Small bursts popping in different regions at random times."""
    size = max(P["r"] * P["cluster"], 1e-3)
    cx = math.floor(position[0] / size)
    cy = math.floor(position[1] / size)
    cz = math.floor(position[2] / size)
    period = max(P["period"], 0.2)
    timeline = _secs(frame, P) + _hash01(31, cx, cy, cz) * period
    epoch = int(timeline // period)
    age = (timeline - epoch * period) / (period * 0.6)
    if age >= 1.0 or _hash01(33, cx, cy, cz, epoch) < 0.25:
        return 0.0
    bx = (cx + 0.25 + 0.5 * _hash01(34, cx, cy, cz, epoch)) * size
    by = (cy + 0.25 + 0.5 * _hash01(35, cx, cy, cz, epoch)) * size
    bz = (cz + 0.25 + 0.5 * _hash01(37, cx, cy, cz, epoch)) * size
    dist = math.sqrt((position[0] - bx) ** 2 + (position[1] - by) ** 2 + (position[2] - bz) ** 2)
    max_r = size * 0.75
    shell = math.exp(-((dist - age * max_r) / (max_r * 0.3)) ** 2) * (1.0 - age) ** 1.5
    core = math.exp(-(dist / (max_r * 0.45)) ** 2) * max(0.0, 1.0 - age * 4.0)
    glint = 0.65 + 0.35 * math.sin(frame * 0.9 + _hash01(36, drone_index) * TAU)
    return _clamp(max(shell, core) * glint)


# --------------------------------------------------------------------------
# one-shot reveals: 0 = still hidden, 1 = revealed. They run once over the
# effect duration (time_fraction 0 -> 1) and are used as a mask on top of the
# looks, so any look can be revealed by any wipe.
# --------------------------------------------------------------------------

def _reveal(progress, s, softness):
    front = progress * (1.0 + softness)
    return _smooth(s, s + softness, front) if softness > 1e-6 else (1.0 if s <= front else 0.0)


def reveal_wipe(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return _reveal(time_fraction, _along(P, position, P["direction"]), P["softness"])


def reveal_center_out(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return _reveal(time_fraction, _radius(P, position), P["softness"])


def reveal_edges_in(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    return _reveal(time_fraction, 1.0 - _radius(P, position), P["softness"])


def reveal_corners_in(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    u, v = _u(P, position), _v(P, position)
    d = min(
        math.hypot(u - cu, v - cv) for cu, cv in ((0, 0), (0, 1), (1, 0), (1, 1))
    ) / math.hypot(0.5, 0.5)
    return _reveal(time_fraction, _clamp(d), P["softness"])


def reveal_split(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Curtains opening from the middle (or closing in from both sides)."""
    s = abs((_u(P, position) if P["axis"] == "horizontal" else _v(P, position)) - 0.5) * 2.0
    return _reveal(time_fraction, s if P["from_center"] else 1.0 - s, P["softness"])


def reveal_dissolve(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Drones switch on one by one in random order with a short flicker."""
    when = _hash01(41, drone_index) * 0.85
    if time_fraction < when:
        return 0.0
    since = (time_fraction - when) / 0.15
    if since < 1.0:
        return 0.5 + 0.5 * abs(math.sin(since * 14.0 + drone_index))
    return 1.0


def reveal_index(frame, time_fraction, drone_index, formation_index, position, drone_count, P):
    """Lights up drone by drone in formation order (drawing the shape)."""
    s = _index(drone_index, formation_index) / max(drone_count - 1, 1)
    return _reveal(time_fraction, s, P["softness"])


# --------------------------------------------------------------------------
# registry: what the composer UI shows
# --------------------------------------------------------------------------

DIRECTIONS = [
    ["l2r", "Left to right"], ["r2l", "Right to left"],
    ["b2t", "Bottom to top"], ["t2b", "Top to bottom"],
    ["diag_up", "Diagonal up"], ["diag_down", "Diagonal down"],
    ["f2b", "Front to back"], ["b2f", "Back to front"],
]


def _num(key, label, default, lo, hi, step=0.05):
    return {"key": key, "label": label, "type": "number", "default": default, "min": lo, "max": hi, "step": step}


def _choice(key, label, default, options):
    return {"key": key, "label": label, "type": "choice", "default": default, "options": options}


def _flag(key, label, default):
    return {"key": key, "label": label, "type": "bool", "default": default}


_DIR = _choice("direction", "Direction", "l2r", DIRECTIONS)
_SPEED = _num("speed", "Speed (cycles / sec)", 0.5, 0.02, 6.0)
_SHARP = _num("sharpness", "Sharpness", 1.0, 0.3, 6.0, 0.1)
_FLOOR = _num("floor", "Minimum brightness", 0.0, 0.0, 1.0)
_SOFT = _num("softness", "Edge softness", 0.12, 0.0, 0.6, 0.02)

REGISTRY = [
    # id, label, group, kind, description, params
    ("solid", "Solid colour", "Static", "look", "One flat colour (the last colour of the ramp).",
     [_num("level", "Ramp position", 1.0, 0.0, 1.0)]),
    ("gradient", "Gradient", "Static", "look", "Colour ramp spread across the formation.", [_DIR]),
    ("gradient_radial", "Radial gradient", "Static", "look", "Colour ramp from the centre outwards.", []),
    ("gradient_flow", "Flowing gradient", "Travelling", "look", "The colour ramp scrolls through the formation.",
     [_DIR, _num("speed", "Speed (cycles / sec)", 0.25, 0.02, 4.0), _num("repeats", "Repeats", 1.0, 0.25, 6.0, 0.25)]),
    ("breathe", "Breathe", "Pulse", "look", "Slow synchronized glow.",
     [_num("speed", "Speed (cycles / sec)", 0.3, 0.02, 4.0), _num("floor", "Minimum brightness", 0.2, 0.0, 1.0)]),
    ("heartbeat", "Heartbeat", "Pulse", "look", "Double-beat pulse.",
     [_num("speed", "Beats / sec", 0.8, 0.1, 4.0), _num("floor", "Minimum brightness", 0.08, 0.0, 1.0)]),
    ("strobe", "Strobe", "Pulse", "look", "Hard synchronized flashes.",
     [_num("speed", "Flashes / sec", 4.0, 0.5, 12.0, 0.5), _num("duty", "On fraction", 0.3, 0.05, 0.95)]),
    ("odd_even", "Odd / even pulse", "Pulse", "look", "Alternate drones pulse in opposite phase.", [_SPEED]),
    ("wave", "Wave", "Travelling", "look", "Smooth sine wave rolling through the formation.",
     [_DIR, _SPEED, _num("waves", "Waves across", 1.5, 0.25, 8.0, 0.25), _SHARP]),
    ("wipe", "Looping wipe", "Travelling", "look", "Bright band with a fading tail that loops.",
     [_DIR, _SPEED, _num("width", "Band width", 0.05, 0.01, 0.5, 0.01), _num("trail", "Trail length", 0.25, 0.0, 1.0)]),
    ("ping_pong", "Scanner", "Travelling", "look", "Band sweeps back and forth.",
     [_DIR, _SPEED, _num("width", "Band width", 0.2, 0.03, 0.8, 0.01), _SHARP, _num("floor", "Minimum brightness", 0.04, 0.0, 1.0)]),
    ("ripple", "Ripple", "Radial", "look", "Rings expanding from the centre.",
     [_SPEED, _num("rings", "Rings", 2.0, 0.5, 8.0, 0.25), _SHARP, _flag("inward", "Collapse inward", False)]),
    ("spiral", "Spiral", "Radial", "look", "Rotating spiral arms.",
     [_num("speed", "Rotations / sec", 0.25, 0.02, 3.0), _num("arms", "Arms", 3, 1, 12, 1), _num("twist", "Twist", 0.6, 0.0, 3.0, 0.1), _SHARP]),
    ("lighthouse", "Lighthouse beam", "Radial", "look", "Beams sweeping around the centre.",
     [_num("speed", "Rotations / sec", 0.25, 0.02, 3.0), _num("beams", "Beams", 1, 1, 12, 1), _num("width_deg", "Beam width (deg)", 30.0, 5.0, 120.0, 1.0)]),
    ("galaxy", "Galaxy swirl", "Radial", "look", "Spiral galaxy with a glowing core.",
     [_SPEED, _num("arms", "Arms", 3, 1, 8, 1), _num("twist", "Twist", 1.6, 0.0, 4.0, 0.1), _num("sharpness", "Sharpness", 3.0, 0.5, 6.0, 0.1)]),
    ("mandala", "Mandala bloom", "Radial", "look", "Counter-rotating petals on expanding rings.",
     [_SPEED, _num("petals", "Petals", 8, 2, 24, 1), _num("rings", "Rings", 2.5, 0.5, 8.0, 0.25), _num("sharpness", "Sharpness", 1.6, 0.5, 5.0, 0.1)]),
    ("comet", "Comet (by index)", "Chase", "look", "Comets running through the drones in formation order.",
     [_num("speed", "Laps / sec", 0.3, 0.02, 3.0), _num("tail", "Tail length", 0.12, 0.02, 0.8, 0.01), _num("comets", "Comets", 1, 1, 8, 1)]),
    ("chase", "Chase ramp (by index)", "Chase", "look", "Ramp chasing through the drones by index.",
     [_num("speed", "Speed", 1.0, 0.05, 8.0), _num("length", "Ramp length", 0.1, 0.02, 1.0, 0.01),
      _choice("shape", "Shape", "ramp_up", [["ramp_up", "Ramp up"], ["ramp_down", "Ramp down"], ["triangle", "Triangle"]])]),
    ("twinkle", "Twinkle", "Sparkle", "look", "Soft star-field twinkle.",
     [_num("speed", "Speed", 0.8, 0.05, 6.0), _num("density", "Density", 0.6, 0.05, 1.0), _num("sharpness", "Sharpness", 3.0, 0.5, 6.0, 0.1), _FLOOR]),
    ("sparkle", "Glitter", "Sparkle", "look", "Hard random flashes.",
     [_num("speed", "Speed", 1.0, 0.1, 4.0), _num("density", "Density", 0.2, 0.02, 1.0, 0.02), _FLOOR]),
    ("firefly", "Fireflies", "Sparkle", "look", "Organic chaotic flicker.",
     [_num("speed", "Speed", 1.0, 0.1, 4.0), _num("floor", "Minimum brightness", 0.15, 0.0, 1.0)]),
    ("plasma", "Plasma", "Organic", "look", "Flowing overlapping waves.",
     [_SPEED, _num("scale", "Blob size", 0.35, 0.05, 2.0)]),
    ("flames", "Rising flames", "Organic", "look", "Flame tongues rising upward.",
     [_SPEED, _num("scale", "Flame size", 0.3, 0.05, 2.0), _num("sharpness", "Sharpness", 2.2, 0.5, 5.0, 0.1)]),
    ("fireworks", "Firework pops", "Organic", "look", "Small bursts in random regions.",
     [_num("period", "Seconds between pops", 2.5, 0.5, 10.0, 0.25), _num("cluster", "Burst size", 0.5, 0.1, 2.0)]),
    # reveals
    ("reveal_wipe", "Wipe", "Reveal", "reveal", "Formation appears along a direction.", [_DIR, _SOFT]),
    ("reveal_center_out", "Centre outward", "Reveal", "reveal", "Appears from the centre outwards.", [_SOFT]),
    ("reveal_edges_in", "Edges inward", "Reveal", "reveal", "Appears from the outside in.", [_SOFT]),
    ("reveal_corners_in", "Four corners inward", "Reveal", "reveal", "Appears from all four corners.", [_SOFT]),
    ("reveal_split", "Curtain", "Reveal", "reveal", "Opens from the middle like curtains.",
     [_choice("axis", "Axis", "horizontal", [["horizontal", "Horizontal"], ["vertical", "Vertical"]]), _flag("from_center", "From the centre", True), _SOFT]),
    ("reveal_dissolve", "Pixel dissolve", "Reveal", "reveal", "Drones flicker on in random order.", []),
    ("reveal_index", "Draw by index", "Reveal", "reveal", "Lights up drone by drone in formation order.", [_SOFT]),
]


def registry_as_json():
    return [
        {"id": i, "label": l, "group": g, "kind": k, "description": d, "params": p}
        for (i, l, g, k, d, p) in REGISTRY
    ]


def default_params(effect_id):
    for (i, _l, _g, _k, _d, params) in REGISTRY:
        if i == effect_id:
            return {p["key"]: p["default"] for p in params}
    raise KeyError(effect_id)
