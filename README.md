# Olesuas Hand

A custom handwritten font family with completed Regular, Bold, and Italic
styles.

## Final release status

**Regular, Bold, and Italic are finalized and approved. No further drawing,
spacing, or kerning changes are required.** The release masters are
`regular-final.sfd`, `bold-final.sfd`, and `italic-final.sfd`; the installable
builds use the corresponding `*-final.ttf` names under `qa/assets/`.

Treat these six files as the immutable completed release. Earlier `redrawn`,
`bold-v6`, `bold-manual`, and Italic review files remain only as development
history or build inputs. See [FINAL_FONTS.md](FINAL_FONTS.md) for the release
contents and verification procedure.

## Font files

- `regular-final.sfd` — completed Regular release master; no changes needed
- `bold-final.sfd` — completed Bold release master; no changes needed
- `italic-final.sfd` — completed Italic release master; no changes needed
- `qa/assets/regular-final.ttf` — completed installable Regular font
- `qa/assets/bold-final.ttf` — completed installable Bold font
- `qa/assets/italic-final.ttf` — completed installable Italic font
- `italic-manual-fixed-v4.sfd` — preserved approved manual Italic release input
- `fontforge/redrawn.sfd` — preserved Regular build input (weight 400; never cleaned in place)
- `fontforge/regular-bold-base.sfd` — overlap-cleaned copy used only as the Bold source
- `fontforge/bold-raw.sfd` — untouched +40 diagnostic
- `fontforge/bold.sfd` — historical approved Bold 1.002 source
- `fontforge/bold-v6.sfd` — historical pre-final Bold v6 candidate (version 1.003)
- `fontforge/italic-review.sfd` — immutable exact-SVG Italic review baseline
- `fontforge/italic-v2-review.sfd` — hybrid Italic candidate with 15 fidelity-approved compact glyphs
- `fontforge/italic-v4-redraw-review.sfd` — frozen Italic v4 checkpoint
- `fontforge/italic-v5-redraw-review.sfd` — isolated ≤100-editable-point Italic v5 review candidate
- `fontforge/italic-v6-redraw-review.sfd` — frozen Italic v6 checkpoint
- `fontforge/italic-v7-redraw-review.sfd` — historical localized-repair Italic v7 review candidate
- `output/font/OlesuasHand-Italic-v4-redraw-review.ttf` — frozen Italic v4 preview font
- `output/font/OlesuasHand-Italic-v5-redraw-review.ttf` — matching Italic v5 preview font
- `output/font/OlesuasHand-Italic-v6-redraw-review.ttf` — matching Italic v6 preview font
- `output/font/OlesuasHand-Italic-v7-redraw-review.ttf` — matching Italic v7 preview font
- `output/font/OlesuasHand-Italic-v2-review.ttf` — matching Italic v2 preview font
- `qa/assets/redrawn.ttf` and `bold-v6.ttf` — historical Regular/Bold v6 QA builds
- `checkpoints/bold-v2-reviewed/` — preserved reviewed Bold v2 and its decisions
- `checkpoints/bold-v3-reviewed/` — preserved fully reviewed Bold v3 and its decisions
- `checkpoints/bold-v4/` — frozen verified Bold v4 sources, reports, decisions, and settings

The final release contains 342 serialized glyphs in Regular and Italic and 341
in Bold. Every style includes the shared encoded repertoire, blank U+0020
SPACE, and the U+2126 OHM SIGN compatibility glyph.

## Historical Italic drawing and review

This workflow produced the completed `italic-final.sfd` release and is retained
as build history. The Italic importer pairs pressure-sensitive XOPP centreline
strokes with the 12 exported SVG pages. The SVG outlines remain the fidelity
reference and safe fallback. Full-family measurement showed that XOPP-first
fitting safely reduced only 15 of 333 drawn glyphs, so exact SVG remained the
production default during development.

The 15 passing candidates are isolated in `fontforge/italic-v2-review.sfd` for manual inspection; every other glyph is unchanged from `fontforge/italic-review.sfd`. Scope, hashes, point-count results, and validation are recorded in `output/italic-v2/manifest.json` and `output/italic-v2/README.md`.

Italic v7 preserves the committed v6 checkpoint and changes seven glyph slots. `Beta` and `uni0412` share one exact canonical outline; `S` preserves its terminal detail; `six` uses longer outer cubic spans; `uni0416` receives a local protected-corner junction repair; `uni042A` aligns handles only on its top loop; and `uni042B` combines a path-preserved left half with the approved `l` on the right. The result contains 27,791 editable points (91.08% fewer than v2), retains the 100-point ceiling, and leaves 327 unchanged decisions valid. Run `fontforge -lang=py -script tools/redraw_italic_v7.py --workers 8`, verify with `fontforge -lang=py -script tools/verify_italic_v7.py`, then review v2 against the actual v7 TTF at `http://127.0.0.1:8015/`.

