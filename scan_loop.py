#!/usr/bin/env python3
"""Fully automatic scan loop: watch the feeder, scan each strip, process it.

Drives the scanner directly via ImageCaptureCore (no Image Capture.app
needed). Getting requestScan() to actually work took reverse-engineering
NAPS2's macOS backend (github.com/cyanfish/naps2) -- the pieces this
NeatScannersICDriver needs that a naive ICDeviceBrowser script doesn't
supply on its own:
  - wait for the `deviceDidBecomeReady:` delegate callback after opening a
    session, before touching the functional unit or requesting a scan
  - explicitly set the functional unit's ScanArea (measurement unit +
    CGRect) rather than leaving it at the driver's default

Transfer mode is file-based (ICScannerTransferModeFileBased), matching how
Image Capture.app itself receives scans and confirmed to produce a correct,
complete image. (Memory-based transfer also triggers a real scan on this
driver, but only ever delivers the first couple of ~64KB bands before
falsely reporting completion -- a bug in this specific driver's banded-
transfer path, not in the scan-triggering mechanism itself.) This driver
also doesn't reliably fire the "file is ready at this URL" callback, so
rather than depend on it, the destination filename is chosen up front and
we simply wait for the didCompleteScan completion signal (which is
reliable) before reading that known path.

Each strip opens a fresh session and closes it afterward (matching NAPS2),
rather than holding one session open across the whole watch loop. Within a
strip, though, long single scans correlate with a native segfault in this
driver, so each strip is scanned in bounded chunks (see scan_one's
max_height_in / --chunk-in) over that one session, continuing automatically
while the feeder still reports paper loaded.

Usage:
    source .venv/bin/activate
    python3 scan_loop.py --process --grayscale-output
"""
import argparse
import datetime
import re
import subprocess
import sys
import time
from pathlib import Path

import objc
from Foundation import NSDate, NSDefaultRunLoopMode, NSObject, NSRunLoop, NSURL
from ImageCaptureCore import (
    ICDeviceBrowser,
    ICDeviceLocationTypeMaskLocal,
    ICDeviceTypeMaskScanner,
    ICScannerBitDepth8Bits,
    ICScannerFunctionalUnitTypeDocumentFeeder,
    ICScannerMeasurementUnitInches,
    ICScannerPixelDataTypeGray,
    ICScannerPixelDataTypeRGB,
    ICScannerTransferModeFileBased,
)
from Quartz import CGRectMake

import drive_upload
from process_strip import process as process_strip

DEFAULT_NAME_HINT = "neat"
PENDING = object()
LAST_NAME_FILE = Path(__file__).parent / ".last_batch_name"
DRIVER_PROCESS_NAME = "NeatScannersICDriver"
MAX_CHUNKS_PER_STRIP = 15

DEBUG = False


def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def load_last_name() -> str | None:
    try:
        return LAST_NAME_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def save_last_name(name: str) -> None:
    LAST_NAME_FILE.write_text(name)


class ScannerController(NSObject):
    def initWithNameHint_(self, name_hint):
        self = objc.super(ScannerController, self).init()
        if self is None:
            return None
        self.name_hint = name_hint.lower()
        self.device = None
        self.session_open = False
        self.session_error = None
        self.ready = False
        self.unit_selected = False
        self.unit_error = None
        self.scan_done = False
        self.scan_error = PENDING
        return self

    # -- ICDeviceBrowserDelegate --
    def deviceBrowser_didAddDevice_moreComing_(self, browser, device, moreComing):
        name = str(device.name() or "")
        if self.device is None and self.name_hint in name.lower():
            self.device = device

    def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, moreGoing):
        if device == self.device:
            self.device = None

    # -- ICDeviceDelegate --
    def device_didOpenSessionWithError_(self, device, error):
        self.session_open = error is None
        self.session_error = error

    def deviceDidBecomeReady_(self, device):
        self.ready = True

    def device_didCloseSessionWithError_(self, device, error):
        self.session_open = False

    def didRemoveDevice_(self, device):
        self.device = None

    def device_didEncounterError_(self, device, error):
        dprint(f"device error: {error}", file=sys.stderr)

    # -- ICScannerDeviceDelegate --
    def scannerDeviceDidBecomeAvailable_(self, scanner):
        pass

    def scannerDevice_didSelectFunctionalUnit_error_(self, scanner, functionalUnit, error):
        self.unit_selected = error is None
        self.unit_error = error

    def scannerDevice_didCompleteScanWithError_(self, scanner, error):
        self.scan_done = True
        self.scan_error = error


def sanitize_name(name: str) -> str:
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)
    return name or datetime.date.today().isoformat()


def prompt_name(current: str) -> str:
    typed = input(f"Name for next batch [{current}]: ").strip()
    if not typed:
        return current
    cleaned = sanitize_name(typed)
    save_last_name(cleaned)
    return cleaned


