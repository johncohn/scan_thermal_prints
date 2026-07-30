#!/usr/bin/env python3
"""Check whether the scanner is connected and whether a strip is loaded.

Connects via Apple's native ImageCaptureCore framework (the same one Image
Capture.app uses), opens a session, and reports the document feeder's
status. Useful as a quick "is it plugged in / is there paper in it" check
before opening Image Capture.

Note: this only reads scanner state -- it can't trigger a scan itself.
Actually issuing a scan command (requestScan / requestScanWithOptions)
through a plain, unbundled `python3` process silently never reaches the
NeatScannersICDriver (confirmed via its own debug log: zero reaction over a
60s+ wait, versus working instantly from Image Capture.app). This looks like
an ImageCaptureCore restriction on which processes may issue actuator
commands, not a bug in this device or its driver. Scanning is triggered
manually in Image Capture; see watch_folder.py for automating everything
downstream of that.

Setup (one-time):
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --index-url https://pypi.org/simple -r requirements.txt

Usage:
    source .venv/bin/activate
    python3 check_scanner.py
"""
import argparse
import sys
import time

import objc
from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop, NSObject
from ImageCaptureCore import (
    ICDeviceBrowser,
    ICDeviceLocationTypeMaskLocal,
    ICDeviceTypeMaskScanner,
    ICScannerFunctionalUnitTypeDocumentFeeder,
)

DEFAULT_NAME_HINT = "neat"


class ScannerController(NSObject):
    def initWithNameHint_(self, name_hint):
        self = objc.super(ScannerController, self).init()
        if self is None:
            return None
        self.name_hint = name_hint.lower()
        self.device = None
        self.session_open = False
        self.session_error = None
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

    def device_didCloseSessionWithError_(self, device, error):
        self.session_open = False

    def didRemoveDevice_(self, device):
        self.device = None

    def device_didEncounterError_(self, device, error):
        print(f"device error: {error}", file=sys.stderr)


def pump(condition, timeout, interval=0.05):
    rl = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        rl.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(interval))
    return condition()


def connect(name_hint, discover_timeout):
    controller = ScannerController.alloc().initWithNameHint_(name_hint)

    browser = ICDeviceBrowser.alloc().init()
    browser.setDelegate_(controller)
    browser.setBrowsedDeviceTypeMask_(ICDeviceTypeMaskScanner | ICDeviceLocationTypeMaskLocal)
    browser.start()
    controller._browser = browser  # keep alive

    print(f"looking for a scanner matching '{name_hint}'...")
    if not pump(lambda: controller.device is not None, timeout=discover_timeout):
        sys.exit("no matching scanner found. Is it plugged in and powered?")

    device = controller.device
    device.setDelegate_(controller)
    print(f"opening session with {device.name()}...")
    device.requestOpenSession()
    if not pump(lambda: controller.session_open or controller.session_error is not None, timeout=15):
        sys.exit("timed out opening session")
    if controller.session_error is not None:
        sys.exit(f"failed to open session: {controller.session_error}")

    # The driver populates its functional units asynchronously after the
    # session opens (takes a couple seconds) and auto-selects one.
    print("waiting for scanner to report its functional unit...")
    if not pump(lambda: device.selectedFunctionalUnit() is not None, timeout=15):
        sys.exit("scanner never reported a functional unit")

    unit = device.selectedFunctionalUnit()
    if unit.type() != ICScannerFunctionalUnitTypeDocumentFeeder:
        device.requestSelectFunctionalUnit_(ICScannerFunctionalUnitTypeDocumentFeeder)
        pump(lambda: device.selectedFunctionalUnit() is not None, timeout=15)
        unit = device.selectedFunctionalUnit()

    return controller, device, unit


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name-hint", default=DEFAULT_NAME_HINT, help="substring to match in the device name")
    ap.add_argument("--discover-timeout", type=float, default=15.0)
    args = ap.parse_args()

    controller, device, unit = connect(args.name_hint, args.discover_timeout)
    print(f"connected: {device.name()}")
    print(f"  document loaded: {bool(unit.documentLoaded())}")
    print(f"  supported resolutions: {list(unit.supportedResolutions() or [])}")
    print(f"  supported bit depths: {list(unit.supportedBitDepths() or [])}")
    print(f"  physical size (in): {unit.physicalSizeInInches()}")
    device.requestCloseSession()
    pump(lambda: not controller.session_open, timeout=5)


if __name__ == "__main__":
    main()
