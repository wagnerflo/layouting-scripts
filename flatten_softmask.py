#!/usr/bin/env python3
"""
flatten_softmask.py
===================

Flatten PDF transparency for the common case:
*opaque images that each carry an 8-bit /SMask (soft alpha), drawn over a
backdrop and/or stacked on top of one another.*

The output is an equivalent PDF in which those images have been pre-composited
into OPAQUE images (the soft mask is baked in and removed), so the file no
longer relies on the PDF 1.4 transparency model. This is the kind of file a
PDF/X-1a (PDF 1.3) workflow accepts.

How it works (per page, top image first):
  1. Parse the page content stream, tracking the graphics state (q/Q + cm) so
     we know the affine placement (CTM) of every image draw.
  2. For each image that has a soft mask, build its backdrop and alpha-over
     composite it into an OPAQUE image, then delete /SMask.

Two backdrop modes:
  * "image" (default): the backdrop is built by compositing the LOWER IMAGES
    in the target's own colour space and pixel grid -- warped into place with
    the same affine maths. Nothing is rendered and no colour conversion
    happens, so CMYK images stay in CMYK. This is exact for the
    images-stacked-on-each-other case and matches the PDF transparency model,
    which defines "over" per channel in the blending colour space.
  * "render": the backdrop is rasterised with poppler. This captures vector,
    gradient and text content beneath the image, but CMYK is round-tripped
    through RGB. Use it when a soft-masked image sits over non-image art and
    the fringe accuracy matters more than exact CMYK values.

In both modes a 1-bit stencil /Mask is emitted for fully-transparent pixels
(allowed in PDF/X-1a), so whatever lies under those areas -- including vector
and text -- shows through untouched; only the soft fringe is ever baked.

Scope / limitations (documented honestly):
  * Images drawn inside form XObjects (some software nests everything in
    forms) are caught by a final sweep and flattened in isolation over the flat
    --bg colour, regardless of nesting depth or reuse across pages. Their
    opaque core and silhouette are exact; only the soft fringe is composited
    over --bg rather than the actual local backdrop. Top-level page-content
    images still get the full placement-aware backdrop treatment.
  * Axis-aligned, flipped, rotated and skewed placement are all supported:
    the backdrop is sampled through the image's affine transform. Only
    degenerate (zero-area) placements are skipped.
  * Top-level page content only; images drawn inside nested form XObjects are
    reported and skipped.
  * Blend modes other than Normal are not modelled (alpha-over only).
  * /Matte (pre-multiplied soft masks) is detected and reported, not undone.
  * Compositing happens in the image's own colour space. In the default
    "image" backdrop mode there is NO colour conversion: CMYK stays CMYK and
    foreground pixels keep their exact channel values. The flat --bg colour
    (default paper white) stands in for any non-image content in the
    partial-alpha fringe; fully-transparent areas are cut out via the stencil
    so real vector/gradient content shows through. In "render" mode the
    backdrop is rasterised by poppler as RGB, so CMYK is round-tripped through
    RGB (pass --icc for a managed conversion).
  * Region rasterisation: where an image's bounding box is transparent, the
    backdrop showing through is baked into the (now opaque) image. Visually
    identical, but any vector/text under that transparent area becomes raster.

Usage:
    python3 flatten_softmask.py in.pdf out.pdf [--icc profile.icc] [--maxdpi 1200]
"""

import argparse
import io
import math
import os
import subprocess
import sys
import tempfile
import zlib

import pikepdf
from pikepdf import Name, Operator, ContentStreamInstruction, Dictionary, Stream
from PIL import Image, ImageCms

Image.MAX_IMAGE_PIXELS = None  # large print images are expected


