#!/usr/bin/env python3
"""
Extract every embedded image from a PDF (pikepdf / QPDF backend).

MODES
-----
FAITHFUL (default): extract with NO colorspace conversion.
  * DCTDecode JPEG -> original stored bytes, verbatim (.jpeg).
  * DeviceCMYK non-DCT -> decompressed raw CMYK samples -> CMYK TIFF.
  * Gray soft masks / other -> lossless PNG.

CONVERT (--output-icc TARGET.icc): colour-manage every colour image from a
  SOURCE profile (default: the PDF/X OutputIntent) to TARGET via littleCMS.
  True CMYK is recovered by inverting Adobe-marked CMYK JPEGs.
  CMYK target -> CMYK TIFF, RGB target -> RGB TIFF, target profile embedded.

ALPHA (--alpha): render each image's soft mask into the image as an alpha
  channel instead of writing it as a separate file.
  * RGB  images -> RGBA TIFF.
  * CMYK images -> 5-channel CMYKA TIFF (photometric=separated,
    ExtraSamples=unassociated-alpha) written via tifffile.
  Works in both faithful and convert modes. In faithful mode the colour
  values are still untouched; the image is only repackaged with its alpha.
  (A JPEG cannot carry alpha, so faithful+alpha decodes the base image.)

pdfimages is avoided because it mangles Adobe-marked CMYK JPEGs.
"""
import argparse
import io
import os
import sys

import pikepdf
from pikepdf import PdfImage
from PIL import Image, ImageCms

INTENTS = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "relative": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "saturation": ImageCms.Intent.SATURATION,
    "absolute": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}
CS_TAG = {"/DeviceCMYK": "cmyk", "/DeviceRGB": "rgb", "/DeviceGray": "gray"}


def is_filter(obj, name):
    f = obj.get("/Filter")
    return f is not None and name in str(f)


def has_adobe_marker(jpeg: bytes) -> bool:
    return b"Adobe" in jpeg[:12000]


def output_intent_icc(pdf):
    try:
        return bytes(pdf.Root.OutputIntents[0].DestOutputProfile.read_bytes())
    except Exception:
        return None


def true_pixels(obj):
    """PIL image in true (non-inverted) CMYK / RGB for a colour image."""
    cs = str(obj.get("/ColorSpace"))
    w, h = int(obj.Width), int(obj.Height)
    if is_filter(obj, "/DCTDecode"):
        jpeg = bytes(obj.read_raw_bytes())
        im = Image.open(io.BytesIO(jpeg)); im.load()
        if im.mode == "CMYK" and has_adobe_marker(jpeg):
            im = im.point(lambda v: 255 - v)
        return im
    if cs == "/DeviceCMYK":
        return Image.frombytes("CMYK", (w, h), bytes(obj.read_bytes()))
    if cs == "/DeviceRGB":
        return Image.frombytes("RGB", (w, h), bytes(obj.read_bytes()))
    return PdfImage(obj).as_pil_image().convert("RGB")


def mask_as_L(smask_obj, size):
    """Decode a soft mask to mode 'L', scaled to `size`."""
    try:
        m = PdfImage(smask_obj).as_pil_image().convert("L")
    except Exception:
        w, h = int(smask_obj.Width), int(smask_obj.Height)
        m = Image.frombytes("L", (w, h), bytes(smask_obj.read_bytes()))
    if m.size != size:
        m = m.resize(size, Image.BILINEAR)
    return m


def save_with_alpha(color_img, mask_L, path, icc_bytes):
    """Write an RGBA TIFF (RGB) or a 5-channel CMYKA TIFF (CMYK)."""
    if color_img.mode == "RGB":
        rgba = color_img.copy(); rgba.putalpha(mask_L)
        kw = {"compression": "tiff_deflate"}
        if icc_bytes:
            kw["icc_profile"] = icc_bytes
        rgba.save(path, **kw)
    elif color_img.mode == "CMYK":
        import numpy as np, tifffile
        arr = np.dstack([np.asarray(color_img), np.asarray(mask_L)])  # H,W,5
        kw = dict(photometric="separated", extrasamples=[2],          # unassoc alpha
                  compression="adobe_deflate")
        if icc_bytes:
            kw["iccprofile"] = icc_bytes
        tifffile.imwrite(path, arr, **kw)
    else:
        raise ValueError(f"unexpected mode {color_img.mode}")


