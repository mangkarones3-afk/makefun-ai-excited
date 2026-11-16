#!/usr/bin/env python3
"""ai-excited-01 entry: "The Impossible Breach".

A colossal humpback whale bursts out of a tiny glowing fishbowl, frozen at
the peak of its arc against a twilight sky: water still exploding upward,
a comet streaking overhead, a sea of clouds far below. The drawing is fully
procedural (Python standard library only) and deterministic: two runs of
this program write byte-identical output.

The scene is rendered on a 2x supersampled canvas and box-downsampled, so
every silhouette edge is anti-aliased. Low-frequency light (the warm glow
behind the bowl, the vignette) is applied afterwards at final resolution.
"""

import math
import os
import random
import struct
import zlib

W = H = 1024          # final image size
SS = 2                # supersampling factor
BW, BH = W * SS, H * SS
NPIX = BW * BH

buf = bytearray(NPIX * 3)
rng = random.Random(20261116)


# --------------------------------------------------------------------------
# small colour helpers
# --------------------------------------------------------------------------

def clamp(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def mix(c1, c2, t):
    return (c1[0] + (c2[0] - c1[0]) * t,
            c1[1] + (c2[1] - c1[1]) * t,
            c1[2] + (c2[2] - c1[2]) * t)


def bytes_of(c):
    return bytes((clamp(c[0]), clamp(c[1]), clamp(c[2])))


# --------------------------------------------------------------------------
# raw pixel primitives (canvas coordinates, i.e. design units * SS)
# --------------------------------------------------------------------------

def span(y, x0, x1, cbytes):
    """Fill a horizontal run of pixels with a solid colour."""
    if y < 0 or y >= BH:
        return
    if x0 < 0:
        x0 = 0
    if x1 > BW - 1:
        x1 = BW - 1
    if x1 < x0:
        return
    row = y * BW * 3
    buf[row + x0 * 3: row + (x1 + 1) * 3] = cbytes * (x1 - x0 + 1)


def blend_px(x, y, c, a):
    i = (y * BW + x) * 3
    ia = 1.0 - a
    buf[i] = clamp(buf[i] * ia + c[0] * a)
    buf[i + 1] = clamp(buf[i + 1] * ia + c[1] * a)
    buf[i + 2] = clamp(buf[i + 2] * ia + c[2] * a)


def add_px(x, y, c, a):
    i = (y * BW + x) * 3
    buf[i] = clamp(buf[i] + c[0] * a)
    buf[i + 1] = clamp(buf[i + 1] + c[1] * a)
    buf[i + 2] = clamp(buf[i + 2] + c[2] * a)


# --------------------------------------------------------------------------
# design-space primitives (coordinates in final-image units, scaled by SS)
# --------------------------------------------------------------------------

def disc(cx, cy, r, c, a=1.0):
    """Filled circle; a<1 blends per pixel (used for small features)."""
    cx, cy, r = cx * SS, cy * SS, r * SS
    cb = bytes_of(c)
    x0, x1 = int(cx - r - 1), int(cx + r + 1)
    y0, y1 = int(cy - r - 1), int(cy + r + 1)
    r2 = r * r
    for y in range(max(0, y0), min(BH - 1, y1) + 1):
        dy = y - cy
        rem = r2 - dy * dy
        if rem < 0:
            continue
        dx = math.sqrt(rem)
        xa, xb = int(cx - dx), int(cx + dx)
        if a >= 1.0:
            span(y, xa, xb, cb)
        else:
            for x in range(max(0, xa), min(BW - 1, xb) + 1):
                blend_px(x, y, c, a)


def disc_soft(cx, cy, r, c, a):
    """Circle with a smooth radial falloff, additively blended."""
    cx, cy, r = cx * SS, cy * SS, r * SS
    x0, x1 = int(cx - r), int(cx + r)
    y0, y1 = int(cy - r), int(cy + r)
    r2 = r * r
    for y in range(max(0, y0), min(BH - 1, y1) + 1):
        dy = y - cy
        for x in range(max(0, x0), min(BW - 1, x1) + 1):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if d2 < r2:
                f = 1.0 - math.sqrt(d2) / r
                add_px(x, y, c, a * f * f)


def line(x0, y0, x1, y1, w, c, a=1.0):
    """Thick round-capped line: a chain of discs along the segment."""
    steps = max(2, int(max(abs(x1 - x0), abs(y1 - y0)) / (w * 0.4)) + 1)
    for i in range(steps + 1):
        t = i / steps
        disc(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, w * 0.5, c, a)


def ring(cx, cy, r, w, c, a):
    """Annulus drawn as per-row spans between outer and inner chords."""
    cx, cy, r = cx * SS, cy * SS, r * SS
    hw = w * SS * 0.5
    ri = r - hw
    cb = bytes_of(c)
    y0, y1 = int(cy - r - 1), int(cy + r + 1)
    for y in range(max(0, y0), min(BH - 1, y1) + 1):
        dy = y - cy
        rem = r * r - dy * dy
        if rem < 0:
            continue
        dxo = math.sqrt(rem)
        remi = ri * ri - dy * dy
        if a >= 1.0:
            if remi <= 0:
                span(y, int(cx - dxo), int(cx + dxo), cb)
            else:
                dxi = math.sqrt(remi)
                span(y, int(cx - dxo), int(cx - dxi), cb)
                span(y, int(cx + dxi), int(cx + dxo), cb)
        else:
            dxi = math.sqrt(remi) if remi > 0 else -1.0
            for x in range(int(cx - dxo), int(cx + dxo) + 1):
                if 0 <= x < BW and abs(x - cx) >= dxi:
                    blend_px(x, y, c, a)


def poly(pts, color_fn):
    """Scanline polygon fill. pts are design-space (x, y); color_fn(y)->rgb."""
    P = [(x * SS, y * SS) for (x, y) in pts]
    ys = [p[1] for p in P]
    y0 = max(0, int(math.floor(min(ys))))
    y1 = min(BH - 1, int(math.ceil(max(ys))))
    n = len(P)
    for y in range(y0, y1 + 1):
        yc = y + 0.5
        nodes = []
        for i in range(n):
            ax, ay = P[i]
            bx, by = P[(i + 1) % n]
            if (ay <= yc < by) or (by <= yc < ay):
                nodes.append(ax + (yc - ay) * (bx - ax) / (by - ay))
        if len(nodes) < 2:
            continue
        nodes.sort()
        cb = bytes_of(color_fn(yc / SS))
        for i in range(0, len(nodes) - 1, 2):
            xa = int(math.ceil(nodes[i] - 0.5))
            xb = int(math.floor(nodes[i + 1] - 0.5))
            span(y, xa, xb, cb)


# --------------------------------------------------------------------------
# background: twilight sky, stars, comet, sea of clouds
# --------------------------------------------------------------------------

SKY_STOPS = [
    (0.00, (7, 10, 38)),
    (0.30, (14, 32, 74)),
    (0.52, (24, 66, 104)),
    (0.68, (38, 108, 128)),
    (0.80, (96, 138, 128)),
    (0.88, (216, 138, 82)),
    (0.94, (250, 186, 110)),
    (1.00, (120, 74, 66)),
]


def sky_color(t):
    for i in range(len(SKY_STOPS) - 1):
        t0, c0 = SKY_STOPS[i]
        t1, c1 = SKY_STOPS[i + 1]
        if t0 <= t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0))
    return SKY_STOPS[-1][1]


