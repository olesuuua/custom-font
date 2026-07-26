#!/usr/bin/env ffpython
"""Verify contour-restoration diagnostics and block unsafe publication."""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import restore_contour_curvature as contour


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def verify_manifest(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for filename, expected in manifest["files"].items():
        actual = sha256(directory / filename)
        if actual != expected.lower():
            raise AssertionError("immutable hash changed: {}".format(directory / filename))


def layer_signature(layer):
    return tuple(tuple((round(point.x, 4), round(point.y, 4), bool(point.on_curve))
                       for point in contour) for contour in layer)


def main(allow_unresolved=False):
    verify_manifest(ROOT / "checkpoints" / "simplified-v126")
    verify_manifest(ROOT / "checkpoints" / "curvature-v2")
    verify_manifest(ROOT / "checkpoints" / "curvature-v3-pre-terminal")
    report_path = ROOT / "qa" / "assets" / "curvature-contour-report.csv"
    details_path = ROOT / "qa" / "assets" / "curvature-contour-details.json"
    review_sfd = ROOT / "fontforge" / "curvature-contour-review.sfd"
    review_ttf = ROOT / "qa" / "assets" / "curvature-contour-review.ttf"
    for path in (report_path, details_path, review_sfd, review_ttf):
        if not path.exists():
            raise AssertionError("missing diagnostic artifact: {}".format(path))
    rows = list(csv.DictReader(report_path.open(encoding="utf-8-sig")))
    if len(rows) != 334:
        raise AssertionError("expected 334 report rows, found {}".format(len(rows)))
    targets = [row for row in rows if row["contour_status"] not in ("not-targeted", "not-selected")]
    if len(targets) != 213:
        raise AssertionError("expected 213 targets, found {}".format(len(targets)))
    unresolved = [row for row in targets if row["contour_status"].startswith("unresolved")]
    safe_paths = (
        ROOT / "fontforge" / "curvature-contour-candidates.sfd",
        ROOT / "qa" / "assets" / "curvature-contour-candidates.ttf",
    )
    if any(not path.exists() for path in safe_paths):
        raise AssertionError("review-safe build is missing fallback artifacts")
    build_path = ROOT / "qa" / "assets" / "curvature-build.json"
    if unresolved:
        if not build_path.exists():
            raise AssertionError("missing diagnostic build manifest")
        build_manifest = json.loads(build_path.read_text(encoding="utf-8"))
        if not build_manifest.get("blocked"):
            raise AssertionError("diagnostic build manifest does not record blocked publication")
        if build_manifest.get("report") != sha256(report_path):
            raise AssertionError("diagnostic report hash mismatch")
        if build_manifest.get("details") != sha256(details_path):
            raise AssertionError("diagnostic details hash mismatch")
        if build_manifest.get("fonts", {}).get("review") != sha256(review_ttf):
            raise AssertionError("diagnostic review-font hash mismatch")
        if build_manifest.get("fonts", {}).get("candidate") != sha256(safe_paths[1]):
            raise AssertionError("diagnostic safe-font hash mismatch")
        if int(build_manifest.get("fallback_count", -1)) != len(unresolved):
            raise AssertionError("diagnostic fallback count mismatch")

    baseline = fontforge.open(str(ROOT / "checkpoints" / "simplified-v126" / "simplified.sfd"))
    review = fontforge.open(str(review_sfd))
    reopened = fontforge.open(str(review_ttf))
    safe = fontforge.open(str(safe_paths[0]))
    safe_reopened = fontforge.open(str(safe_paths[1]))
    approved = fontforge.open(str(ROOT / "checkpoints" / "curvature-v2" / "curvature-candidates.ttf"))
    try:
        baseline_names = [glyph.glyphname for glyph in baseline.glyphs()]
        if baseline_names != [glyph.glyphname for glyph in review.glyphs()]:
            raise AssertionError("glyph order changed")
        if baseline_names != [glyph.glyphname for glyph in safe.glyphs()]:
            raise AssertionError("safe glyph order changed")
        row_by_name = {row["glyph"]: row for row in rows}
        target_names = {row["glyph"] for row in targets}
        for name in baseline_names:
            if review[name].unicode != baseline[name].unicode:
                raise AssertionError("Unicode mapping changed: {}".format(name))
            if review[name].width != baseline[name].width or review[name].vwidth != baseline[name].vwidth:
                raise AssertionError("metrics changed: {}".format(name))
            if name not in target_names and layer_signature(review[name].foreground) != layer_signature(
                    baseline[name].foreground):
                raise AssertionError("non-target outline changed: {}".format(name))
            points = sum(len(value) for value in review[name].foreground)
            reopened_points = sum(len(value) for value in reopened[name].foreground)
            if points > 80 or reopened_points > 80:
                raise AssertionError("point budget exceeded: {}".format(name))
            row = row_by_name[name]
            if row.get("contour_points") and int(row["contour_points"]) != points:
                raise AssertionError("report point mismatch: {}".format(name))
            safe_points = sum(len(value) for value in safe[name].foreground)
            safe_ttf_points = sum(len(value) for value in safe_reopened[name].foreground)
            if safe_points > 80 or safe_ttf_points > 80:
                raise AssertionError("safe point budget exceeded: {}".format(name))
            accepted = row["contour_status"] in ("complete", "source-straight", "manual-approved")
            expected = review[name].foreground if accepted else baseline[name].foreground
            if layer_signature(safe[name].foreground) != layer_signature(expected):
                raise AssertionError("safe fallback mismatch: {}".format(name))
            if row.get("manual_review_status") == "approved-v2-lock" and layer_signature(
                    review[name].foreground) != layer_signature(approved[name].foreground):
                raise AssertionError("manual v2 lock differs: {}".format(name))
            if row["contour_status"] == "complete":
                if row["required_curve_arcs"] != row["covered_curve_arcs"]:
                    raise AssertionError("incomplete arc coverage: {}".format(name))
                if int(row["visible_off_curve_points"] or 0) <= 0:
                    raise AssertionError("no visible curve: {}".format(name))
                if int(row.get("required_terminal_arcs") or 0):
                    if row["required_terminal_arcs"] != row["covered_terminal_arcs"]:
                        raise AssertionError("incomplete terminal coverage: {}".format(name))
                    if float(row.get("terminal_boundary_p95") or 0) > contour.TERMINAL_P95_LIMIT:
                        raise AssertionError("terminal p95 failed: {}".format(name))
                    if float(row.get("terminal_boundary_max") or 0) > contour.TERMINAL_MAX_LIMIT:
                        raise AssertionError("terminal maximum failed: {}".format(name))
        targeted_priority = [row for row in targets if row.get("processing_priority")]
        sequences = {
            label: [int(row["processing_sequence"]) for row in targeted_priority
                    if row["processing_priority"] == label]
            for label in ("letters", "symbols", "arrows")
        }
        if not (max(sequences["letters"]) < min(sequences["symbols"])
                and max(sequences["symbols"]) < min(sequences["arrows"])):
            raise AssertionError("processing priority order differs")
    finally:
        for font in (baseline, review, reopened, safe, safe_reopened, approved):
            try:
                font.close()
            except RuntimeError:
                pass
    if unresolved and not allow_unresolved:
        raise AssertionError("{} unresolved targets block safe publication: {}".format(
            len(unresolved), " ".join(row["glyph"] for row in unresolved)))
    print("verified diagnostics: 213 targets, {} complete, {} unresolved".format(
        len(targets) - len(unresolved), len(unresolved)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-unresolved", action="store_true")
    args = parser.parse_args()
    main(args.allow_unresolved)
