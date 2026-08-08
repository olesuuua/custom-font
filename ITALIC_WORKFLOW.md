# Olesuas Hand Italic drawing workflow

The generated template contains all 334 visible outlines from `fontforge/redrawn.sfd`. The six empty/control glyphs are intentionally absent and are copied unchanged when the Italic font is built.

## Drawing in Xournal++

1. Open `output/pdf/olesuas-hand-italic-drawing-template.pdf` with **File > Annotate PDF**.
2. Rename the top annotation layer exactly `ITALIC INK` on every page.
3. Draw only with a solid black pressure-sensitive pen. Keep labels, guides, and the gray Regular glyph in the PDF background.
4. Do not crop, resize, rotate, reorder, add, or delete pages.
5. Save the journal as `.xopp`.
6. Export all 12 pages as SVG files into one directory.
7. Keep the `.xopp`, the 12 SVG pages, and the original template PDF together.

The 15-degree rails are drawing guides. The pale glyph remains upright so the italic construction is visibly yours rather than a mechanical shear.

## Building after the drawings are returned

Run the importer with normal Python. It performs the PathOps cleanup and then launches FontForge's Python worker automatically:

```powershell
python tools\import_italic_xopp.py path\to\drawings.xopp --svg-pages path\to\svg-pages
```

The importer:

- validates 12 unchanged A4 pages and the exact `ITALIC INK` layer;
- reads black pen strokes and pressure samples from `.xopp`;
- assigns strokes using the manifest's page/row/column coordinates;
- expands, unions, clips, and simplifies each outline;
- writes inspectable expanded and cleaned per-glyph SVG files under `output/italic-import/`;
- applies 50-unit automatic left and right sidebearings;
- creates `fontforge/italic.sfd` and `output/font/OlesuasHand-Italic.ttf`;
- preserves all glyph slots and empty controls while removing Regular kerning.

Use `--extract-only` to inspect cut SVGs without changing or creating a font. Use `--allow-partial` only for test builds; the normal full build refuses incomplete templates.

## Verification

Regenerate the template:

```powershell
python tools\generate_italic_template.py
```

Verify the manifest, PDF geometry, Xournal++ extraction, and a temporary FontForge round trip:

```powershell
python tools\verify_italic_workflow.py --full-fontforge
```
