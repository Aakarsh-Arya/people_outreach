"""
Phase 1: Contact Resolution via Google People API.
Reads a normalized alumni CSV or AlmaConnect export, looks up @iimu.ac.in emails,
and writes the results to Google Sheet.
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
    initialize_sheet,
    read_all_rows,
    update_row_multiple,
)

NORMALIZED_NAME_HEADERS = {"Name"}
NORMALIZED_YEAR_HEADERS = {"Graduation Year", "Graduation_Year"}
ALMACONNECT_HEADERS = {
    "directory_user_info href",
    "ng-binding",
    "text_caption_small",
    "text_body_light",
}

PEOPLE_API_THROTTLE_SECONDS = 1
PEOPLE_API_RATE_LIMIT_RECOVERY_SECONDS = 60
DIRECTORY_CONFIDENCE_LEVEL = "directory_verified"


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


def _detect_csv_schema(fieldnames) -> str:
    headers = set(fieldnames or [])

    if NORMALIZED_NAME_HEADERS.issubset(headers) and headers.intersection(NORMALIZED_YEAR_HEADERS):
        return "normalized"

    if ALMACONNECT_HEADERS.issubset(headers):
        return "almaconnect"

    raise ValueError(
        "Unsupported CSV headers. Expected either "
        "`Name, Graduation Year, Current Company, LinkedIn URL` "
        "or the raw AlmaConnect export columns."
    )


def _parse_normalized_row(row: dict) -> dict:
    return {
        "name": row.get("Name", "").strip(),
        "graduation_year": row.get("Graduation Year", row.get("Graduation_Year", "")).strip(),
        "company": row.get("Current Company", row.get("Current_Company", "")).strip(),
        "linkedin_url": row.get("LinkedIn URL", row.get("LinkedIn_URL", "")).strip(),
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
    csv_path = Path(filepath or config.INPUT_CSV)
    if not csv_path.is_absolute():
        csv_path = config.BASE_DIR / csv_path

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {csv_path}. Set INPUT_CSV in .env.local or pass a path to "
            "`python main.py phase1 <csv_path>`."
        )

    alumni = []
    skipped = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        schema = _detect_csv_schema(reader.fieldnames)
        parser = _parse_normalized_row if schema == "normalized" else _parse_almaconnect_row

        for row in reader:
            parsed = parser(row)
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


def _sleep_after_people_api_attempt() -> None:
    time.sleep(PEOPLE_API_THROTTLE_SECONDS)


def _sleep_for_rate_limit_recovery() -> None:
    print(
        "[Rate limit hit] Waiting 60s for quota reset before continuing..."
    )
    time.sleep(PEOPLE_API_RATE_LIMIT_RECOVERY_SECONDS)


def confidence_level_for_email_source(source: str) -> str:
    if source == "people_api":
        return DIRECTORY_CONFIDENCE_LEVEL
    return ""


def lookup_email_people_api(people_service, name, graduation_year=""):
    """
    Look up a person's email from the @iimu.ac.in directory using People API.
    Returns (email, source) tuple.
    """
    guess = derive_email_guess(name, graduation_year)

    try:
        results = people_service.people().searchDirectoryPeople(
            query=name,
            readMask="names,emailAddresses",
            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
        ).execute()

        people = results.get("people", [])

        if not people:
            _sleep_after_people_api_attempt()
            return guess, "guessed"

        if len(people) == 1:
            email = _extract_primary_email(people[0])
            if email:
                _sleep_after_people_api_attempt()
                return email, "people_api"

            _sleep_after_people_api_attempt()
            return guess, "guessed"

        year_match = _pick_unique_year_match(people, graduation_year)
        if year_match:
            _sleep_after_people_api_attempt()
            return _extract_primary_email(year_match), "people_api"

        name_match = _pick_unique_name_match(name, people)
        if name_match:
            _sleep_after_people_api_attempt()
            return _extract_primary_email(name_match), "people_api"

        _log_ambiguous_lookup(name, people)
        _sleep_after_people_api_attempt()
        return guess, "ambiguous"

    except Exception as error:
        if _is_rate_limited_error(error):
            print(f"[Phase 1] People API rate limit for '{name}': {error}")
            _sleep_for_rate_limit_recovery()
            _sleep_after_people_api_attempt()
            return guess, "guessed"

        print(f"[Phase 1] People API error for '{name}': {error}")
        _sleep_after_people_api_attempt()
        return guess, "guessed"


def run_phase1(csv_path=None):
    """
    Execute Phase 1:
    1. Load CSV
    2. Look up emails via People API
    3. Write results to Google Sheet
    """
    print("=" * 60)
    print("PHASE 1: Contact Resolution")
    print("=" * 60)

    alumni = load_alumni_csv(csv_path)
    if not alumni:
        print("[Phase 1] No alumni found. Exiting.")
        return []

    initialize_sheet()
    people_service = get_people_service()

    resolved = []
    for i, person in enumerate(alumni):
        name = person["name"]
        year = person["graduation_year"]

        print(f"[Phase 1] ({i + 1}/{len(alumni)}) Looking up: {name}...")
        email, source = lookup_email_people_api(people_service, name, year)

        row = {
            "Name": name,
            "Email": email,
            "Email_Source": source,
            "Confidence_Level": confidence_level_for_email_source(source),
            "Graduation_Year": year,
            "AlmaConnect_Company": person["company"],
            "Verified_Company": "",
            "LinkedIn_URL": person["linkedin_url"],
            "Enrichment_Notes": "",
            "Enrichment_Source": "",
            "Subject": "",
            "Body": "",
            "Sent": "",
        }
        resolved.append(row)
        print(f"         -> {email} ({source})")

    append_rows(resolved)
    print(f"\n[Phase 1] Done. {len(resolved)} contacts written to Google Sheet.")

    api_count = sum(1 for row in resolved if row["Email_Source"] == "people_api")
    ambiguous_count = sum(1 for row in resolved if row["Email_Source"] == "ambiguous")
    guess_count = sum(1 for row in resolved if row["Email_Source"] == "guessed")
    print(
        "         "
        f"People API: {api_count} | Ambiguous: {ambiguous_count} | Guessed: {guess_count}"
    )

    return resolved


def run_phase1_retry():
    """Retry only guessed or ambiguous rows already present in the Google Sheet."""
    print("=" * 60)
    print("PHASE 1 RETRY: Re-check guessed and ambiguous rows")
    print("=" * 60)

    initialize_sheet()
    rows = read_all_rows()
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
            updates = {
                "Email": email,
                "Email_Source": source,
                "Confidence_Level": confidence_level_for_email_source(source),
            }
            update_row_multiple(row_index, updates)
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
