#!/usr/bin/env python3
"""Watch a folder for new scans (saved there by Image Capture) and process each one.

Workflow: open Image Capture, select the scanner, set its output/destination
folder to this project's `incoming/` directory, then load a strip and click
Scan (or press the scanner's button) as usual. This script polls that folder,
waits for each new file to finish writing, then runs process_strip.py on it
and moves the raw scan into incoming/processed/ so it isn't picked up again.

Usage:
    python3 watch_folder.py --process --grayscale-output
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}


def is_stable(path: Path, last_sizes: dict) -> bool:
    """A file is considered done writing once its size stops changing between polls."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    prev = last_sizes.get(path)
    last_sizes[path] = size
    return prev is not None and prev == size and size > 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch-dir", default="incoming", help="folder Image Capture saves scans into")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--dpi", type=int, default=300, help="resolution the scan was captured at")
    ap.add_argument("--grayscale-output", action="store_true")
    ap.add_argument("--poll-interval", type=float, default=2.0)
    args = ap.parse_args()

    watch_dir = Path(args.watch_dir).resolve()
    processed_dir = watch_dir / "processed"
    watch_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    print(f"watching '{watch_dir}/' for new scans (Ctrl+C to stop)...")
    print("point Image Capture's scan destination at this folder, then load a strip and click Scan.")

    last_sizes = {}
    warned = set()
    n = 0
    try:
        while True:
            all_files = [p for p in watch_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
            for p in all_files:
                if p.suffix.lower() not in IMAGE_EXTENSIONS and p not in warned:
                    print(f"skipping {p.name}: unrecognized format {p.suffix!r} "
                          f"(expected one of {sorted(IMAGE_EXTENSIONS)} -- "
                          f"check Image Capture's scan Format setting)", file=sys.stderr)
                    warned.add(p)

            candidates = sorted(p for p in all_files if p.suffix.lower() in IMAGE_EXTENSIONS)
            for path in candidates:
                if not is_stable(path, last_sizes):
                    continue

                n += 1
                print(f"[{n}] {path.name}")
                cmd = [
                    sys.executable,
                    str(Path(__file__).parent / "process_strip.py"),
                    str(path),
                    "-o", args.out_dir,
                    "--dpi", str(args.dpi),
                ]
                if args.grayscale_output:
                    cmd.append("--grayscale-output")
                subprocess.run(cmd)

                shutil.move(str(path), str(processed_dir / path.name))
                last_sizes.pop(path, None)

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print(f"\nstopped. {n} strip(s) processed.")


if __name__ == "__main__":
    main()
