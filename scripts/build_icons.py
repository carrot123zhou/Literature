#!/usr/bin/env python3
"""Build the Literature app icon assets from a single geometry definition.

Standard practice for an app icon is: keep one vector master (SVG) plus one
1024x1024 raster master, and derive every platform-specific file from them.
This script is that generator — nothing in `src-tauri/icons/` is hand-edited.

Marks:

  mark      default: the approved "L / book" glyph (bold stem, book foot with
            a wavy fore-edge, two dots) taken from assets/brand/monogram.png.
  cards     alternate: two rounded cards + a 45-degree ribbon where source and
            translation line up (Apple system blue).
  monogram  alternate: vector "L / book" glyph with two dots, monochrome.

Outputs
  src-tauri/icons/                  `mark` (the icon Tauri bundles)
  assets/brand/monogram/            `monogram` vector alternate set
  assets/brand/lockup.png|.svg      mark + wordmark for the README

Usage
  python3 scripts/build_icons.py            # write every asset
  python3 scripts/build_icons.py --check    # preview only, nothing written
"""
from __future__ import annotations

import base64
import io
import math
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CANVAS = 1024
SS = 4                                    # supersampling factor for smooth edges

# The macOS icon body: every current macOS app insets its artwork so the
# squircle sits ~10% inside the 1024 canvas.
BODY = dict(x=104, y=104, w=816, h=816, n=5.0)

# ---------------------------------------------------------------- palette ---
TILE_TOP, TILE_BOTTOM = "#ffffff", "#eff1f5"
TILE_MONO_TOP, TILE_MONO_BOTTOM = "#ffffff", "#f4f5f7"
CARD_TOP, CARD_BOTTOM = (26, 140, 255), (0, 102, 214)        # #1A8CFF -> #0066D6
RIBBON_TOP, RIBBON_BOTTOM = (77, 163, 255), (10, 132, 255)   # #4DA3FF -> #0A84FF
INK = (17, 17, 17)                                            # monogram black
SHADOW_CARD_A = (9, 42, 88, 52)
SHADOW_CARD_B = (9, 42, 88, 34)
CARD_R = 108
EDGE_LIGHT = 6
HAIRLINE = (11, 60, 120, 30)      # keeps the white card readable at 16px

# ------------------------------------------------- cards mark (the default) ---
A = dict(x=223, y=359, w=422, h=440)      # back card, lower left
B = dict(x=393, y=220, w=407, h=441)      # front card, upper right

# 45-degree ribbon between the two lines x = y + 62 and x = y - 111
RIB_HALF_W = 86.5 / math.sqrt(2)          # 61.2
RIB_CAP_TOP = (327.0, 351.0)
RIB_CAP_BOTTOM = (603.0, 627.0)
RIB_CLIP = dict(x=B["x"], y=A["y"], w=A["x"] + A["w"] - B["x"], h=A["h"])

# ---------------------------------------------- monogram mark (alternate) ---
# Measured from the approved raster: a bold "L" whose foot is a book (spine,
# page slot, fore-edge), plus two dots echoing the reference logo style.
MONO_STEM = dict(x=268, y=224, w=208, h=442)      # stem, ends where the page slot opens
MONO_BOOK = dict(x=268, y=620, w=487, h=197)      # book block (spine, fore-edge, base)
MONO_SLOT = dict(x=367, y=666, w=348, h=100)      # the open pages (cut out of the block)
MONO_SLOT_SKEW = -0.15                             # pages lean back, like a real fore-edge
MONO_DOT_BIG = (641, 298, 61)
MONO_DOT_SMALL = (710, 412, 57)
MONO_R_STEM = 78
MONO_R = 22
MONO_SLOT_R = 14
MONO_SCALE = 0.86                                 # glyph size inside the body
MONO_TARGET_H = 506                               # opt. glyph height on the body

# ------------------------------------------------ approved raster mark ---
# The user-approved artwork (a bold "L" whose foot is a book with a wavy
# fore-edge, plus two dots). It is the shipped icon's source of truth: we lift
# its ink to an alpha mask and drop it on the macOS squircle tile, so it no
# longer carries its own white page and stays crisp when downscaled.
MARK_SOURCE = os.path.join(ROOT, "assets", "brand", "monogram.png")
MARK_TARGET_H = 640                               # mark ink height inside the body (~0.78 of the 816 body, matching the reference icons)


