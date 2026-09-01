#!/usr/bin/env python3
"""Package the three approved Olesuas Hand TTFs as one final family release."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT / "release"
PACKAGE_DIR = RELEASE_ROOT / "OlesuasHand"
ARCHIVE = RELEASE_ROOT / "OlesuasHand-Final.zip"

FONTS = (
    ("Regular", ROOT / "qa" / "assets" / "regular-final.ttf", "OlesuasHand-Regular.ttf"),
    ("Bold", ROOT / "qa" / "assets" / "bold-final.ttf", "OlesuasHand-Bold.ttf"),
    ("Italic", ROOT / "qa" / "assets" / "italic-final.ttf", "OlesuasHand-Italic.ttf"),
)

README = """# Olesuas Hand — final family

This package contains the three approved installable styles:

- `OlesuasHand-Regular.ttf`
- `OlesuasHand-Bold.ttf`
- `OlesuasHand-Italic.ttf`

Install all three files together so applications can select the Regular,
Bold, and Italic styles from the **Olesuas Hand** family.

The files are byte-for-byte copies of the final TTF builds in `qa/assets/`.
The archival FontForge masters remain at the repository root as
`regular-final.sfd`, `bold-final.sfd`, and `italic-final.sfd`.
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    missing = [str(source) for _, source, _ in FONTS if not source.is_file()]
    if missing:
        raise FileNotFoundError("Missing final font(s): " + ", ".join(missing))

    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR)
    PACKAGE_DIR.mkdir(parents=True)

    manifest = {"family": "Olesuas Hand", "status": "final", "styles": []}
    for style, source, filename in FONTS:
        destination = PACKAGE_DIR / filename
        shutil.copyfile(source, destination)
        if sha256(source) != sha256(destination):
            raise RuntimeError(f"Copied font differs from source: {style}")
        manifest["styles"].append(
            {
                "style": style,
                "file": filename,
                "source": str(source.relative_to(ROOT)),
                "sha256": sha256(destination),
                "bytes": destination.stat().st_size,
            }
        )

    (PACKAGE_DIR / "README.md").write_text(README, encoding="utf-8")
    (PACKAGE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ARCHIVE.unlink(missing_ok=True)
    with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE_DIR.iterdir(), key=lambda item: item.name):
            archive.write(path, Path(PACKAGE_DIR.name) / path.name)

    print(json.dumps({"package": str(PACKAGE_DIR), "archive": str(ARCHIVE)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
