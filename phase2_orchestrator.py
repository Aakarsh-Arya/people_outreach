"""
Phase 2 Orchestrator: Research + Email Generation.
For each alumni, runs the LLM research agent then generates a verified email.
Can operate from Google Sheet or from alumni_clean.json (local-first mode).
"""

import json
import time
from pathlib import Path

import config
from phase2a_alumni_research import is_profile_usable, research_alumni
from phase2b_email_generation import (
    generate_email_base_template,
    generate_email_from_profile,
)

BASE_DIR = Path(__file__).parent


def _clean_verified_company(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in {"unknown", "blank", "none", "n/a", "-"}:
        return ""
    return text


def run_phase2(source="json", limit=0, start_row=1):
    """
    Execute Phase 2: Research + Email Generation.

    Args:
        source: "json" to read from alumni_clean.json (local-first, no Sheets needed)
                "sheet" to read/write from Google Sheet
        limit:  max alumni to process (0 = all)
        start_row: 1-based data-row offset to begin processing from
    """
    if start_row < 1:
        raise ValueError("start_row must be >= 1")

    print("=" * 60)
    print("PHASE 2: LLM Research + Email Generation")
    print(f"  Source: {source}")
    print(f"  Model: {config.OPENROUTER_MODEL}")
    print(f"  Start row: {start_row}")
    print("=" * 60)

    if source == "json":
        _run_from_json(limit, start_row)
    elif source == "sheet":
        _run_from_sheet(limit, start_row)
    else:
        print(f"[Phase 2] Unknown source: {source}")


def _run_from_json(limit: int, start_row: int):
    """Read alumni_clean.json, research, gen emails, output to alumni_outreach.json."""
    input_path = BASE_DIR / "alumni_clean.json"
    output_path = BASE_DIR / "alumni_outreach.json"

    if not input_path.exists():
        print(f"[Phase 2] {input_path.name} not found. Run parse_alumni_csv.py first.")
        return

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    all_alumni = data.get("alumni", [])
    total = len(all_alumni)
    start_index = start_row - 1
    alumni_list = all_alumni[start_index:]
    if limit:
        alumni_list = alumni_list[:limit]
        print(
            f"[Phase 2] Processing {len(alumni_list)} of {total} alumni "
            f"(limit={limit}, start_row={start_row})"
        )
    else:
        print(f"[Phase 2] Processing {len(alumni_list)} alumni starting at row {start_row} of {total}")

    results = []
    stats = {
        "processed": 0,
        "high_conf": 0,
        "medium_conf": 0,
        "low_conf": 0,
        "base_template": 0,
        "errors": 0,
    }

    for i, alumnus in enumerate(alumni_list):
        name = alumnus["name"]
        batch = alumnus.get("batch", "")
        role = alumnus.get("current_role", "")
        location = alumnus.get("location", "")
        profile_url = alumnus.get("profile_url", "")

        print(f"\n[{i + 1}/{len(alumni_list)}] Researching: {name} ({batch})")

        profile = research_alumni(
            name=name,
            batch=batch,
            last_known_role=role,
            location=location or "",
            profile_url=profile_url or "",
        )

        verified_company = ""
        if not profile:
            print("  [Research]  FAILED -> using base template")
            subject, body = generate_email_base_template(name, batch)
            enrichment_source = "base_template"
            stats["base_template"] += 1
            confidence = "FAILED"
            research_raw = ""
        elif not is_profile_usable(profile):
            print(f"  [Research]  Low confidence ({profile['confidence_level']}) -> base template")
            subject, body = generate_email_base_template(name, batch)
            enrichment_source = "base_template"
            stats["low_conf"] += 1
            confidence = profile.get("confidence", "Low")
            research_raw = profile.get("raw_profile", "")
        else:
            conf = profile["confidence_level"]
            print(f"  [Research]  Confidence: {conf}")
            verified_company = _clean_verified_company(profile.get("company", ""))
            confidence = profile.get("confidence", "")
            research_raw = profile.get("raw_profile", "")
            if not verified_company:
                print("  [Research]  No verified company -> base template")
                subject, body = generate_email_base_template(name, batch)
                enrichment_source = "base_template"
                stats["base_template"] += 1
            else:
                if conf in ("very_high", "high"):
                    stats["high_conf"] += 1
                else:
                    stats["medium_conf"] += 1

                print("  [Email Gen] Generating from verified profile...")
                subject, body = generate_email_from_profile(name, profile)
                enrichment_source = "llm_research"

        if not subject or not body:
            print(f"  [Email Gen] FAILED for {name}")
            stats["errors"] += 1
            continue

        stats["processed"] += 1
        print(f"  [Done]  Subject: {subject}")

        record = {
            **alumnus,
            "research_confidence": confidence,
            "enrichment_source": enrichment_source,
            "verified_role": profile.get("current_role", role) if profile else role,
            "verified_company": verified_company,
            "flags": profile.get("flags", "") if profile else "",
            "email_hooks": profile.get("email_hooks", []) if profile else [],
            "subject": subject,
            "body": body,
            "research_raw": research_raw[:2000],
        }
        results.append(record)

        time.sleep(config.ENRICHMENT_DELAY)

    output_data = {
        "meta": {
            "total_processed": stats["processed"],
            "high_confidence": stats["high_conf"],
            "medium_confidence": stats["medium_conf"],
            "low_conf_base_template": stats["low_conf"] + stats["base_template"],
            "errors": stats["errors"],
        },
        "alumni": results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    _print_summary(stats, output_path)


def _run_from_sheet(limit: int, start_row: int):
    """Read from Google Sheet, research, generate emails, and write back to the sheet."""
    from sheets_helper import initialize_sheet, read_all_rows, update_row_multiple

    initialize_sheet()
    rows = read_all_rows()
    if not rows:
        print("[Phase 2] No rows in sheet. Run Phase 1 first.")
        return

    total = len(rows)
    start_index = start_row - 1
    if start_index >= total:
        print(f"[Phase 2] start_row {start_row} is beyond the available {total} sheet rows.")
        return

    stats = {
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "high_conf": 0,
        "medium_conf": 0,
        "base_template": 0,
        "low_conf": 0,
    }

    attempts = 0
    for offset, row in enumerate(rows[start_index:]):
        sheet_row_index = start_index + offset
        if limit and attempts >= limit:
            break

        name = row.get("Name", "")
        email = row.get("Email", "")
        subject = row.get("Subject", "")
        body = row.get("Body", "")
        sent = row.get("Sent", "")

        if (subject and body) or sent == "YES":
            stats["skipped"] += 1
            print(f"[Phase 2] ({sheet_row_index + 1}/{total}) Skipping {name} -- already processed")
            continue

        attempts += 1
        if not email:
            stats["skipped"] += 1
            continue

        print(f"\n[Phase 2] ({sheet_row_index + 1}/{total}) Researching: {name}")

        company = row.get("AlmaConnect_Company", "")
        linkedin_url = row.get("LinkedIn_URL", "")
        batch = row.get("Graduation_Year", "")

        if row.get("Email_Source") == "people_api":
            profile = research_alumni(
                name=name,
                batch=batch,
                last_known_role=company,
                location="",
                profile_url=linkedin_url or "",
            )
        else:
            profile = None

        if not profile or not is_profile_usable(profile):
            time.sleep(2)
            subject, body = generate_email_base_template(name, batch)
            enrichment_source = "base_template"
            enrichment_notes = profile.get("raw_profile", "")[:1000] if profile else ""
            verified_company = ""
            if profile:
                stats["low_conf"] += 1
            else:
                stats["base_template"] += 1
        else:
            verified_company = _clean_verified_company(profile.get("company", ""))
            enrichment_notes = profile.get("raw_profile", "")[:1000]
            if not verified_company:
                print("  [Research]  No verified company -> base template")
                time.sleep(2)
                subject, body = generate_email_base_template(name, batch)
                enrichment_source = "base_template"
                stats["base_template"] += 1
            else:
                print("  [Email Gen] Generating from verified profile...")
                time.sleep(2)
                subject, body = generate_email_from_profile(name, profile)
                enrichment_source = "llm_research"
                if profile["confidence_level"] in ("very_high", "high"):
                    stats["high_conf"] += 1
                else:
                    stats["medium_conf"] += 1

        if not subject or not body:
            stats["errors"] += 1
            continue

        updates = {
            "Verified_Company": verified_company,
            "Enrichment_Notes": enrichment_notes,
            "Enrichment_Source": enrichment_source,
            "Subject": subject,
            "Body": body,
        }
        update_row_multiple(sheet_row_index, updates)
        stats["processed"] += 1
        print(f"  [Done]  Subject: {subject}")

        time.sleep(config.ENRICHMENT_DELAY)

    _print_summary(stats)


def _print_summary(stats: dict, output_path=None):
    print(f"\n{'=' * 60}")
    print("PHASE 2 COMPLETE")
    print(f"  Processed:           {stats['processed']}")
    print(f"  High confidence:     {stats.get('high_conf', 0)}")
    print(f"  Medium confidence:   {stats.get('medium_conf', 0)}")
    print(f"  Base template (low): {stats.get('base_template', 0) + stats.get('low_conf', 0)}")
    print(f"  Errors:              {stats.get('errors', 0)}")
    if output_path:
        print(f"  Output:              {output_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import sys

    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    run_phase2(source="json", limit=lim)
