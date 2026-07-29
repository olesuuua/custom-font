#!/usr/bin/env python
"""Conservatively remove overlap from editable Bold in isolated batches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOLD = ROOT / "fontforge" / "bold.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-post-cleanup.csv"
META = ROOT / "qa" / "assets" / "bold-post-cleanup.json"
BATCH = ROOT / "tools" / "prepare_bold_base_batch.py"
FFPYTHON = Path(r"C:\Program Files\FontForgeBuilds\bin\ffpython.exe")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(path):
    return sum(line.startswith("StartChar: ") for line in path.read_text(
        encoding="utf-8", errors="replace").splitlines())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffpython", type=Path, default=FFPYTHON)
    args = parser.parse_args()
    before_hash = sha256(BOLD)
    rows = []
    with tempfile.TemporaryDirectory(prefix="bold-post-cleanup-", dir=str(ROOT / "qa")) as temp:
        work = Path(temp)
        source = work / "bold-before-cleanup.sfd"
        shutil.copy2(BOLD, source)
        for start in range(0, count(source), 10):
            end = min(start + 10, count(source))
            row_file = work / "rows-{:04d}.json".format(start)
            subprocess.run([
                str(args.ffpython), str(BATCH), str(source), str(BOLD),
                str(start), str(end), str(work / "glyphs"), str(row_file)
            ], check=True)
            rows.extend(json.loads(row_file.read_text(encoding="utf-8")))
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    blocked = [row["glyph"] for row in rows if row["status"] == "base_cleanup_blocked"]
    payload = {
        "version": "bold-post-cleanup-v2",
        "before_sha256": before_hash,
        "bold_sha256": sha256(BOLD),
        "accepted_count": sum(row["status"] == "clean" and row.get("changed") == "true" for row in rows),
        "blocked_count": len(blocked),
        "blocked_glyphs": blocked,
    }
    META.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