def draw_sky():
    for y in range(BH):
        cb = bytes_of(sky_color(y / BH))
        buf[y * BW * 3:(y + 1) * BW * 3] = cb * BW


def draw_stars():
    for _ in range(430):
        x = rng.uniform(0, W)
        y = rng.uniform(0, 640) * rng.uniform(0.25, 1.0)
        r = rng.uniform(0.7, 2.1)
        a = rng.uniform(0.25, 0.95) * (1.0 - y / 900.0)
        tint = rng.choice([(255, 255, 255), (200, 220, 255), (255, 238, 200)])
        disc(x, y, r, tint, max(0.05, a))
    # a few hero stars with cross sparkles
    for (x, y) in [(150, 120), (880, 90), (700, 210), (240, 300), (960, 320)]:
        disc(x, y, 2.6, (255, 255, 255), 0.95)
        line(x - 13, y, x + 13, y, 1.1, (220, 230, 255), 0.5)
        line(x, y - 13, x, y + 13, 1.1, (220, 230, 255), 0.5)


def draw_comet():
    # a bright comet arcing across the upper right, head low-left, tail up-right
    hx, hy = 742, 150
    tx, ty = 985, 52
    steps = 46
    for i in range(steps + 1):
        t = i / steps
        x = hx + (tx - hx) * t
        y = hy + (ty - hy) * t - 16 * math.sin(t * math.pi)
        a = 0.55 * (1.0 - t) ** 1.6
        disc(x, y, 5.5 * (1.0 - t) + 0.8, (255, 214, 140), a)
        disc(x, y, 2.6 * (1.0 - t) + 0.4, (255, 246, 220), a)
    disc(hx, hy, 6.5, (255, 240, 205), 0.95)
    disc(hx, hy, 3.2, (255, 255, 255), 1.0)
    disc_soft(hx, hy, 26, (255, 190, 100), 0.35)


