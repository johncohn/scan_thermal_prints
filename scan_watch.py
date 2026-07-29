#!/usr/bin/env python3
"""Watch the NM-1000 feeder and scan each strip as it's loaded.

Drives the scanner via SANE's `scanimage` CLI (the `epjitsu` backend targets
the exact chipset used in NeatReceipts/NeatDesk scanners, so this should
detect the NM-1000 without any vendor driver involved). Loops indefinitely:
scan attempt -> if a strip is loaded, save it and wait for it to be
replaced; if not, wait and retry.

Setup (one-time):
    brew install sane-backends
    scanimage -L        # confirm the NM-1000 is detected; note the device name

Usage:
    python3 scan_watch.py                      # auto-detect + watch, save-only
    python3 scan_watch.py --process             # also run process_strip.py on each scan
    python3 scan_watch.py --device "epjitsu:libusb:001:002"   # skip auto-detect
"""
import argparse
import datetime
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2

DEFAULT_DEVICE_HINT = "epjitsu"
NO_PAPER_MARKERS = (
    "document feeder out of documents",
    "no documents",
    "out of paper",
    "cover open",
    "no such device",
)


def find_device(hint: str) -> str:
    out = subprocess.run(["scanimage", "-L"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if hint.lower() in line.lower() and "`" in line:
            return line.split("`", 1)[1].split("'", 1)[0]
    return ""


def is_blank(path: Path, std_threshold: float = 4.0, mean_threshold: float = 250.0) -> bool:
    """Fallback in case scanimage happily returns a blank page instead of erroring."""
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return True
    return bool(img.std() < std_threshold and img.mean() > mean_threshold)


def scan_once(device: str, out_path: Path, resolution: int, mode: str) -> bool:
    cmd = [
        "scanimage",
        "--device-name", device,
        "--resolution", str(resolution),
        "--mode", mode,
        "--format", "tiff",
        "-o", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip().lower()
        if not any(marker in stderr for marker in NO_PAPER_MARKERS):
            print(f"scanimage error: {result.stderr.strip()}", file=sys.stderr)
        out_path.unlink(missing_ok=True)
        return False

    if not out_path.exists() or out_path.stat().st_size == 0:
        return False

    if is_blank(out_path):
        out_path.unlink(missing_ok=True)
        return False

    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", default="incoming", help="folder to drop raw scans into")
    ap.add_argument("--device", default=None, help="explicit SANE device name (skips auto-detect)")
    ap.add_argument("--device-hint", default=DEFAULT_DEVICE_HINT, help="substring to match against `scanimage -L`")
    ap.add_argument("--resolution", type=int, default=300)
    ap.add_argument("--mode", default="Color", choices=["Color", "Gray", "Lineart"])
    ap.add_argument("--poll-interval", type=float, default=2.0, help="seconds between retries when no paper is loaded")
    ap.add_argument("--debounce", type=float, default=3.0, help="seconds to pause after a scan before polling again")
    ap.add_argument("--process", action="store_true", help="run process_strip.py on each scan immediately")
    ap.add_argument("--out-dir", default="output", help="output folder, passed through to process_strip.py")
    ap.add_argument("--grayscale-output", action="store_true",
                     help="passed through to process_strip.py: save crops as grayscale JPEGs "
                          "even though the scan itself is captured in color")
    args = ap.parse_args()

    incoming = Path(args.incoming)
    incoming.mkdir(parents=True, exist_ok=True)

    device = args.device or find_device(args.device_hint)
    if not device:
        print(
            f"No scanner matching '{args.device_hint}' found.\n"
            f"  - Check the NM-1000 is plugged in and powered.\n"
            f"  - Run `scanimage -L` to list what SANE currently sees.\n"
            f"  - Pass --device '<name>' to skip auto-detect.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Using device: {device}")
    print(f"Watching for strips in '{incoming}/' (Ctrl+C to stop)...")

    n = 0
    try:
        while True:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            tmp_path = incoming / f".tmp_{stamp}.tiff"
            if scan_once(device, tmp_path, args.resolution, args.mode):
                n += 1
                final_path = incoming / f"strip_{stamp}.tiff"
                shutil.move(str(tmp_path), str(final_path))
                print(f"[{n}] saved {final_path.name}")

                if args.process:
                    cmd = [
                        sys.executable,
                        str(Path(__file__).parent / "process_strip.py"),
                        str(final_path),
                        "-o", args.out_dir,
                        "--dpi", str(args.resolution),
                    ]
                    if args.grayscale_output:
                        cmd.append("--grayscale-output")
                    subprocess.run(cmd)

                print("remove the strip and load the next one...")
                time.sleep(args.debounce)
            else:
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print(f"\nstopped. {n} strip(s) scanned.")


if __name__ == "__main__":
    main()
