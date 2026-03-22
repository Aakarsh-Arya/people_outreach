"""
Main Runner -- Gmail Automation Pipeline.
Orchestrates Phase 1 -> Phase 2 (Phase 3 runs in Apps Script).
"""

import sys


def _configure_realtime_output() -> None:
    """Force line-buffered, write-through stdout/stderr for streaming logs."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True, write_through=True)


def _parse_phase2_args(raw_args):
    limit = None
    start_row = None
    tab_name = None
    force = False
    positionals = []

    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token == "--count":
            limit = int(raw_args[index + 1])
            index += 2
        elif token == "--start":
            start_row = int(raw_args[index + 1])
            index += 2
        elif token == "--tab":
            tab_name = raw_args[index + 1]
            index += 2
        elif token == "--force":
            force = True
            index += 1
        else:
            positionals.append(token)
            index += 1

    if limit is None:
        limit = int(positionals[0]) if len(positionals) > 0 else 0
    if start_row is None:
        start_row = int(positionals[1]) if len(positionals) > 1 else 1

    return limit, start_row, tab_name, force


def _parse_phase1_args(raw_args):
    csv_path = None
    tab_name = None
    force_clear = False
    positionals = []

    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token == "--tab":
            tab_name = raw_args[index + 1]
            index += 2
        elif token == "--force-clear":
            force_clear = True
            index += 1
        else:
            positionals.append(token)
            index += 1

    if positionals:
        csv_path = positionals[0]

    return csv_path, tab_name, force_clear


def _parse_cohort_run_args(raw_args):
    cohort_year = None
    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token == "--cohort":
            cohort_year = raw_args[index + 1]
            index += 2
        else:
            raise ValueError(f"Unknown cohort-run option: {token}")
    return cohort_year


def _parse_sync_manifest_args(raw_args):
    cohort_year = None
    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token == "--cohort":
            cohort_year = raw_args[index + 1]
            index += 2
        else:
            raise ValueError(f"Unknown sync-manifest option: {token}")
    return cohort_year


def print_banner():
    print()
    print("=" * 60)
    print("  GMAIL AUTOMATION PIPELINE")
    print("  IIM Udaipur Alumni Outreach")
    print("=" * 60)
    print()


def run_all(csv_path=None):
    """Run Phase 1 then Phase 2 sequentially."""
    from phase1_contact_resolution import infer_tab_name_from_csv, run_phase1
    from phase2_orchestrator import run_phase2

    print_banner()
    print("Running full pipeline: Phase 1 -> Phase 2")
    print("(Phase 3 runs separately in Google Apps Script)\n")

    target_tab = infer_tab_name_from_csv(csv_path)
    run_phase1(csv_path=csv_path, tab_name=target_tab)
    print()
    run_phase2(source="sheet", tab_name=target_tab)

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("Next step: Open Google Sheet -> Apps Script -> run sendBatch()")
    print("=" * 60)


def print_usage():
    print_banner()
    print("Usage:")
    print("  python main.py test              -- Test OpenRouter API connection")
    print("  python main.py research NAME     -- Research one alumni by name from alumni_clean.json")
    print("  python main.py phase2 [N] [S]    -- Phase 2: Research + Gen emails (local JSON mode)")
    print("                                      Optional N = count, S = 1-based start row")
    print("  python main.py phase2-sheet [N] [S] [--tab TAB] [--count N] [--start S] [--force]")
    print("                                      Phase 2 in Google Sheet mode with optional tab override")
    print("                                      --force bypasses checkpoint prompt")
    print("  python main.py phase1 [CSV] [--tab TAB] [--force-clear]")
    print("                                      Phase 1: CSV -> People API -> idempotent sheet upsert")
    print("  python main.py phase1-retry [--tab TAB]")
    print("                                      Retry guessed/ambiguous Phase 1 rows in Sheet")
    print("  python main.py cohort-run [--cohort YEAR] -- Run Phase 1/2 for one manifest cohort")
    print("  python main.py sync-manifest [--cohort YEAR] -- Refresh manifest counts/status from sheet tabs")
    print("  python main.py all [CSV]         -- Full: Phase 1 + Phase 2 (sheet mode)")
    print()
    print("Phase 3 (sending) runs in Google Apps Script -- see apps_script_sender.js")
    print()


def run_test():
    """Quick test of OpenRouter API connectivity."""
    print_banner()
    print("Testing OpenRouter API (Qwen 3 Max)...\n")

    from phase2b_email_generation import generate_email_base_template
    subject, body = generate_email_base_template("Test User", "PGP '23")

    if subject and body:
        print("API connection successful!\n")
        print(f"SUBJECT: {subject}\n")
        print(f"BODY:\n{body}")
    else:
        print("API connection FAILED. Check your API key in .env.local")


def run_research_one(name_query: str):
    """Research a single alumni by name (looks up in alumni_clean.json)."""
    import json
    from pathlib import Path
    from phase2a_alumni_research import research_alumni, is_profile_usable, get_safe_hooks
    from phase2b_email_generation import generate_email_from_profile, generate_email_base_template

    print_banner()

    data_path = Path(__file__).parent / "alumni_clean.json"
    if not data_path.exists():
        print("alumni_clean.json not found. Run parse_alumni_csv.py first.")
        return

    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)

    # Find the alumni by name (case-insensitive partial match)
    matches = [
        a for a in data["alumni"]
        if name_query.lower() in a["name"].lower()
    ]

    if not matches:
        print(f"No alumni found matching '{name_query}'")
        return

    if len(matches) > 1:
        print(f"Multiple matches for '{name_query}':")
        for m in matches:
            print(f"  - {m['name']} ({m['batch']})")
        print("Using first match.\n")

    alumnus = matches[0]
    print(f"Researching: {alumnus['name']} ({alumnus.get('batch', '')})")
    print(f"  Last role: {alumnus.get('current_role', 'N/A')}")
    print(f"  Location:  {alumnus.get('location', 'N/A')}")
    print()

    # Step 1: Research
    profile = research_alumni(
        name=alumnus["name"],
        batch=alumnus.get("batch", ""),
        last_known_role=alumnus.get("current_role", ""),
        location=alumnus.get("location", "") or "",
        profile_url=alumnus.get("profile_url", "") or "",
    )

    if not profile:
        print("\n[Research FAILED]\n")
        return

    print("\n--- PARSED PROFILE ---")
    display = {k: v for k, v in profile.items() if k != "raw_profile"}
    print(json.dumps(display, indent=2, ensure_ascii=False))
    print(f"\nUsable: {is_profile_usable(profile)}")
    print(f"Safe hooks: {get_safe_hooks(profile)}")

    # Step 2: Generate email
    print("\n--- GENERATING EMAIL ---")
    if is_profile_usable(profile):
        subject, body = generate_email_from_profile(
            alumnus["name"],
            profile,
            enrichment_source="llm_research",
        )
    else:
        subject, body = generate_email_base_template(alumnus["name"], alumnus.get("batch", ""))

    if subject and body:
        print(f"\nSUBJECT: {subject}")
        print(f"\nBODY:\n{body}")
    else:
        print("\nEmail generation FAILED.")

    print(f"\n--- RAW RESEARCH OUTPUT ---\n{profile.get('raw_profile', '')}")


if __name__ == "__main__":
    _configure_realtime_output()

    if len(sys.argv) < 2:
        print_usage()
        sys.exit(0)

    command = sys.argv[1].lower()

    try:
        if command == "all":
            csv_path = sys.argv[2] if len(sys.argv) > 2 else None
            run_all(csv_path=csv_path)
        elif command == "phase1":
            from phase1_contact_resolution import run_phase1
            print_banner()
            csv_path, tab_name, force_clear = _parse_phase1_args(sys.argv[2:])
            run_phase1(csv_path=csv_path, tab_name=tab_name, force_clear=force_clear)
        elif command == "phase1-retry":
            from phase1_contact_resolution import run_phase1_retry
            print_banner()
            tab_name = None
            retry_args = sys.argv[2:]
            idx = 0
            while idx < len(retry_args):
                if retry_args[idx] == "--tab":
                    tab_name = retry_args[idx + 1]
                    idx += 2
                else:
                    raise ValueError(f"Unknown phase1-retry option: {retry_args[idx]}")
                    idx += 1
            run_phase1_retry(tab_name=tab_name)
        elif command == "phase2":
            from phase2_orchestrator import run_phase2
            print_banner()
            lim, start_row, tab_name, force = _parse_phase2_args(sys.argv[2:])
            run_phase2(source="json", limit=lim, start_row=start_row, tab_name=tab_name, force=force)
        elif command == "phase2-sheet":
            from phase2_orchestrator import run_phase2
            print_banner()
            lim, start_row, tab_name, force = _parse_phase2_args(sys.argv[2:])
            run_phase2(source="sheet", limit=lim, start_row=start_row, tab_name=tab_name, force=force)
        elif command == "cohort-run":
            from cohort_runner import run_cohort
            print_banner()
            cohort_year = _parse_cohort_run_args(sys.argv[2:])
            run_cohort(cohort_year=cohort_year)
        elif command == "sync-manifest":
            from cohort_runner import sync_manifest
            print_banner()
            cohort_year = _parse_sync_manifest_args(sys.argv[2:])
            sync_manifest(cohort_year=cohort_year)
        elif command == "test":
            run_test()
        elif command == "research":
            if len(sys.argv) < 3:
                print("Usage: python main.py research <name>")
                print("Example: python main.py research 'Gaurav Singh'")
                sys.exit(1)
            name = " ".join(sys.argv[2:])
            run_research_one(name)
        else:
            print(f"Unknown command: {command}")
            print_usage()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