def draw_cloud_sea():
    # rolling cloud tops along the bottom, warm-lit
    base = [(0, 906), (1024, 884)]
    pts = []
    for x in range(0, 1025, 32):
        pts.append((x, 902 - 26 * math.sin(x * 0.011 + 1.2)
                    - 14 * math.sin(x * 0.027 + 4.0)))
    pts += [(1024, 1024), (0, 1024)]

    def cloud_col(y):
        t = (y - 860) / 170.0
        return mix((255, 205, 150), (146, 92, 92), max(0.0, min(1.0, t)))

    poly(pts, cloud_col)
    for _ in range(30):
        x = rng.uniform(-40, W + 40)
        y = rng.uniform(892, 1000)
        r = rng.uniform(38, 120)
        c = mix((255, 214, 160), (255, 170, 120), rng.random())
        disc_soft(x, y, r, c, 0.10)
    for _ in range(16):
        x = rng.uniform(0, W)
        y = rng.uniform(940, 1024)
        disc_soft(x, y, rng.uniform(40, 90), (90, 50, 70), 0.10)


# --------------------------------------------------------------------------
# the whale
# --------------------------------------------------------------------------

SP0 = (300.0, 612.0)    # tail stock, just above the bowl rim
SP1 = (470.0, 470.0)
SP2 = (610.0, 318.0)
SP3 = (818.0, 492.0)    # snout


def spine(t):
    mt = 1.0 - t
    x = (mt ** 3 * SP0[0] + 3 * mt * mt * t * SP1[0]
         + 3 * mt * t * t * SP2[0] + t ** 3 * SP3[0])
    y = (mt ** 3 * SP0[1] + 3 * mt * mt * t * SP1[1]
         + 3 * mt * t * t * SP2[1] + t ** 3 * SP3[1])
    return (x, y)


def spine_tan(t):
    mt = 1.0 - t
    x = (3 * mt * mt * (SP1[0] - SP0[0]) + 6 * mt * t * (SP2[0] - SP1[0])
         + 3 * t * t * (SP3[0] - SP2[0]))
    y = (3 * mt * mt * (SP1[1] - SP0[1]) + 6 * mt * t * (SP2[1] - SP1[1])
         + 3 * t * t * (SP3[1] - SP2[1]))
    L = math.hypot(x, y) or 1.0
    return (x / L, y / L)


def half_width(t):
    # a real trunk profile, not a ribbon: massive mid-body bulge, a broad
    # blunt head of its own, and a narrow peduncle running into the fluke
    w = 20.0 + 104.0 * math.exp(-(((t - 0.40) / 0.30) ** 2))
    w += 36.0 * math.exp(-(((t - 0.94) / 0.11) ** 2))
    if t < 0.12:
        u = t / 0.12
        w *= 0.30 + 0.70 * u * u
    if t > 0.90:
        u = (t - 0.90) / 0.10
        w *= 1.0 - 0.10 * u * u
    return w


