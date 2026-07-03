#!/usr/bin/env python3
"""
extract_icc.py — list / extract the embedded ICC profiles in a PDF.

Finds every distinct ICC profile attached to:
  * image colour spaces ([/ICCBased ...], including Indexed bases),
  * the OutputIntent(s) (DestOutputProfile),
  * named resource colour spaces and Separation/DeviceN alternates.

Profiles are de-duplicated by content (MD5), so a profile shared by 20 images
is reported once with a count and the list of places it is used.

By default it writes each distinct profile to an .icc file and prints a table.
With --md5 it prints only the checksums and does not write any files.

Usage:
    python3 extract_icc.py input.pdf [--outdir DIR]
    python3 extract_icc.py input.pdf --md5
"""
import argparse
import hashlib
import os
import struct
import sys
from collections import Counter

import pikepdf
from pikepdf import Name, Array, Dictionary, Stream


def icc_from_colorspace(cs):
    """Return the ICC profile stream for an ICCBased colour space (or the
    ICCBased base of an Indexed space), else None."""
    if isinstance(cs, Array) and len(cs) >= 2:
        head = str(cs[0])
        if head == "/ICCBased":
            return cs[1]
        if head == "/Indexed":
            return icc_from_colorspace(cs[1])
    return None


def parse_desc(data):
    """Best-effort read of the ICC 'desc' tag (v2 textDescription or v4 mluc)."""
    try:
        n = struct.unpack(">I", data[128:132])[0]
        for i in range(n):
            off = 132 + i * 12
            if data[off:off + 4] == b"desc":
                toff = struct.unpack(">I", data[off + 4:off + 8])[0]
                typ = data[toff:toff + 4]
                if typ == b"desc":                                  # ICC v2
                    cnt = struct.unpack(">I", data[toff + 8:toff + 12])[0]
                    return data[toff + 12:toff + 12 + cnt].split(b"\x00")[0].decode(
                        "latin1", "replace")
                if typ == b"mluc":                                  # ICC v4
                    rbase = toff + 16
                    length = struct.unpack(">I", data[rbase + 4:rbase + 8])[0]
                    soff = struct.unpack(">I", data[rbase + 8:rbase + 12])[0]
                    return data[toff + soff:toff + soff + length].decode(
                        "utf-16-be", "replace").strip("\x00")
    except Exception:
        pass
    return None


def icc_info(data):
    """Pull a few identifying fields from the 128-byte ICC header."""
    if len(data) < 132:
        return {"size": len(data), "space": "?", "cls": "?", "version": "?",
                "desc": None}
    return {
        "size": struct.unpack(">I", data[0:4])[0],
        "version": f"{data[8]}.{data[9] >> 4}",
        "cls": data[12:16].decode("latin1", "replace").strip(),
        "space": data[16:20].decode("latin1", "replace").strip(),
        "pcs": data[20:24].decode("latin1", "replace").strip(),
        "desc": parse_desc(data),
    }


def collect(pdf):
    """Return {md5: {data, sources(Counter)}} for every distinct ICC profile."""
    profiles = {}

    def add(stream, source):
        if stream is None:
            return
        try:
            data = bytes(stream.read_bytes())
        except Exception:
            return
        if not data:
            return
        h = hashlib.md5(data).hexdigest()
        rec = profiles.setdefault(h, {"data": data, "sources": Counter()})
        rec["sources"][source] += 1

    # images
    for o in pdf.objects:
        if isinstance(o, (Dictionary, Stream)) and str(o.get("/Subtype")) == "/Image":
            add(icc_from_colorspace(o.get("/ColorSpace")), "image")

    # output intents
    ois = pdf.Root.get("/OutputIntents")
    if ois is not None:
        for oi in ois:
            add(oi.get("/DestOutputProfile"), "OutputIntent")

    # named resource colour spaces + Separation/DeviceN alternates
    def scan_resources(res):
        if res is None:
            return
        csd = res.get("/ColorSpace")
        if csd is None:
            return
        for k in csd.keys():
            cs = csd[k]
            add(icc_from_colorspace(cs), "named-colorspace")
            if isinstance(cs, Array) and str(cs[0]) in ("/Separation", "/DeviceN") \
                    and len(cs) >= 3:
                add(icc_from_colorspace(cs[2]), "spot-alternate")

    for o in pdf.objects:
        if isinstance(o, (Dictionary, Stream)) and o.get("/Resources") is not None:
            scan_resources(o.Resources)
    for p in pdf.pages:
        try:
            scan_resources(p.Resources)
        except Exception:
            pass

    return profiles


def main():
    ap = argparse.ArgumentParser(description="Extract/list embedded ICC profiles in a PDF.")
    ap.add_argument("input")
    ap.add_argument("--md5", "-m", action="store_true",
                    help="only print MD5 checksums; do not write .icc files")
    ap.add_argument("--outdir", "-o", default=".",
                    help="directory for extracted .icc files (default: .)")
    args = ap.parse_args()

    pdf = pikepdf.open(args.input)
    profiles = collect(pdf)

    if not profiles:
        print("No embedded ICC profiles found.")
        return

    if not args.md5:
        os.makedirs(args.outdir, exist_ok=True)

    for h, rec in sorted(profiles.items(),
                         key=lambda kv: -sum(kv[1]["sources"].values())):
        info = icc_info(rec["data"])
        src = ", ".join(f"{s}\u00d7{n}" for s, n in rec["sources"].most_common())
        if args.md5:
            print(f"{h}  {info['size']:>8} B  {info['space']:<4}  ({src})")
        else:
            space = info["space"] or "ICC"
            fname = f"profile_{space.strip() or 'ICC'}_{h[:8]}.icc"
            path = os.path.join(args.outdir, fname)
            with open(path, "wb") as f:
                f.write(rec["data"])
            print(f"{fname}")
            print(f"   md5     : {h}")
            print(f"   size    : {info['size']} bytes")
            print(f"   class   : {info['cls']}   data space: {info['space']}   "
                  f"PCS: {info.get('pcs','?')}   v{info['version']}")
            if info["desc"]:
                print(f"   desc    : {info['desc']}")
            print(f"   used by : {src}")

    if not args.md5:
        print(f"\n{len(profiles)} distinct profile(s) written to {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    main()
