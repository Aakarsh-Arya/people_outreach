"""
Phase 1: Contact Resolution via Google People API.
Reads a normalized alumni CSV or AlmaConnect export, looks up @iimu.ac.in emails,
proactively throttles People API requests to stay under quota, retries 429s with
backoff, and writes only de-duplicated rows to Google Sheet.
"""

import csv
import re
import time
from pathlib import Path

from googleapiclient.errors import HttpError

import config
from google_auth_helper import get_people_service
from sheets_helper import (
    append_rows,
    clear_tab_data,
    initialize_sheet,
    read_all_rows,
    update_row_multiple,
)

NORMALIZED_REQUIRED_HEADERS = {"Name", "Graduation_Year"}
ALMACONNECT_HEADERS = {
    "directory_user_info href",
    "ng-binding",
    "text_caption_small",
    "text_body_light",
}

PEOPLE_API_THROTTLE_SECONDS = 1.1
PEOPLE_API_RATE_LIMIT_RECOVERY_SECONDS = 65
PEOPLE_API_MAX_RETRIES = 3
_LAST_PEOPLE_API_CALL_AT = 0.0


def _resolve_csv_path(filepath=None) -> Path:
    csv_path = Path(filepath or config.INPUT_CSV)
    if not csv_path.is_absolute():
        csv_path = config.BASE_DIR / csv_path
    return csv_path


def infer_tab_name_from_csv(csv_path=None, explicit_tab_name: str | None = None) -> str:
    """Infer a cohort tab name from the CSV path unless an explicit tab is provided."""
    if explicit_tab_name:
        return explicit_tab_name

    resolved_path = _resolve_csv_path(csv_path)
    matches = re.findall(r"(?<!\d)(20\d{2})(?!\d)", resolved_path.stem)
    if matches:
        return config.cohort_tab_name(matches[-1])

    return config.SHEET_NAME


def _normalize_lookup_name(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_lookup_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_lookup_year(value: str) -> str:
    return (value or "").strip()


def _phase1_exact_key(name: str, email: str, graduation_year: str) -> tuple[str, str, str]:
    return (
        _normalize_lookup_name(name),
        _normalize_lookup_email(email),
        _normalize_lookup_year(graduation_year),
    )


def _phase1_name_year_key(name: str, graduation_year: str) -> tuple[str, str]:
    return (
        _normalize_lookup_name(name),
        _normalize_lookup_year(graduation_year),
    )


def _build_phase1_row(person: dict, email: str, source: str) -> dict[str, str]:
    return {
        "Name": person["name"],
        "Email": email,
        "Email_Source": source,
        "Confidence_Level": confidence_level_for_email_source(source),
        "Graduation_Year": person["graduation_year"],
        "AlmaConnect_Company": person["company"],
        "Verified_Company": "",
        "LinkedIn_URL": person["linkedin_url"],
        "Enrichment_Notes": "",
        "Enrichment_Source": "",
        "Subject": "",
        "Body": "",
        "Sent": "",
        "STATUS": config.STATUS_PENDING,
    }


def _build_phase1_update(row: dict) -> dict[str, str]:
    """Only update Phase 1-owned fields; preserve downstream Phase 2/send state."""
    return {
        "Name": row["Name"],
        "Email": row["Email"],
        "Email_Source": row["Email_Source"],
        "Confidence_Level": row["Confidence_Level"],
        "Graduation_Year": row["Graduation_Year"],
        "AlmaConnect_Company": row["AlmaConnect_Company"],
        "LinkedIn_URL": row["LinkedIn_URL"],
    }


def _build_existing_row_lookups(existing_rows: list[dict[str, str]]) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], list[int]]]:
    exact_lookup: dict[tuple[str, str, str], int] = {}
    name_year_lookup: dict[tuple[str, str], list[int]] = {}

    for row_index, row in enumerate(existing_rows):
        name_year_key = _phase1_name_year_key(row.get("Name", ""), row.get("Graduation_Year", ""))
        if name_year_key[0] and name_year_key[1]:
            name_year_lookup.setdefault(name_year_key, []).append(row_index)

        exact_key = _phase1_exact_key(row.get("Name", ""), row.get("Email", ""), row.get("Graduation_Year", ""))
        if exact_key[0] and exact_key[1] and exact_key[2]:
            exact_lookup.setdefault(exact_key, row_index)

    return exact_lookup, name_year_lookup