def whale_geometry():
    """Return (back_pts, belly_pts, spine_pts) in design space."""
    N = 56
    spine_pts = [spine(i / N) for i in range(N + 1)]
    # decide which normal side faces down (the belly side)
    tx, ty = spine_tan(0.5)
    nx, ny = -ty, tx
    belly_sign = 1.0 if ny > 0 else -1.0
    back, belly = [], []
    for i in range(N + 1):
        t = i / N
        x, y = spine_pts[i]
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        w = half_width(t)
        # the belly side deepens toward the head (throat and jaw)
        u = max(0.0, min(1.0, (t - 0.72) / 0.28))
        bf = 0.58 + 0.14 * (u * u * (3.0 - 2.0 * u))
        back.append((x - belly_sign * nx * w * 0.62,
                     y - belly_sign * ny * w * 0.62))
        belly.append((x + belly_sign * nx * w * bf,
                      y + belly_sign * ny * w * bf))
    return back, belly, spine_pts, belly_sign


def draw_whale():
    back, belly, spine_pts, bsign = whale_geometry()
    # outline with rounded caps at snout and tail stock
    tx, ty = spine_tan(1.0)
    snout_cap = (spine_pts[-1][0] + tx * 8, spine_pts[-1][1] + ty * 8)
    tx0, ty0 = spine_tan(0.0)
    tail_cap = (spine_pts[0][0] - tx0 * 10, spine_pts[0][1] - ty0 * 10)
    outline = back + [snout_cap] + belly[::-1] + [tail_cap]

    def body_col(y):
        t = (y - 130) / 600.0
        t = max(0.0, min(1.0, t))
        return mix((46, 86, 132), (26, 48, 84), t)

    poly(outline, body_col)

    # pale belly: a band along the underside, its inner edge riding just
    # below the spine so the dark back stays the dominant mass
    inner = []
    for i in range(57):
        t = i / 56.0
        x, y = spine(t)
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        u = max(0.0, min(1.0, (t - 0.60) / 0.40))
        off = half_width(t) * (0.30 - 0.16 * (u * u * (3.0 - 2.0 * u)))
        inner.append((x + bsign * nx * off, y + bsign * ny * off))
    belly_poly = belly + inner[::-1]

    def belly_col(y):
        t = (y - 300) / 420.0
        t = max(0.0, min(1.0, t))
        return mix((186, 220, 232), (140, 182, 204), t)

    poly(belly_poly, belly_col)

    # throat pleats: a short fan of soft grooves on the throat only
    for k in range(6):
        tt = 0.80 + 0.022 * k
        pts = []
        for j in range(7):
            u = j / 6.0
            t = tt + 0.07 * u
            x, y = spine(t)
            tx, ty = spine_tan(t)
            nx, ny = -ty, tx
            off = half_width(t) * (0.55 - 0.33 * u)
            pts.append((x + bsign * nx * off, y + bsign * ny * off))
        for a, b in zip(pts, pts[1:]):
            line(a[0], a[1], b[0], b[1], 1.5, (141, 179, 199), 0.25)

    # pectoral fin: grown from the belly line, long and slim, sweeping back
    def belly_pt(t, extra=0.0):
        x, y = spine(t)
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        off = half_width(t) * 0.58 + extra
        return (x + bsign * nx * off, y + bsign * ny * off)

    f_base1 = belly_pt(0.46)
    f_base2 = belly_pt(0.60)
    f_tip = (f_base1[0] - 150, f_base1[1] + 46)
    f_mid = ((f_base1[0] + f_tip[0]) / 2 - 10, (f_base1[1] + f_tip[1]) / 2 + 6)
    f_in = ((f_base2[0] + f_tip[0]) / 2 + 6, (f_base2[1] + f_tip[1]) / 2 - 4)
    fin = [f_base1, f_mid, f_tip, f_in, f_base2]

    def draw_fin():
        poly(fin, lambda y: mix((44, 78, 122), (28, 54, 92),
                                max(0.0, min(1.0, (y - 430) / 220.0))))
        line(f_base1[0], f_base1[1], f_mid[0], f_mid[1], 2.2,
             (255, 196, 107), 0.5)
        line(f_mid[0], f_mid[1], f_tip[0], f_tip[1], 2.2,
             (255, 196, 107), 0.5)

    globals()["draw_fin"] = draw_fin

    # tail fluke in a fixed, near-horizontal frame at the end of the stock:
    # two swept lobes with a centre notch, tilted just off horizontal
    cx0, cy0 = spine(0.0)
    ax, ay = 0.95, -0.31            # fluke spread axis, right tip raised
    bx, by = -0.72, 0.69            # trailing direction, back and down
    C = (cx0 + bx * 2, cy0 + by * 2)
    tip_a = (C[0] + ax * 80 + bx * 16, C[1] + ay * 80 + by * 16)
    tip_b = (C[0] - ax * 80 + bx * 16, C[1] - ay * 80 + by * 16)
    lead = (C[0] - bx * 10, C[1] - by * 10)
    lead_a = (C[0] + ax * 42 - bx * 5, C[1] + ay * 42 - by * 5)
    lead_b = (C[0] - ax * 42 - bx * 5, C[1] - ay * 42 - by * 5)
    notch = (C[0] + bx * 24, C[1] + by * 24)
    fluke = [tip_b, lead_b, lead, lead_a, tip_a, notch]
    poly(fluke, lambda y: (28, 54, 88))
    line(tip_b[0], tip_b[1], lead_b[0], lead_b[1], 2.4, (255, 196, 107), 0.55)
    line(lead_b[0], lead_b[1], lead[0], lead[1], 2.4, (255, 196, 107), 0.55)
    line(lead[0], lead[1], lead_a[0], lead_a[1], 2.4, (255, 196, 107), 0.55)
    line(lead_a[0], lead_a[1], tip_a[0], tip_a[1], 2.4, (255, 196, 107), 0.55)

    # golden rim light along the back
    for a, b in zip(back, back[1:]):
        line(a[0], a[1], b[0], b[1], 9.0, (255, 176, 84), 0.16)
    for a, b in zip(back, back[1:]):
        line(a[0], a[1], b[0], b[1], 3.6, (255, 205, 120), 0.85)

    # eye and mouth line near the snout
    ex, ey = spine(0.93)
    tx, ty = spine_tan(0.93)
    nx, ny = -ty, tx
    eoff = half_width(0.93) * 0.26
    ex += bsign * nx * eoff + tx * 6
    ey += bsign * ny * eoff + ty * 6
    disc(ex, ey, 5.2, (8, 14, 24), 1.0)
    disc(ex + 1.6, ey - 1.6, 1.8, (255, 255, 255), 1.0)
    mouth = []
    for j in range(13):
        t = 0.865 + 0.125 * (j / 12.0)
        x, y = spine(t)
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        off = half_width(t) * 0.36
        mouth.append((x + bsign * nx * off, y + bsign * ny * off))
    for a, b in zip(mouth, mouth[1:]):
        line(a[0], a[1], b[0], b[1], 1.8, (16, 30, 48), 0.4)

    return back, belly, bsign


