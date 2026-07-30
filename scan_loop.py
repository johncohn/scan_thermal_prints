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

Each scan opens a fresh session and closes it afterward (matching NAPS2),
rather than holding one session open across the whole watch loop.

Usage:
    source .venv/bin/activate
    python3 scan_loop.py --process --grayscale-output
"""
import argparse
import datetime
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

DEFAULT_NAME_HINT = "neat"
PENDING = object()


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
        print(f"device error: {error}", file=sys.stderr)

    # -- ICScannerDeviceDelegate --
    def scannerDeviceDidBecomeAvailable_(self, scanner):
        pass

    def scannerDevice_didSelectFunctionalUnit_error_(self, scanner, functionalUnit, error):
        self.unit_selected = error is None
        self.unit_error = error

    def scannerDevice_didCompleteScanWithError_(self, scanner, error):
        self.scan_done = True
        self.scan_error = error


def pump(condition, timeout, interval=0.05):
    rl = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        rl.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(interval))
    return condition()


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


def scan_one(controller, device, unit, out_path: Path, resolution: int, mode: str) -> int:
    """Scan to out_path (whose stem becomes the driver's document name). Returns the resolution actually used."""
    supported = [int(r) for r in (unit.supportedResolutions() or [])]
    if supported and resolution not in supported:
        resolution = min(supported, key=lambda r: abs(r - resolution))

    unit.setMeasurementUnit_(ICScannerMeasurementUnitInches)
    unit.setResolution_(resolution)
    unit.setPixelDataType_(ICScannerPixelDataTypeRGB if mode == "Color" else ICScannerPixelDataTypeGray)
    unit.setBitDepth_(ICScannerBitDepth8Bits)
    size = unit.physicalSizeInInches()
    unit.setScanArea_(CGRectMake(0, 0, size.width, size.height))

    device.setTransferMode_(ICScannerTransferModeFileBased)
    device.setDocumentUTI_("public.tiff")
    device.setDocumentName_(out_path.stem)
    device.setDownloadsDirectory_(NSURL.fileURLWithPath_(str(out_path.parent)))

    controller.scan_done = False
    controller.scan_error = PENDING
    device.requestScan()
    if not pump(lambda: controller.scan_done, timeout=120):
        raise TimeoutError("scan timed out")
    if controller.scan_error is not None:
        raise RuntimeError(f"scan error: {controller.scan_error}")
    if not out_path.exists():
        raise RuntimeError(f"scan reported success but {out_path} wasn't written")

    return resolution


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", default="incoming")
    ap.add_argument("--name-hint", default=DEFAULT_NAME_HINT)
    ap.add_argument("--resolution", type=int, default=300)
    ap.add_argument("--mode", default="Color", choices=["Color", "Gray"])
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--debounce", type=float, default=3.0)
    ap.add_argument("--process", action="store_true")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--grayscale-output", action="store_true")
    ap.add_argument("--discover-timeout", type=float, default=15.0)
    args = ap.parse_args()

    incoming = Path(args.incoming).resolve()
    incoming.mkdir(parents=True, exist_ok=True)

    print("watching for strips (Ctrl+C to stop)...")
    n = 0
    try:
        while True:
            controller, device, unit = connect_and_select_feeder(args.name_hint, args.discover_timeout)
            loaded = bool(unit.documentLoaded())
            if not loaded:
                close_session(controller, device)
                time.sleep(args.poll_interval)
                continue

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            final_path = incoming / f"strip_{stamp}.tiff"
            print("strip detected, scanning...")
            try:
                used_dpi = scan_one(controller, device, unit, final_path, args.resolution, args.mode)
            except (TimeoutError, RuntimeError) as e:
                print(f"scan failed: {e}", file=sys.stderr)
                close_session(controller, device)
                time.sleep(args.poll_interval)
                continue
            close_session(controller, device)

            n += 1
            print(f"[{n}] saved {final_path.name}")

            if args.process:
                cmd = [
                    sys.executable,
                    str(Path(__file__).parent / "process_strip.py"),
                    str(final_path),
                    "-o", args.out_dir,
                    "--dpi", str(used_dpi),
                ]
                if args.grayscale_output:
                    cmd.append("--grayscale-output")
                subprocess.run(cmd)

            print("load the next strip when ready...")
            time.sleep(args.debounce)
    except KeyboardInterrupt:
        print(f"\nstopped. {n} strip(s) scanned.")


if __name__ == "__main__":
    main()