def run(pdf_path, out_dir, output_icc=None, source_icc=None,
        intent="relative", bpc=True, alpha=False):
    os.makedirs(out_dir, exist_ok=True)
    pdf = pikepdf.open(pdf_path)

    images, smask_ids = {}, {}
    for obj in pdf.objects:
        try:
            if obj.get("/Subtype") == "/Image":
                images[obj.objgen] = obj
                if "/SMask" in obj:
                    images[obj.SMask.objgen] = obj.SMask
                    smask_ids[obj.SMask.objgen] = obj.objgen
        except Exception:
            continue

    converting = output_icc is not None
    src_prof = dst_prof = dst_bytes = dst_space = None
    flags = ImageCms.Flags.HIGHRESPRECALC
    if converting:
        src_bytes = (open(source_icc, "rb").read() if source_icc
                     else output_intent_icc(pdf))
        if src_bytes is None:
            sys.exit("ERROR: no OutputIntent in PDF; pass --source-icc.")
        src_prof = ImageCms.ImageCmsProfile(io.BytesIO(src_bytes))
        dst_bytes = open(output_icc, "rb").read()
        dst_prof = ImageCms.getOpenProfile(io.BytesIO(dst_bytes))
        dst_space = dst_prof.profile.xcolor_space.strip()
        if bpc:
            flags |= ImageCms.Flags.BLACKPOINTCOMPENSATION
        print(f"Converting: source='{ImageCms.getProfileName(src_prof).strip()}' "
              f"-> '{os.path.basename(output_icc)}' ({dst_space}) "
              f"intent={intent} bpc={bpc}")
    if alpha:
        print("Alpha: soft masks are rendered into the images (RGBA / CMYKA TIFF).")

    def convert(img):
        out_mode = "CMYK" if "CMYK" in dst_space else "RGB"
        key = (img.mode, out_mode)
        if key not in tcache:
            tcache[key] = ImageCms.buildTransform(
                src_prof, dst_prof, img.mode, out_mode,
                renderingIntent=INTENTS[intent], flags=flags)
        return ImageCms.applyTransform(img, tcache[key]), out_mode

    tcache, manifest, idx = {}, [], 0
    for og, obj in images.items():
        is_mask = og in smask_ids
        if is_mask and alpha:
            continue                       # merged into its base image instead
        w, h = int(obj.Width), int(obj.Height)
        cs = str(obj.get("/ColorSpace"))
        cs_tag = CS_TAG.get(cs, cs.strip("/").lower())
        objid = og[0]
        if not is_mask:
            idx += 1
        stem = (f"image_{idx:02d}_obj{objid}_{cs_tag}" if not is_mask
                else f"alphamask_obj{objid}_for-obj{smask_ids[og][0]}")

        is_colour = (not is_mask) and cs in ("/DeviceCMYK", "/DeviceRGB")
        want_alpha = alpha and is_colour and ("/SMask" in obj)

        # ---- colour images (convert and/or alpha) ----
        if is_colour and (converting or want_alpha):
            img = true_pixels(obj)
            embed = None
            if converting:
                img, out_mode = convert(img)
                embed = dst_bytes
                tag = out_mode
            else:
                tag = img.mode
            if want_alpha:
                m = mask_as_L(obj.SMask, img.size)
                fn = f"{stem}__{tag}_alpha.tiff"
                save_with_alpha(img, m, os.path.join(out_dir, fn), embed)
                manifest.append(f"{fn}\timage(alpha{'+conv' if converting else ''})"
                                f"\t{w}x{h}\t{tag}+A")
            else:  # converting only
                fn = f"{stem}__to_{tag}.tiff"
                kw = {"compression": "tiff_deflate", "icc_profile": embed} if embed else {}
                img.save(os.path.join(out_dir, fn), **kw)
                manifest.append(f"{fn}\timage(converted)\t{w}x{h}\t->{tag}")
            continue

        # ---- faithful extraction (no alpha requested / no smask) ----
        if is_filter(obj, "/DCTDecode"):
            data = bytes(obj.read_raw_bytes())
            fn = f"{stem}.jpeg"; open(os.path.join(out_dir, fn), "wb").write(data)
            note = f"stored=jpeg\tbytes={len(data)}"
        elif cs == "/DeviceCMYK":
            raw = bytes(obj.read_bytes())
            assert len(raw) == w * h * 4
            fn = f"{stem}.tiff"
            Image.frombytes("CMYK", (w, h), raw).save(
                os.path.join(out_dir, fn), compression="tiff_deflate")
            note = f"stored=flate-cmyk\tbytes={len(raw)}"
        else:
            im = PdfImage(obj).as_pil_image()
            fn = f"{stem}.png"; im.save(os.path.join(out_dir, fn))
            note = f"stored={str(obj.get('/Filter'))}\tmode={im.mode}"
        role = "alphamask" if is_mask else "image(faithful)"
        manifest.append(f"{fn}\t{role}\t{w}x{h}\tcs={cs_tag}\t{note}")

    print(("Colour-managed extraction" if converting
           else "Faithful raw extraction - no colorspace conversion") +
          (" | alpha merged" if alpha else ""))
    print(f"source_pdf: {pdf_path}")
    if converting:
        print(f"target_profile: {output_icc}")
    print()
    print("\n".join(manifest))
    print()

    n_masks = 0 if alpha else len(smask_ids)
    print(f"Done: {idx} colour images + {n_masks} separate masks -> {out_dir}/")


def main():
    ap = argparse.ArgumentParser(description="Extract PDF images (pikepdf); optional ICC conversion and alpha merge.")
    ap.add_argument("pdf"); ap.add_argument("out_dir")
    ap.add_argument("--output-icc", metavar="ICC", help="target ICC; enables conversion")
    ap.add_argument("--source-icc", metavar="ICC", help="source ICC (default: PDF OutputIntent)")
    ap.add_argument("--intent", choices=list(INTENTS), default="relative")
    ap.add_argument("--no-bpc", action="store_true", help="disable black point compensation")
    ap.add_argument("--alpha", action="store_true",
                    help="render soft masks into images (RGBA / CMYKA TIFF)")
    a = ap.parse_args()
    run(a.pdf, a.out_dir, a.output_icc, a.source_icc, a.intent, not a.no_bpc, a.alpha)


if __name__ == "__main__":
    main()