# --------------------------------------------------------------------------
# the fishbowl and the burst of water
# --------------------------------------------------------------------------

BOWL = (352.0, 764.0)
BOWL_R = 94.0


def draw_bowl():
    cx, cy = BOWL
    # warm halo behind the bowl
    disc_soft(cx, cy - 30, 190, (255, 170, 80), 0.30)
    # glass body: a whisper of pale cyan over the sky
    disc(cx, cy, BOWL_R, (150, 220, 235), 0.10)
    # the water inside: bottom arc swept right-to-left, then the sloshing
    # surface walked back left-to-right so the polygon never crosses itself
    pts = []
    for i in range(33):
        a = math.pi * (0.08 + 0.84 * (i / 32.0))
        pts.append((cx + (BOWL_R - 7) * math.cos(a),
                    cy + (BOWL_R - 7) * math.sin(a)))
    for i in range(17):
        x = cx - (BOWL_R - 7) + 2 * (BOWL_R - 7) * (i / 16.0)
        pts.append((x, cy - 34 + 5 * math.sin(i * 1.3)))

    def water_col(y):
        t = (y - (cy - 40)) / (2 * BOWL_R)
        return mix((64, 196, 214), (16, 96, 138), max(0.0, min(1.0, t)))

    poly(pts, water_col)
    # a tiny goldfish, looking up at the impossible
    disc(330, 792, 11, (255, 158, 64), 1.0)
    poly([(319, 792), (303, 782), (304, 801)], lambda y: (244, 130, 44))
    disc(334, 789, 2.2, (30, 30, 30), 1.0)
    disc(322, 764, 3.0, (220, 250, 255), 0.55)
    disc(342, 750, 2.2, (220, 250, 255), 0.5)
    # glass rim: a true annulus, plus a bright crescent on the upper left
    ring(cx, cy, BOWL_R, 3.2, (205, 245, 252), 0.55)
    for i in range(30):
        a = math.pi * (0.78 + 0.42 * (i / 29.0))
        disc(cx + (BOWL_R - 8) * math.cos(a), cy + (BOWL_R - 8) * math.sin(a),
             3.2, (255, 255, 255), 0.5)
    # soft shadow pooling on the clouds beneath the floating bowl
    for k in range(5):
        disc_soft(cx, cy + BOWL_R + 34, 60 - k * 8, (70, 44, 60), 0.05)


