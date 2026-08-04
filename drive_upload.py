#!/usr/bin/env python3
"""Upload processed photos to a Google Drive folder via rclone.

Setup (one-time, already done for this project):
    brew install rclone
    rclone authorize "drive"
    rclone config create gdrive drive token='<paste token JSON>' root_folder_id='<drive folder id>'

Everything here is relative to the rclone remote's configured
root_folder_id, so a "folder name" argument below becomes a subfolder of
that target Drive folder.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REMOTE = "gdrive"


def upload_files(paths: list[Path], folder_name: str, remote: str = DEFAULT_REMOTE) -> int:
    """Upload all of paths into the named Drive subfolder in a single rclone
    invocation (rclone creates the destination folder automatically, and
    transfers files concurrently). Skips any file that already exists there
    -- this is the dedup mechanism.

    Doing this as one call instead of one rclone process per file matters:
    each invocation pays its own startup/auth overhead, so uploading a
    strip's worth of photos one-by-one was the dominant source of the delay
    after each scan.
    """
    if not paths:
        return 0
    src_dir = paths[0].parent
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(p.name for p in paths))
        list_path = f.name
    try:
        result = subprocess.run(
            ["rclone", "copy", str(src_dir), f"{remote}:{folder_name}",
             "--files-from-raw", list_path, "--ignore-existing"],
            capture_output=True, text=True,
        )
    finally:
        Path(list_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"drive: upload failed: {result.stderr.strip()}", file=sys.stderr)
        return 0
    for p in paths:
        print(f"  uploaded {p.name} -> {remote}:{folder_name}/")
    return len(paths)