def next_start_index(out_dir: Path, stem: str) -> int:
    pattern = re.compile(rf"^{re.escape(stem)}_(\d+)\.jpg$", re.IGNORECASE)
    max_i = 0
    if out_dir.exists():
        for p in out_dir.iterdir():
            m = pattern.match(p.name)
            if m:
                max_i = max(max_i, int(m.group(1)))
    return max_i + 1


def pump(condition, timeout, interval=0.05):
    rl = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        rl.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(interval))
    return condition()


def kill_driver():
    """The vendor driver process occasionally wedges (pegged CPU, unresponsive
    to new sessions) and macOS relaunches it fresh the next time it's needed --
    same recovery as manually killing it in Activity Monitor."""
    subprocess.run(["pkill", "-f", DRIVER_PROCESS_NAME], check=False)


def connect_and_select_feeder(name_hint, discover_timeout):
    controller = ScannerController.alloc().initWithNameHint_(name_hint)

    browser = ICDeviceBrowser.alloc().init()
    browser.setDelegate_(controller)
    browser.setBrowsedDeviceTypeMask_(ICDeviceTypeMaskScanner | ICDeviceLocationTypeMaskLocal)
    browser.start()
    controller._browser = browser  # keep alive

    if not pump(lambda: controller.device is not None, timeout=discover_timeout):
        sys.exit("no matching scanner found. Is it plugged in and powered?")

    device = controller.device
    device.setDelegate_(controller)
    device.requestOpenSession()
    if not pump(lambda: controller.session_open or controller.session_error is not None, timeout=15):
        sys.exit("timed out opening session")
    if controller.session_error is not None:
        sys.exit(f"failed to open session: {controller.session_error}")

    if not pump(lambda: controller.ready, timeout=15):
        sys.exit("scanner never became ready")

    if not pump(lambda: device.selectedFunctionalUnit() is not None, timeout=15):
        sys.exit("scanner never reported a functional unit")

    unit = device.selectedFunctionalUnit()
    if unit.type() != ICScannerFunctionalUnitTypeDocumentFeeder:
        device.requestSelectFunctionalUnit_(ICScannerFunctionalUnitTypeDocumentFeeder)
        if not pump(lambda: controller.unit_selected or controller.unit_error is not None, timeout=15):
            sys.exit("timed out selecting document feeder")
        if controller.unit_error is not None:
            sys.exit(f"failed to select document feeder: {controller.unit_error}")
        unit = device.selectedFunctionalUnit()

    return controller, device, unit


def close_session(controller, device):
    device.requestCloseSession()
    pump(lambda: not controller.session_open, timeout=5)


