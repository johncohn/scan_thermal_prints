#!/bin/bash
# Fully automatic: watches the feeder, scans each strip itself (no Image
# Capture needed), splits/deskews/crops, and saves grayscale JPEGs to
# output/. Just load a strip and this handles the rest.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate

python3 scan_loop.py \
    --resolution 300 \
    --mode Color \
    --process \
    --grayscale-output
