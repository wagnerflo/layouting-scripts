#!/usr/bin/env python3
"""finalize_x1a.py — convert an already-transparency-flattened PDF/X-4 file
into PDF/X-1a:2001.

It performs the structural part of an X-4 -> X-1a conversion that the
soft-mask flattener does not:
  * removes transparency-group markers (/Group /S /Transparency) from the page
    and from form XObjects;
  * relabels ICCBased colour spaces to their Device equivalent (DeviceCMYK /
    DeviceGray) for images and named/indexed/Separation-alternate spaces -- a
    pure relabel that keeps the channel numbers, which is appearance-neutral
    when the embedded profile equals the OutputIntent profile (verify first!);
  * sets the PDF/X identification to PDF/X-1a:2001 in DocInfo and XMP;
  * writes PDF 1.3 with a classic xref table (no object/xref streams).

It deliberately does NOT touch colour numbers, fonts, page geometry, or the
OutputIntent. ICCBased RGB (N=3) cannot be safely relabelled and is reported.

Usage: python3 finalize_x1a.py in.pdf out.pdf
"""
import sys
import pikepdf
from pikepdf import Name, Array, Dictionary, Stream, String

ICC_TO_DEV = {1: Name.DeviceGray, 3: Name.DeviceRGB, 4: Name.DeviceCMYK}


def main(src, dst):
    pdf = pikepdf.open(src)
    warnings = []

    def fix_cs(cs):
        """Return a device-space replacement for ICC/CIE spaces, recursing into
        Indexed bases and Separation/DeviceN alternates. Returns cs unchanged if
        already a device space."""
        if isinstance(cs, Name):
            return cs
        if isinstance(cs, Array) and len(cs) >= 1:
            head = str(cs[0])
            if head == "/ICCBased":
                n = int(cs[1].get("/N"))
                if n == 3:
                    warnings.append("ICCBased RGB (N=3) found - NOT relabelled; "
                                    "needs real colour conversion for X-1a")
                    return None
                return ICC_TO_DEV[n]
            if head in ("/CalRGB", "/CalGray", "/Lab"):
                warnings.append(f"{head} colour space found - needs conversion")
                return None
            if head == "/Indexed":
                base = fix_cs(cs[1])
                if base is not None:
                    cs[1] = base
                return cs
            if head in ("/Separation", "/DeviceN"):
                alt = fix_cs(cs[2])
                if alt is not None:
                    cs[2] = alt
                return cs
        return cs

    def fix_resources(res):
        if res is None:
            return
        csd = res.get("/ColorSpace")
        if csd is None:
            return
        for k in list(csd.keys()):
            new = fix_cs(csd[k])
            if new is not None:
                csd[k] = new

    # 1) images: relabel ICC colour spaces; 2) drop transparency groups everywhere
    for obj in pdf.objects:
        if not isinstance(obj, (Dictionary, Stream)):
            continue
        if str(obj.get("/Subtype")) == "/Image" and obj.get("/ColorSpace") is not None:
            new = fix_cs(obj.ColorSpace)
            if new is not None:
                obj.ColorSpace = new
        if obj.get("/Group") is not None:
            del obj["/Group"]
        if obj.get("/Resources") is not None:
            fix_resources(obj.Resources)

    for page in pdf.pages:
        if "/Group" in page.obj:
            del page.obj["/Group"]
        fix_resources(page.Resources)

    # 3) PDF/X identification -> X-1a:2001 (DocInfo + XMP)
    pdf.docinfo["/GTS_PDFXVersion"] = String("PDF/X-1a:2001")
    pdf.docinfo["/GTS_PDFXConformance"] = String("PDF/X-1a:2001")
    md = pdf.Root.get("/Metadata")
    if md is not None:
        data = bytes(md.read_bytes())
        data = data.replace(b"PDF/X-4", b"PDF/X-1a:2001")
        md.write(data)

    # 4) write PDF 1.3 with a classic xref table (no 1.5 object/xref streams)
    pdf.save(dst, force_version="1.3",
             object_stream_mode=pikepdf.ObjectStreamMode.disable,
             fix_metadata_version=False)
    pdf.close()

    print("Wrote", dst)
    if warnings:
        print("WARNINGS:")
        for w in set(warnings):
            print("  -", w)
    else:
        print("No colour-conversion warnings.")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
