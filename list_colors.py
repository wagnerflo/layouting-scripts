#!/usr/bin/env python3
"""
List every colour used by TEXT and VECTOR graphics in a PDF (images are
ignored). Read-only: the PDF is never modified.

It walks page content streams, Form XObjects (recursively) and annotation
appearance streams, tracks the graphics-state colour-space stack (q/Q) and
records the operands of the colour operators:

    g/G  rg/RG  k/K            device gray / rgb / cmyk
    cs/CS + sc/SC/scn/SCN      colour in a named colour space
                               (ICCBased, Separation, DeviceN, Lab, Indexed,
                                CalRGB/CalGray, Pattern)
    sh                         shadings (reported as used, not enumerated)

With --output-icc it additionally shows, for each colour, the equivalent in
a target ICC profile (littleCMS / Pillow). Sources, mirroring the image
script: DeviceCMYK -> the PDF's OutputIntent profile (or --source-cmyk);
DeviceRGB -> sRGB (or --source-rgb); ICCBased -> its embedded profile.
DeviceGray without --source-gray falls back to a documented device mapping.
Separation/DeviceN/Indexed/Lab/Pattern are reported but not numerically
converted. The conversion is printed only; nothing is written back.
"""
import argparse
import io
import os
import sys
from collections import OrderedDict

import pikepdf
from PIL import Image, ImageCms

INTENTS = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "relative": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "saturation": ImageCms.Intent.SATURATION,
    "absolute": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}
DEVICE = {"/DeviceGray": ("gray", 1), "/DeviceRGB": ("rgb", 3),
          "/DeviceCMYK": ("cmyk", 4)}
MODE = {"gray": "L", "rgb": "RGB", "cmyk": "CMYK"}


# --------------------------------------------------------------------------- #
#  colour-space resolution
# --------------------------------------------------------------------------- #
def resolve_cs(obj, resources, _depth=0):
    """Return a descriptor dict for a colour-space object."""
    try:
        if obj is None:
            return {"kind": "gray", "n": 1}
        name = str(obj)
        if name in DEVICE:
            k, n = DEVICE[name]
            return {"kind": k, "n": n}
        if name == "/Pattern":
            return {"kind": "pattern", "n": 0}
        # a named entry -> look up in /ColorSpace resources
        if isinstance(obj, pikepdf.Name):
            cs = resources.get("/ColorSpace")
            if cs is not None and name in cs:
                return resolve_cs(cs[name], resources, _depth + 1)
            return {"kind": "unknown", "n": 0, "name": name}
        # array-based colour spaces
        if isinstance(obj, pikepdf.Array) and len(obj) >= 1:
            family = str(obj[0])
            if family == "/ICCBased":
                stream = obj[1]
                n = int(stream.get("/N", 0)) or {1: 1, 3: 3, 4: 4}.get(0, 3)
                try:
                    prof = bytes(stream.read_bytes())
                except Exception:
                    prof = None
                kind = {1: "gray", 3: "rgb", 4: "cmyk"}.get(n, "rgb")
                return {"kind": "icc", "n": n, "profile": prof, "under": kind}
            if family == "/Separation":
                alt = resolve_cs(obj[2], resources, _depth + 1)
                return {"kind": "sep", "n": 1, "name": str(obj[1]), "alt": alt}
            if family == "/DeviceN":
                names = [str(x) for x in obj[1]]
                alt = resolve_cs(obj[2], resources, _depth + 1)
                return {"kind": "devicen", "n": len(names), "names": names, "alt": alt}
            if family == "/Indexed":
                base = resolve_cs(obj[1], resources, _depth + 1)
                return {"kind": "indexed", "n": 1, "base": base,
                        "hival": int(obj[2])}
            if family in ("/CalRGB",):
                return {"kind": "calrgb", "n": 3}
            if family in ("/CalGray",):
                return {"kind": "calgray", "n": 1}
            if family == "/Lab":
                return {"kind": "lab", "n": 3}
            if family == "/Pattern":
                return {"kind": "pattern", "n": 0}
    except Exception:
        pass
    return {"kind": "unknown", "n": 0}


# --------------------------------------------------------------------------- #
#  content-stream walker
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self):
        self.colors = OrderedDict()   # key -> record
        self.streams = 0
        self.shadings = 0

    def add(self, kind, comps, info, is_fill):
        comps = tuple(round(float(c), 5) for c in comps)
        key = (kind, comps, info.get("name") if info else None)
        rec = self.colors.get(key)
        if rec is None:
            rec = {"kind": kind, "comps": comps, "info": info or {},
                   "fill": 0, "stroke": 0}
            self.colors[key] = rec
        rec["fill" if is_fill else "stroke"] += 1


def numbers_and_names(operands):
    nums, names = [], []
    for o in operands:
        if isinstance(o, pikepdf.Name):
            names.append(str(o))
        else:
            try:
                nums.append(float(o))
            except Exception:
                pass
    return nums, names