# ----------------------------------------------------------------------------
# 3x3 affine matrix helpers (PDF row-vector convention)
#   matrix = (a, b, c, d, e, f)  ->  [x' y'] = [x y 1] * [[a b 0],[c d 0],[e f 1]]
# ----------------------------------------------------------------------------
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1, m2):
    """Return m1 concatenated with m2 (m1 applied first), PDF `cm` semantics."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


# ----------------------------------------------------------------------------
# Content-stream walk: find soft-masked image draws + their CTMs
# ----------------------------------------------------------------------------
class Target:
    def __init__(self, index, name, ctm, is_softmask):
        self.index = index          # instruction index of the `Do`
        self.name = name            # XObject resource name, e.g. '/Im0'
        self.ctm = ctm              # CTM in effect at the Do
        self.is_softmask = is_softmask


def find_softmask_images(page):
    """Return (instructions, images, skipped) for one page's top-level content.
    `images` is every image draw in painting order (each a Target with an
    is_softmask flag), so a soft-masked image can use the images beneath it as
    its native backdrop."""
    instructions = list(pikepdf.parse_content_stream(page))
    try:
        xobjects = page.Resources.XObject
    except (KeyError, AttributeError):
        return instructions, [], []

    ctm = IDENTITY
    stack = []
    images = []
    skipped = []

    for idx, instr in enumerate(instructions):
        op = str(instr.operator)
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm":
            m = tuple(float(o) for o in instr.operands)
            ctm = mat_mul(m, ctm)
        elif op == "Do":
            name = str(instr.operands[0])
            xobj = xobjects.get(name)
            if xobj is None:
                continue
            subtype = str(xobj.get("/Subtype"))
            if subtype == "/Image":
                sm = xobj.get("/SMask")
                is_sm = sm is not None and str(sm) != "/None"
                images.append(Target(idx, name, ctm, is_sm))
            elif subtype == "/Form":
                grp = xobj.get("/Group")
                if grp is not None:
                    skipped.append((name, "form XObject (not recursed)"))
    return instructions, images, skipped


def device_rect(ctm):
    """Bounding rect (in PDF points) of the unit image square under ctm,
    plus an axis-aligned flag."""
    corners = [apply(ctm, 0, 0), apply(ctm, 1, 0), apply(ctm, 1, 1), apply(ctm, 0, 1)]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    a, b, c, d, e, f = ctm
    axis_aligned = abs(b) < 1e-4 and abs(c) < 1e-4
    return (min(xs), min(ys), max(xs), max(ys)), axis_aligned


def warp_coeffs(mt, wt, ht, ml, wl, hl):
    """PIL AFFINE coefficients (A..F) that map an output pixel (col,row) in the
    TARGET image grid (size wt x ht, placed by ctm `mt`) to the input pixel in a
    LOWER image grid (size wl x hl, placed by ctm `ml`). Returns None if `ml`
    is degenerate. Built by evaluating the exact affine chain at three points:
        target pixel -> unit square -> device (mt) -> unit square (ml^-1)
                     -> lower-image pixel.
    """
    al, bl, cl, dl, el, fl = ml
    det = al * dl - cl * bl
    if abs(det) < 1e-12:
        return None

    def map_pt(xt, yt):
        u = (xt + 0.5) / wt
        v = 1.0 - (yt + 0.5) / ht
        X, Y = apply(mt, u, v)                 # device point
        dx, dy = X - el, Y - fl
        ul = (dl * dx - cl * dy) / det          # unit coords in lower image
        vl = (-bl * dx + al * dy) / det
        xl = ul * wl - 0.5
        yl = (1.0 - vl) * hl - 0.5
        return xl, yl

    x00, y00 = map_pt(0, 0)
    x10, y10 = map_pt(1, 0)
    x01, y01 = map_pt(0, 1)
    return (x10 - x00, x01 - x00, x00, y10 - y00, y01 - y00, y00)


def _full_alpha(size):
    return Image.new("L", size, 255)


def build_backdrop_native(xobjects, target, lower_images, out_mode, out_size, bg_tuple):
    """Build the backdrop beneath `target`, in the target's own pixel grid and
    in its native colour space, by compositing the lower images (in paint
    order) -- no rendering, no colour conversion. Non-image content (vector,
    gradients, text) is NOT captured here; the flat `bg_tuple` stands in for it
    and the 1-bit stencil preserves it wherever the target is fully transparent.
    """
    canvas = Image.new(out_mode, out_size, bg_tuple)
    wt, ht = out_size
    fill = bg_tuple
    for low in lower_images:
        lx = xobjects.get(low.name)
        if lx is None:
            continue
        try:
            lcol = pikepdf.PdfImage(lx).as_pil_image()
        except Exception:
            continue
        if lcol.mode != out_mode:
            lcol = lcol.convert(out_mode)
        coeffs = warp_coeffs(target.ctm, wt, ht, low.ctm, lcol.width, lcol.height)
        if coeffs is None:
            continue
        col_w = lcol.transform(out_size, Image.AFFINE, coeffs,
                               resample=Image.BILINEAR, fillcolor=fill)
        if low.is_softmask:
            la = pikepdf.PdfImage(lx.SMask).as_pil_image().convert("L")
            if la.size != lcol.size:
                la = la.resize(lcol.size, Image.LANCZOS)
        else:
            la = _full_alpha(lcol.size)
        a_w = la.transform(out_size, Image.AFFINE, coeffs,
                           resample=Image.BILINEAR, fillcolor=0)
        canvas = Image.composite(col_w, canvas, a_w)
    return canvas


# ----------------------------------------------------------------------------
# Backdrop rendering: render the content BENEATH a given instruction index
# ----------------------------------------------------------------------------
def q_depth(instructions):
    d = 0
    for instr in instructions:
        op = str(instr.operator)
        if op == "q":
            d += 1
        elif op == "Q":
            d -= 1
    return max(d, 0)


def render_backdrop(src_path, page_index, prefix, mediabox, ctm, fg_w, fg_h, maxdpi):
    """Render `prefix` (the content beneath the target image) and resample it
    into the image's OWN pixel grid, following the image's affine placement
    `ctm`. This handles axis-aligned, flipped, rotated and skewed placement
    uniformly, because we sample the backdrop through the exact transform that
    maps each image pixel to device space.
    Returns an RGB PIL image of size (fg_w, fg_h)."""
    # Rebalance graphics state so the truncated stream is well-formed.
    prefix = list(prefix) + [ContentStreamInstruction([], Operator("Q"))] * q_depth(prefix)
    data = pikepdf.unparse_content_stream(prefix)

    pdf = pikepdf.open(src_path)
    page = pdf.pages[page_index]
    page.Contents = pdf.make_stream(data)
    tmp = tempfile.mktemp(suffix=".pdf")
    pdf.save(tmp)
    pdf.close()

    mb_llx, mb_lly, mb_urx, mb_ury = [float(v) for v in mediabox]
    a, b, c, d, e, f = ctm
    W, H = fg_w, fg_h

    # Device-space bounding box of the (possibly rotated) image quad.
    corners = [apply(ctm, 0, 0), apply(ctm, 1, 0), apply(ctm, 1, 1), apply(ctm, 0, 1)]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    # Render DPI from the image's own edge resolution (length of each image
    # axis in device space), so the warp has ~1 backdrop sample per image pixel.
    Lu = math.hypot(a, b)   # device length of the image's width axis (points)
    Lv = math.hypot(c, d)   # device length of the image's height axis (points)
    ppi_u = fg_w * 72.0 / Lu if Lu > 1e-6 else 72.0
    ppi_v = fg_h * 72.0 / Lv if Lv > 1e-6 else 72.0
    dpi = max(min(max(ppi_u, ppi_v), maxdpi), 1.0)
    scale = dpi / 72.0

    # Crop the render to the image's device bounding box (clamped to page).
    cl = max(int(math.floor((x0 - mb_llx) * scale)), 0)
    ct = max(int(math.floor((mb_ury - y1) * scale)), 0)
    wpx = int(math.ceil((x1 - x0) * scale)) + 2
    hpx = int(math.ceil((y1 - y0) * scale)) + 2

    out_prefix = tempfile.mktemp()
    cmd = [
        "pdftoppm", "-png", "-r", f"{dpi:.4f}",
        "-x", str(cl), "-y", str(ct), "-W", str(wpx), "-H", str(hpx),
        "-f", str(page_index + 1), "-l", str(page_index + 1), "-singlefile",
        tmp, out_prefix,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    png = out_prefix + ".png"
    backdrop_dev = Image.open(png).convert("RGB")

    # Affine mapping: output pixel (col,row) in image space -> input pixel in
    # the device-space backdrop crop. Image pixel (col,row) -> unit square
    # (u,v) = ((col+.5)/W, 1-(row+.5)/H) -> device (X,Y) via ctm -> crop pixel.
    Xc = a * 0.5 / W + c * (1 - 0.5 / H) + e
    Yc = b * 0.5 / W + d * (1 - 0.5 / H) + f
    A = scale * a / W
    B = scale * (-c / H)
    C = scale * Xc - mb_llx * scale - cl
    D = -scale * b / W
    E = scale * d / H
    F = -scale * Yc + scale * mb_ury - ct

    backdrop = backdrop_dev.transform(
        (W, H), Image.AFFINE, (A, B, C, D, E, F),
        resample=Image.BILINEAR, fillcolor=(255, 255, 255),
    )

    for p in (tmp, png):
        try:
            os.remove(p)
        except OSError:
            pass
    return backdrop


# ----------------------------------------------------------------------------
# Colour conversion for the backdrop
# ----------------------------------------------------------------------------
def make_rgb_to_cmyk(icc_path):
    if not icc_path:
        return lambda im: im.convert("CMYK")
    srgb = ImageCms.createProfile("sRGB")
    out = ImageCms.getOpenProfile(icc_path)
    tfm = ImageCms.buildTransform(srgb, out, "RGB", "CMYK",
                                  renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
    return lambda im: ImageCms.applyTransform(im, tfm)


# ----------------------------------------------------------------------------
# Main per-image flatten
# ----------------------------------------------------------------------------
def _write_opaque_image(pdf, xobj, out_img, alpha_arr, ncomp, no_stencil):
    """Write composited opaque pixels back into an image XObject, add a 1-bit
    stencil for fully-transparent pixels, and strip transparency keys."""
    import numpy as np
    raw = out_img.tobytes()
    xobj.write(zlib.compress(raw, 6), filter=Name.FlateDecode)
    xobj.Width = out_img.width
    xobj.Height = out_img.height
    xobj.BitsPerComponent = 8
    stencil = None
    if not no_stencil and bool((alpha_arr == 0).any()):
        masked = (alpha_arr == 0).astype(np.uint8)
        packed = np.packbits(masked, axis=1)
        stencil = pdf.make_stream(b"")
        stencil.write(zlib.compress(packed.tobytes(), 6), filter=Name.FlateDecode)
        stencil.Type = Name.XObject
        stencil.Subtype = Name.Image
        stencil.Width = out_img.width
        stencil.Height = out_img.height
        stencil.ImageMask = True
        stencil.BitsPerComponent = 1
    if xobj.get("/ColorSpace") is None:
        xobj.ColorSpace = {4: Name.DeviceCMYK, 3: Name.DeviceRGB,
                           1: Name.DeviceGray}[ncomp]
    for k in ("/SMask", "/Mask", "/Decode", "/Matte", "/SMaskInData"):
        if k in xobj:
            del xobj[k]
    if stencil is not None:
        xobj.Mask = stencil


def _bg_tuple(work, bg):
    bgi = [int(round(x)) for x in bg] if bg else None
    if work == "CMYK":
        return tuple(bgi) if bgi else (0, 0, 0, 0)
    if work == "RGB":
        return tuple(bgi) if bgi else (255, 255, 255)
    return (bgi[0] if bgi else 255)


def flatten_isolated(pdf, xobj, bg, no_stencil):
    """Flatten one soft-masked image XObject in isolation: composite it over a
    flat background colour (with a 1-bit stencil for the fully-transparent
    pixels) and remove /SMask. Placement-independent, so it works no matter how
    deeply the image is nested in form XObjects and is safe for images reused on
    several pages. The opaque core and the cut-out silhouette are exact; only
    the soft fringe is composited over the flat background."""
    import numpy as np
    fg = pikepdf.PdfImage(xobj).as_pil_image()
    if fg.mode == "CMYK":
        work, ncomp = "CMYK", 4
    elif fg.mode == "RGB":
        work, ncomp = "RGB", 3
    elif fg.mode == "L":
        work, ncomp = "L", 1
    else:
        fg = fg.convert("RGB")
        work, ncomp = "RGB", 3
    alpha = pikepdf.PdfImage(xobj.SMask).as_pil_image().convert("L")
    if alpha.size != fg.size:
        alpha = alpha.resize(fg.size, Image.LANCZOS)
    canvas = Image.new(work, fg.size, _bg_tuple(work, bg))
    out_img = Image.composite(fg, canvas, alpha)
    _write_opaque_image(pdf, xobj, out_img, np.asarray(alpha), ncomp, no_stencil)
    return fg.size, work


def flatten(in_path, out_path, icc_path=None, maxdpi=1200.0, no_stencil=False,
            backdrop_mode="image", bg=None, verbose=True):
    rgb_to_cmyk = make_rgb_to_cmyk(icc_path)
    pdf = pikepdf.open(in_path)
    total_flattened = 0
    report = []

    for page_index, page in enumerate(pdf.pages):
        instructions, images, skipped = find_softmask_images(page)
        for name, why in skipped:
            report.append(f"  page {page_index+1}: skipped {name} ({why})")
        targets = [im for im in images if im.is_softmask]
        if not targets:
            continue

        mediabox = page.MediaBox
        xobjects = page.Resources.XObject

        # Process top image first so that, in native-CMYK mode, the lower images
        # used as a backdrop are still in their original (un-flattened) state.
        for t in sorted(targets, key=lambda x: -x.index):
            xobj = xobjects.get(t.name)
            a, b, c, d, e, f = t.ctm
            if abs(a * d - b * c) < 1e-9:
                report.append(f"  page {page_index+1}: skipped {t.name} "
                              f"(degenerate/zero-area placement)")
                continue
            rotated = abs(b) > 1e-4 or abs(c) > 1e-4

            try:
                fg = pikepdf.PdfImage(xobj).as_pil_image()
            except Exception as ex:
                report.append(f"  page {page_index+1}: skipped {t.name} "
                              f"(cannot decode image: {ex})")
                continue

            # Normalise foreground mode.
            if fg.mode == "CMYK":
                work, ncomp = "CMYK", 4
            elif fg.mode == "RGB":
                work, ncomp = "RGB", 3
            elif fg.mode == "L":
                work, ncomp = "L", 1
            else:
                fg = fg.convert("RGB")
                work, ncomp = "RGB", 3
            mode = fg.mode

            sm = xobj.SMask
            if sm.get("/Matte") is not None:
                report.append(f"  page {page_index+1}: {t.name} has /Matte "
                              f"(pre-multiplied) - baking without un-matte")
            alpha = pikepdf.PdfImage(sm).as_pil_image().convert("L")
            if alpha.size != fg.size:
                alpha = alpha.resize(fg.size, Image.LANCZOS)

            fg_w, fg_h = fg.size

            # Default flat backdrop colour for the partial-alpha fringe / paper.
            bgi = [int(round(x)) for x in bg] if bg else None
            if work == "CMYK":
                bg_tuple = tuple(bgi) if bgi else (0, 0, 0, 0)
            elif work == "RGB":
                bg_tuple = tuple(bgi) if bgi else (255, 255, 255)
            else:
                bg_tuple = (bgi[0] if bgi else 255)

            if backdrop_mode == "image":
                # Native: composite the LOWER IMAGES in the target's own colour
                # space and grid. No rendering, no colour conversion.
                lower = [im for im in images if im.index < t.index]
                backdrop = build_backdrop_native(xobjects, t, lower, work,
                                                 (fg_w, fg_h), bg_tuple)
                out_img = Image.composite(fg, backdrop, alpha)
            else:
                # Render-based backdrop (captures vector/gradient/text), but
                # CMYK goes through an RGB round-trip.
                prefix = instructions[: t.index]
                backdrop = render_backdrop(in_path, page_index, prefix, mediabox,
                                           t.ctm, fg_w, fg_h, maxdpi)
                if work == "CMYK":
                    out_img = Image.composite(fg, rgb_to_cmyk(backdrop), alpha)
                elif work == "RGB":
                    out_img = Image.composite(fg, backdrop, alpha)
                else:
                    out_img = Image.composite(fg, backdrop.convert("L"), alpha)

            raw = out_img.tobytes()
            xobj.write(zlib.compress(raw, 6), filter=Name.FlateDecode)
            xobj.Width = out_img.width
            xobj.Height = out_img.height
            xobj.BitsPerComponent = 8

            # Build a 1-bit stencil for fully-transparent pixels so vector/text
            # under those areas is NOT rasterised. Allowed in PDF/X-1a.
            import numpy as np
            a_arr = np.asarray(alpha)
            stencil = None
            if not no_stencil and bool((a_arr == 0).any()):
                masked = (a_arr == 0).astype(np.uint8)        # 1 = do not paint
                packed = np.packbits(masked, axis=1)           # MSB-first, row padded
                stencil = pdf.make_stream(b"")
                stencil.write(zlib.compress(packed.tobytes(), 6), filter=Name.FlateDecode)
                stencil.Type = Name.XObject
                stencil.Subtype = Name.Image
                stencil.Width = out_img.width
                stencil.Height = out_img.height
                stencil.ImageMask = True
                stencil.BitsPerComponent = 1
            # Preserve the original colour space object when component count
            # matches (keeps ICCBased CMYK intent); else fall back to Device*.
            keep_cs = False
            orig_cs = xobj.get("/ColorSpace")
            if orig_cs is not None:
                keep_cs = True  # channel values unchanged; same space is correct
            if not keep_cs:
                xobj.ColorSpace = {4: Name.DeviceCMYK, 3: Name.DeviceRGB,
                                   1: Name.DeviceGray}[ncomp]

            for k in ("/SMask", "/Mask", "/Decode", "/Matte", "/SMaskInData"):
                if k in xobj:
                    del xobj[k]
            if stencil is not None:
                xobj.Mask = stencil

            total_flattened += 1
            report.append(f"  page {page_index+1}: flattened {t.name} "
                          f"({fg_w}x{fg_h}, {mode}"
                          f"{', rotated/skewed' if rotated else ''})")

    # Catch-all sweep: any soft-masked image XObject the page-content pass did
    # not reach -- typically images drawn INSIDE form XObjects (some software
    # nests all content in forms) -- is flattened in isolation here. The
    # page-content pass already removed /SMask from what it handled, so this
    # only sees the leftovers. De-duplicated by object, so an image reused on
    # several pages is flattened once.
    leftovers = []
    seen = set()
    for o in pdf.objects:
        if not isinstance(o, (Dictionary, Stream)):
            continue
        if str(o.get("/Subtype")) != "/Image":
            continue
        sm = o.get("/SMask")
        if sm is None or str(sm) == "/None":
            continue
        if o.objgen in seen:
            continue
        seen.add(o.objgen)
        leftovers.append(o.objgen)

    swept = 0
    for og in leftovers:
        xobj = pdf.get_object(og)
        try:
            size, work = flatten_isolated(pdf, xobj, bg, no_stencil)
            swept += 1
            total_flattened += 1
            report.append(f"  sweep: flattened nested/other image {og} "
                          f"({size[0]}x{size[1]}, {work}, isolated)")
        except Exception as ex:
            report.append(f"  sweep: skipped image {og} ({ex})")
    if swept:
        report.append(f"  sweep flattened {swept} soft-masked image(s) not in page content")

    # PDF 1.3 has no live transparency; advertise that level on output.
    pdf.save(out_path, min_version="1.3")
    pdf.close()

    if verbose:
        print(f"Flattened {total_flattened} soft-masked image(s).")
        for line in report:
            print(line)
    return total_flattened


def main():
    ap = argparse.ArgumentParser(description="Flatten soft-masked images in a PDF.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--backdrop", choices=["image", "render"], default="image",
                    help="'image' (default): native-colour-space compositing of "
                         "the lower images, no conversion. 'render': rasterise "
                         "the backdrop with poppler (captures vector/gradient/"
                         "text but round-trips CMYK through RGB).")
    ap.add_argument("--bg", default=None,
                    help="flat backdrop colour for the fringe/paper, space-"
                         "separated channel values 0-255 in the image's colour "
                         "space (e.g. CMYK '0 0 0 0' = white).")
    ap.add_argument("--icc", default=None,
                    help="CMYK ICC profile for backdrop conversion (render mode)")
    ap.add_argument("--maxdpi", type=float, default=1200.0)
    ap.add_argument("--no-stencil", action="store_true",
                    help="do not emit a 1-bit mask for fully-transparent pixels")
    args = ap.parse_args()
    bg = [float(x) for x in args.bg.split()] if args.bg else None
    flatten(args.input, args.output, args.icc, args.maxdpi, args.no_stencil,
            args.backdrop, bg)


if __name__ == "__main__":
    main()
