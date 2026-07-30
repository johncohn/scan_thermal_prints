#!/bin/bash
# Watch incoming/ for scans saved there by Image Capture, split/deskew/crop
# each strip, and save grayscale JPEGs to output/.
#
# Scanning itself is still manual: open Image Capture, select the scanner,
# set its destination folder to incoming/ (300dpi, Color), then load a strip
# and click Scan (see README.md for why full automation isn't possible here).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate

python3 watch_folder.py \
    --dpi 300 \
    --grayscale-output
