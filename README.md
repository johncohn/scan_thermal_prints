# scan_thermal_prints

Automates scanning strips of photos from a kid's thermal photo-printing
camera (halftone B&W images on receipt paper) using a NeatReceipts NM-1000
scanner, then splits each strip into individual photos, deskews and crops
them, and saves them as JPEGs.

## How it works

1. **`scan_watch.py`** drives the scanner via SANE's `scanimage` CLI. The
   `epjitsu` SANE backend was written specifically for the NeatReceipts /
   NeatDesk chipset, so no vendor driver is needed. It loops: try a scan,
   and if a strip is loaded, save it and wait for it to be swapped out; if
   not, wait and retry. Because the NM-1000 is a feed-through scanner, a
   scanned strip is physically ejected out the other side, so this loop
   naturally detects "a new strip was loaded" without extra sensors.

2. **`process_strip.py`** takes a raw scan (one strip, possibly containing
   several photos separated by blank paper) and:
   - blurs past the halftone dot pattern to find solid photo regions
   - finds each photo's bounding box and rotation angle
   - deskews and crops each one
   - saves each as its own JPEG

Color scanning is used by default even though the source prints are B&W,
since it reduces moire artifacts on the halftone dots. Use
`--grayscale-output` to convert the final crops to true grayscale JPEGs
regardless of scan mode.

## Setup

```
brew install sane-backends
pip install -r requirements.txt
scanimage -L        # confirm the NM-1000 is detected
```

If `scanimage -L` doesn't find the scanner, check it's plugged in and
powered, or fall back to Image Capture / a vendor-driver-based approach.

## Usage

Scan, split, and crop in one step:

```
./run_scan.sh
```

(300dpi, color scan, grayscale JPEG output — edit the flags in
`run_scan.sh` to change defaults.)

Or run the pieces directly:

```
python3 scan_watch.py --resolution 300 --mode Color --process --grayscale-output
python3 process_strip.py incoming/strip_20260729_081700.tiff -o output/
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
  square inches — raise it if dust/noise specks get picked up as photos.
- `process_strip.py --pad-in`: padding kept around each crop.
- If photos get merged together or over-split, the fix is usually the
  morphological close kernel size in `find_photo_regions()` (currently
  `dpi // 12`), relative to the blank gap size between photos on the strip.