def draw_burst(belly, bsign):
    cx, cy = BOWL
    # a curtain of water still sheeting off the tail and belly, drawn as
    # many thin falling streaks rather than one solid sheet
    for _ in range(90):
        t = rng.uniform(0.01, 0.30)
        x, y = spine(t)
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        off = half_width(t) * 0.58 - 2
        x += bsign * nx * off
        y += bsign * ny * off
        L = rng.uniform(8, 38) * (1.0 - t)
        line(x, y, x + rng.uniform(-3, 3), y + L,
             rng.uniform(0.8, 1.4), (226, 248, 253), 0.30)
    # splash crown around the rim
    for _ in range(58):
        a = rng.uniform(-2.7, -0.45)
        rr = BOWL_R * rng.uniform(0.75, 1.0)
        x = cx + rr * math.cos(a)
        y = cy + 30 * math.sin(a) - rng.uniform(0, 22)
        r = rng.uniform(1.2, 4.2)
        disc(x, y, r * 2.0, (150, 225, 245), 0.30)
        disc(x, y, r, (255, 255, 255), 0.92)
    # ballistic droplets bursting up and out
    for _ in range(430):
        a = rng.uniform(-2.95, -0.20)
        sp = rng.uniform(30, 300) ** 1.12 / 8.0
        x0, y0 = cx + rng.uniform(-46, 46), cy - 58
        x = x0 + math.cos(a) * sp * rng.uniform(0.5, 1.4)
        y = y0 + math.sin(a) * sp - rng.uniform(0, 60)
        r = rng.uniform(0.8, 3.4) * (1.0 - sp / 460.0)
        if r <= 0.25:
            continue
        disc(x, y, r * 2.1, (140, 220, 248), 0.28)
        disc(x, y, r, (255, 255, 255), 0.9)
    # long sparkling streaks radiating from the burst
    for _ in range(12):
        a = rng.uniform(-2.9, -0.25)
        L = rng.uniform(60, 180)
        x0, y0 = cx + rng.uniform(-30, 30), cy - 66
        line(x0, y0, x0 + math.cos(a) * L, y0 + math.sin(a) * L,
             rng.uniform(1.0, 2.0), (235, 250, 255), 0.45)
    # droplets trailing along the whale's underside (kept forward of the
    # pectoral fin so the fin stays readable)
    for _ in range(240):
        t = rng.uniform(0.03, 0.45)
        x, y = spine(t)
        tx, ty = spine_tan(t)
        nx, ny = -ty, tx
        off = half_width(t) * 0.58 + rng.uniform(2, 30)
        x += bsign * nx * off + rng.uniform(-8, 8)
        y += bsign * ny * off + rng.uniform(0, 26)
        r = rng.uniform(0.7, 2.8)
        disc(x, y, r * 2.0, (140, 220, 248), 0.25)
        disc(x, y, r, (250, 253, 255), 0.85)
    # foam where the fluke slaps the surface
    for _ in range(44):
        x = cx + rng.uniform(-95, 95)
        y = cy - 66 + rng.uniform(-10, 16)
        disc(x, y, rng.uniform(1.5, 5.5), (255, 255, 255), 0.75)


