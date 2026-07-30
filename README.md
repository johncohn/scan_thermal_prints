# scan_thermal_prints

Automates scanning and processing strips of photos from a kid's thermal
photo-printing camera (halftone B&W images on receipt paper), using a
NeatReceipts NM-1000: detects when a strip is loaded, scans it, splits it
into individual photos, deskews and crops each one, and saves them as
JPEGs.

## How it works

1. **`scan_loop.py`** drives the scanner directly via Apple's
   `ImageCaptureCore` framework (no Image Capture.app needed) -- polls for
   a loaded strip, scans it, and hands the result to `process_strip.py`.
2. **`process_strip.py`** takes a raw scan (one strip, possibly containing
   several photos separated by blank paper) and:
   - separates photo content from both the blank paper gaps *and* the
     scanner's solid-black background (see "Segmentation" below)
   - finds each photo's bounding box and rotation angle, deskews and crops it
   - rotates it upright (this camera prints sideways relative to the
     strip's feed direction) and saves it as its own JPEG in `output/`

Color scanning is used even though the source prints are B&W, since it
reduces moire artifacts on the halftone dots. Use `--grayscale-output` to
convert the final crops to true grayscale JPEGs regardless of scan mode.

## Getting requestScan() to actually work

This ended up being the hard part. Two dead ends first:

- **SANE** (`scanimage`): the `epjitsu` backend targets exactly this
  chipset family, but this NM-1000 unit reports USB vendor ID `0x1f44`
  ("The Neat Company"), which doesn't match any installed SANE backend.
- **Naive ImageCaptureCore**: discovery, opening a session, and reading
  live scanner state (paper-loaded status, capabilities) all worked fine
  from a plain PyObjC script. But `requestScan()` silently did nothing --
  confirmed via the driver's own debug log (`log show --predicate
  'process CONTAINS "Neat"'`), which showed zero reaction to the command
  for 90+ seconds, versus working instantly through Image Capture.app.

The fix came from reading NAPS2's (github.com/cyanfish/naps2) macOS
backend (`NAPS2.Sdk/Scan/Internal/Apple/DeviceOperator.cs`), which does
two things a naive implementation doesn't:
- waits for the `deviceDidBecomeReady:` delegate callback after opening a
  session, before touching the functional unit or requesting a scan
- explicitly sets the functional unit's `ScanArea` (plus `MeasurementUnit`)
  rather than leaving it at the driver's default

Adding just those two made `requestScan()` actually trigger a real,
physical scan. NAPS2 itself uses memory-based transfer (streaming raw
pixel bands through a delegate callback) -- reproducing that here revealed
a further bug specific to this old driver: it only ever delivers the first
~64KB of banded data before falsely reporting completion. Switching to
**file-based transfer** (the same mechanism Image Capture.app itself uses)
avoided that entirely. One remaining quirk: this driver doesn't reliably
fire the "file is ready" callback, so rather than depend on it, the
destination filename is chosen up front and the code waits on the
(reliable) scan-completion signal before reading that known path.

`check_scanner.py` remains as a lightweight connectivity/paper-status
diagnostic, and `watch_folder.py` is kept as a manual-scan fallback (you
trigger scans in Image Capture yourself; it watches a folder and processes
whatever shows up) in case a different scanner/driver combination hits the
same wall `scan_loop.py` worked around.

## Segmentation

The scanner bed background is solid black -- just as "dark" as the
halftone dots in a photo, so a naive dark/light threshold classifies both
as foreground and fuses whole photos to the background wherever they
touch. `process_strip.py` instead splits blurred brightness into three
bands (background / photo content / blank paper) and keeps only the middle
one, tuned against a real problem scan (`--bg-cutoff` / `--paper-cutoff`
are adjustable if a different scan's exposure needs different values).

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install --index-url https://pypi.org/simple -r requirements.txt
```

(The `--index-url` override is needed if pip is configured to use an
internal package mirror that doesn't carry the PyObjC framework packages.)

## Usage

```
source .venv/bin/activate
./run_scan.sh
```

Load a strip -- it's detected, scanned, and split/cropped JPEGs appear in
`output/` automatically. The paper ejects itself; no need to pull it out.
`scan_loop.py` keeps running afterward, so just cut the next strip off the
roll and feed it in whenever you're ready -- one run handles as many
strips as you want, back to back.

The scanner's hardware/driver cap is 8.5" x 30" (confirmed via
`physicalSizeInInches`), but that's a hard ceiling, not a safe target --
this is an old friction-fed portable scanner and thermal paper likes to
curl. Cut strips to roughly 5-6 photos (~21-25", at ~3.35" per photo + a
~0.9" gap) rather than pushing close to 30", to leave margin against
skewing/jamming.

Check scanner connectivity/paper status directly:

```
python3 check_scanner.py
```

## Testing without the scanner

`make_test_strip.py` generates a synthetic scanned strip (halftone-textured
photos at random rotations, separated by blank gaps, on a realistic paper
tone) so the split/deskew/crop pipeline can be sanity-checked without
hardware:

```
python3 make_test_strip.py -o incoming/test_strip.tiff
python3 process_strip.py incoming/test_strip.tiff -o output/
```

## Tuning

- `process_strip.py --min-photo-in2`: discards blobs smaller than this many
  square inches -- raise it if dust/noise specks get picked up as photos.
- `process_strip.py --pad-in`: padding kept around each crop.
- `process_strip.py --bg-cutoff` / `--paper-cutoff`: the two brightness
  thresholds separating background/photo/paper -- adjust if a different
  scan's exposure doesn't split cleanly with the defaults (25 / 170).
- `process_strip.py --rotate {none,cw,ccw,180}`: default is `cw`, matching
  this camera's print orientation.
- If photos get merged together or over-split, the fix is usually the
  morphological close kernel size in `find_photo_regions()` (currently
  `dpi // 15`), relative to the blank gap size between photos on the strip.
