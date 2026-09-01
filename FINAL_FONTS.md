# Final Regular, Bold, and Italic Fonts

Status: **FINAL AND APPROVED — Italic added 1 September 2026**

The Regular, Bold, and Italic styles are complete. Their glyph drawings,
widths, sidebearings, spaces, and kerning have been reviewed and finalized.
**These files need no further changes.** If the family is extended later,
preserve this release and create a separately versioned successor.

## Release files

| Style | FontForge master | Installable QA build |
| --- | --- | --- |
| Regular | `regular-final.sfd` | `qa/assets/regular-final.ttf` |
| Bold | `bold-final.sfd` | `qa/assets/bold-final.ttf` |
| Italic | `italic-final.sfd` | `qa/assets/italic-final.ttf` |

The SFD files are the archival release masters. The TTF files are generated
from those final masters and are the versions used by the sentence QA page.

The ready-to-install three-style family is also available in
`release/OlesuasHand/` and as `release/OlesuasHand-Final.zip`. Recreate that
package without modifying the final masters by running
`python tools/package_final_family.py`.

`bold-manual.sfd`, `fontforge/redrawn.sfd`, and
`italic-manual-fixed-v4.sfd` are preserved build inputs and development
history. They are not the release fonts and should not replace the `*-final`
files.

## Finalized behavior

- U+0020 SPACE is blank, with a width of 341 units in Regular and Italic and
  381 units in Bold.
- Bold outlined glyphs match Regular left and right sidebearings while retaining
  their manually approved Bold shapes.
- Bold uses its finalized generated kerning set: 44,930 closer pairs, with a
  15-unit minimum and no empty-glyph pairs.
- Italic preserves the approved manual v4 drawings and metrics and contains
  65,969 finalized kerning pairs, with a 15-unit minimum and no empty-glyph
  pairs.
- U+2126 OHM SIGN (`Omega`) matches U+03A9 GREEK CAPITAL LETTER OMEGA
  (`uni03A9`) in every final style, including outlines, metrics, and kerning
  behavior.
- The QA page compares identical multilingual, punctuation, currency, arrow,
  subscript, superscript, and mathematical specimens in all three styles.

## Verification

Run the final verifier with FontForge's Python runtime:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_final.py
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_italic_final.py
```

The detailed build records and source/output hashes are stored in
`qa/assets/bold-final-report.json` and `qa/assets/italic-final-report.json`.

The final files can be reproduced intentionally with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\finalize_bold_metrics.py --force
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\finalize_italic_release.py --force
```

Rebuilding is not part of normal use. The checked-in release artifacts are
already complete and require no additional editing.