def draw_birds():
    for (x, y, s) in [(120, 470, 15), (168, 512, 11), (905, 560, 13),
                      (950, 610, 9), (80, 590, 9)]:
        line(x - s, y - s * 0.42, x, y, 2.0, (16, 24, 44), 0.8)
        line(x, y, x + s, y - s * 0.42, 2.0, (16, 24, 44), 0.8)


# --------------------------------------------------------------------------
# final-resolution light pass and PNG output
# --------------------------------------------------------------------------

def downsample():
    out = bytearray(W * H * 3)
    for y in range(H):
        r0 = (2 * y) * BW * 3
        r1 = (2 * y + 1) * BW * 3
        o = y * W * 3
        top = buf[r0:r0 + BW * 3]
        bot = buf[r1:r1 + BW * 3]
        row = [(top[i] + top[i + 3] + bot[i] + bot[i + 3] + 2) >> 2
               for i in range(0, BW * 3, 6)]
        rowg = [(top[i] + top[i + 3] + bot[i] + bot[i + 3] + 2) >> 2
                for i in range(1, BW * 3, 6)]
        rowb = [(top[i] + top[i + 3] + bot[i] + bot[i + 3] + 2) >> 2
                for i in range(2, BW * 3, 6)]
        merged = bytearray(W * 3)
        merged[0::3] = bytes(row)
        merged[1::3] = bytes(rowg)
        merged[2::3] = bytes(rowb)
        out[o:o + W * 3] = merged
    return out


def light_pass(img):
    """Warm glow behind the bowl, gentle cool lift up top, vignette."""
    gx, gy = 352.0, 700.0
    for y in range(H):
        dy = y - gy
        oy = y * W * 3
        for x in range(W):
            dx = x - gx
            d2 = dx * dx + dy * dy
            glow = 88.0 / (1.0 + d2 / 42000.0)
            vx = (x - 512) / 512.0
            vy = (y - 512) / 512.0
            vig = 1.0 - 0.26 * max(0.0, vx * vx + vy * vy - 0.35)
            i = oy + x * 3
            r = img[i] * vig + glow * 1.00
            g = img[i + 1] * vig + glow * 0.62
            b = img[i + 2] * vig + glow * 0.30
            img[i] = clamp(r)
            img[i + 1] = clamp(g)
            img[i + 2] = clamp(b)
    return img


def write_png(path, img):
    raw = bytearray()
    prev = bytes(W * 3)
    for y in range(H):
        row = bytes(img[y * W * 3:(y + 1) * W * 3])
        up = bytes((a - b) & 0xFF for a, b in zip(row, prev))
        s_row = sum(abs(b - 256 if b > 127 else b) for b in row[::53])
        s_up = sum(abs(b - 256 if b > 127 else b) for b in up[::53])
        if s_up < s_row:
            raw.append(2)
            raw += up
        else:
            raw.append(0)
            raw += row
        prev = row
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", comp)
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    draw_sky()
    draw_stars()
    draw_comet()
    # faint nebula washes so the empty sky has depth
    disc_soft(190, 400, 250, (46, 110, 138), 0.085)
    disc_soft(880, 330, 210, (96, 62, 142), 0.075)
    draw_cloud_sea()
    draw_bowl()
    back, belly_pts, bsign = draw_whale()
    draw_burst(belly_pts, bsign)
    draw_fin()  # the fin rides in front of the spray sheeting off the body
    draw_birds()
    img = downsample()
    img = light_pass(img)
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    write_png(os.path.join(out_dir, "entry.png"), img)


if __name__ == "__main__":
    main()
