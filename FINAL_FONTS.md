# Final Regular and Bold Fonts

Status: **FINAL AND APPROVED — 28 August 2026**

The Regular and Bold styles are complete. Their glyph drawings, widths,
sidebearings, spaces, and kerning have been reviewed and finalized. **These
files need no further changes.** If the family is extended later, preserve this
release and create a separately versioned successor.

## Release files

| Style | FontForge master | Installable QA build |
| --- | --- | --- |
| Regular | `regular-final.sfd` | `qa/assets/regular-final.ttf` |
| Bold | `bold-final.sfd` | `qa/assets/bold-final.ttf` |

The SFD files are the archival release masters. The TTF files are generated
from those final masters and are the versions used by the sentence QA page.

`bold-manual.sfd` and `fontforge/redrawn.sfd` are preserved build inputs and
development history. They are not the release fonts and should not replace the
`*-final` files.

## Finalized behavior

- U+0020 SPACE is blank, with a width of 341 units in Regular and 381 units in
  Bold.
- Bold outlined glyphs match Regular left and right sidebearings while retaining
  their manually approved Bold shapes.
- Bold uses its finalized generated kerning set: 44,930 closer pairs, with a
  15-unit minimum and no empty-glyph pairs.
- Regular U+2126 OHM SIGN (`Omega`) is an exact copy of U+03A9 GREEK CAPITAL
  LETTER OMEGA (`uni03A9`), including outlines, metrics, and kerning behavior.
- The QA page compares identical multilingual, punctuation, currency, arrow,
  subscript, superscript, and mathematical specimens in Regular and Bold.

## Verification

Run the final verifier with FontForge's Python runtime:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_final.py
```

The detailed build record, source/output hashes, spacing changes, and Bold
kerning pairs are stored in `qa/assets/bold-final-report.json`.

The final files can be reproduced intentionally with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\finalize_bold_metrics.py --force
```

Rebuilding is not part of normal use. The checked-in release artifacts are
already complete and require no additional editing.
