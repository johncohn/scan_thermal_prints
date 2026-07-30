#!/usr/bin/env python3
"""Generate a synthetic scanned strip for testing process_strip.py without the scanner.

Produces a long off-white image with a few halftone-textured "photos" at
slight random rotations, separated by blank paper gaps -- roughly what a
300dpi color scan of a thermal photo-camera strip looks like.
"""
import argparse
from pathlib import Path

import cv2
import numpy as np


def make_halftone_photo(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # Smooth grayscale "scene" (blobs of varying tone).
    base = rng.integers(40, 220, size=(h // 8, w // 8), dtype=np.uint8)
    base = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    base = cv2.GaussianBlur(base, (0, 0), sigmaX=w / 40)

    # Halftone dither: bigger dots in darker areas.
    yy, xx = np.mgrid[0:h, 0:w]
    dot_period = max(3, w // 120)
    pattern = (np.sin(xx * np.pi / dot_period) * np.sin(yy * np.pi / dot_period))
    pattern = (pattern - pattern.min()) / (pattern.max() - pattern.min())
    halftone = np.where(pattern * 255 > base, 255, 0).astype(np.uint8)
    # Real scans arrive with the dots already optically softened (scanner
    # resolution vs. print resolution); without that, a hard binary dot
    # pattern doesn't behave like real halftone under a median blur.
    halftone = cv2.GaussianBlur(halftone, (0, 0), sigmaX=1.2)

    photo = cv2.cvtColor(halftone, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(photo, (2, 2), (w - 3, h - 3), (0, 0, 0), 3)  # thin printed border
    return photo


def paste_rotated(canvas: np.ndarray, photo: np.ndarray, cx: int, cy: int, angle_deg: float) -> None:
    h, w = photo.shape[:2]
    ch, cw = canvas.shape[:2]
    diag = int(np.hypot(w, h)) + 4
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    M[0, 2] += diag / 2 - w / 2
    M[1, 2] += diag / 2 - h / 2
    rotated = cv2.warpAffine(photo, M, (diag, diag), borderValue=(255, 255, 255))
    mask = np.any(rotated != 255, axis=2)

    x0, y0 = cx - diag // 2, cy - diag // 2
    for yy in range(diag):
        ty = y0 + yy
        if not (0 <= ty < ch):
            continue
        row_mask = mask[yy]
        xs = np.where(row_mask)[0]
        if xs.size == 0:
            continue
        tx = x0 + xs
        valid = (tx >= 0) & (tx < cw)
        canvas[ty, tx[valid]] = rotated[yy, xs[valid]]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", type=Path, default=Path("incoming/test_strip.tiff"))
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--n-photos", type=int, default=3)
    args = ap.parse_args()

    dpi = args.dpi
    strip_w = int(2.2 * dpi)
    photo_w, photo_h = int(1.8 * dpi), int(1.8 * dpi)
    gap = int(0.5 * dpi)
    strip_h = gap + args.n_photos * (photo_h + gap)

    canvas = np.full((strip_h, strip_w, 3), 195, dtype=np.uint8)  # receipt paper (real scans read ~180-199, not pure white)

    rng = np.random.default_rng(42)
    for i in range(args.n_photos):
        photo = make_halftone_photo(photo_w, photo_h, seed=i)
        cy = gap + i * (photo_h + gap) + photo_h // 2
        cx = strip_w // 2 + rng.integers(-15, 15)
        angle = rng.uniform(-6, 6)
        paste_rotated(canvas, photo, cx, cy, angle)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out), canvas)
    print(f"wrote {args.out} ({strip_w}x{strip_h}, {args.n_photos} photos)")


if __name__ == "__main__":
    main()
