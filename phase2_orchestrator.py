"""
Phase 2 Orchestrator: Research + Email Generation.
For each alumni, runs the LLM research agent then generates a verified email.
Can operate from Google Sheet or from alumni_clean.json (local-first mode).
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import config
from phase2a_alumni_research import (
    ProfileFenceError,
    is_profile_usable,
    parse_profile_response,
    research_alumni,
    research_alumni_async,
)
from phase2a_enrichment import (
    AllTavilyKeysExhaustedError,
    clear_tavily_exhaustion_state,
    extract_confidence_level,
)
from phase2b_email_generation import (
    EmailFenceError,
    generate_email_base_template,
    generate_email_base_template_async,
    generate_email_from_profile,
    generate_email_from_profile_async,
)
from utils.deduplication import detect_duplicates, detect_fuzzy_name_duplicates
from utils.run_context import (
    clear_current_row,
    load_progress,
    log_event,
    save_progress,
    set_current_row,
    start_run,
)

BASE_DIR = Path(__file__).parent
log = logging.getLogger(__name__)

_VALID_CONFIDENCE_LEVELS = {"very_high", "high", "medium", "low", "unconfirmed"}


def _normalize_confidence_level(conf_level: str | None) -> str:
    normalized = (conf_level or "").strip().lower()
    if normalized not in _VALID_CONFIDENCE_LEVELS:
        return "unconfirmed"
    return normalized


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_company(value: str) -> str:
    return (value or "").strip().lower()


def _preflight_reset_updates() -> dict[str, str]:
    return {
        "Verified_Company": "",
        "Enrichment_Notes": "",
        "Enrichment_Source": "",
        "STATUS": config.STATUS_PENDING,
        "Subject": "",
        "Body": "",
        "Subject_v1": "",
        "Body_v1": "",
        "Subject_v2": "",
        "Body_v2": "",
        "Subject_v3": "",
        "Body_v3": "",
    }


def _maybe_add_warning(row: dict[str, str], message: str, updates: dict[str, str]) -> None:
    if "WARNING" in row:
        updates["WARNING"] = message


def run_duplicate_preflight(all_rows_list: list[dict[str, str]], sheet_cache, tab_name: str) -> dict[str, int]:
    exact_duplicates = detect_duplicates(all_rows_list)
    fuzzy_pairs = detect_fuzzy_name_duplicates(all_rows_list, threshold=0.85)

    if not exact_duplicates and not fuzzy_pairs:
        print("[Preflight] No duplicates. Clean.")
        return {
            "exact_dupes_skipped": 0,
            "diff_person_pairs_reset": 0,
            "suspicious_pairs_flagged": 0,
            "rows_modified": 0,
        }

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

            row = sheet_cache.get_row(row_index)
            if (row.get("STATUS") or "").strip() in {config.STATUS_EMAIL_DONE, config.STATUS_SENT, "SKIP_DUPLICATE", "SENT"}:
                print(f"Skipping terminal-state row {row_index} (STATUS={row.get('STATUS')}) in preflight")
                continue

            expected_name = all_rows_list[row_index].get("Name", "")
            actual_name = row.get("Name", "")
            if actual_name != expected_name:
                raise ValueError(
                    f"Row mismatch - expected {expected_name}, found {actual_name}. Aborting."
                )

            updates = {
                "STATUS": "SKIP_DUPLICATE",
                "Subject": "",
                "Body": "",
            }
            # NOTE: must be called before asyncio.gather; writes are NOT lock-protected.
            sheet_cache.write_row(row_index, updates)
            row.update(updates)
            modified_rows.add(row_index)
            exact_duplicate_rows.add(row_index)
            exact_dupes_skipped += 1
            print(
                f"EXACT_DUPE: kept row {kept_index + 2}, skipped row {row_index + 2} "
                f"(email: {email})"
            )

    processed_pairs: set[tuple[int, int]] = set()
    for row_index_a, row_index_b, _ratio in fuzzy_pairs:
        pair_key = tuple(sorted((row_index_a, row_index_b)))
        if pair_key in processed_pairs:
            continue
        processed_pairs.add(pair_key)

        row_a = all_rows_list[row_index_a]
        row_b = all_rows_list[row_index_b]
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
                f"DIFF_PEOPLE: rows {row_index_a + 2} & {row_index_b + 2} same name, "
                f"different emails - both reset to PENDING for base template"
            )
            diff_person_pairs_reset += 1
        elif different_companies:
            message = (
                f"DIFF_PEOPLE: rows {row_index_a + 2} & {row_index_b + 2} same name, "
                f"diff company - both reset to PENDING for base template"
            )
            diff_person_pairs_reset += 1
        else:
            message = (
                f"SUSPICIOUS_DUPE: rows {row_index_a + 2} & {row_index_b + 2} - "
                f"same name + same/missing company. Possible People API error. Manual review needed."
            )
            suspicious_pairs_flagged += 1

        for row_index in pair_key:
            live_row = sheet_cache.get_row(row_index)
            if (live_row.get("STATUS") or "").strip() in {config.STATUS_EMAIL_DONE, config.STATUS_SENT, "SKIP_DUPLICATE", "SENT"}:
                print(f"Skipping terminal-state row {row_index} (STATUS={live_row.get('STATUS')}) in fuzzy preflight")
                continue

            expected_name = all_rows_list[row_index].get("Name", "")
            actual_name = live_row.get("Name", "")
            if actual_name != expected_name:
                raise ValueError(
                    f"Row mismatch - expected {expected_name}, found {actual_name}. Aborting."
                )

            updates = _preflight_reset_updates()
            _maybe_add_warning(live_row, message, updates)
            # NOTE: must be called before asyncio.gather; writes are NOT lock-protected.
            sheet_cache.write_row(row_index, updates)
            live_row.update(updates)
            modified_rows.add(row_index)

        print(message)

    print("=== Preflight Summary ===")
    print(f"Exact dupes skipped: {exact_dupes_skipped}")
    print(f"Confirmed diff-person pairs reset: {diff_person_pairs_reset}")
    print(f"Suspicious pairs flagged: {suspicious_pairs_flagged}")
    print(f"Total rows modified: {len(modified_rows)}")

    if modified_rows:
        sheet_cache.load(tab_name)

    return {
        "exact_dupes_skipped": exact_dupes_skipped,
        "diff_person_pairs_reset": diff_person_pairs_reset,
        "suspicious_pairs_flagged": suspicious_pairs_flagged,
        "rows_modified": len(modified_rows),
    }


def _create_email_backup_shifts(current_row: dict) -> dict[str, str] | None:
    """Build the circular-buffer shift dict for email backup (max 3 slots).

    On every call: V3 <- V2, V2 <- V1, V1 <- current Subject/Body.
    Returns the shift dict, or None if there was nothing to back up.
    The caller is responsible for writing the dict to the sheet.
    """
    old_subject = (current_row.get("Subject") or "").strip()
    old_body = (current_row.get("Body") or "").strip()
    if not old_subject and not old_body:
        return None

    shift = {
        "Subject_v3": (current_row.get("Subject_v2") or ""),
        "Body_v3": (current_row.get("Body_v2") or ""),
        "Subject_v2": (current_row.get("Subject_v1") or ""),
        "Body_v2": (current_row.get("Body_v1") or ""),
        "Subject_v1": old_subject,
        "Body_v1": old_body,
    }
    print("  [Backup]  Email shifted into circular buffer (v3←v2, v2←v1, v1←current)")
    return shift


def _serialize_tavily_metadata(profile: dict | None, fallback: str = "") -> str:
    if not profile:
        return fallback
    metadata = profile.get("tavily_metadata", fallback)
    if isinstance(metadata, str):
        return metadata[:10000]
    return json.dumps(metadata, ensure_ascii=True)[:10000]


def _clean_verified_company(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in {"unknown", "blank", "none", "n/a", "-"}:
        return ""
    return text


def _clear_checkpoint() -> None:
    """Delete the checkpoint file so the next run starts fresh."""
    progress_path = Path(config.PROGRESS_FILE)
    if progress_path.exists():
        progress_path.unlink()
        print("[Checkpoint] Cleared.")


def _maybe_resume_from_checkpoint(
    start_index: int, cohort_file: str, tab_name: str = "", *, force: bool = False
) -> int:
    progress = load_progress()
    if not progress:
        return start_index

    if progress.get("cohort_file") != cohort_file or progress.get("tab_name", "") != tab_name:
        return start_index

    resume_index = int(progress.get("last_row_completed", 0))
    assert resume_index >= 0, f"Invalid checkpoint cache index: {resume_index}"
    if resume_index <= start_index:
        return start_index

    if force:
        print(
            f"[Checkpoint] --force flag set. Ignoring checkpoint (cache index {resume_index}), "
            f"starting from cache index {start_index}."
        )
        return start_index

    prompt = (
        f"Checkpoint found at cache index {resume_index}. Current start_index={start_index}.\n"
        f"  Options:\n"
        f"    y - Resume from checkpoint (cache index {resume_index})\n"
        f"    n - Start fresh from specified start_index ({start_index})\n"
        f"    c - Clear checkpoint and start fresh\n"
        f"  Your choice (y/n/c): "
    )
    answer = input(prompt).strip().lower()
    if answer == "y":
        log.info(f"Resuming from cache index {resume_index}")
        return resume_index
    if answer == "c":
        _clear_checkpoint()
    return start_index


def _mark_remaining_rows_pending(sheet_cache, start_index: int, total: int) -> int:
    """Mark unfinished rows as PENDING (not FAILED) so they are retry-friendly.

    When Tavily keys are exhausted mid-batch, the remaining rows haven't actually
    failed — they just couldn't be attempted yet.  PENDING lets the next run pick
    them up automatically instead requiring a manual status reset.
    """
    count = 0
    for row_index in range(start_index, total):
        row = sheet_cache.get_row(row_index)
        status = row.get("STATUS", "")
        if status in {config.STATUS_EMAIL_DONE, config.STATUS_SENT} or row.get("Sent", "") == "YES":
            continue
        enrichment_notes = (row.get("Enrichment_Notes") or "").strip()
        if status in {config.STATUS_FAILED_PARSE, config.STATUS_RESEARCH_DONE} or enrichment_notes:
            print(f"Preserving row {row_index} with STATUS={status} — not resetting to PENDING")
            continue
        if status != config.STATUS_PENDING:
            sheet_cache.set_row_status(row_index, config.STATUS_PENDING)
            count += 1
    return count


def run_phase2(
    source="json", limit=0, start_row=1, tab_name: str | None = None, *, force: bool = False
):
    """
    Execute Phase 2: Research + Email Generation.

    Args:
        source: "json" to read from alumni_clean.json (local-first, no Sheets needed)
                "sheet" to read/write from Google Sheet
        limit:  max alumni to process (0 = all)
        start_row: 1-based data-row offset to begin processing from
        force: if True, bypass checkpoint prompt and start from start_row
    """
    if start_row < 1:
        raise ValueError("start_row must be >= 1")

    clear_tavily_exhaustion_state()

    print("=" * 60)
    print("PHASE 2: LLM Research + Email Generation")
    print(f"  Source: {source}")
    print(f"  Provider: {config.MODEL_PROVIDER}")
    print(f"  Model: {config.LLM_MODEL}")
    print(f"  Start row: {start_row}")
    if source == "sheet":
        print(f"  Tab: {tab_name or config.SHEET_NAME}")
    print("=" * 60)

    if source == "json":
        _run_from_json(limit, start_row, force=force)
    elif source == "sheet":
        asyncio.run(_run_from_sheet_async(limit, start_row, tab_name=tab_name, force=force))
        try:
            from cohort_runner import sync_manifest

            sync_manifest()
            print("[Phase 2] Manifest synced.")
        except Exception as error:
            print(f"[Phase 2] WARNING: manifest sync failed: {error}")
    else:
        print(f"[Phase 2] Unknown source: {source}")


def _run_from_json(limit: int, start_row: int, *, force: bool = False):
    """Read alumni_clean.json, research, gen emails, output to alumni_outreach.json."""
    input_path = BASE_DIR / "alumni_clean.json"
    output_path = BASE_DIR / "alumni_outreach.json"

    if not input_path.exists():
        print(f"[Phase 2] {input_path.name} not found. Run parse_alumni_csv.py first.")
        return

    start_run(phase="2", cohort_file=input_path.name, tab_name="")
    requested_start_index = start_row - 1
    start_index = _maybe_resume_from_checkpoint(requested_start_index, input_path.name, "", force=force)
    start_row = start_index + 1

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    all_alumni = data.get("alumni", [])
    total = len(all_alumni)
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

        visible_row_number = start_index + i + 1
        set_current_row(row_number=visible_row_number, alumni_name=name)
        try:
            try:
                profile = research_alumni(
                    name=name,
                    batch=batch,
                    graduation_year=batch,
                    last_known_role=role,
                    location=location or "",
                    profile_url=profile_url or "",
                )
            except ProfileFenceError as error:
                log_event(
                    phase="2A",
                    row_number=visible_row_number,
                    alumni_name=name,
                    api_called="Gemini Research",
                    error_type=config.STATUS_FAILED_PARSE,
                    raw_response_snippet=error.raw_response,
                )
                stats["errors"] += 1
                continue
            except AllTavilyKeysExhaustedError as error:
                log_event(
                    phase="2A",
                    row_number=visible_row_number,
                    alumni_name=name,
                    api_called="Tavily",
                    http_status=429,
                    error_type="ALL_TAVILY_KEYS_EXHAUSTED",
                    raw_response_snippet=str(error),
                )
                print("[Phase 2] All Tavily keys exhausted. Exiting cleanly.")
                break

            verified_company = ""
            if not profile:
                print("  [Research]  FAILED -> using base template")
                try:
                    subject, body = generate_email_base_template(name, batch)
                except EmailFenceError as error:
                    log_event(
                        phase="2B",
                        row_number=visible_row_number,
                        alumni_name=name,
                        api_called="Gemini Base Template",
                        error_type=config.STATUS_FAILED_PARSE,
                        raw_response_snippet=error.raw_response,
                    )
                    stats["errors"] += 1
                    continue
                enrichment_source = "base_template"
                stats["base_template"] += 1
                confidence = "FAILED"
                research_raw = ""
            elif not is_profile_usable(profile):
                profile_conf = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
                print(f"  [Research]  Low confidence ({profile_conf}) -> base template")
                try:
                    subject, body = generate_email_base_template(name, batch)
                except EmailFenceError as error:
                    log_event(
                        phase="2B",
                        row_number=visible_row_number,
                        alumni_name=name,
                        api_called="Gemini Base Template",
                        error_type=config.STATUS_FAILED_PARSE,
                        raw_response_snippet=error.raw_response,
                    )
                    stats["errors"] += 1
                    continue
                enrichment_source = "base_template"
                stats["low_conf"] += 1
                confidence = profile.get("confidence", "Low")
                research_raw = profile.get("raw_profile", "")
            else:
                conf = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
                print(f"  [Research]  Confidence: {conf}")
                verified_company = _clean_verified_company(profile.get("company", ""))
                confidence = profile.get("confidence", "")
                research_raw = profile.get("raw_profile", "")
                if not verified_company:
                    print("  [Research]  No verified company -> base template")
                    try:
                        subject, body = generate_email_base_template(name, batch)
                    except EmailFenceError as error:
                        log_event(
                            phase="2B",
                            row_number=visible_row_number,
                            alumni_name=name,
                            api_called="Gemini Base Template",
                            error_type=config.STATUS_FAILED_PARSE,
                            raw_response_snippet=error.raw_response,
                        )
                        stats["errors"] += 1
                        continue
                    enrichment_source = "base_template"
                    stats["base_template"] += 1
                else:
                    if conf in ("very_high", "high"):
                        stats["high_conf"] += 1
                    else:
                        stats["medium_conf"] += 1

                    print("  [Email Gen] Generating from verified profile...")
                    try:
                        subject, body = generate_email_from_profile(
                            name,
                            profile,
                            enrichment_source=enrichment_source,
                        )
                    except EmailFenceError as error:
                        log_event(
                            phase="2B",
                            row_number=visible_row_number,
                            alumni_name=name,
                            api_called="Gemini Email",
                            error_type=config.STATUS_FAILED_PARSE,
                            raw_response_snippet=error.raw_response,
                        )
                        stats["errors"] += 1
                        continue
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
                "tavily_raw": profile.get("tavily_raw", "")[:10000] if profile else "",
                "tavily_metadata": _serialize_tavily_metadata(profile),
            }
            results.append(record)
            save_progress(last_row_completed=start_index + i + 1, cohort_file=input_path.name, tab_name="")

            time.sleep(config.ENRICHMENT_DELAY)
        finally:
            clear_current_row()

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


# ---------------------------------------------------------------------------
# Async sheet orchestrator
# ---------------------------------------------------------------------------

_PROVIDER_CONCURRENCY = {
    "gemini_aistudio": config.GEMINI_CONCURRENCY,
    "kimi_direct": config.KIMI_CONCURRENCY,
    "openrouter_kimi": 3,
    "openrouter_claude": 3,
    "openrouter_gpt": 3,
}

_GEMINI_CONCURRENCY = _PROVIDER_CONCURRENCY.get(config.MODEL_PROVIDER, 3)
_TAVILY_CONCURRENCY = config.TAVILY_CONCURRENCY


async def _process_row_async(
    sheet_cache,
    sheet_row_index: int,
    total: int,
    active_tab: str,
    gemini_sem: asyncio.Semaphore,
    kimi_sem: asyncio.Semaphore,
    tavily_sem: asyncio.Semaphore,
    stop_event: asyncio.Event,
) -> dict:
    """Process a single sheet row asynchronously.  Returns a result dict."""
    if stop_event.is_set():
        return {"outcome": "cancelled"}

    row = sheet_cache.get_row(sheet_row_index)
    sheet_row_number = sheet_row_index + 2
    name = row.get("Name", "")
    email = row.get("Email", "")
    status = row.get("STATUS", "")
    batch = row.get("Graduation_Year", "")
    research_sem = kimi_sem if config.MODEL_PROVIDER == "kimi_direct" else gemini_sem

    set_current_row(row_number=sheet_row_number, alumni_name=name)
    try:
        async with sheet_cache._lock:
            await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_PROCESSING)
            row["STATUS"] = config.STATUS_PROCESSING

        if not email:
            async with sheet_cache._lock:
                await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_FAILED)
            log_event(
                phase="2B",
                row_number=sheet_row_number,
                alumni_name=name,
                api_called="Sheet Validation",
                error_type="MISSING_EMAIL",
                raw_response_snippet="Row has no email address.",
            )
            return {"outcome": "skipped", "reason": "no_email"}

        print(f"\n[Phase 2] (sheet row {sheet_row_number}/{total + 1}) Researching: {name}")

        company = row.get("AlmaConnect_Company", "")
        linkedin_url = row.get("LinkedIn_URL", "")
        profile = None
        research_complete = status == config.STATUS_RESEARCH_DONE

        # --- Research phase ---
        try:
            if status == config.STATUS_RESEARCH_DONE:
                profile = parse_profile_response(row.get("Enrichment_Notes", ""), name=name)
                if not profile:
                    broken_xml = row.get("Enrichment_Notes", "")
                    log_event(
                        phase="2A",
                        row_number=sheet_row_number,
                        alumni_name=name,
                        api_called="Stored PROFILE",
                        error_type=config.STATUS_FAILED_PARSE,
                        raw_response_snippet=broken_xml[:300],
                    )
                    cleanup_updates = {
                        "Verified_Company": "",
                        "Enrichment_Notes": "",
                        "Enrichment_Source": "",
                        "STATUS": config.STATUS_FAILED_PARSE,
                    }
                    async with sheet_cache._lock:
                        await asyncio.to_thread(sheet_cache.write_row, sheet_row_index, cleanup_updates)
                        row.update(cleanup_updates)
                    return {"outcome": "error", "type": "stored_profile_parse"}
            else:
                if stop_event.is_set():
                    return {"outcome": "cancelled"}
                profile = await research_alumni_async(
                    name=name,
                    batch=batch,
                    graduation_year=batch,
                    last_known_role=company,
                    location="",
                    profile_url=linkedin_url or "",
                    gemini_sem=gemini_sem,
                    research_sem=research_sem,
                    tavily_sem=tavily_sem,
                )
                if profile:
                    research_complete = True
                    confidence_level = (
                        extract_confidence_level(profile.get("raw_profile", ""))
                        or profile.get("confidence_level", "")
                    )
                    research_updates = {
                        "Verified_Company": _clean_verified_company(profile.get("company", "")),
                        "Enrichment_Notes": profile.get("raw_profile", "")[:1000],
                        "Tavily_Raw": profile.get("tavily_raw", "")[:10000],
                        "Tavily_Metadata": _serialize_tavily_metadata(profile),
                        "Enrichment_Source": "llm_research",
                        "STATUS": config.STATUS_RESEARCH_DONE,
                    }
                    research_updates["Confidence_Level"] = confidence_level
                    log.info(
                        "[Phase 2A] Extracted confidence for row %s: %s",
                        sheet_row_number,
                        confidence_level,
                    )
                    log.info(
                        "[Phase 2A] Research updates keys for row %s: %s",
                        sheet_row_number,
                        sorted(research_updates.keys()),
                    )
                    async with sheet_cache._lock:
                        await asyncio.to_thread(sheet_cache.write_row, sheet_row_index, research_updates)
                        row.update(research_updates)
        except ProfileFenceError as error:
            async with sheet_cache._lock:
                await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_FAILED_PARSE)
            log_event(
                phase="2A",
                row_number=sheet_row_number,
                alumni_name=name,
                api_called="Gemini Research",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=error.raw_response,
            )
            return {"outcome": "error", "type": "profile_fence"}
        except AllTavilyKeysExhaustedError as error:
            log_event(
                phase="2A",
                row_number=sheet_row_number,
                alumni_name=name,
                api_called="Tavily",
                http_status=429,
                error_type="ALL_TAVILY_KEYS_EXHAUSTED",
                raw_response_snippet=str(error),
            )
            stop_event.set()
            return {"outcome": "tavily_exhausted"}

        if stop_event.is_set():
            return {"outcome": "cancelled"}

        # --- Email generation phase ---
        enrichment_notes = row.get("Enrichment_Notes", "")
        tavily_raw = row.get("Tavily_Raw", "")
        tavily_metadata = row.get("Tavily_Metadata", "")
        verified_company = row.get("Verified_Company", "")
        enrichment_source = row.get("Enrichment_Source", "")
        subject = None
        body = None

        try:
            if not profile or not is_profile_usable(profile):
                subject, body = await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
                enrichment_source = "base_template"
            else:
                # --- Confidence & flag gating (second line of defense) ---
                conf_level = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
                flags_str = (profile.get("flags", "") or "").upper()
                if conf_level in ("low", "unconfirmed"):
                    print(f"  [Phase 2] Low confidence ({conf_level}) for {name}; forcing base template")
                    subject, body = await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
                    enrichment_source = "base_template"
                elif (
                    "IDENTITY_UNCONFIRMED" in flags_str
                    or "BATCH_YEAR_MISMATCH" in flags_str
                    or "WRONG_PERSON" in flags_str
                    or "EDUCATION_MISMATCH" in flags_str
                ):
                    print(f"  [Phase 2] Identity flags detected for {name}; using base template")
                    subject, body = await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
                    enrichment_source = "base_template"
                else:
                    verified_company = _clean_verified_company(profile.get("company", verified_company))
                    enrichment_notes = profile.get("raw_profile", enrichment_notes)[:1000]
                    tavily_raw = profile.get("tavily_raw", tavily_raw)[:10000]
                    tavily_metadata = _serialize_tavily_metadata(profile, tavily_metadata)
                    if not verified_company:
                        print("  [Research]  No verified company -> base template")
                        subject, body = await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
                        enrichment_source = "base_template"
                    else:
                        print("  [Email Gen] Generating from verified profile...")
                        subject, body = await generate_email_from_profile_async(
                            name,
                            profile,
                            enrichment_source=enrichment_source,
                            gemini_sem=gemini_sem,
                        )
                        enrichment_source = "llm_research"
        except EmailFenceError as error:
            async with sheet_cache._lock:
                await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_FAILED_PARSE)
            log_event(
                phase="2B",
                row_number=sheet_row_number,
                alumni_name=name,
                api_called="Gemini Email",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=error.raw_response,
            )
            return {"outcome": "error", "type": "email_fence"}

        if not subject or not body:
            async with sheet_cache._lock:
                if research_complete:
                    await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_RESEARCH_DONE)
                else:
                    await asyncio.to_thread(sheet_cache.set_row_status, sheet_row_index, config.STATUS_FAILED)
            return {"outcome": "error", "type": "empty_email"}

        updates = {
            "Verified_Company": verified_company,
            "Enrichment_Notes": enrichment_notes,
            "Tavily_Raw": tavily_raw,
            "Tavily_Metadata": tavily_metadata,
            "Enrichment_Source": enrichment_source,
            "Subject": subject,
            "Body": body,
            "STATUS": config.STATUS_EMAIL_DONE,
        }
        async with sheet_cache._lock:
            backup_shift = _create_email_backup_shifts(row)
            if backup_shift:
                await asyncio.to_thread(sheet_cache.write_row, sheet_row_index, backup_shift)
                row.update(backup_shift)
            await asyncio.to_thread(sheet_cache.write_row, sheet_row_index, updates)
            row.update(updates)

        conf_level = (
            _normalize_confidence_level(profile.get("confidence_level", "unknown"))
            if profile and is_profile_usable(profile)
            else None
        )
        print(f"  [Done]  Subject: {subject}")
        return {
            "outcome": "processed",
            "conf_level": conf_level,
            "enrichment_source": enrichment_source,
            "next_cache_index": sheet_row_index + 1,
            "sheet_row_number": sheet_row_number,
        }
    finally:
        clear_current_row()


async def _run_from_sheet_async(limit: int, start_row: int, tab_name: str | None = None, *, force: bool = False):
    """Async version of _run_from_sheet — processes rows concurrently."""
    from sheets_helper import SheetCache, initialize_sheet

    active_tab = tab_name or config.SHEET_NAME
    initialize_sheet(active_tab, create_if_missing=False)
    start_run(phase="2", cohort_file="sheet", tab_name=active_tab)
    requested_start_index = 0 if start_row <= 1 else start_row - 2
    start_index = _maybe_resume_from_checkpoint(requested_start_index, "sheet", active_tab, force=force)

    sheet_cache = SheetCache(active_tab, create_if_missing=False).load(active_tab)
    rows = sheet_cache.rows
    if not rows:
        print("[Phase 2] No rows in sheet. Run Phase 1 first.")
        return

    all_rows_list = [sheet_cache.get_row(i) for i in sorted(rows.keys())]
    run_duplicate_preflight(all_rows_list, sheet_cache, active_tab)
    rows = sheet_cache.rows

    recovered_processing = 0
    for sheet_row_index in sorted(rows.keys()):
        row = sheet_cache.get_row(sheet_row_index)
        if (row.get("STATUS") or "").strip() == config.STATUS_PROCESSING:
            sheet_cache.set_row_status(sheet_row_index, config.STATUS_PENDING)
            recovered_processing += 1
    if recovered_processing:
        print(f"[Phase 2] Recovered {recovered_processing} stuck PROCESSING rows -> PENDING")
        log.info("Recovered %s stuck PROCESSING rows", recovered_processing)

    total = len(rows)
    if start_index >= total:
        print(f"[Phase 2] start_row {start_row} is beyond the available {total} sheet rows.")
        return

    # Collect eligible row indices
    eligible = []
    skipped_prior = 0
    for sheet_row_index in range(start_index, total):
        if limit and len(eligible) >= limit:
            break
        row = sheet_cache.get_row(sheet_row_index)
        status = row.get("STATUS", "").strip()
        sent = row.get("Sent", "").strip().upper()
        if status in {config.STATUS_EMAIL_DONE, config.STATUS_SENT} or sent == "YES":
            skipped_prior += 1
            name = row.get("Name", "")
            sheet_row_number = sheet_row_index + 2
            print(f"[Phase 2] (sheet row {sheet_row_number}/{total + 1}) Skipping {name} -- already processed")
            continue
        eligible.append(sheet_row_index)

    if not eligible:
        print("[Phase 2] No eligible rows to process.")
        return

    print(f"\n[Phase 2] Launching {len(eligible)} rows concurrently "
          f"(Gemini concurrency={_GEMINI_CONCURRENCY}, Tavily concurrency={_TAVILY_CONCURRENCY})")
    log.info(f"Concurrency config — Gemini: {_GEMINI_CONCURRENCY}, Tavily: {_TAVILY_CONCURRENCY}")
    research_semaphore_name = "kimi_sem" if config.MODEL_PROVIDER == "kimi_direct" else "gemini_sem"
    print(
        f"[Phase 2] Semaphore routing — 2A research: {research_semaphore_name}; "
        f"2B email: gemini_sem"
    )
    log.info(
        "Semaphore routing — 2A research: %s; 2B email: gemini_sem",
        research_semaphore_name,
    )

    gemini_sem = asyncio.Semaphore(_GEMINI_CONCURRENCY)
    kimi_sem = asyncio.Semaphore(config.KIMI_CONCURRENCY)
    tavily_sem = asyncio.Semaphore(_TAVILY_CONCURRENCY)
    stop_event = asyncio.Event()

    tasks = [
        _process_row_async(sheet_cache, idx, total, active_tab, gemini_sem, kimi_sem, tavily_sem, stop_event)
        for idx in eligible
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Aggregate stats
    stats = {
        "processed": 0,
        "skipped": skipped_prior,
        "errors": 0,
        "high_conf": 0,
        "medium_conf": 0,
        "base_template": 0,
        "low_conf": 0,
    }
    tavily_exhausted = False
    max_completed_index = 0

    for result in results:
        if isinstance(result, Exception):
            stats["errors"] += 1
            print(f"[Phase 2] Unexpected task error: {result}")
            continue
        outcome = result.get("outcome")
        if outcome == "processed":
            stats["processed"] += 1
            next_cache_index = result.get("next_cache_index", 0)
            if next_cache_index > max_completed_index:
                max_completed_index = next_cache_index
            conf = result.get("conf_level")
            src = result.get("enrichment_source")
            if src == "base_template":
                if conf:
                    stats["low_conf"] += 1
                else:
                    stats["base_template"] += 1
            elif conf in ("very_high", "high"):
                stats["high_conf"] += 1
            else:
                stats["medium_conf"] += 1
        elif outcome == "error":
            stats["errors"] += 1
        elif outcome == "skipped":
            stats["skipped"] += 1
        elif outcome == "tavily_exhausted":
            tavily_exhausted = True
        elif outcome == "cancelled":
            stats["skipped"] += 1

    if tavily_exhausted:
        pending_count = _mark_remaining_rows_pending(sheet_cache, start_index, total)
        print(f"[Phase 2] All Tavily keys exhausted. Marked {pending_count} remaining rows PENDING.")

    if max_completed_index:
        save_progress(last_row_completed=max_completed_index, cohort_file="sheet", tab_name=active_tab)

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
