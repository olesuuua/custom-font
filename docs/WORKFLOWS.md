# Olesuas Hand — build workflows

This file documents the workflows that produced the final Regular, Bold, and
Italic releases. The full per-iteration artifacts (checkpoints, per-version
reports, preview TTFs, and intermediate SFD files) were archived outside the
repository on 2026-09-01; every step below is reproducible from the scripts in
`tools/`, which are the durable record of each workflow.

Contents of the archive (not in the repository):

- `checkpoints/` — frozen per-version SFD sources, reports, and decision files
- `fontforge/` — iteration SFD candidates (simplified, redraw, curvature,
  italic v2–v10 review files, bold batch/pilot files)
- `output/` — per-version glyph reports, manifests, preview TTFs, per-glyph
  SVGs, and italic template page SVGs
- `qa/` — per-version QA page states and review decision JSON files
- `tmp/` — scratch analysis

Only the final release masters, the final TTFs and reports under
`qa/assets/`, the italic drawing template under `output/pdf/`, `tools/`, and
the documentation are kept in the repository.

## 1. Regular — simplification (first attempt)

Reduced the drawn Regular outlines (`fontforge/original.sfd` in the archive)
to a point-budgeted, curve-consistent font.

1. `python tools/simplify_font.py` — the core pipeline: overlap removal,
   contour-direction preservation, per-glyph pipeline overrides, soft point
   cap tracking, conservative curve smoothing, and isolated outline
   rebuilding with similarity gating (glyphs whose rebuild added points or
   drifted too far fall back to the original outline).
2. `python tools/inspect_glyphs.py`, `python tools/fast_highres_raster.py`,
   `python tools/experiment_cyrillic.py` — inspection and raster comparison.
3. `python tools/verify_build.py`, `python tools/verify_contour_curvature.py`
   — build and curvature verification.
4. `python tools/restore_curvature.py`,
   `python tools/restore_contour_curvature.py`,
   `python tools/restore_metadata.py` — curvature/metadata restoration passes.
5. `python tools/repair_almost_done_glyphs.py` — point-budget relocation for
   glyphs that nearly passed, then topology-safe refinement and parallel
   simplified-v2 recovery until the 80-point simplification completed.

Result: `fontforge/simplified.sfd` and redraw candidates (archived),
superseded by the redraw workflow below.

## 2. Regular — redraw and targeted repairs (v5–v9)

1. `fontforge -lang=py -script tools/redraw_font.py` — stroke-model
   reconstruction of the simplified outlines.
2. Iterative repair passes: `tools/repair_redraw_v2.py` …
   `tools/repair_redraw_v9.py` (targeted repairs, curve and fraction repairs,
   evenly distributed curve anchors, stroke-model reconstruction), with
   `tools/reconcile_redraw_v3.py`, `tools/reconcile_redraw_v4.py`,
   `tools/reconcile_redraw_v8.py`, `tools/reconcile_redraw_v9.py`,
   `tools/reconcile_targeted_repairs.py`, and
   `tools/repair_uni0451_dots_v4.py`.
3. Review: `python tools/serve_redraw_qa.py` (local QA server),
   `tools/refresh_redraw_status.py`, decisions frozen with
   `tools/freeze_redraw_review*.py`, metadata preserved with
   `tools/restore_redraw_metadata.py`.
4. Verification per version: `tools/verify_redraw.py`,
   `tools/verify_redraw_v4.py` … `tools/verify_redraw_v9.py`,
   `tools/verify_targeted_repairs.py`.

Result: `regular-final.sfd` (the immutable release master).

## 3. Bold — weight build, repairs, and manual review (v1–v6)

1. Base: `tools/build_bold.py`, `tools/prepare_bold_base.py` /
   `prepare_bold_base_v2.py` / `prepare_bold_base_batch.py` — overlap-cleaned
   Bold derived from Regular.
2. v2/v3 repair passes: `tools/build_bold_v2.py`, `tools/protect_bold_v2.py`,
   `tools/apply_bold_protection_batch.py`,
   `tools/protect_bold_glyph_worker.py`, `tools/post_cleanup_bold_v2.py`,
   `tools/cleanup_bold_base_worker.py`, `tools/run_bold_v3_cleanup.py`,
   `tools/repair_bold_geometric_v3.py`, `tools/repair_bold_dots_v3.py`,
   `tools/protect_bold_dot_components_v3.py`,
   `tools/migrate_bold_v3_decisions.py`.
3. v4: `tools/repair_bold_v4.py`, `tools/repair_bold_v4_pathops.py`,
   `tools/repair_bold_v4_residuals.py`,
   `tools/repair_bold_v4_canonical_rings.py`,
   `tools/repair_bold_v4_six_counters.py`,
   `tools/resolve_bold_v4_counters.py`, `tools/union_bold_v4_percent.py`,
   `tools/union_bold_v4_white_fills.py`, `tools/fill_bold_v4_unmatched.py`,
   `tools/raster_rebuild_bold_v4.py`,
   `tools/finalize_bold_v4_cleanup.py`, `tools/migrate_bold_v4_decisions.py`,
   `tools/repair_uni0451_dots_v4.py`.
4. v4.1/v5 batches: `tools/build_bold_pilot.py`,
   `tools/repair_bold_batch.py`, `tools/repair_bold_batch2.py`,
   `tools/promote_bold_v51.py`, `tools/audit_bold_v51_indents.py`,
   `tools/migrate_bold_v51_decisions.py`.
5. v6 manual correction cycle: `tools/build_bold_v6.py`,
   `tools/refresh_bold_qa.py`, `tools/migrate_bold_v6_decisions.py`,
   `tools/freeze_bold_v6.py`, served by `tools/start_bold_qa.ps1` /
   `tools/serve_qa.py`.
6. Verification per version: `tools/verify_bold.py`,
   `tools/verify_bold_v2.py` … `tools/verify_bold_v6.py`.

Do not rerun repair or migration tools after manual edits unless intentionally
starting a new repair pass. Result: `bold-final.sfd`; verify with
`tools/verify_bold_final.py` and finalize metrics with
`tools/finalize_bold_metrics.py`.

## 4. Italic — XOPP-first drawing and redraw review (v2–v10)

Full procedure in [ITALIC_WORKFLOW.md](../ITALIC_WORKFLOW.md). Summary:

1. `python tools/generate_italic_template.py` — drawing template (kept in
   `output/pdf/`).
2. Draw in Xournal++ over the template, export the 12 pages as SVG.
3. `python tools/import_italic_xopp.py drawings.xopp --svg-pages <dir>` —
   exact-SVG outlines unioned with PathOps; optional `--outline-mode compare`
   / `xopp-compact` for XOPP centerline experiments.
4. `python tools/verify_italic_workflow.py --full-fontforge` — pipeline
   verification.
5. Redraw review loop: `tools/redraw_italic_v4.py` …
   `tools/redraw_italic_v10.py` with `tools/verify_italic_v4.py` …
   `tools/verify_italic_v10.py`; review with `python tools/serve_italic_qa.py`
   at `http://127.0.0.1:8015/`.

Result: `italic-final.sfd` (with `italic-manual-fixed-v4.sfd` as the approved
manual input); verify with `tools/verify_italic_final.py`.

## 5. Release packaging

`python tools/package_final_family.py` — builds `release/OlesuasHand/` and
`release/OlesuasHand-Final.zip` from the three final masters without
modifying them. See [FINAL_FONTS.md](../FINAL_FONTS.md).

## Environment

Python dependencies: `tools/requirements.txt` (locked in
`tools/requirements.lock`). FontForge steps require FontForge's Python
runtime (`fontforge -lang=py -script …`).