def _resolve_existing_row_index(
    exact_lookup: dict[tuple[str, str, str], int],
    name_year_lookup: dict[tuple[str, str], list[int]],
    *,
    name: str,
    email: str,
    graduation_year: str,
) -> int | None:
    exact_key = _phase1_exact_key(name, email, graduation_year)
    if exact_key in exact_lookup:
        return exact_lookup[exact_key]

    name_year_key = _phase1_name_year_key(name, graduation_year)
    candidates = name_year_lookup.get(name_year_key, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        chosen = min(candidates)
        print(
            f"[Phase 1] Warning: multiple existing rows matched {name} ({graduation_year}); "
            f"updating earliest row {chosen + 2}."
        )
        return chosen
    return None


def _extract_graduation_year(value: str) -> str:
    """Convert batch labels like `PGP '15` to a 4-digit year."""
    if not value:
        return ""

    matches = re.findall(r"\d{2,4}", value)
    if not matches:
        return ""

    year = matches[-1]
    if len(year) == 2:
        return f"20{year}"
    return year


def _extract_company_from_role(role_text: str) -> str:
    """Extract company from a role string like `Lead Data Scientist at Tavant`."""
    if not role_text:
        return ""

    role = role_text.strip().strip(",")
    role = role.split("|", 1)[0].strip()

    match = re.search(r"\s+at\s+(.+)$", role, re.IGNORECASE)
    if not match:
        return ""

    return match.group(1).strip(" ,")


def _normalize_fieldname(fieldname: str) -> str:
    clean = (fieldname or "").strip()
    return config.COLUMN_NAME_MAP.get(clean, clean)


def _normalize_csv_row(row: dict) -> dict:
    normalized = {}
    for key, value in row.items():
        normalized_key = _normalize_fieldname(key)
        normalized.setdefault(normalized_key, value)
        if not normalized.get(normalized_key) and value:
            normalized[normalized_key] = value
    return normalized


def _detect_and_validate_csv_schema(fieldnames) -> tuple[str, list[str]]:
    raw_headers = [header for header in (fieldnames or []) if header]
    normalized_headers = [_normalize_fieldname(header) for header in raw_headers]

    if ALMACONNECT_HEADERS.issubset(set(raw_headers)):
        return "almaconnect", normalized_headers

    normalized_header_set = set(normalized_headers)
    if NORMALIZED_REQUIRED_HEADERS.issubset(normalized_header_set):
        return "normalized", normalized_headers

    missing = sorted(NORMALIZED_REQUIRED_HEADERS - normalized_header_set)
    found = ", ".join(normalized_headers) if normalized_headers else "<none>"
    raise ValueError(
        "Unsupported CSV headers after normalization. "
        f"Missing required columns: {', '.join(missing)}. Found columns: {found}"
    )


def _parse_normalized_row(row: dict) -> dict:
    return {
        "name": row.get("Name", "").strip(),
        "graduation_year": row.get("Graduation_Year", "").strip(),
        "company": row.get("AlmaConnect_Company", "").strip(),
        "linkedin_url": row.get("LinkedIn_URL", "").strip(),
    }


def _parse_almaconnect_row(row: dict) -> dict:
    return {
        "name": row.get("ng-binding", "").strip(),
        "graduation_year": _extract_graduation_year(row.get("text_caption_small", "").strip()),
        "company": _extract_company_from_role(row.get("text_body_light", "")),
        "linkedin_url": "",
    }


def load_alumni_csv(filepath=None):
    """Load alumni data from CSV and normalize it for Phase 1 processing."""
    csv_path = _resolve_csv_path(filepath)

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {csv_path}. Set INPUT_CSV in .env.local or pass a path to "
            "`python main.py phase1 <csv_path>`."
        )

    alumni = []
    skipped = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        schema, normalized_headers = _detect_and_validate_csv_schema(reader.fieldnames)
        parser = _parse_normalized_row if schema == "normalized" else _parse_almaconnect_row

        for row in reader:
            parsed = parser(_normalize_csv_row(row) if schema == "normalized" else row)
            if not parsed["name"]:
                skipped += 1
                continue
            alumni.append(parsed)

    print(f"[Phase 1] Loaded {len(alumni)} alumni from {csv_path.name}")
    if skipped:
        print(f"[Phase 1] Skipped {skipped} rows without a name.")

    return alumni


