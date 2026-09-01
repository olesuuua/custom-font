# Olesuas Hand Italic drawing workflow

## Final release

Italic is finalized. The immutable release master is `italic-final.sfd`, and
the installable build is `qa/assets/italic-final.ttf`. The approved manual input
is preserved as `italic-manual-fixed-v4.sfd`; do not edit the `*-final` files in
place. Verify the release with:

```bash
fontforge -lang=py -script tools/verify_italic_final.py
```

The remaining workflow is retained as development history.

The generated template contains 334 review slots. The 333 handwritten outlines come from the exported page SVGs; `.notdef` is preserved from Regular. Six empty/control glyphs remain empty.

## Drawing in Xournal++

1. Open `output/pdf/olesuas-hand-italic-drawing-template.pdf` with **File > Annotate PDF**.
2. Prefer naming the annotation layer `ITALIC INK` on every page. The importer also accepts one unambiguous unnamed layer, as in the current journal, and records a warning.
3. Draw only with a solid black pressure-sensitive pen. Keep labels, guides, and the gray Regular glyph in the PDF background.
4. Do not crop, resize, rotate, reorder, add, or delete pages.
5. Save the journal as `.xopp`.
6. Export all 12 pages as SVG files into one directory.
7. Keep the `.xopp`, the 12 SVG pages, and the original template PDF together.

The 15-degree rails are drawing guides. The pale glyph remains upright so the italic construction is visibly yours rather than a mechanical shear.

## Building after the drawings are returned

Run the importer with Python. `--svg-pages` is required because these files, not reconstructed XOPP centreline strokes, are the authoritative outlines:

```powershell
python tools\import_italic_xopp.py path\to\drawings.xopp --svg-pages path\to\svg-pages
```

The importer:

- validates 12 unchanged A4 pages and an unambiguous ink layer;
- pairs every exported SVG path with its XOPP stroke by page order and verifies their bounds;
- uses XOPP only for glyph assignment, thickness reporting, and provenance;
- unions exact SVG outlines with PathOps while preserving counters; it performs no whole-glyph thickness normalization, FontForge overlap removal, clipping, or simplification;
- applies versioned optical repairs only to `at`, `braceleft`, and `uni212B`;
- writes exact-source, canonical, targeted-override, imported, and actual final-TTF per-glyph SVGs under `output/italic-import/`;
- applies 50-unit automatic left and right sidebearings;
- creates `fontforge/italic-review.sfd`, `output/font/OlesuasHand-Italic-review.ttf`, per-glyph SVGs, a CSV geometry report, and a build manifest;
- preserves all glyph slots and empty controls while removing Regular kerning.

The only accepted drawing fallback is `.notdef`, which is preserved from Regular. Use `--extract-only` to inspect SVGs without creating a font. Use `--allow-partial` only for synthetic tests.

## Comparing XOPP-first compact outlines

The exact-SVG workflow remains the default. To benchmark pressure-aware XOPP
centerline reduction without changing the review font geometry, run:

```powershell
python tools\import_italic_xopp.py path\to\drawings.xopp --svg-pages path\to\svg-pages --outline-mode compare
```

Use `--compact-glyph H --compact-glyph Q` to limit an exploratory run. The
comparison tests pressure-aware polyline reduction and open cubic fitting. Each
reconstructed outline is checked against the exported SVG at 32, 64, and 128
pixels, with component/counter checks at 128, 256, 512, and 1024 pixels. Bounds,
ink error, boundary error, point counts, processing time, and fallback reasons
are written to `italic-glyph-report.csv` and
`italic-compact-comparison.json`.

To build the hybrid candidate font, use `--outline-mode xopp-compact`. The
smallest passing candidate is selected per glyph; a glyph for which no smaller
candidate passes every gate remains the exact SVG outline and is reported as
`exact-fallback`. Targeted repairs remain versioned and are applied before the
reference/candidate comparison. Do not make this mode the production default
unless the comparison report recommends it and the visual review is accepted.

