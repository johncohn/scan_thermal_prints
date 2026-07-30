# scan_thermal_prints

Automates processing strips of photos from a kid's thermal photo-printing
camera (halftone B&W images on receipt paper), scanned with a NeatReceipts
NM-1000: splits each scanned strip into individual photos, deskews and
crops them, and saves them as JPEGs.

## How it works

Scanning itself is **manual** (see "Why not fully automatic" below) -- you
trigger each scan in Image Capture as usual. Everything downstream of that
is automatic:

1. Open **Image Capture**, select the scanner, set its destination folder
   to this project's `incoming/`, resolution to 300dpi, mode to Color (see
   "Why color" below). Load a strip and click Scan.
2. **`watch_folder.py`** polls `incoming/` for new files, waits for each to
   finish writing, then hands it to `process_strip.py` and moves the raw
   scan into `incoming/processed/`.
3. **`process_strip.py`** takes a raw scan (one strip, possibly containing
   several photos separated by blank paper) and:
   - blurs past the halftone dot pattern to find solid photo regions
   - finds each photo's bounding box and rotation angle
   - deskews and crops each one
   - saves each as its own JPEG in `output/`

Color scanning is used even though the source prints are B&W, since it
reduces moire artifacts on the halftone dots. Use `--grayscale-output` to
convert the final crops to true grayscale JPEGs regardless of scan mode.

## Why not fully automatic

The original plan was to also drive the scanner itself from code (detect a
loaded strip, trigger the scan, no button click needed). Two approaches
were tried and ruled out:

- **SANE** (`scanimage`): the `epjitsu` backend is written for exactly this
  chipset family, but this specific NM-1000 unit reports USB vendor ID
  `0x1f44` ("The Neat Company"), which doesn't match any installed SANE
  backend. Dead end without protocol reverse-engineering.
- **ImageCaptureCore** (Apple's native framework, via PyObjC): this
  actually works for discovery, opening a session, and reading live scanner
  state (`check_scanner.py` uses exactly this and works). But issuing the
  actual scan command (`requestScan` / `requestScanWithOptions:completion:`)
  silently never reaches the driver -- confirmed via the driver's own debug
  log, which shows zero reaction to the command versus working instantly
  from Image Capture.app. This looks like an ImageCaptureCore restriction
  on which processes may issue actuator commands (Image Capture.app has no
  AppleScript dictionary either, so scripting it that way isn't available).
  A real compiled, properly bundled native app (Swift, not Python) might
  get past this, but that's unverified.

`check_scanner.py` is kept as a working diagnostic -- run it to confirm the
scanner is connected and whether a strip is currently loaded, without
opening Image Capture.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install --index-url https://pypi.org/simple -r requirements.txt
```

(The `--index-url` override is needed if pip is configured to use an
internal package mirror that doesn't carry `pyobjc-framework-ImageCaptureCore`.)

## Usage

```
source .venv/bin/activate
./run_scan.sh
```

Then open Image Capture, point its destination at `incoming/`, and scan.
Split/cropped JPEGs appear in `output/` automatically.

Check scanner connectivity/paper status without opening Image Capture:

```
python3 check_scanner.py
```

## Testing without the scanner

`make_test_strip.py` generates a synthetic scanned strip (halftone-textured
photos at random rotations, separated by blank gaps) so the split/deskew/
crop pipeline can be sanity-checked without hardware:

```
python3 make_test_strip.py -o incoming/test_strip.tiff
python3 process_strip.py incoming/test_strip.tiff -o output/
```

## Tuning

- `process_strip.py --min-photo-in2`: discards blobs smaller than this many
  square inches -- raise it if dust/noise specks get picked up as photos.
- `process_strip.py --pad-in`: padding kept around each crop.
- If photos get merged together or over-split, the fix is usually the
  morphological close kernel size in `find_photo_regions()` (currently
  `dpi // 12`), relative to the blank gap size between photos on the strip.
