# Olesuas Hand Italic drawing workflow

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

## Reviewing every glyph

Start the local Italic reviewer:

```powershell
python tools\serve_italic_qa.py
```

Open `http://127.0.0.1:8015/`. Both panels use the same fixed font-coordinate viewport and show ascender, cap-height, x-height, baseline, and descender guides. Compare the exact export with the actual outline read back from the TTF, then mark every slot **Pass**, **Almost done**, or **Complete rework**. Decisions are bound to source and candidate hashes, so rebuilding one glyph invalidates only that glyph's old decision.

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
