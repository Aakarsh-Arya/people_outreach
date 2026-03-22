"""
One-time script to fix broken email signatures already written to the sheet.

Reads rows with a Body column, applies _normalize_email_signature + _ensure_greeting,
and writes back only rows that actually changed.

Usage:
  python manual_workflow/fix_signatures.py --tab=cohort_2013          # dry run
  python manual_workflow/fix_signatures.py --tab=cohort_2013 --write  # live write
  python manual_workflow/fix_signatures.py --all                      # dry run all tabs
  python manual_workflow/fix_signatures.py --all --write              # live write all tabs
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse

import config
import sheets_helper
from manual_workflow import _ensure_greeting, _normalize_email_signature


def fix_tab(tab: str, write: bool) -> int:
    try:
        rows = sheets_helper.read_all_rows(tab_name=tab)
    except Exception as e:
        print(f"[FixSig] ERROR reading tab '{tab}': {e}")
        return 0

    fixes = []
    for i, row in enumerate(rows):
        body = (row.get("Body") or "").strip()
        if not body:
            continue
        name = (row.get("Name") or "").strip()
        fixed = _ensure_greeting(body, name)
        fixed = _normalize_email_signature(fixed)
        if fixed != body:
            fixes.append((i, row.get("Name", "unknown"), body, fixed))

    if not fixes:
        print(f"[FixSig] {tab}: no signature issues found.")
        return 0

    print(f"[FixSig] {tab}: {len(fixes)} rows need fixing {'(DRY RUN)' if not write else ''}")
    for row_idx, name, old_body, new_body in fixes:
        # Show just the signature diff
        old_sig = old_body.split("\n")[-5:]
        new_sig = new_body.split("\n")[-5:]
        print(f"  Row {row_idx + 2}: {name}")
        print(f"    OLD: {' | '.join(l.strip() for l in old_sig if l.strip())}")
        print(f"    NEW: {' | '.join(l.strip() for l in new_sig if l.strip())}")

    if write:
        updates = [(row_idx, {"Body": new_body}, tab) for row_idx, _, _, new_body in fixes]
        sheets_helper.batch_write_rows(updates)
        print(f"[FixSig] {tab}: {len(fixes)} rows updated.")
    else:
        print(f"[FixSig] Dry run complete. Pass --write to apply.")

    return len(fixes)


def main():
    parser = argparse.ArgumentParser(description="Fix broken email signatures in the sheet")
    parser.add_argument("--tab", help="Cohort tab name")
    parser.add_argument("--all", action="store_true", help="Process all cohort tabs")
    parser.add_argument("--write", action="store_true", help="Actually write changes (default is dry run)")
    args = parser.parse_args()

    if not args.tab and not args.all:
        print("ERROR: Specify --tab TAB or --all")
        return

    if args.all:
        tabs = [config.cohort_tab_name(y) for y in config.COHORT_YEARS]
    else:
        tabs = [args.tab]

    total = 0
    for tab in tabs:
        total += fix_tab(tab, args.write)

    print(f"\n[FixSig] Total rows {'fixed' if args.write else 'to fix'}: {total}")


if __name__ == "__main__":
    main()