def derive_email_guess(name, graduation_year=""):
    """
    Generate a best-guess @iimu.ac.in email from name.
    Format: firstname.lastname.YYYY@iimu.ac.in
    """
    parts = name.lower().split()
    if len(parts) < 2:
        return ""
    first = re.sub(r"[^a-z]", "", parts[0])
    last = re.sub(r"[^a-z]", "", parts[-1])
    year = graduation_year if graduation_year else "2023"
    return f"{first}.{last}.{year}@iimu.ac.in"


def _extract_primary_email(person: dict) -> str:
    for email in person.get("emailAddresses", []):
        value = email.get("value", "").strip()
        if value:
            return value
    return ""


def _extract_display_name(person: dict) -> str:
    for name in person.get("names", []):
        display_name = name.get("displayName", "").strip()
        if display_name:
            return display_name
    return ""


def _normalize_name_for_match(name: str) -> str:
    tokens = re.findall(r"[a-z]+", name.lower())
    tokens = [token for token in tokens if len(token) > 1] or tokens
    return " ".join(tokens)


def _pick_unique_year_match(people: list[dict], graduation_year: str):
    if not graduation_year:
        return None

    matches = [
        person
        for person in people
        if graduation_year in _extract_primary_email(person)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _pick_unique_name_match(query_name: str, people: list[dict]):
    query_normalized = _normalize_name_for_match(query_name)
    query_tokens = set(query_normalized.split())
    exact_matches = []
    partial_matches = []

    for person in people:
        email = _extract_primary_email(person)
        display_name = _extract_display_name(person)
        if not email or not display_name:
            continue

        candidate_normalized = _normalize_name_for_match(display_name)
        candidate_tokens = set(candidate_normalized.split())

        if candidate_normalized == query_normalized:
            exact_matches.append(person)
            continue

        if query_tokens and candidate_tokens and (
            query_tokens.issubset(candidate_tokens) or candidate_tokens.issubset(query_tokens)
        ):
            partial_matches.append(person)

    if len(exact_matches) == 1:
        return exact_matches[0]

    if not exact_matches and len(partial_matches) == 1:
        return partial_matches[0]

    return None


def _log_ambiguous_lookup(name: str, people: list[dict]) -> None:
    emails = [_extract_primary_email(person) or "<no email>" for person in people]
    print(
        f"[Phase 1][Ambiguous] {name}: {len(people)} results in contention -> "
        f"{', '.join(emails)}"
    )


def _is_rate_limited_error(error: Exception) -> bool:
    if isinstance(error, HttpError):
        status = getattr(error.resp, "status", None)
        if status == 429:
            return True

        content = getattr(error, "content", b"") or b""
        content_text = content.decode("utf-8", errors="ignore").lower()
        if status == 403 and any(
            reason in content_text
            for reason in ("ratelimitexceeded", "userratelimitexceeded", "quota")
        ):
            return True

    error_text = str(error).lower()
    return "429" in error_text or "rate limit" in error_text


def _wait_for_people_api_slot() -> None:
    """Enforce a minimum gap between directory API calls."""
    global _LAST_PEOPLE_API_CALL_AT

    now = time.monotonic()
    elapsed = now - _LAST_PEOPLE_API_CALL_AT
    if elapsed >= PEOPLE_API_THROTTLE_SECONDS:
        _LAST_PEOPLE_API_CALL_AT = now
        return

    wait_seconds = PEOPLE_API_THROTTLE_SECONDS - elapsed
    print(f"[Phase 1] Throttling People API for {wait_seconds:.2f}s to stay under quota")
    time.sleep(wait_seconds)
    _LAST_PEOPLE_API_CALL_AT = time.monotonic()


def _sleep_for_rate_limit_recovery() -> None:
    print(
        f"[Rate limit hit] Waiting {PEOPLE_API_RATE_LIMIT_RECOVERY_SECONDS}s for quota reset before retrying..."
    )
    time.sleep(PEOPLE_API_RATE_LIMIT_RECOVERY_SECONDS)


def confidence_level_for_email_source(source: str) -> str:
    return ""


def lookup_email_people_api(people_service, name, graduation_year=""):
    """
    Look up a person's email from the @iimu.ac.in directory using People API.
    Applies proactive throttling before every request and retries 429 responses
    for the same row before falling back to guessed output.
    Returns (email, source) tuple.
    """
    guess = derive_email_guess(name, graduation_year)

    for attempt in range(1, PEOPLE_API_MAX_RETRIES + 1):
        try:
            _wait_for_people_api_slot()
            results = people_service.people().searchDirectoryPeople(
                query=name,
                readMask="names,emailAddresses",
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            ).execute()

            people = results.get("people", [])

            if not people:
                return guess, "guessed"

            if len(people) == 1:
                email = _extract_primary_email(people[0])
                if email:
                    return email, "people_api"
                return guess, "guessed"

            year_match = _pick_unique_year_match(people, graduation_year)
            if year_match:
                return _extract_primary_email(year_match), "people_api"

            name_match = _pick_unique_name_match(name, people)
            if name_match:
                return _extract_primary_email(name_match), "people_api"

            _log_ambiguous_lookup(name, people)
            return guess, "ambiguous"

        except HttpError as error:
            if _is_rate_limited_error(error):
                print(
                    f"[Phase 1] People API 429 for '{name}' on attempt {attempt}/{PEOPLE_API_MAX_RETRIES}: {error}"
                )
                if attempt < PEOPLE_API_MAX_RETRIES:
                    _sleep_for_rate_limit_recovery()
                    continue
                print(f"[Phase 1] Retries exhausted for '{name}'. Falling back to guessed email.")
                return guess, "guessed"

            print(f"[Phase 1] People API error for '{name}': {error}")
            return guess, "guessed"
        except Exception as error:
            if _is_rate_limited_error(error):
                print(
                    f"[Phase 1] People API 429 for '{name}' on attempt {attempt}/{PEOPLE_API_MAX_RETRIES}: {error}"
                )
                if attempt < PEOPLE_API_MAX_RETRIES:
                    _sleep_for_rate_limit_recovery()
                    continue
                print(f"[Phase 1] Retries exhausted for '{name}'. Falling back to guessed email.")
                return guess, "guessed"

            print(f"[Phase 1] People API error for '{name}': {error}")
            return guess, "guessed"

    return guess, "guessed"


def run_phase1(csv_path=None, tab_name: str | None = None, *, force_clear: bool = False):
    """
    Execute Phase 1:
    1. Load CSV
    2. Look up emails via throttled/retried People API
    3. Update existing alumni rows idempotently when they already exist
    4. Append only truly new alumni rows to Google Sheet
    """
    print("=" * 60)
    print("PHASE 1: Contact Resolution")
    print("=" * 60)

    active_tab = infer_tab_name_from_csv(csv_path, tab_name)
    print(f"[Phase 1] Target tab: {active_tab}")

    alumni = load_alumni_csv(csv_path)
    if not alumni:
        print("[Phase 1] No alumni found. Exiting.")
        return []

    initialize_sheet(active_tab)

    if force_clear:
        clear_tab_data(tab_name=active_tab)
        print("[Phase 1] Cleared existing tab data")

    try:
        existing_rows = read_all_rows(tab_name=active_tab)
        existing_lookup, existing_name_year_lookup = _build_existing_row_lookups(existing_rows)
        print(f"[Phase 1] Found {len(existing_rows)} existing rows in sheet")
    except Exception as e:
        print(f"[Phase 1] Could not read existing rows: {e}")
        existing_rows = []
        existing_lookup = {}
        existing_name_year_lookup = {}

    people_service = get_people_service()

    appended_rows = []
    processed_rows = []
    pending_lookup: dict[tuple[str, str, str], int] = {}
    pending_name_year_lookup: dict[tuple[str, str], list[int]] = {}
    updated_count = 0
    appended_count = 0
    for i, person in enumerate(alumni):
        name = person["name"]
        year = person["graduation_year"]

        print(f"[Phase 1] ({i + 1}/{len(alumni)}) Looking up: {name}...")
        email, source = lookup_email_people_api(people_service, name, year)

        row = _build_phase1_row(person, email, source)
        processed_rows.append(row)
        row_index = _resolve_existing_row_index(
            existing_lookup,
            existing_name_year_lookup,
            name=name,
            email=email,
            graduation_year=year,
        )

        if row_index is not None:
            update_row_multiple(row_index, _build_phase1_update(row), tab_name=active_tab)
            existing_rows[row_index].update(_build_phase1_update(row))
            existing_lookup, existing_name_year_lookup = _build_existing_row_lookups(existing_rows)
            updated_count += 1
            print(f"         -> UPDATED row {row_index + 2} ({email or '<blank>'}, {source})")
            continue

        pending_exact_key = _phase1_exact_key(name, email, year)
        pending_name_year_key = _phase1_name_year_key(name, year)
        pending_index = pending_lookup.get(pending_exact_key)
        if pending_index is None:
            pending_candidates = pending_name_year_lookup.get(pending_name_year_key, [])
            if len(pending_candidates) == 1:
                pending_index = pending_candidates[0]

        if pending_index is not None:
            appended_rows[pending_index] = row
            pending_lookup, pending_name_year_lookup = _build_existing_row_lookups(appended_rows)
            print(f"         -> UPDATED pending new row ({email or '<blank>'}, {source})")
            continue

        appended_rows.append(row)
        pending_lookup, pending_name_year_lookup = _build_existing_row_lookups(appended_rows)
        appended_count += 1
        print(f"         -> APPEND queued ({email or '<blank>'}, {source})")

    if appended_rows:
        append_rows(appended_rows, tab_name=active_tab)
    else:
        print("[Phase 1] No new rows to append.")
    print(f"\n[Phase 1] Done. {updated_count} rows updated, {appended_count} new rows appended.")

    api_count = sum(1 for row in processed_rows if row["Email_Source"] == "people_api")
    ambiguous_count = sum(1 for row in processed_rows if row["Email_Source"] == "ambiguous")
    guess_count = sum(1 for row in processed_rows if row["Email_Source"] == "guessed")
    print(
        "         "
        f"People API: {api_count} | Ambiguous: {ambiguous_count} | Guessed: {guess_count}"
    )

    return appended_rows


def run_phase1_retry(tab_name: str | None = None):
    """Retry guessed or ambiguous rows with the same rate-limit protections and dedup guard."""
    active_tab = tab_name or config.SHEET_NAME
    print("=" * 60)
    print("PHASE 1 RETRY: Re-check guessed and ambiguous rows")
    print(f"  Tab: {active_tab}")
    print("=" * 60)

    initialize_sheet(active_tab)
    rows = read_all_rows(tab_name=active_tab)

    # --- Collect existing verified emails to prevent re-adding ---
    existing_verified_emails = {
        row.get("Email", "").strip().lower()
        for row in rows
        if row.get("Email_Source", "").strip().lower() == "people_api"
           and row.get("Email", "").strip()
    }

    retry_rows = [
        (row_index, row)
        for row_index, row in enumerate(rows)
        if row.get("Email_Source", "").strip().lower() in {"guessed", "ambiguous"}
    ]

    if not retry_rows:
        print("[Phase 1 Retry] No guessed or ambiguous rows found. Exiting.")
        return {"attempted": 0, "upgraded": 0, "unresolved": 0}

    people_service = get_people_service()
    attempted = len(retry_rows)
    upgraded = 0
    unresolved = 0

    for attempt_index, (row_index, row) in enumerate(retry_rows, start=1):
        name = row.get("Name", "").strip()
        graduation_year = row.get("Graduation_Year", "").strip()
        current_source = row.get("Email_Source", "").strip().lower()

        if current_source == "people_api":
            print(f"[Phase 1 Retry] ({attempt_index}/{attempted}) Skipping {name} -- already verified")
            continue

        print(f"[Phase 1 Retry] ({attempt_index}/{attempted}) Re-checking: {name}...")
        email, source = lookup_email_people_api(people_service, name, graduation_year)

        if source == "people_api":
            # Check if this email already belongs to another verified row
            if email and email.strip().lower() in existing_verified_emails:
                print(f"               -> SKIPPED (email {email} already verified for another row)")
                unresolved += 1
                continue

            updates = {
                "Email": email,
                "Email_Source": source,
                "Confidence_Level": confidence_level_for_email_source(source),
            }
            update_row_multiple(row_index, updates, tab_name=active_tab)
            if email:
                existing_verified_emails.add(email.strip().lower())
            upgraded += 1
            print(f"               -> upgraded to {email} ({source})")
            continue

        unresolved += 1
        print("               -> unchanged")

    print("\n[Phase 1 Retry] Summary")
    print(f"  Attempted:   {attempted}")
    print(f"  Upgraded:    {upgraded}")
    print(f"  Unresolved:  {unresolved}")

    return {"attempted": attempted, "upgraded": upgraded, "unresolved": unresolved}


if __name__ == "__main__":
    run_phase1()
