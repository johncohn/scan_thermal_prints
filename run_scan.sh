#!/bin/bash
# Fully automatic: watches the feeder, scans each strip itself (no Image
# Capture needed), splits/deskews/crops, saves grayscale JPEGs to output/,
# and uploads them to Google Drive. Prompts for a batch name after each
# strip (Enter to keep the same one).
#
# The vendor's scanner driver is old and occasionally crashes outright
# (a native segfault, not something Python can catch) -- if that happens
# this just restarts scan_loop.py automatically; numbering/uploads pick up
# where they left off since they're derived from what's already on disk.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    echo "no .venv found, creating one..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --quiet --index-url https://pypi.org/simple -r requirements.txt
else
    source .venv/bin/activate
fi

while true; do
    if python3 scan_loop.py \
        --resolution 300 \
        --mode Color \
        --process \
        --grayscale-output \
        --upload; then
        break
    fi
    echo "scan_loop.py exited unexpectedly, restarting in 2s (Ctrl+C to stop for good)..."
    sleep 2
done
