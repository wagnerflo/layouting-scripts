#!/usr/bin/env python3
"""finalize_pdfx.py — turn an already-transparency-flattened PDF into a valid
PDF/X-1a or PDF/X-3 file. One tool, selected with --target.

Both targets (structural work done for either):
  * remove transparency-group markers (/Group /S /Transparency) from the page
    and form XObjects -- neither X-1a nor X-3 allows live transparency;
  * clear any stray Catalog /Version override and write the correct PDF version
    with a classic xref table (no object/xref streams);
  * set the PDF/X identification in both DocInfo and XMP;
  * verify the OutputIntent (both standards require one) and warn -- rather than
    silently proceed -- if any soft masks, constant alpha, blend modes, or
    uncalibrated DeviceRGB remain (illegal in both; run the flattener first).

Difference between the targets -- colour:
  --target x1a : "blind"/device exchange. ICCBased CMYK/Gray colour spaces are
                 relabelled to DeviceCMYK/DeviceGray (a number-preserving
                 relabel; appearance-neutral when the profile matches the
                 OutputIntent). Sets GTS_PDFXVersion = PDF/X-1a:2001 and
                 GTS_PDFXConformance = PDF/X-1a:2001. Writes PDF 1.3.
  --target x3  : colour-managed exchange. ICC/CIE colour is allowed, so colour
                 spaces are left untouched. Sets GTS_PDFXVersion = PDF/X-3:2003
                 (or :2002) and removes GTS_PDFXConformance (X-1-family only).
                 Writes PDF 1.4 (2003) or PDF 1.3 (2002).

It never changes colour numbers, fonts, page geometry, or the OutputIntent.

Usage:
    python3 finalize_pdfx.py in.pdf out.pdf --target x1a
    python3 finalize_pdfx.py in.pdf out.pdf --target x3 [--year 2002|2003]
"""
import argparse
import io
import re
import sys
import pikepdf
from pikepdf import Name, Array, Dictionary, Stream, String

ICC_TO_DEV = {1: Name.DeviceGray, 3: Name.DeviceRGB, 4: Name.DeviceCMYK}


def read_input_bytes(path):
    """Read a PDF into bytes from a file, or from stdin when path is '-'."""
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def save_pdf(pdf, path, **save_kwargs):
    """Save to a file, or to stdout when path is '-' (buffered for pipes)."""
    if path == "-":
        buf = io.BytesIO()
        pdf.save(buf, **save_kwargs)
        sys.stdout.buffer.write(buf.getvalue())
        sys.stdout.buffer.flush()
    else:
        pdf.save(path, **save_kwargs)


def fix_cs(cs, warnings):
    """Relabel ICC/CIE colour spaces to their Device equivalent (for X-1a),
    recursing into Indexed bases and Separation/DeviceN alternates. Returns a
    replacement space, or the input unchanged, or None if it can't be safely
    relabelled."""
    if isinstance(cs, Name):
        return cs
    if isinstance(cs, Array) and len(cs) >= 1:
        head = str(cs[0])
        if head == "/ICCBased":
            n = int(cs[1].get("/N"))
            if n == 3:
                warnings.append("ICCBased RGB (N=3) not relabelled - needs real "
                                "colour conversion for X-1a")
                return None
            return ICC_TO_DEV[n]
        if head in ("/CalRGB", "/CalGray", "/Lab"):
            warnings.append(f"{head} space found - needs conversion for X-1a")
            return None
        if head == "/Indexed":
            base = fix_cs(cs[1], warnings)
            if base is not None:
                cs[1] = base
            return cs
        if head in ("/Separation", "/DeviceN"):
            alt = fix_cs(cs[2], warnings)
            if alt is not None:
                cs[2] = alt
            return cs
    return cs


def relabel_all_colourspaces(pdf, warnings):
    def fix_resources(res):
        if res is None:
            return
        csd = res.get("/ColorSpace")
        if csd is None:
            return
        for k in list(csd.keys()):
            new = fix_cs(csd[k], warnings)
            if new is not None:
                csd[k] = new

    for obj in pdf.objects:
        if not isinstance(obj, (Dictionary, Stream)):
            continue
        if str(obj.get("/Subtype")) == "/Image" and obj.get("/ColorSpace") is not None:
            new = fix_cs(obj.ColorSpace, warnings)
            if new is not None:
                obj.ColorSpace = new
        if obj.get("/Resources") is not None:
            fix_resources(obj.Resources)
    for page in pdf.pages:
        fix_resources(page.Resources)


def set_xmp(xmp, version_str, conformance_str):
    """Set the XMP GTS_PDFXVersion; set GTS_PDFXConformance if a value is given,
    or strip it if conformance_str is None. Handles element and attribute forms
    with any namespace prefix."""
    xmp = re.sub(r"(<[\w]*:?GTS_PDFXVersion>)[^<]*(</[\w]*:?GTS_PDFXVersion>)",
                 r"\1" + version_str + r"\2", xmp)
    xmp = re.sub(r"([\w]*:?GTS_PDFXVersion=\")[^\"]*(\")",
                 r"\1" + version_str + r"\2", xmp)
    if conformance_str is None:
        xmp = re.sub(r"\s*<[\w]*:?GTS_PDFXConformance>[^<]*</[\w]*:?GTS_PDFXConformance>",
                     "", xmp)
        xmp = re.sub(r"\s*[\w]*:?GTS_PDFXConformance=\"[^\"]*\"", "", xmp)
    else:
        xmp = re.sub(r"(<[\w]*:?GTS_PDFXConformance>)[^<]*(</[\w]*:?GTS_PDFXConformance>)",
                     r"\1" + conformance_str + r"\2", xmp)
        xmp = re.sub(r"([\w]*:?GTS_PDFXConformance=\")[^\"]*(\")",
                     r"\1" + conformance_str + r"\2", xmp)
    return xmp


