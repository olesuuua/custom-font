# Olesuas Hand

A custom handwritten font family with completed Regular, Bold, and Italic
styles.

## Final release status

**Regular, Bold, and Italic are finalized and approved. No further drawing,
spacing, or kerning changes are required.** The release masters are
`regular-final.sfd`, `bold-final.sfd`, and `italic-final.sfd`. See
[FINAL_FONTS.md](FINAL_FONTS.md) for the release contents and verification
procedure.

## Font files

- `regular-final.sfd` — completed Regular release master
- `bold-final.sfd` — completed Bold release master
- `italic-final.sfd` — completed Italic release master
- `italic-manual-fixed-v4.sfd` — preserved approved manual Italic input
- `qa/assets/regular-final.ttf`, `qa/assets/bold-final.ttf`,
  `qa/assets/italic-final.ttf` — installable builds and final build reports
- `output/pdf/` — the Italic drawing template for future drawing rounds
- `release/OlesuasHand/` and `release/OlesuasHand-Final.zip` —
  ready-to-install three-style package

The final release contains 342 serialized glyphs in Regular and Italic and
341 in Bold. Every style includes the shared encoded repertoire, blank
U+0020 SPACE, and the U+2126 OHM SIGN compatibility glyph.

## Workflows

Every workflow that produced these files is documented in
[docs/WORKFLOWS.md](docs/WORKFLOWS.md), together with the full script list in
`tools/`:

1. **Regular simplification (first attempt)** — point-budgeted glyph
   simplification with similarity-gated rebuilding.
2. **Regular redraw repairs (v5–v9)** — stroke-model reconstruction and
   targeted repair passes with hash-bound QA review.
3. **Bold (v1–v6)** — weight build, geometric repairs, and the manual
   review/freeze cycle.
4. **Italic (v2–v10)** — XOPP-first drawing, exact-SVG import, and the
   redraw review loop; see [ITALIC_WORKFLOW.md](ITALIC_WORKFLOW.md).

The italic QA reviewer runs with `python tools/serve_italic_qa.py` and opens
at [http://127.0.0.1:8015/](http://127.0.0.1:8015/). The historical Bold QA
page used `./tools/start_bold_qa.ps1` and `tools/refresh_bold_qa.py`.

## Repository cleanup note

Intermediate iteration data (`checkpoints/`, the `fontforge/` iteration SFD
candidates, `output/` reports and preview builds, `qa/` per-version review
states, and `tmp/`) was archived outside the repository on 2026-09-01 and is
no longer tracked. The workflows remain fully documented and reproducible;
see [docs/WORKFLOWS.md](docs/WORKFLOWS.md). Intentional exceptions kept in
the repository are listed under *Font files* above.
