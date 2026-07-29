#!/usr/bin/env python
"""Protect counters and separated parts in editable Bold, preserving raw Bold."""

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
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
PROTECTION = ROOT / "qa" / "assets" / "bold-protection.json"
BUILD = ROOT / "qa" / "assets" / "bold-build.json"
WORKER = ROOT / "tools" / "protect_bold_glyph_worker.py"
APPLY = ROOT / "tools" / "apply_bold_protection_batch.py"
FFPYTHON = Path(r"C:\Program Files\FontForgeBuilds\bin\ffpython.exe")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def needs_protection(row):
    for size in (128, 256, 512):
        if int(row["bold_components_{}".format(size)]) < int(row["regular_components_{}".format(size)]):
            return True
        if int(row["bold_counters_{}".format(size)]) < int(row["regular_counters_{}".format(size)]):
            return True
    return int(row["regular_counters_128"]) > 0 and float(row["counter_area_ratio"]) < 0.75


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ffpython", type=Path, default=FFPYTHON)
    args = parser.parse_args()
    if not args.force:
        raise SystemExit("Protection rebuild replaces editable bold.sfd; pass --force.")
    rows = list(csv.DictReader(REPORT.open(encoding="utf-8-sig", newline="")))
    names = [row["glyph"] for row in rows if needs_protection(row)]
    shutil.copy2(RAW, BOLD)
    results = []
    candidates = []
    with tempfile.TemporaryDirectory(prefix="bold-protection-", dir=str(ROOT / "qa")) as temp:
        work = Path(temp)
        for index, name in enumerate(names):
            output = work / "{:04d}.sfd".format(index)
            report = work / "{:04d}.json".format(index)
            run = subprocess.run(
                [str(args.ffpython), str(WORKER), str(BASE), name, str(output), str(report)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {
                "glyph": name, "status": "blocked", "error": "worker-crashed"
            }
            results.append(data)
            if run.returncode == 0 and data.get("status") == "ok" and output.exists():
                candidates.append({"glyph": name, "sfd": str(output)})
        for start in range(0, len(candidates), 10):
            manifest = work / "apply-{:04d}.json".format(start)
            manifest.write_text(json.dumps(candidates[start:start + 10]), encoding="utf-8")
            subprocess.run([str(args.ffpython), str(APPLY), str(BOLD), str(manifest)], check=True)
    blocked = [item["glyph"] for item in results if item.get("status") != "ok"]
    payload = {
        "version": "bold-protection-v2",
        "base_sha256": sha256(BASE),
        "raw_bold_sha256": sha256(RAW),
        "bold_sha256": sha256(BOLD),
        "considered_count": len(names),
        "protected_count": len(candidates),
        "blocked_count": len(blocked),
        "blocked_glyphs": blocked,
        "review_ready": not blocked,
        "glyphs": results,
    }
    PROTECTION.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    build = json.loads(BUILD.read_text(encoding="utf-8"))
    build["bold_sha256"] = payload["bold_sha256"]
    build["protection_report"] = "qa/assets/bold-protection.json"
    for item in results:
        if item.get("status") == "ok":
            build["cleanup_methods"][item["glyph"]] = item["method"]
    BUILD.write_text(json.dumps(build, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in (
        "considered_count", "protected_count", "blocked_count", "blocked_glyphs", "review_ready"
    )}, sort_keys=True))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
