"""
Backup sync helper — copies all gitignored files (except backup/ and .venv/)
into backup/, preserving relative paths. Writes MANIFEST.txt with timestamps.

Run: python backup_sync_helper.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _long_path(path: Path) -> str:
    """Add \\\\?\\ prefix on Windows to handle long paths."""
    raw = str(path.resolve())
    if os.name != "nt":
        return raw
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        return normalized
    return "\\\\?\\" + normalized


def main() -> int:
    root = Path(__file__).resolve().parent
    os.chdir(root)

    backup_dir = root / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Folders always excluded from backup (infinite loop + too large)
    EXCLUDED_PREFIXES = ("backup/", ".git/", ".venv/", "venv/")

    copied: list[str] = []
    errors: list[str] = []

    for source in root.rglob("*"):
        if not source.is_file():
            continue

        rel = source.relative_to(root).as_posix()

        # Skip excluded folders
        if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        # Skip nested .git dirs (e.g. scratch/publish-clean/.git/)
        if "/.git/" in rel or ".git/" in rel:
            continue

        # Check if file is gitignored
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", rel],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            continue

        target = backup_dir / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_long_path(source), _long_path(target))
            copied.append(rel)
        except Exception as exc:
            errors.append(f"{rel}: {exc}")

    # Write manifest
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = backup_dir / "MANIFEST.txt"
    lines = [f"# Backup sync — {now}", f"# Files: {len(copied)}", ""]
    for rel in sorted(copied):
        lines.append(rel)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Summary
    if copied:
        print(f"Synced {len(copied)} files to backup/")
    else:
        print("No gitignored files found to copy.")

    if errors:
        print(f"Errors ({len(errors)}):")
        for err in errors:
            print(f"  {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