def walk(stream, resources, coll, seen, depth=0):
    coll.streams += 1
    fill_cs = {"kind": "gray", "n": 1}
    stroke_cs = {"kind": "gray", "n": 1}
    stack = []
    try:
        instructions = pikepdf.parse_content_stream(stream)
    except Exception:
        return
    for ins in instructions:
        try:
            op = str(ins.operator)
        except Exception:
            continue                                   # inline image etc.
        ops = ins.operands

        if op == "q":
            stack.append((fill_cs, stroke_cs))
        elif op == "Q":
            if stack:
                fill_cs, stroke_cs = stack.pop()
        elif op == "g":
            coll.add("gray", [float(ops[0])], None, True); fill_cs = {"kind": "gray", "n": 1}
        elif op == "G":
            coll.add("gray", [float(ops[0])], None, False); stroke_cs = {"kind": "gray", "n": 1}
        elif op == "rg":
            coll.add("rgb", [float(x) for x in ops[:3]], None, True); fill_cs = {"kind": "rgb", "n": 3}
        elif op == "RG":
            coll.add("rgb", [float(x) for x in ops[:3]], None, False); stroke_cs = {"kind": "rgb", "n": 3}
        elif op == "k":
            coll.add("cmyk", [float(x) for x in ops[:4]], None, True); fill_cs = {"kind": "cmyk", "n": 4}
        elif op == "K":
            coll.add("cmyk", [float(x) for x in ops[:4]], None, False); stroke_cs = {"kind": "cmyk", "n": 4}
        elif op == "cs":
            fill_cs = resolve_cs(ops[0], resources)
        elif op == "CS":
            stroke_cs = resolve_cs(ops[0], resources)
        elif op in ("sc", "scn", "SC", "SCN"):
            is_fill = op in ("sc", "scn")
            cs = fill_cs if is_fill else stroke_cs
            nums, names = numbers_and_names(ops)
            if names and not nums:
                coll.add("pattern", [], {"name": names[0]}, is_fill)
            elif nums:
                coll.add(cs["kind"], nums, cs, is_fill)
        elif op == "sh":
            coll.shadings += 1
        elif op == "Do":
            name = str(ops[0])
            xobj = resources.get("/XObject", {})
            obj = xobj.get(name) if xobj else None
            if (obj is not None and obj.get("/Subtype") == "/Form"
                    and obj.objgen not in seen):
                seen.add(obj.objgen)
                walk(obj, obj.get("/Resources", resources), coll, seen, depth + 1)


def walk_annotations(page, coll, seen):
    for annot in page.get("/Annots", []) or []:
        try:
            ap = annot.get("/AP", {}).get("/N")
        except Exception:
            ap = None
        if ap is None:
            continue
        streams = ap.values() if isinstance(ap, pikepdf.Dictionary) else [ap]
        for s in streams:
            try:
                if s.get("/Subtype") == "/Form" and s.objgen not in seen:
                    seen.add(s.objgen)
                    walk(s, s.get("/Resources", page.get("/Resources", pikepdf.Dictionary())), coll, seen)
            except Exception:
                continue


# --------------------------------------------------------------------------- #
#  optional ICC conversion
# --------------------------------------------------------------------------- #
class Converter:
    def __init__(self, pdf, output_icc, source_cmyk, source_rgb, source_gray,
                 intent, bpc):
        self.dst_bytes = open(output_icc, "rb").read()
        self.dst = ImageCms.getOpenProfile(io.BytesIO(self.dst_bytes))
        self.dst_space = self.dst.profile.xcolor_space.strip()
        self.out_mode = "CMYK" if "CMYK" in self.dst_space else "RGB"
        self.intent = INTENTS[intent]
        self.flags = ImageCms.Flags.HIGHRESPRECALC | (
            ImageCms.Flags.BLACKPOINTCOMPENSATION if bpc else 0)
        # source profiles
        cmyk_b = (open(source_cmyk, "rb").read() if source_cmyk
                  else output_intent_icc(pdf))
        self.cmyk = ImageCms.ImageCmsProfile(io.BytesIO(cmyk_b)) if cmyk_b else None
        rgb_b = open(source_rgb, "rb").read() if source_rgb else None
        self.rgb = (ImageCms.ImageCmsProfile(io.BytesIO(rgb_b)) if rgb_b
                    else ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")))
        self.gray = (ImageCms.ImageCmsProfile(io.BytesIO(open(source_gray, "rb").read()))
                     if source_gray else None)
        self.dst_name = os.path.basename(output_icc)
        self._t = {}
        self._icc = {}

    def _xf(self, src_prof, in_mode):
        key = (id(src_prof), in_mode, self.out_mode)
        if key not in self._t:
            self._t[key] = ImageCms.buildTransform(
                src_prof, self.dst, in_mode, self.out_mode,
                renderingIntent=self.intent, flags=self.flags)
        return self._t[key]

    def convert(self, rec):
        """Return (values_0_1, note) in target space, or (None, reason)."""
        kind, comps, info = rec["kind"], rec["comps"], rec["info"]
        src = mode = None
        if kind == "cmyk":
            src, mode = self.cmyk, "CMYK"
            if src is None:
                return None, "no CMYK source (OutputIntent missing; use --source-cmyk)"
        elif kind == "rgb":
            src, mode = self.rgb, "RGB"
        elif kind == "icc":
            prof = info.get("profile")
            if not prof:
                return None, "embedded ICC unreadable"
            pid = id(prof)
            if pid not in self._icc:
                self._icc[pid] = ImageCms.ImageCmsProfile(io.BytesIO(prof))
            src = self._icc[pid]
            mode = {1: "L", 3: "RGB", 4: "CMYK"}[info.get("n", 3)]
        elif kind == "gray":
            if self.gray is not None:
                src, mode = self.gray, "L"
            else:                                        # documented device fallback
                v = comps[0]
                if self.out_mode == "CMYK":
                    return [0.0, 0.0, 0.0, round(1 - v, 4)], "device gray->K"
                return [round(v, 4)] * 3, "device gray replicate"
        else:
            return None, "not converted (%s)" % kind

        pix = tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in comps)
        if mode == "L":
            pix = pix[0]
        img = Image.new(mode, (1, 1), pix)
        out = ImageCms.applyTransform(img, self._xf(src, mode)).getpixel((0, 0))
        if isinstance(out, int):
            out = (out,)
        return [round(v / 255, 4) for v in out], None


