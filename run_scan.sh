#!/bin/bash
# Watch the NM-1000, scan each strip at 300dpi color, split/deskew/crop, save as grayscale JPEGs.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

python3 scan_watch.py \
    --resolution 300 \
    --mode Color \
    --process \
    --grayscale-output