Start the reviewer with `python tools\serve_italic_qa.py`, then open [http://127.0.0.1:8015/](http://127.0.0.1:8015/). Opening `qa/italic.html` directly is supported for read-only review; saving decisions requires the server. See `ITALIC_WORKFLOW.md` for build, comparison, acceptance, and verification commands.

## Historical Bold v6 editing and review cycle

This workflow produced the now-completed Bold release and is retained as build
history. The `*-final` release files supersede this candidate. Bold v6 is one
complete 340-glyph candidate, not a set of visible batches. Rebuild its report
with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\build_bold_v6.py --force
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\refresh_bold_qa.py
python tools\migrate_bold_v6_decisions.py
```

The historical guarded review server can be started with
`./tools/start_bold_qa.ps1`, using `-ReplaceProjectServer` when it identifies an
older server from this project. The active `qa/index.html` now shows final
Regular/Bold/Italic sentence specimens instead of glyph review controls.

The page shows only untouched Regular, current full Bold v6, and an optional overlay. Search, review state, glyph category, and the compact issue filter remain available. Historical raw, base, pilot, and batch fonts are retained only in checkpoints and are not loaded by the active page.

Decisions are written atomically to `qa/bold-manual-decisions.json`. Every glyph whose v6 outline differs from approved Bold 1.002 is removed from the active decision document and appears Unreviewed, regardless of its earlier state. Unchanged glyph decisions remain hash-bound and valid. A glyph changed again in a later full revision resets to Unreviewed again.

Freeze the active full revision with `python tools/freeze_bold_v6.py --revision revision-NN-name`. Run the full verifier with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_v6.py
```

Intentional filled counters were permitted for `asterisk`, `uni041D`,
`uni0427`, and `uni043D` during that review stage.

## Bold v4.1 triage

Bold v4.1 keeps all SFD sources unchanged, replaces the generic counter/component warning with specific evidence-based classifications, and preserves the existing manual decisions. Opening `qa/index.html` directly now displays recovery instructions because review APIs require the local HTTP server.

## Bold v5 pilot

The isolated pilot is built from `checkpoints/bold-v4/bold.sfd`; it never overwrites `fontforge/bold.sfd`. The QA page can preview the accepted pilot and the rejected manual alternatives. Review controls are disabled in preview modes so decisions remain bound to final Bold. Rebuild intentionally with `ffpython tools\build_bold_pilot.py --backend auto --force`.

## Bold v5 Batch 1 review

Batch 1 is an isolated eight-glyph candidate in `fontforge/bold-v5-batch1.sfd`; it does not overwrite the editable `fontforge/bold.sfd`. On the QA page, set Review state to **All states**, set Automatic finding to **Bold v5 Batch 1**, then press **Bold v5 Batch 1** to compare the candidate against Regular. Review controls are disabled in candidate mode.

Rebuild and verify the frozen candidate with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\repair_bold_batch.py --force
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_v5_batch1.py
```

Do not start Batch 2 or promote this candidate into `fontforge/bold.sfd` until Batch 1 has been visually reviewed.

## Bold v5.1 and Batch 2 review

Seven approved Batch 1 glyphs are promoted into `fontforge/bold.sfd`, and the installable Bold metadata is version 1.002. The promoted review totals are 226 Pass, 60 Almost Done, and 54 Needs Rework. The rejected Batch 1 `uni2010` outline was not promoted.

The complete Almost Done cohort is classified in `qa/assets/bold-v51-indent-audit.json`. Batch 2 remains an isolated preview in `fontforge/bold-v5-batch2.sfd`. Its second revision preserves the original Regular width of `uni2010`, adds 40 units of vertical weight without replacing its recognizable Bold silhouette, and constructs every quote from an exact affine expansion of its own authoritative Regular contour. Press **Bold v5 Batch 2** on the QA page; it automatically selects All states and the eight-glyph cohort. Review controls remain disabled until the candidate is accepted.

Rebuild and verify Batch 2 with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\repair_bold_batch2.py --force
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_v5_batch2.py
```

Do not promote Batch 2 or begin another repair batch before visual review.

## Bold v4 verification

Bold v4 smooths the priority punctuation and symbols, removes persistent unmatched white patches and self-intersections, and aligns replacement dots to the authoritative Regular ink centroids at 95% of their v3 diameter.

Run the invariant and repair verifier with:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\verify_bold_v4.py
```

The repair scripts are retained for diagnosis and for an intentional new repair pass. Do not rerun them after later manual Bold edits. The normal edit/review cycle uses only `refresh_bold_qa.py`.

## Earlier Bold v3 repair tools

These tools created v3 and are retained for diagnosis or an intentional new repair run:

- `tools/run_bold_v3_cleanup.py` — isolated cleanup/refit workers for cleanup-only glyphs
- `tools/repair_bold_geometric_v3.py` — geometric punctuation, initial dot, hole-policy, and targeted weight repair
- `tools/repair_bold_dots_v3.py` — native four-on-curve dot ellipses
- `tools/protect_bold_dot_components_v3.py` — dot/stem separation and micro-hole protection
- `tools/migrate_bold_v3_decisions.py` — v2-to-v3 decision and Ready-to-Pass migration
- `tools/verify_bold_v3.py` — invariant, outline, target-weight, and decision verification

Do not rerun repair or migration tools after later manual Bold edits unless intentionally starting a new repair pass. The normal cycle uses only `refresh_bold_qa.py`.

Bold Italic is not part of this phase.
