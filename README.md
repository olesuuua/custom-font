# Olesuas Hand

A custom handwritten font family with a reviewed Regular source and an independently editable Bold candidate.

## Font files

- `fontforge/redrawn.sfd` — authoritative updated Regular source (weight 400; never cleaned in place)
- `fontforge/regular-bold-base.sfd` — overlap-cleaned copy used only as the Bold source
- `fontforge/bold-raw.sfd` — untouched +40 diagnostic
- `fontforge/bold.sfd` — repaired, manually editable Bold source
- `qa/assets/redrawn.ttf`, `regular-bold-base.ttf`, `bold-raw.ttf`, and `bold.ttf` — derived QA builds
- `checkpoints/bold-v2-reviewed/` — preserved reviewed Bold v2 and its decisions
- `checkpoints/bold-v3-reviewed/` — preserved fully reviewed Bold v3 and its decisions
- `checkpoints/bold-v4/` — frozen verified Bold v4 sources, reports, decisions, and settings

The family contains 340 serialized glyphs: 334 outlined glyphs and six empty/control glyphs.

## Normal Bold editing and review cycle

After editing and saving `fontforge/bold.sfd` in FontForge, regenerate only the TTFs and QA report. This command never overwrites either the authoritative Regular, the cleaned base, or `bold.sfd`:

```powershell
& 'C:\Program Files\FontForgeBuilds\bin\ffpython.exe' tools\refresh_bold_qa.py
```

Start the guarded local review server:

```powershell
.\tools\start_bold_qa.ps1
```

If the launcher identifies an older server from this same project, replace it explicitly with `-ReplaceProjectServer`. It refuses to stop unrelated listeners.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). If an older server is already running, stop it before starting this command so the v4.1 decision rules are loaded.

The default page compares untouched Regular with final Bold. “Cleaned base” adds the overlap-cleaned Regular, and “Show raw Bold” switches the Bold panel to the original +40 diagnostic. The archived redraw review remains at `/redraw.html`.

The Bold page supports Pass, Almost Done, Needs Complete Rework, Unreviewed, and the derived Ready to Pass queue. Decisions are stored atomically in `qa/bold-manual-decisions.json` and bind the Regular, cleaned-base, Bold, and metrics hashes. A changed outline invalidates only its own decision. Ready to Pass never automatically promotes a glyph; it presents qualifying repaired Almost Done glyphs for one-click confirmation.

Intentional filled counters are permitted for `asterisk`, `uni041D`, `uni0427`, and `uni043D`, plus strictly tiny invisible holes. Bold-only white regions that do not match a visible Regular counter are removed. Real current outline, component, and required-counter defects still block Pass.

## Bold v4.1 triage

Bold v4.1 keeps all SFD sources unchanged, replaces the generic counter/component warning with specific evidence-based classifications, and preserves the existing manual decisions. Opening `qa/index.html` directly now displays recovery instructions because review APIs require the local HTTP server.

## Bold v5 pilot

The isolated pilot is built from `checkpoints/bold-v4/bold.sfd`; it never overwrites `fontforge/bold.sfd`. The QA page can preview the accepted pilot and the rejected manual alternatives. Review controls are disabled in preview modes so decisions remain bound to final Bold. Rebuild intentionally with `ffpython tools\build_bold_pilot.py --backend auto --force`.

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

Italic and Bold Italic are not part of this phase.