The 2026-08-25 full-family evaluation is preserved in
`output/italic-compact-evaluation.json`: only 15 of 333 drawn glyphs compacted
with a real point saving (4.50%), reducing total editable outline points by
1.97%. This fails the 90% coverage and 70% reduction gates, so exact SVG remains
the production default. XOPP compaction is retained as a safe experimental tool
and possible narrow per-glyph workflow, not as the family-wide replacement.
For comparison, the existing Regular redraw fitter reduced the complete Italic
outline from 317,938 to 25,675 editable points (91.92%); 189 of 334 outlined
glyphs passed automatically and 145 require manual review. FontForge Simplify
at a 1.1-unit tolerance reduced the outline to 100,402 points but failed the
bounds gate on 275 glyphs. The measured answer is therefore: XOPP-first is not
faster or easier family-wide, its exact fallback is reliable, and it preserves
shape safely only because it declines almost all complex glyphs. Continue with
the established redraw workflow for Italic, using XOPP compaction only for the
15 passing glyphs listed in `output/italic-compact-evaluation.json` if desired.

Those 15 fidelity-approved candidates are promoted for manual inspection in
`fontforge/italic-v2-review.sfd`, with the matching preview font at
`output/font/OlesuasHand-Italic-v2-review.ttf`. All other glyphs remain exact.
The promoted glyph list, file hashes, and verification results are recorded in
`output/italic-v2/manifest.json`; the original `italic-review.sfd` remains
unchanged. `output/italic-v2/README.md` describes the candidate artifacts and
manual inspection boundary.

## Reviewing every glyph

### Italic v8 hybrid centerline/detail redraw

The accepted v2 geometry is redrawn without modifying either earlier Italic source. Build and verify the isolated candidate with:

Each newly generated Italic variation receives the next whole-number version (v8, v9, and so on). Decimal or point-release candidate names are not used.

```bash
fontforge -lang=py -script tools/redraw_italic_v8.py --workers 8
fontforge -lang=py -script tools/verify_italic_v8.py
```

Every non-empty editable SFD glyph is capped at 100 points. v8 changes six slots: shared `Beta`/`uni0412`, `S`, `six`, `uni0416`, and `uni042A`. `S` and `six` are rebuilt from smoothed XOPP centerlines with coupled stroke boundaries; junction and loop glyphs use adaptive source fitting with explicitly protected detail anchors and local cubic repairs. The report and family registry are stored under `output/italic-v8-redraw/`.

The v8 QA page shows exactly the accepted v2 source and the actual v8 TTF outline on a shared 1,443-unit vertical viewport, so descenders and overlays remain visible and aligned. Decisions are bound to the v2 source hash and actual v8 TTF outline hash. Saving a decision propagates the lowest tier to registered identical Latin/Cyrillic/Greek forms. Finalization refuses any missing, non-pass, or stale decision:

```bash
fontforge -lang=py -script tools/redraw_italic_v8.py --finalize
```

### Italic v9 feedback-directed redraw

v9 is built from the reviewed v8 candidate and changes only the 15 glyphs
identified in the v8 review. Exact donor copies are used for equivalent
Latin/Cyrillic/Greek forms. Structural glyphs use protected-detail ladders or
clean earlier donors instead of fixed whole-contour simplification. The
seven-eighths glyph uses the selected v5 outline: its seven, fraction slash,
lower loop, counters, and all on-curve coordinates are preserved, while only
the top-apex handles of the 8 are aligned.

```sh
fontforge -lang=py -script tools/redraw_italic_v9.py --workers 8
fontforge -lang=py -script tools/verify_italic_v9.py
python3 tools/serve_italic_qa.py
```

The v9 QA page compares the accepted v2 source with the actual v9 TTF outline.
It inherits decisions only when both bound hashes are unchanged; all 15 changed
slots return to unreviewed. v9 remains an uncommitted review candidate until
the next QA round is complete.

Start the local Italic reviewer:

```powershell
python tools\serve_italic_qa.py
```

Open `http://127.0.0.1:8015/`. The exact export, optional XOPP compact candidate,
and actual TTF panels use the same fixed font-coordinate viewport and guides.
Compare them, then mark every slot **Pass**, **Almost done**, or **Complete
rework**. Decisions are bound to source and actual candidate hashes; running
comparison mode alone does not invalidate exact-mode decisions.

After all 334 slots are passed, create the release artifacts:

```powershell
python tools\import_italic_xopp.py --finalize
```

This writes `fontforge/italic.sfd` and `output/font/OlesuasHand-Italic.ttf`. Finalization refuses incomplete or stale reviews.

## Verification

Regenerate the template:

```powershell
python tools\generate_italic_template.py
```

Verify the manifest, PDF geometry, structural source compatibility, Xournal++ extraction, and a temporary FontForge round trip:

```powershell
python tools\verify_italic_workflow.py --full-fontforge
```
