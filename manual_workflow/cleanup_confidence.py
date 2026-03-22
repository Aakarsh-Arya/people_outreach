import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import sheets_helper


def normalize_confidence(value: str) -> str:
    if not (value or "").strip():
        return ""
    normalized = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    normalized = " ".join(normalized.split())
    if normalized == "very high":
        return "very_high"
    if normalized in {"high", "medium", "low", "unconfirmed"}:
        return normalized
    return value


def resolve_tabs(args) -> list[str]:
    if args.all:
        manifest_path = Path(config.COHORTS_MANIFEST_FILE)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return [cohort["tab_name"] for cohort in manifest.get("cohorts", [])]
        return [config.cohort_tab_name(year) for year in config.COHORT_YEARS]
    return [args.tab]


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize dirty Confidence_Level values in cohort tabs")
    scope_group = parser.add_mutually_exclusive_group(required=True)
    scope_group.add_argument("--tab", help="Specific cohort tab name")
    scope_group.add_argument("--all", action="store_true", help="Normalize all cohort tabs")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default behavior)")
    parser.add_argument("--write", action="store_true", help="Apply normalized values to the sheet")
    args = parser.parse_args()

    dry_run = not args.write or args.dry_run
    if dry_run:
        print("[cleanup_confidence] Dry run mode active. No sheet values will be written.")

    for tab_name in resolve_tabs(args):
        rows = sheets_helper.read_all_rows(tab_name=tab_name)
        normalized_count = 0
        already_clean_count = 0
        pending_updates: list[tuple[int, dict, str | None]] = []
        for row_index, row in enumerate(rows):
            raw_value = (row.get("Confidence_Level") or "").strip()
            if not raw_value:
                already_clean_count += 1
                continue
            normalized_value = normalize_confidence(raw_value)
            if normalized_value != raw_value:
                normalized_count += 1
                if dry_run:
                    print(
                        f"[cleanup_confidence] {tab_name} row {row_index + 2}: '{raw_value}' -> '{normalized_value}'"
                    )
                else:
                    pending_updates.append((row_index, {"Confidence_Level": normalized_value}, tab_name))
            else:
                already_clean_count += 1
        if pending_updates and not dry_run:
            sheets_helper.batch_write_rows(pending_updates)
        print(f"{tab_name}: {normalized_count} rows normalized, {already_clean_count} rows already clean.")


if __name__ == "__main__":
    main()