def scan_one(controller, device, unit, out_path: Path, resolution: int, mode: str,
             max_height_in: float | None = None) -> int:
    """Scan to out_path (whose stem becomes the driver's document name). Returns the resolution actually used.

    max_height_in caps the requested scan area below the driver's reported
    physicalSizeInInches -- long single scans (long strips, more photos)
    correlate with a native segfault in this old driver, so scanning in
    bounded chunks keeps each individual request in the range that's held up
    reliably, even on strips longer than any one chunk (see scan_strip)."""
    supported = [int(r) for r in (unit.supportedResolutions() or [])]
    if supported and resolution not in supported:
        resolution = min(supported, key=lambda r: abs(r - resolution))

    unit.setMeasurementUnit_(ICScannerMeasurementUnitInches)
    unit.setResolution_(resolution)
    unit.setPixelDataType_(ICScannerPixelDataTypeRGB if mode == "Color" else ICScannerPixelDataTypeGray)
    unit.setBitDepth_(ICScannerBitDepth8Bits)
    size = unit.physicalSizeInInches()
    height = min(size.height, max_height_in) if max_height_in else size.height
    unit.setScanArea_(CGRectMake(0, 0, size.width, height))

    device.setTransferMode_(ICScannerTransferModeFileBased)
    device.setDocumentUTI_("public.tiff")
    device.setDocumentName_(out_path.stem)
    device.setDownloadsDirectory_(NSURL.fileURLWithPath_(str(out_path.parent)))

    controller.scan_done = False
    controller.scan_error = PENDING
    device.requestScan()
    start = time.time()
    heartbeat = 5.0
    while not controller.scan_done:
        if pump(lambda: controller.scan_done, timeout=heartbeat):
            break
        elapsed = time.time() - start
        if elapsed > 120:
            raise TimeoutError("scan timed out")
        print(f"  still scanning... ({elapsed:.0f}s)")
    if controller.scan_error is not None:
        raise RuntimeError(f"scan error: {controller.scan_error}")
    if not out_path.exists():
        raise RuntimeError(f"scan reported success but {out_path} wasn't written")

    return resolution


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", default="incoming")
    ap.add_argument("--name-hint", default=DEFAULT_NAME_HINT)
    ap.add_argument("--resolution", type=int, default=300)
    ap.add_argument("--mode", default="Color", choices=["Color", "Gray"])
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--process", action="store_true")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--grayscale-output", action="store_true")
    ap.add_argument("--discover-timeout", type=float, default=15.0)
    ap.add_argument("--batch-name", default=None,
                     help="initial batch name (event/date/etc.); if omitted you'll be prompted")
    ap.add_argument("--upload", action="store_true",
                     help="upload each strip's photos to Google Drive via rclone, into a "
                          "subfolder named after the current batch name (requires --process)")
    ap.add_argument("--drive-remote", default=drive_upload.DEFAULT_REMOTE,
                     help="rclone remote name to upload to")
    ap.add_argument("--debug", action="store_true", help="show verbose scanner/driver diagnostics")
    ap.add_argument("--chunk-in", type=float, default=None,
                     help="cap each scan request to this many inches instead of the driver's full "
                          "reported scan area. EXPERIMENTAL: on this driver, requesting a scan area "
                          "smaller than the loaded paper has been observed to hang indefinitely "
                          "(rather than complete or error), so this defaults to off (full scan area, "
                          "matching the behavior already proven reliable up to ~6 photos/strip). Only "
                          "pass this if you're deliberately re-testing it")
    args = ap.parse_args()
    DEBUG = args.debug

    if args.upload and not args.process:
        sys.exit("--upload requires --process (nothing to upload otherwise)")

    incoming = Path(args.incoming).resolve()
    out_dir = Path(args.out_dir).resolve()
    incoming.mkdir(parents=True, exist_ok=True)

    initial = args.batch_name
    if initial is None:
        last = load_last_name()
        default = last or datetime.date.today().isoformat()
        typed = input(f"Name for this batch (event/date/etc.) [{default}]: ").strip()
        initial = typed or default
    current_name = sanitize_name(initial)
    save_last_name(current_name)

    consecutive_connect_failures = 0
    try:
        while True:
            try:
                controller, device, unit = connect_and_select_feeder(args.name_hint, args.discover_timeout)
            except SystemExit as e:
                consecutive_connect_failures += 1
                if consecutive_connect_failures >= 5:
                    sys.exit(f"giving up after {consecutive_connect_failures} failed connection attempts "
                             f"(last error: {e}); check the scanner's power/cable")
                print(f"scanner driver isn't responding ({e}), restarting it...")
                kill_driver()
                time.sleep(3.0)
                continue
            consecutive_connect_failures = 0

            while not pump(lambda: bool(unit.documentLoaded()), timeout=args.poll_interval):
                pass

            print("strip detected, scanning...")
            strip_total = 0
            chunk_num = 0
            first_chunk_failed = False
            while True:
                chunk_num += 1
                if chunk_num > MAX_CHUNKS_PER_STRIP:
                    print(f"  hit the {MAX_CHUNKS_PER_STRIP}-chunk safety cap for one strip, "
                          f"stopping here (feeder may be misreporting paper-loaded status)")
                    break

                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                final_path = incoming / f"strip_{stamp}_c{chunk_num:02d}.tiff"
                if chunk_num > 1:
                    dprint(f"  more paper loaded, scanning chunk {chunk_num}...")
                scan_start = time.time()
                try:
                    used_dpi = scan_one(controller, device, unit, final_path,
                                         args.resolution, args.mode, args.chunk_in)
                except (TimeoutError, RuntimeError) as e:
                    print(f"scan failed: {e}", file=sys.stderr)
                    first_chunk_failed = chunk_num == 1
                    break
                dprint(f"  chunk {chunk_num} scanned in {time.time() - scan_start:.1f}s")

                if args.process:
                    proc_start = time.time()
                    start_index = next_start_index(out_dir, current_name)
                    written = process_strip(
                        final_path, out_dir, used_dpi,
                        min_photo_in2=0.75, pad_in=0.03, jpeg_quality=95,
                        grayscale_output=args.grayscale_output,
                        name_stem=current_name, start_index=start_index,
                        verbose=DEBUG,
                    )
                    strip_total += len(written)
                    dprint(f"  chunk {chunk_num} processed in {time.time() - proc_start:.1f}s "
                           f"({len(written)} photo(s), running total {strip_total})")
                    if args.upload and written:
                        print("uploading...")
                        drive_upload.upload_files(written, current_name, args.drive_remote)

                if not pump(lambda: bool(unit.documentLoaded()), timeout=1.5):
                    break

            close_session(controller, device)
            if first_chunk_failed:
                print("retrying...")
                time.sleep(1.0)
                continue

            print(f"scanned {strip_total} photo(s) as '{current_name}'")
            current_name = prompt_name(current_name)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