# ------------------------------------------------------------- primitives ---
def squircle(box, n=5.0, steps=192):
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    a, b = box["w"] / 2, box["h"] / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append((cx + math.copysign(abs(ct) ** (2 / n), ct) * a,
                    cy + math.copysign(abs(st) ** (2 / n), st) * b))
    return pts


def rounded_rect(box, r, radii=None, steps=32):
    """Clockwise from the top-right corner; radii=(tl, tr, br, bl), 0 = square."""
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["w"], y0 + box["h"]
    tl, tr, br, bl = radii if radii else (r, r, r, r)
    corners = [(x1 - tr, y0 + tr, -90, 0, tr), (x1 - br, y1 - br, 0, 90, br),
               (x0 + bl, y1 - bl, 90, 180, bl), (x0 + tl, y0 + tl, 180, 270, tl)]
    pts = []
    for cx, cy, a0, a1, cr in corners:
        if cr <= 0:
            pts.append((cx, cy))
            continue
        for i in range(steps + 1):
            t = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + cr * math.cos(t), cy + cr * math.sin(t)))
    return pts


def ribbon_parts():
    """Return (rect polygon, cap circles) of the 45-degree stadium."""
    nx, ny = math.sqrt(0.5), -math.sqrt(0.5)
    ux, uy = math.sqrt(0.5), math.sqrt(0.5)
    t, b, w = RIB_CAP_TOP, RIB_CAP_BOTTOM, RIB_HALF_W
    p1 = (t[0] - ux * w, t[1] - uy * w)
    p2 = (b[0] + ux * w, b[1] + uy * w)
    rect = [(p1[0] + w * nx, p1[1] + w * ny), (p2[0] + w * nx, p2[1] + w * ny),
            (p2[0] - w * nx, p2[1] - w * ny), (p1[0] - w * nx, p1[1] - w * ny)]
    return rect, (t, b)


def vgrad(size, c0, c1):
    if isinstance(c0, str):
        c0 = tuple(int(c0[i:i + 2], 16) for i in (1, 3, 5))
        c1 = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    strip = Image.new("RGB", (1, size[1]))
    px = strip.load()
    for y in range(size[1]):
        t = y / max(1, size[1] - 1)
        px[0, y] = tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    return strip.resize(size, Image.BILINEAR)


def hexof(c):
    return "#%02x%02x%02x" % c


# ------------------------------------------------------------- rasteriser ---
def _poly(size, pts, s, fill=255):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).polygon([(x * s, y * s) for x, y in pts], fill=fill)
    return m


