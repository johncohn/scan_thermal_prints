#!/usr/bin/env python3
"""Split a scanned strip of thermal-printer photos into individual, deskewed JPEGs.

Usage:
    python3 process_strip.py incoming/strip_20260729_081700.tiff -o output/
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def load_image(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    return img


def find_photo_regions(img: np.ndarray, dpi: int, min_photo_in2: float,
                        bg_cutoff: int, paper_cutoff: int) -> list[np.ndarray]:
    """Return a list of contours, one per photo, sorted in strip order.

    The scanner bed background is solid black, which is just as "dark" as
    the halftone dots in a photo -- a simple dark/light threshold classifies
    both as foreground and fuses whole photos to the background wherever
    they touch. Instead this splits blurred brightness into three bands
    (background / photo content / blank paper) and keeps only the middle
    one, since background and blank paper are both flat while halftone
    photo content isn't.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Blur enough that individual halftone dots average out to each photo's
    # local midtone, rather than trying to threshold dot-by-dot.
    blur_k = (dpi // 10) | 1
    blurred = cv2.medianBlur(gray, blur_k)

    mask = ((blurred > bg_cutoff) & (blurred < paper_cutoff)).astype(np.uint8) * 255

    # Fill small internal holes (halftone dropout, thin light borders)
    # without bridging the (much larger) blank-paper gap between photos.
    close_px = max(3, dpi // 15)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (close_px, close_px))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    closed = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_area = min_photo_in2 * dpi * dpi
    contours = [c for c in contours if cv2.contourArea(c) >= min_area]

    # Order photos the way they were laid out on the strip: top-to-bottom if
    # the strip is taller than it is wide, else left-to-right.
    h, w = img.shape[:2]
    vertical = h >= w
    def sort_key(c):
        (cx, cy), _, _ = cv2.minAreaRect(c)
        return cy if vertical else cx
    contours.sort(key=sort_key)
    return contours


def deskew_crop(img: np.ndarray, contour: np.ndarray, pad_px: int) -> np.ndarray:
    (cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)

    # cv2.minAreaRect's angle/w/h assignment is not guaranteed to match the
    # photo's "natural" upright orientation, so normalize to the rotation
    # nearest 0 degrees rather than assuming w >= h.
    if angle < -45:
        angle += 90
        rw, rh = rh, rw
    elif angle > 45:
        angle -= 90
        rw, rh = rh, rw

    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    x0 = int(round(cx - rw / 2 - pad_px))
    y0 = int(round(cy - rh / 2 - pad_px))
    x1 = int(round(cx + rw / 2 + pad_px))
    y1 = int(round(cy + rh / 2 + pad_px))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    return rotated[y0:y1, x0:x1]


ROTATIONS = {
    "none": None,
    "cw": cv2.ROTATE_90_CLOCKWISE,
    "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE,
    "180": cv2.ROTATE_180,
}


def process(src: Path, out_dir: Path, dpi: int, min_photo_in2: float,
            pad_in: float, jpeg_quality: int, grayscale_output: bool = False,
            bg_cutoff: int = 25, paper_cutoff: int = 170, rotate: str = "cw") -> list[Path]:
    img = load_image(src)
    contours = find_photo_regions(img, dpi, min_photo_in2, bg_cutoff, paper_cutoff)
    if not contours:
        print(f"  no photo regions found in {src.name}", file=sys.stderr)
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    pad_px = int(pad_in * dpi)
    stem = src.stem
    rotate_code = ROTATIONS[rotate]
    written = []
    for i, contour in enumerate(contours, start=1):
        crop = deskew_crop(img, contour, pad_px)
        if crop.size == 0:
            continue
        if rotate_code is not None:
            crop = cv2.rotate(crop, rotate_code)
        if grayscale_output:
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        out_path = out_dir / f"{stem}_photo{i:02d}.jpg"
        cv2.imwrite(str(out_path), crop, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        written.append(out_path)
        print(f"  wrote {out_path} ({crop.shape[1]}x{crop.shape[0]})")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", type=Path, help="scanned strip image(s) (TIFF/PNG)")
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("output"))
    ap.add_argument("--dpi", type=int, default=300, help="scan resolution used")
    ap.add_argument("--min-photo-in2", type=float, default=0.75,
                     help="discard blobs smaller than this many square inches (dust/noise)")
    ap.add_argument("--pad-in", type=float, default=0.03, help="padding around each crop, in inches")
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument("--grayscale-output", action="store_true",
                     help="convert crops to grayscale before saving, even if the source scan is color "
                          "(useful since color scanning reduces moire on halftone prints, but the "
                          "photos themselves are B&W)")
    ap.add_argument("--bg-cutoff", type=int, default=25,
                     help="blurred brightness (0-255) below this is scanner-bed background, not paper")
    ap.add_argument("--paper-cutoff", type=int, default=170,
                     help="blurred brightness (0-255) above this is blank paper, not photo content")
    ap.add_argument("--rotate", choices=sorted(ROTATIONS), default="cw",
                     help="rotate each crop after deskewing (photos print sideways relative to the "
                          "strip's feed direction on this camera)")
    args = ap.parse_args()

    total = 0
    for src in args.inputs:
        print(f"{src}:")
        total += len(process(src, args.out_dir, args.dpi, args.min_photo_in2, args.pad_in,
                              args.jpeg_quality, args.grayscale_output,
                              args.bg_cutoff, args.paper_cutoff, args.rotate))
    print(f"done: {total} photo(s) written to {args.out_dir}/")


if __name__ == "__main__":
    main()