def main(src, dst, target, year):
    if target == "x1a":
        version_str = "PDF/X-1a:2001"
        conformance = "PDF/X-1a:2001"
        pdf_version = "1.3"
        convert_icc = True
    else:  # x3
        version_str = f"PDF/X-3:{year}"
        conformance = None
        pdf_version = "1.4" if year == 2003 else "1.3"
        convert_icc = False

    pdf = pikepdf.open(io.BytesIO(read_input_bytes(src)))
    warnings = []

    # 1) remove transparency-group markers (page + forms)
    groups_removed = 0
    for obj in pdf.objects:
        if isinstance(obj, (Dictionary, Stream)) and obj.get("/Group") is not None:
            del obj["/Group"]
            groups_removed += 1
    for page in pdf.pages:
        if "/Group" in page.obj:
            del page.obj["/Group"]
            groups_removed += 1

    # 2) colour: relabel ICC/CIE -> Device for X-1a; leave untouched for X-3
    if convert_icc:
        relabel_all_colourspaces(pdf, warnings)

    # 3) safety scan for things illegal in both standards
    def is_none(v):
        return v is None or (isinstance(v, Name) and str(v) == "/None")

    soft = alpha = blend = egsm = rgb = 0
    for o in pdf.objects:
        if not isinstance(o, (Dictionary, Stream)):
            continue
        t = str(o.get("/Type"))
        st = str(o.get("/Subtype"))
        if st == "/Image":
            if not is_none(o.get("/SMask")):
                soft += 1
            cs = o.get("/ColorSpace")
            if isinstance(cs, Name) and str(cs) == "/DeviceRGB":
                rgb += 1
        if t == "/ExtGState":
            for k in ("/ca", "/CA"):
                v = o.get(k)
                if v is not None and float(v) < 1.0:
                    alpha += 1
            bm = o.get("/BM")
            if bm is not None and str(bm) not in ("/Normal", "/Compatible"):
                blend += 1
            if not is_none(o.get("/SMask")):
                egsm += 1
    if soft:
        warnings.append(f"{soft} image soft mask(s) remain - run the flattener first")
    if alpha:
        warnings.append(f"{alpha} ExtGState(s) with constant alpha <1 remain")
    if blend:
        warnings.append(f"{blend} non-Normal blend mode(s) remain")
    if egsm:
        warnings.append(f"{egsm} graphics-state soft mask(s) remain")
    if rgb:
        warnings.append(f"{rgb} uncalibrated DeviceRGB image(s) - not allowed in "
                        f"PDF/X (needs colour conversion)")

    # 4) OutputIntent is required by both standards
    ois = pdf.Root.get("/OutputIntents")
    if ois is None:
        warnings.append("no OutputIntent present - PDF/X requires one (with a profile)")
    elif "/DestOutputProfile" not in ois[0]:
        warnings.append("OutputIntent has no embedded DestOutputProfile")

    # 5) PDF/X identification (DocInfo + XMP)
    pdf.docinfo["/GTS_PDFXVersion"] = String(version_str)
    if conformance is not None:
        pdf.docinfo["/GTS_PDFXConformance"] = String(conformance)
    elif "/GTS_PDFXConformance" in pdf.docinfo:
        del pdf.docinfo["/GTS_PDFXConformance"]
    md = pdf.Root.get("/Metadata")
    if md is not None:
        xmp = bytes(md.read_bytes()).decode("utf-8", "replace")
        md.write(set_xmp(xmp, version_str, conformance).encode("utf-8"))

    # 6) clear stray Catalog /Version override, write correct version + classic xref
    if "/Version" in pdf.Root:
        del pdf.Root["/Version"]
    save_pdf(pdf, dst, force_version=pdf_version,
             object_stream_mode=pikepdf.ObjectStreamMode.disable,
             fix_metadata_version=False)
    pdf.close()

    # When the PDF goes to stdout, log to stderr so it doesn't corrupt it.
    log = sys.stderr if dst == "-" else sys.stdout
    print(f"Wrote {'<stdout>' if dst == '-' else dst}  ->  {version_str}  "
          f"(PDF {pdf_version})", file=log)
    print(f"  - removed {groups_removed} transparency group marker(s)", file=log)
    if convert_icc:
        print("  - relabelled ICC/CIE colour spaces to Device (X-1a)", file=log)
    else:
        print("  - colour spaces left untouched (ICC/CIE colour allowed in X-3)", file=log)
    if warnings:
        print("  WARNINGS:", file=log)
        for w in dict.fromkeys(warnings):  # de-dup, keep order
            print("    -", w, file=log)
    else:
        print("  - no transparency/colour warnings", file=log)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Finalize a flattened PDF as PDF/X-1a or PDF/X-3.")
    ap.add_argument("input", help="input PDF, or - for stdin")
    ap.add_argument("output", help="output PDF, or - for stdout")
    ap.add_argument("--target", required=True, choices=["x1a", "x3"],
                    help="x1a = PDF/X-1a:2001 (device CMYK); x3 = PDF/X-3 (colour-managed)")
    ap.add_argument("--year", type=int, choices=[2002, 2003], default=2003,
                    help="for --target x3: PDF/X-3:2003 (PDF 1.4, default) or "
                         ":2002 (PDF 1.3). Ignored for x1a.")
    args = ap.parse_args()
    main(args.input, args.output, args.target, args.year)