def _drop(canvas, mask, blur, dy, colour, s):
    m = mask.filter(ImageFilter.GaussianBlur(blur * s))
    shifted = Image.new("L", m.size, 0)
    shifted.paste(m, (0, int(dy * s)))
    layer = Image.new("RGBA", m.size, colour)
    layer.putalpha(shifted.point(lambda v: v * colour[3] // 255))
    canvas.alpha_composite(layer)


def _edge_light(canvas, box, radius, colour, s, strength=1.0):
    """The inner top highlight every Apple icon has."""
    outer = _poly(canvas.size[0], rounded_rect(box, radius), s)
    inner = _poly(canvas.size[0], rounded_rect(
        dict(x=box["x"] + EDGE_LIGHT, y=box["y"] + EDGE_LIGHT,
             w=box["w"] - 2 * EDGE_LIGHT, h=box["h"] - 2 * EDGE_LIGHT),
        max(radius - EDGE_LIGHT, 0)), s)
    ring = Image.new("L", outer.size, 0)
    ring.paste(outer)
    ring.paste(0, (0, 0), inner)
    top = ring.size[1]
    ramp = Image.new("L", (1, top))
    px = ramp.load()
    for y in range(top):
        t = y / top
        px[0, y] = 0 if t > 0.45 else int(255 * (1 - t / 0.45) * strength)
    ramp = ramp.resize(ring.size, Image.BILINEAR)
    ring = Image.frombytes("L", ring.size, bytes(
        min(ring.getpixel((x, y)), ramp.getpixel((x, y)))
        for y in range(ring.size[1]) for x in range(ring.size[0])))
    layer = Image.new("RGBA", ring.size, colour + (255,))
    layer.putalpha(ring)
    canvas.alpha_composite(layer)


def render_cards(size=CANVAS):
    s = size / CANVAS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile = _poly(size, squircle(BODY, BODY["n"], 480), s)
    base = vgrad((size, size), TILE_TOP, TILE_BOTTOM).convert("RGBA")
    base.putalpha(tile)
    img.alpha_composite(base)

    _drop(img, _poly(size, rounded_rect(A, CARD_R), s), 26, 12, SHADOW_CARD_A, s)
    a_fill = vgrad((size, size), CARD_TOP, CARD_BOTTOM).convert("RGBA")
    a_fill.putalpha(_poly(size, rounded_rect(A, CARD_R), s))
    img.alpha_composite(a_fill)

    _drop(img, _poly(size, rounded_rect(B, CARD_R), s), 12, 5, SHADOW_CARD_B, s)
    b_fill = Image.new("RGBA", (size, size), (255, 255, 255, 245))
    b_fill.putalpha(_poly(size, rounded_rect(B, CARD_R), s).point(lambda v: v * 245 // 255))
    img.alpha_composite(b_fill)

    # hairline on the front card: without it the silhouette vanishes at 16px
    outline = Image.new("L", (size, size), 0)
    ImageDraw.Draw(outline).polygon(
        [(x * s, y * s) for x, y in rounded_rect(B, CARD_R)],
        outline=255, width=max(1, int(2 * s)))
    layer = Image.new("RGBA", (size, size), HAIRLINE[:3] + (255,))
    layer.putalpha(outline.point(lambda v: v * HAIRLINE[3] // 255))
    img.alpha_composite(layer)

    rib = Image.new("L", (size, size), 0)
    rd = ImageDraw.Draw(rib)
    rect, (cap_t, cap_b) = ribbon_parts()
    rd.polygon([(x * s, y * s) for x, y in rect], fill=255)
    for cx, cy in (cap_t, cap_b):
        r = RIB_HALF_W * s
        rd.ellipse([cx * s - r, cy * s - r, cx * s + r, cy * s + r], fill=255)
    clip = _poly(size, rounded_rect(RIB_CLIP, CARD_R, radii=(0, CARD_R, CARD_R, 0)), s)
    rib = Image.composite(rib, Image.new("L", rib.size, 0), clip)
    rib_fill = vgrad((size, size), RIBBON_TOP, RIBBON_BOTTOM).convert("RGBA")
    rib_fill.putalpha(rib)
    img.alpha_composite(rib_fill)

    _edge_light(img, A, CARD_R, (255, 255, 255), s, 0.38)
    _edge_light(img, B, CARD_R, (255, 255, 255), s, 0.95)
    return img


def render_monogram(size=CANVAS):
    s = size / CANVAS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile = _poly(size, squircle(BODY, BODY["n"], 480), s)
    base = vgrad((size, size), TILE_MONO_TOP, TILE_MONO_BOTTOM).convert("RGBA")
    base.putalpha(tile)
    img.alpha_composite(base)

    # the glyph is drawn in its own coordinate space, then scaled and centred
    raw = Image.new("L", (CANVAS, CANVAS), 0)
    d = ImageDraw.Draw(raw)
    d.polygon(rounded_rect(MONO_STEM, MONO_R_STEM,
                           radii=(MONO_R_STEM, MONO_R_STEM, 0, 0)), fill=255)
    d.polygon(rounded_rect(MONO_BOOK, MONO_R), fill=255)
    slot = _poly(CANVAS, rounded_rect(MONO_SLOT, MONO_SLOT_R), 1.0)
    slot = slot.transform(slot.size, Image.AFFINE,
                          (1, MONO_SLOT_SKEW, -MONO_SLOT_SKEW * MONO_SLOT["y"], 0, 1, 0))
    raw.paste(0, (0, 0), slot)
    for cx, cy, r in (MONO_DOT_BIG, MONO_DOT_SMALL):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    bbox = raw.getbbox()
    glyph = raw.crop(bbox)
    k = (MONO_TARGET_H / glyph.size[1]) * s
    glyph = glyph.resize((max(1, round(glyph.size[0] * k)),
                          max(1, round(glyph.size[1] * k))), Image.LANCZOS)
    ink = Image.new("RGBA", glyph.size, INK + (255,))
    ink.putalpha(glyph)
    img.alpha_composite(ink, ((size - glyph.size[0]) // 2,
                              (size - glyph.size[1]) // 2))
    return img


def _mark_mask():
    """Alpha mask of the approved mark, cropped to its ink bounds.

    The artwork sits on a near-white page, so "255 - luminance" leaves the page
    at ~1 rather than 0 and ``getbbox`` would return the whole canvas; threshold
    a copy for the bbox while keeping the soft mask for compositing.
    """
    gray = Image.open(MARK_SOURCE).convert("L")
    alpha = gray.point(lambda v: max(0, 255 - v))
    bbox = alpha.point(lambda v: 255 if v > 16 else 0).getbbox()
    return alpha.crop(bbox) if bbox else alpha


def _mark_ink(s):
    mask = _mark_mask()
    k = (MARK_TARGET_H / mask.size[1]) * s
    mask = mask.resize((max(1, round(mask.size[0] * k)),
                        max(1, round(mask.size[1] * k))), Image.LANCZOS)
    ink = Image.new("RGBA", mask.size, INK + (255,))
    ink.putalpha(mask)
    return ink


def render_mark(size=CANVAS):
    s = size / CANVAS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile = _poly(size, squircle(BODY, BODY["n"], 480), s)
    base = vgrad((size, size), TILE_MONO_TOP, TILE_MONO_BOTTOM).convert("RGBA")
    base.putalpha(tile)
    img.alpha_composite(base)
    ink = _mark_ink(s)
    img.alpha_composite(ink, ((size - ink.size[0]) // 2, (size - ink.size[1]) // 2))
    return img


RENDERERS = dict(cards=render_cards, monogram=render_monogram, mark=render_mark)


# -------------------------------------------------------------------- svg ---
def d_of(pts):
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"


def rr_d(box, r, radii=None):
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["w"], y0 + box["h"]
    tl, tr, br, bl = radii if radii else (r, r, r, r)
    return (f"M{x0 + tl} {y0} H{x1 - tr} A{tr} {tr} 0 0 1 {x1} {y0 + tr} "
            f"V{y1 - br} A{br} {br} 0 0 1 {x1 - br} {y1} H{x0 + bl} "
            f"A{bl} {bl} 0 0 1 {x0} {y1 - bl} V{y0 + tl} A{tl} {tl} 0 0 1 {x0 + tl} {y0} Z")


def stadium_d():
    nx, ny = math.sqrt(0.5), -math.sqrt(0.5)
    ux, uy = math.sqrt(0.5), math.sqrt(0.5)
    w, t, b = RIB_HALF_W, RIB_CAP_TOP, RIB_CAP_BOTTOM
    p1 = (t[0] + w * nx, t[1] + w * ny)
    p2 = (b[0] + w * nx, b[1] + w * ny)
    p3 = (b[0] - w * nx, b[1] - w * ny)
    p4 = (t[0] - w * nx, t[1] - w * ny)
    return (f"M{p1[0]:.1f} {p1[1]:.1f} L{p2[0]:.1f} {p2[1]:.1f} "
            f"A{w:.1f} {w:.1f} 0 0 1 {p3[0]:.1f} {p3[1]:.1f} "
            f"L{p4[0]:.1f} {p4[1]:.1f} A{w:.1f} {w:.1f} 0 0 1 {p1[0]:.1f} {p1[1]:.1f} Z")


HAIRLINE_HEX = hexof(HAIRLINE[:3])

SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" '
            'viewBox="0 0 1024 1024" role="img" aria-label="Literature">\n')


def svg_cards() -> str:
    return SVG_HEAD + f'''  <!-- Literature app icon - generated by scripts/build_icons.py
       macOS body 816/1024 with an Apple-style squircle; two rounded cards
       offset diagonally; the 45-degree ribbon marks where source text and
       translation line up. Palette: Apple system blue. -->
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/>
    </linearGradient>
    <linearGradient id="card" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{hexof(CARD_TOP)}"/><stop offset="1" stop-color="{hexof(CARD_BOTTOM)}"/>
    </linearGradient>
    <linearGradient id="ribbon" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{hexof(RIBBON_TOP)}"/><stop offset="1" stop-color="{hexof(RIBBON_BOTTOM)}"/>
    </linearGradient>
    <linearGradient id="edgeLight" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity=".95"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <filter id="shadowA" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="0" dy="12" stdDeviation="13" flood-color="#092a58" flood-opacity=".20"/>
    </filter>
    <filter id="shadowB" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="0" dy="5" stdDeviation="6" flood-color="#092a58" flood-opacity=".13"/>
    </filter>
    <clipPath id="ribbonClip"><path d="{rr_d(RIB_CLIP, CARD_R, radii=(0, CARD_R, CARD_R, 0))}"/></clipPath>
  </defs>
  <path d="{d_of(squircle(BODY, BODY["n"], 192))}" fill="url(#tile)"/>
  <g filter="url(#shadowA)"><path d="{rr_d(A, CARD_R)}" fill="url(#card)"/></g>
  <g filter="url(#shadowB)"><path d="{rr_d(B, CARD_R)}" fill="#ffffff" fill-opacity=".96"/></g>
  <g clip-path="url(#ribbonClip)"><path d="{stadium_d()}" fill="url(#ribbon)"/></g>
  <path d="{rr_d(A, CARD_R)}" fill="none" stroke="url(#edgeLight)" stroke-width="6"/>
  <path d="{rr_d(B, CARD_R)}" fill="none" stroke="{HAIRLINE_HEX}" stroke-width="2"/>
  <path d="{rr_d(B, CARD_R)}" fill="none" stroke="url(#edgeLight)" stroke-width="6"/>
</svg>
'''


def svg_monogram() -> str:
    glyph = [f'    <path d="{rr_d(MONO_STEM, MONO_R_STEM, radii=(MONO_R_STEM, MONO_R_STEM, 0, 0))}"/>',
             f'    <path mask="url(#pageSlot)" d="{rr_d(MONO_BOOK, MONO_R)}"/>']
    for cx, cy, r in (MONO_DOT_BIG, MONO_DOT_SMALL):
        glyph.append(f'    <circle cx="{cx}" cy="{cy}" r="{r}"/>')
    body = "\n".join(glyph)
    k = MONO_TARGET_H / 593.0
    tx = (CANVAS - 491 * k) / 2 - 268 * k
    ty = (CANVAS - MONO_TARGET_H) / 2 - 224 * k
    return SVG_HEAD + f'''  <!-- Literature alternate mark - generated by scripts/build_icons.py
       Bold "L" whose foot reads as a book (spine, page slot, fore-edge)
       plus two dots. Monochrome, sits on the same macOS squircle body. -->
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{TILE_MONO_TOP}"/><stop offset="1" stop-color="{TILE_MONO_BOTTOM}"/>
    </linearGradient>
  </defs>
  <path d="{d_of(squircle(BODY, BODY["n"], 192))}" fill="url(#tile)"/>
  <mask id="pageSlot">
    <rect width="1024" height="1024" fill="#fff"/>
    <rect x="{MONO_SLOT["x"]}" y="{MONO_SLOT["y"]}" width="{MONO_SLOT["w"]}" height="{MONO_SLOT["h"]}"
          rx="{MONO_SLOT_R}" fill="#000"
          transform="matrix(1 0 {MONO_SLOT_SKEW} 1 {-MONO_SLOT_SKEW * MONO_SLOT["y"]:.1f} 0)"/>
  </mask>
  <g fill="{hexof(INK)}" transform="translate({tx:.1f} {ty:.1f}) scale({k:.4f})">
{body}
  </g>
</svg>
'''


def svg_mark() -> str:
    ink = _mark_ink(1.0)
    buf = io.BytesIO()
    ink.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    x, y = (CANVAS - ink.size[0]) // 2, (CANVAS - ink.size[1]) // 2
    return SVG_HEAD + f'''  <!-- Literature app icon - mark from assets/brand/monogram.png -->
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{TILE_MONO_TOP}"/><stop offset="1" stop-color="{TILE_MONO_BOTTOM}"/>
    </linearGradient>
  </defs>
  <path d="{d_of(squircle(BODY, BODY["n"], 192))}" fill="url(#tile)"/>
  <image x="{x}" y="{y}" width="{ink.size[0]}" height="{ink.size[1]}" href="data:image/png;base64,{data}"/>
</svg>
'''


SVGS = dict(cards=svg_cards, monogram=svg_monogram, mark=svg_mark)


# ------------------------------------------------------------------ outputs ---
def write_pngs(master: Image.Image, outdir: str, sizes=(16, 32, 48, 64, 128, 256, 512, 1024)):
    written = {}
    for px in sizes:
        im = master.resize((px, px), Image.LANCZOS)
        if px == 1024:
            name = "icon.png"
        elif px in (32, 128, 256):
            name = f"{px}x{px}.png"
        elif px == 512:
            name = "icon-512.png"
        else:
            continue
        path = os.path.join(outdir, name)
        im.save(path)
        written[px] = path
    # Tauri's default template also expects these names
    master.resize((128, 128), Image.LANCZOS).save(os.path.join(outdir, "128x128.png"))
    master.resize((256, 256), Image.LANCZOS).save(os.path.join(outdir, "128x128@2x.png"))
    return written


def write_icns(master: Image.Image, outdir: str, icns_name="icon.icns"):
    tmp = os.path.join(ROOT, "tmp", "iconset")
    iconset = os.path.join(tmp, "icon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for pts in (16, 32, 128, 256, 512):
        im = master.resize((pts, pts), Image.LANCZOS)
        im.save(os.path.join(iconset, f"icon_{pts}x{pts}.png"))
        master.resize((pts * 2, pts * 2), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{pts}x{pts}@2x.png"))
    out = os.path.join(outdir, icns_name)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    return out


def write_ico(master: Image.Image, outdir: str, ico_name="icon.ico"):
    out = os.path.join(outdir, ico_name)
    master.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
    return out


def favicon_assets(master: Image.Image, outdir: str, svg: str | None = None):
    os.makedirs(outdir, exist_ok=True)
    if svg:
        with open(os.path.join(outdir, "favicon.svg"), "w") as fh:
            fh.write(svg)
    master.resize((180, 180), Image.LANCZOS).save(
        os.path.join(outdir, "apple-touch-icon.png"))
    master.resize((32, 32), Image.LANCZOS).save(os.path.join(outdir, "favicon-32x32.png"))
    master.resize((16, 16), Image.LANCZOS).save(os.path.join(outdir, "favicon-16x16.png"))
    master.save(os.path.join(outdir, "favicon.ico"),
                sizes=[(16, 16), (32, 32), (48, 48)])


def lockup(mark: Image.Image):
    """Horizontal lockup: app mark + "Litera" wordmark, for the README."""
    scale = 0.34
    size = 1024
    mark_small = mark.resize((int(size * scale), int(size * scale)), Image.LANCZOS)
    gap, pad = 60, 80
    font = None
    for candidate in ("/System/Library/Fonts/SFNS.ttf",
                      "/System/Library/Fonts/Helvetica.ttc",
                      "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, 240)
                break
            except OSError:
                continue
    if font is None:
        return None, None
    text = "Litera"
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tw = probe.textlength(text, font=font)
    W = pad * 2 + mark_small.size[0] + gap + int(tw)
    H = pad * 2 + mark_small.size[1]
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    canvas.alpha_composite(mark_small, (pad, pad))
    d = ImageDraw.Draw(canvas)
    box = d.textbbox((0, 0), text, font=font)
    d.text((pad + mark_small.size[0] + gap - box[0],
            (H - (box[3] - box[1])) // 2 - box[1]), text, font=font, fill=(17, 17, 17, 255))
    return canvas, font


# --------------------------------------------------------------------- main ---
def emit(name: str, outdir: str, with_favicon=False, with_lockup=False):
    master = RENDERERS[name](SS * CANVAS).resize((CANVAS, CANVAS), Image.LANCZOS)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "icon.svg"), "w") as fh:
        fh.write(SVGS[name]())
    pngs = write_pngs(master, outdir)
    icns = write_icns(master, outdir)
    ico = write_ico(master, outdir)
    result = dict(master=master, pngs=pngs, icns=icns, ico=ico)
    if with_favicon:
        favicon_assets(master, os.path.join(ROOT, "frontend/public"), SVGS[name]())
    if with_lockup:
        result["lockup"] = lockup(master)
    return result


def main():
    check = "--check" in sys.argv
    if check:
        prev = os.path.join(ROOT, "tmp", "icon-preview")
        os.makedirs(prev, exist_ok=True)
        for name in RENDERERS:
            RENDERERS[name](SS * CANVAS).resize((CANVAS, CANVAS), Image.LANCZOS).save(
                os.path.join(prev, f"{name}.png"))
            with open(os.path.join(prev, f"{name}.svg"), "w") as fh:
                fh.write(SVGS[name]())
        print("preview written to tmp/icon-preview/")
        return

    app = emit("mark", os.path.join(ROOT, "src-tauri/icons"),
               with_favicon=True, with_lockup=True)
    cards = emit("cards", os.path.join(ROOT, "assets/brand/cards"))
    alt = emit("monogram", os.path.join(ROOT, "assets/brand/monogram"))
    print("app icon  ->", os.path.join(ROOT, "src-tauri/icons"))
    print("  icns", os.path.getsize(app["icns"]), "bytes")
    print("  ico ", os.path.getsize(app["ico"]), "bytes")
    print("alternates ->", os.path.join(ROOT, "assets/brand"))
    lock = app.get("lockup", (None, None))[0]
    if lock is not None:
        lock_path = os.path.join(ROOT, "assets/brand/lockup.png")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock.save(lock_path)
        print("lockup    ->", lock_path, lock.size)


if __name__ == "__main__":
    main()
