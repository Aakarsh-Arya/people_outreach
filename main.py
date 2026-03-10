"""
Main Runner -- Gmail Automation Pipeline.
Orchestrates Phase 1 -> Phase 2 (Phase 3 runs in Apps Script).
"""

import sys


def print_banner():
    print()
    print("=" * 60)
    print("  GMAIL AUTOMATION PIPELINE")
    print("  IIM Udaipur Alumni Outreach")
    print("=" * 60)
    print()


def run_all(csv_path=None):
    """Run Phase 1 then Phase 2 sequentially."""
    from phase1_contact_resolution import run_phase1
    from phase2_orchestrator import run_phase2

    print_banner()
    print("Running full pipeline: Phase 1 -> Phase 2")
    print("(Phase 3 runs separately in Google Apps Script)\n")

    run_phase1(csv_path=csv_path)
    print()
    run_phase2(source="sheet")

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
    print("  python main.py phase2-sheet [N] [S]  -- Phase 2: Same but reads/writes Google Sheet")
    print("  python main.py phase1 [CSV]      -- Phase 1: CSV -> People API -> Sheet")
    print("  python main.py phase1-retry      -- Retry guessed/ambiguous Phase 1 rows in Sheet")
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
        subject, body = generate_email_from_profile(alumnus["name"], profile)
    else:
        subject, body = generate_email_base_template(alumnus["name"], alumnus.get("batch", ""))

    if subject and body:
        print(f"\nSUBJECT: {subject}")
        print(f"\nBODY:\n{body}")
    else:
        print("\nEmail generation FAILED.")

    print(f"\n--- RAW RESEARCH OUTPUT ---\n{profile.get('raw_profile', '')}")


if __name__ == "__main__":
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
            csv_path = sys.argv[2] if len(sys.argv) > 2 else None
            run_phase1(csv_path=csv_path)
        elif command == "phase1-retry":
            from phase1_contact_resolution import run_phase1_retry
            print_banner()
            run_phase1_retry()
        elif command == "phase2":
            from phase2_orchestrator import run_phase2
            print_banner()
            lim = int(sys.argv[2]) if len(sys.argv) > 2 else 0
            start_row = int(sys.argv[3]) if len(sys.argv) > 3 else 1
            run_phase2(source="json", limit=lim, start_row=start_row)
        elif command == "phase2-sheet":
            from phase2_orchestrator import run_phase2
            print_banner()
            lim = int(sys.argv[2]) if len(sys.argv) > 2 else 0
            start_row = int(sys.argv[3]) if len(sys.argv) > 3 else 1
            run_phase2(source="sheet", limit=lim, start_row=start_row)
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
