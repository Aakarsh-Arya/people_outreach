"""Apply duplicate preflight fixes directly to the Google Sheet."""

from utils.deduplication import detect_duplicates, detect_fuzzy_name_duplicates
from sheets_helper import initialize_sheet, read_all_rows, update_row_multiple


def _sheet_row(row_index: int) -> int:
    return row_index + 2


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_company(value: str) -> str:
    return (value or "").strip().lower()


def _reset_updates() -> dict[str, str]:
    return {
        "Verified_Company": "",
        "Enrichment_Notes": "",
        "Enrichment_Source": "",
        "STATUS": "PENDING",
        "Subject": "",
        "Body": "",
    }


def _print_row_summary(row_index: int, row: dict[str, str]) -> None:
    print(
        f"Sheet row {_sheet_row(row_index)} | "
        f"Name={row.get('Name', '')} | "
        f"Email={row.get('Email', '')} | "
        f"Company={row.get('AlmaConnect_Company', '')} | "
        f"STATUS={row.get('STATUS', '')}"
    )


def _require_expected_name(row_index: int, expected_name: str, row: dict[str, str]) -> None:
    actual_name = row.get("Name", "")
    if actual_name != expected_name:
        raise ValueError(
            f"Row mismatch - expected {expected_name}, found {actual_name}. Aborting."
        )


def main() -> None:
    initialize_sheet()
    rows = read_all_rows()

    exact_duplicates = detect_duplicates(rows)
    fuzzy_pairs = detect_fuzzy_name_duplicates(rows, threshold=0.85)

    if not exact_duplicates and not fuzzy_pairs:
        print("[Preflight] No duplicates. Clean.")
        print("=== Preflight Summary ===")
        print("Exact dupes skipped: 0")
        print("Confirmed diff-person pairs reset: 0")
        print("Suspicious pairs flagged: 0")
        print("Total rows modified: 0")
        return

    candidate_rows: set[int] = set()
    for indices in exact_duplicates.values():
        candidate_rows.update(indices)
    for row_index_a, row_index_b, _ratio in fuzzy_pairs:
        row_a = rows[row_index_a]
        row_b = rows[row_index_b]
        email_a = _normalize_email(row_a.get("Email", ""))
        email_b = _normalize_email(row_b.get("Email", ""))
        if email_a and email_a == email_b:
            continue
        candidate_rows.add(row_index_a)
        candidate_rows.add(row_index_b)

    print("Pre-flight check for duplicate candidates:")
    for row_index in sorted(candidate_rows):
        _print_row_summary(row_index, rows[row_index])

    answer = input("Proceed with fixes? (y/n): ").strip().lower()
    if answer != "y":
        print("Aborted. No changes written.")
        return

    current_rows = read_all_rows()
    exact_dupes_skipped = 0
    diff_person_pairs_reset = 0
    suspicious_pairs_flagged = 0
    modified_rows: set[int] = set()
    exact_duplicate_rows: set[int] = set()

    for email, indices in sorted(exact_duplicates.items()):
        kept_index = min(indices)
        for row_index in sorted(indices):
            if row_index == kept_index:
                continue

            expected_name = rows[row_index].get("Name", "")
            _require_expected_name(row_index, expected_name, current_rows[row_index])

            updates = {
                "STATUS": "SKIP_DUPLICATE",
                "Subject": "",
                "Body": "",
            }
            update_row_multiple(row_index, updates)
            modified_rows.add(row_index)
            exact_duplicate_rows.add(row_index)
            exact_dupes_skipped += 1
            print(
                f"EXACT_DUPE: kept row {_sheet_row(kept_index)}, skipped row {_sheet_row(row_index)} "
                f"(email: {email})"
            )

    processed_pairs: set[tuple[int, int]] = set()
    for row_index_a, row_index_b, _ratio in fuzzy_pairs:
        pair_key = tuple(sorted((row_index_a, row_index_b)))
        if pair_key in processed_pairs:
            continue
        processed_pairs.add(pair_key)

        row_a = rows[row_index_a]
        row_b = rows[row_index_b]
        email_a = _normalize_email(row_a.get("Email", ""))
        email_b = _normalize_email(row_b.get("Email", ""))

        if email_a and email_a == email_b:
            continue
        if row_index_a in exact_duplicate_rows or row_index_b in exact_duplicate_rows:
            continue

        different_emails = bool(email_a and email_b and email_a != email_b)
        company_a = _normalize_company(row_a.get("AlmaConnect_Company", ""))
        company_b = _normalize_company(row_b.get("AlmaConnect_Company", ""))
        both_companies_present = bool(company_a and company_b)
        different_companies = both_companies_present and company_a != company_b

        if different_emails:
            message = (
                f"DIFF_PEOPLE: rows {_sheet_row(row_index_a)} & {_sheet_row(row_index_b)} same name, "
                f"different emails - both reset to PENDING for base template"
            )
            diff_person_pairs_reset += 1
        elif different_companies:
            message = (
                f"DIFF_PEOPLE: rows {_sheet_row(row_index_a)} & {_sheet_row(row_index_b)} same name, "
                f"diff company - both reset to PENDING for base template"
            )
            diff_person_pairs_reset += 1
        else:
            message = (
                f"SUSPICIOUS_DUPE: rows {_sheet_row(row_index_a)} & {_sheet_row(row_index_b)} - "
                f"same name + same/missing company. Possible People API error. Manual review needed."
            )
            suspicious_pairs_flagged += 1

        for row_index in pair_key:
            expected_name = rows[row_index].get("Name", "")
            _require_expected_name(row_index, expected_name, current_rows[row_index])
            updates = _reset_updates()
            if "WARNING" in current_rows[row_index]:
                updates["WARNING"] = message
            update_row_multiple(row_index, updates)
            modified_rows.add(row_index)

        print(message)

    print("=== Preflight Summary ===")
    print(f"Exact dupes skipped: {exact_dupes_skipped}")
    print(f"Confirmed diff-person pairs reset: {diff_person_pairs_reset}")
    print(f"Suspicious pairs flagged: {suspicious_pairs_flagged}")
    print(f"Total rows modified: {len(modified_rows)}")


if __name__ == "__main__":
    main()