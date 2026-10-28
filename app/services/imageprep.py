"""Validate and slice full-page screenshots so very tall pages stay legible for the vision model."""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from ..errors import ValidationFailed

Image.MAX_IMAGE_PIXELS = 400_000_000  # full-page captures can be huge but legitimate

ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
TARGET_WIDTH = 1500
SLICE_HEIGHT = 1700
OVERLAP = 140
MAX_SLICES = 14
MAX_SLICE_BYTES = 4_500_000


@dataclass
class Slice:
    data: bytes
    media_type: str
    index: int
    total: int


def inspect_image(raw: bytes) -> tuple[str, int, int]:
    """Return (format, width, height) or raise ValidationFailed."""
    if not raw:
        raise ValidationFailed("The uploaded file is empty.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
            im.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ValidationFailed("This file is not a readable image. Upload a PNG, JPG or WEBP screenshot.", details=str(e))
    if fmt not in ALLOWED_FORMATS:
        raise ValidationFailed(f"Unsupported image format '{fmt}'. Use PNG, JPG or WEBP (GoFullPage can save PNG).")
    if w < 200 or h < 200:
        raise ValidationFailed("Image is too small to contain a package page.")
    return fmt, w, h


def make_slices(raw: bytes) -> list[Slice]:
    inspect_image(raw)
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        else:
            im = im.convert("RGB")

        w, h = im.size
        # Normalise width: shrink very wide captures, gently enlarge narrow ones (small text reads better).
        if w != TARGET_WIDTH:
            scale = TARGET_WIDTH / w if w < 1000 or w > TARGET_WIDTH else 1.0
            if scale != 1.0:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        w, h = im.size

        step = SLICE_HEIGHT - OVERLAP
        boxes = []
        top = 0
        while top < h:
            bottom = min(top + SLICE_HEIGHT, h)
            boxes.append((0, top, w, bottom))
            if bottom >= h:
                break
            top += step
        # Avoid a tiny last slice: merge it into the previous one when it is mostly overlap.
        if len(boxes) > 1 and boxes[-1][3] - boxes[-1][1] < OVERLAP * 2:
            boxes.pop()
            boxes[-1] = (0, boxes[-1][1], w, h)

        if len(boxes) > MAX_SLICES:  # extremely tall page: shrink so it fits into MAX_SLICES
            factor = (MAX_SLICES * step) / h
            im = im.resize((max(1, int(w * factor)), max(1, int(h * factor))), Image.LANCZOS)
            return _cut(im)
        return _encode(im, boxes)


def _cut(im: Image.Image) -> list[Slice]:
    w, h = im.size
    step = SLICE_HEIGHT - OVERLAP
    boxes, top = [], 0
    while top < h and len(boxes) < MAX_SLICES:
        boxes.append((0, top, w, min(top + SLICE_HEIGHT, h)))
        if boxes[-1][3] >= h:
            break
        top += step
    return _encode(im, boxes)


def _encode(im: Image.Image, boxes: list[tuple[int, int, int, int]]) -> list[Slice]:
    out: list[Slice] = []
    for i, box in enumerate(boxes, start=1):
        crop = im.crop(box)
        quality = 90
        while True:
            buf = io.BytesIO()
            crop.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_SLICE_BYTES or quality <= 50:
                break
            quality -= 10
        out.append(Slice(buf.getvalue(), "image/jpeg", i, len(boxes)))
    return out