def output_intent_icc(pdf):
    try:
        return bytes(pdf.Root.OutputIntents[0].DestOutputProfile.read_bytes())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
def fmt(comps):
    return " ".join(f"{c:.3f}" for c in comps)


def main():
    ap = argparse.ArgumentParser(description="List text/vector colours in a PDF (images ignored); read-only.")
    ap.add_argument("pdf")
    ap.add_argument("--output-icc", metavar="ICC", help="target ICC -> also show converted values")
    ap.add_argument("--source-cmyk", metavar="ICC", help="source CMYK profile (default: PDF OutputIntent)")
    ap.add_argument("--source-rgb", metavar="ICC", help="source RGB profile (default: sRGB)")
    ap.add_argument("--source-gray", metavar="ICC", help="source Gray profile (default: device mapping)")
    ap.add_argument("--intent", choices=list(INTENTS), default="relative")
    ap.add_argument("--no-bpc", action="store_true")
    ap.add_argument("--include-annotations", action="store_true",
                    help="also scan annotation appearance streams")
    a = ap.parse_args()

    pdf = pikepdf.open(a.pdf)                              # read-only; never saved
    coll = Collector()
    seen = set()
    for page in pdf.pages:
        walk(page, page.get("/Resources", pikepdf.Dictionary()), coll, seen)
        if a.include_annotations:
            walk_annotations(page, coll, seen)

    conv = None
    if a.output_icc:
        conv = Converter(pdf, a.output_icc, a.source_cmyk, a.source_rgb,
                         a.source_gray, a.intent, not a.no_bpc)

    # ---- report ----
    print(f"PDF: {a.pdf}")
    print(f"scanned {coll.streams} content stream(s); "
          f"{len(coll.colors)} distinct text/vector colour(s)"
          + (f"; {coll.shadings} shading(s) used" if coll.shadings else ""))
    if conv:
        print(f"converting to: {conv.dst_name} ({conv.dst_space}), "
              f"intent={a.intent}, bpc={not a.no_bpc}")
    print("-" * 78)

    order = {"gray": 0, "rgb": 1, "cmyk": 2, "icc": 3, "sep": 4, "devicen": 5,
             "lab": 6, "indexed": 7, "calrgb": 8, "calgray": 9, "pattern": 10,
             "unknown": 11}
    for key in sorted(coll.colors, key=lambda k: (order.get(k[0], 99), k[1])):
        rec = coll.colors[key]
        kind = rec["kind"]
        label = kind.upper()
        if kind == "sep":
            label = f"SEPARATION '{rec['info'].get('name','?').strip('/')}'"
        elif kind == "icc":
            label = f"ICCBased(N={rec['info'].get('n')})"
        use = []
        if rec["fill"]:
            use.append(f"fill×{rec['fill']}")
        if rec["stroke"]:
            use.append(f"stroke×{rec['stroke']}")
        line = f"{label:22} [{fmt(rec['comps'])}]  ({', '.join(use)})"
        if conv:
            vals, note = conv.convert(rec)
            if vals is not None:
                line += f"   ->  [{fmt(vals)}]"
                if note:
                    line += f"  ({note})"
            else:
                line += f"   ->  (skipped: {note})"
        print(line)


if __name__ == "__main__":
    main()
