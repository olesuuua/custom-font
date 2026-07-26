# Olesuas Hand

An experimental redraw of a handwritten font, focused on cleaner curves, consistent stroke weight, and compact outlines. The font contains 334 glyphs, with a hard limit of 100 serialized spline points for rebuilt glyphs.

## Files

- `fontforge/original.sfd` - original FontForge source
- `fontforge/redrawn.sfd` - current redrawn source
- `qa/assets/redrawn.ttf` - current TrueType build
- `checkpoints/redraw-v9/` - latest reproducible checkpoint, report, and review decisions

## QA

Start the local review page:

```sh
python tools/serve_redraw_qa.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). Review decisions are stored in `qa/redraw-manual-decisions.json`.

## Validation

Install the Python dependencies and run the v9 verifier with FontForge's Python runtime:

```sh
python -m pip install --target .font-deps -r tools/requirements.txt
ffpython tools/verify_redraw_v9.py
```

The verifier checks glyph count and order, cmap, metrics, layout metadata, outline scope, point limits, topology, intersections, and raster similarity.
