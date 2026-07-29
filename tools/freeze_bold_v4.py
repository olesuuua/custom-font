#!/usr/bin/env python3
"""Freeze the verified Bold v4 sources, builds, reports, and review state."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "checkpoints" / "bold-v4"

ARTIFACTS = {
    "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
    "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
    "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
    "regular-bold-base.ttf": ROOT / "qa" / "assets" / "regular-bold-base.ttf",
    "bold-raw.ttf": ROOT / "qa" / "assets" / "bold-raw.ttf",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-ready-to-pass.json": ROOT / "qa" / "bold-ready-to-pass.json",
    "bold-v4-repairs.json": ROOT / "qa" / "assets" / "bold-v4-repairs.json",
    "bold-v4-pathops.json": ROOT / "qa" / "assets" / "bold-v4-pathops.json",
    "bold-v4-final-cleanup.json": ROOT / "qa" / "assets" / "bold-v4-final-cleanup.json",
    "bold-v4-residuals.json": ROOT / "qa" / "assets" / "bold-v4-residuals.json",
    "bold-v4-white-patch-fills.json": ROOT / "qa" / "assets" / "bold-v4-white-patch-fills.json",
    "bold-v4-white-union.json": ROOT / "qa" / "assets" / "bold-v4-white-union.json",
    "bold-v4-counter-resolution.json": ROOT / "qa" / "assets" / "bold-v4-counter-resolution.json",
    "bold-v4-raster-rebuild.json": ROOT / "qa" / "assets" / "bold-v4-raster-rebuild.json",
    "bold-v4-canonical-rings.json": ROOT / "qa" / "assets" / "bold-v4-canonical-rings.json",
    "bold-v4-percent-union.json": ROOT / "qa" / "assets" / "bold-v4-percent-union.json",
    "bold-v4-uni0451-dots.json": ROOT / "qa" / "assets" / "bold-v4-uni0451-dots.json",
    "bold-v4-six-counters.json": ROOT / "qa" / "assets" / "bold-v4-six-counters.json",
    "qa-index.html": ROOT / "qa" / "index.html",
    "README.md": ROOT / "README.md",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if (CHECKPOINT / "manifest.json").exists() and not args.force:
        raise SystemExit("Bold v4 checkpoint already exists; use --force to replace it intentionally")
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        if not source.is_file():
            raise SystemExit(f"Missing checkpoint artifact: {source}")
        target = CHECKPOINT / name
        shutil.copy2(source, target)
        hashes[name] = sha256(target)
    report = json.loads((ROOT / "qa" / "assets" / "bold-report.json").read_text("utf-8"))
    manifest = {
        "version": "bold-v4",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "fontforge/redrawn.sfd",
        "cleaned_base": "fontforge/regular-bold-base.sfd",
        "reviewed_predecessor": "checkpoints/bold-v3-reviewed",
        "settings": {
            "weight_change": 40,
            "type": "LCG",
            "counter_type": "retain",
            "metrics_version": "bold-metrics-v4",
            "decision_version": "bold-manual-review-v4",
            "dot_scale_from_v3": 0.95,
            "dot_centroid_reference_px": 512,
            "dot_center_tolerance_units": 1,
            "persistent_topology_px": [256, 512],
            "ready_iou_minimum": {"64": 0.995, "128": 0.995},
            "required_counter_area_minimum": 0.75,
            "required_counter_width_proxy_minimum": 0.70,
            "unmatched_white_region_count": report["unmatched_white_region_count"],
            "self_intersection_glyph_count": report["self_intersection_glyph_count"],
        },
        "artifact_hashes": hashes,
    }
    (CHECKPOINT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Frozen {len(hashes)} artifacts in {CHECKPOINT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
