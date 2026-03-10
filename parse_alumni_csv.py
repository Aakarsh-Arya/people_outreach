"""
parse_alumni_csv.py
Parse iimu_2025.csv (AlmaConnect export) into alumni_clean.json.
Handles all role-format variants found in the real data.
"""

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).parent

INPUT_CSV  = BASE_DIR / "iimu_2025.csv"
OUTPUT_JSON = BASE_DIR / "alumni_clean.json"

# ─── Column indices (0-based, matches actual CSV) ─────────────────────────────
COL_PROFILE_URL  = 0
COL_IMAGE_URL    = 1   # dropped
COL_NAME         = 2
COL_BATCH        = 3
COL_ROLE         = 4
COL_LOCATION     = 5
COL_LOCATION_URL = 6   # dropped

# Junk role values to blank out
JUNK_ROLES = {
    "none at none yet :)",
    "none",
    "n/a",
    "-",
    ".",
}


def title_case_name(name: str) -> str:
    """Normalise ALL-CAPS or all-lowercase names to Title Case."""
    if not name:
        return name
    # If every alphabetic char is upper or every is lower → recase
    alpha = [c for c in name if c.isalpha()]
    if alpha and (all(c.isupper() for c in alpha) or all(c.islower() for c in alpha)):
        return name.title()
    return name


def clean_role(raw: str) -> str:
    """
    Extract a clean job title from a messy AlmaConnect role string.

    Patterns handled (from real data):
      "Lead Data Scientist at Tavant Technologies,"          → "Lead Data Scientist"
      "Sr. Manager - Global Procurement at PepsiCo | Ex…"   → "Sr. Manager - Global Procurement"
      "Supply Chain Planning, Product & Analytics| Ex-…"    → "Supply Chain Planning, Product & Analytics"
      "AVP Partnerships @ Piramal| Retail lending…"         → "AVP Partnerships"
      "Sustainability Learner || Building… at HCLTech || …"  → "Sustainability Learner"
      "MBA (Cranfield) | Assistant Vice President …"         → kept as-is (credential prefix)
      "Operations, Strategy, Business Development,"          → "Operations, Strategy, Business Development"
      "oyo rooms,"                                           → "oyo rooms"  (ambiguous, keep)
      "None at none yet :),"                                 → ""
      "",  "  "                                              → ""
    """
    role = raw.strip().strip(",").strip()

    # Collapse internal newlines / extra whitespace
    role = re.sub(r"[\r\n]+", " ", role).strip()

    # Blank junk
    if role.lower() in JUNK_ROLES:
        return ""

    # Normalise separators: "||" → "|",  " @ " → " at "
    role = role.replace("||", "|").replace(" @ ", " at ")

    # Step 1 — strip everything after first " | " (pipe-separated bio noise)
    # But if the first segment is just a credential prefix (MBA, B.Tech, etc.)
    # skip it and use the second segment as the role instead.
    if "|" in role:
        segments = [s.strip().strip(",").strip() for s in role.split("|")]
        # Credential-prefix pattern: short segment that starts with a degree keyword
        _credential_re = re.compile(
            r"^(MBA|PGDM|PGPX|PGP|B\.?Tech|M\.?Tech|MS|MSc|BE|BTech|MTech|BBA|CA|CFA|CPA|IIM)\b",
            re.IGNORECASE,
        )
        if segments and _credential_re.match(segments[0]) and len(segments) > 1:
            role = segments[1]  # jump to the actual role segment
        else:
            role = segments[0]

    # Step 2 — strip company after " at " (case-insensitive)
    at_match = re.search(r"\s+at\s+", role, re.IGNORECASE)
    if at_match:
        role = role[: at_match.start()].strip()

    # Final trim
    role = role.strip(",").strip()
    return role


def make_searchable(name: str, batch: str, role: str, location: str) -> str:
    parts = [p for p in [name, batch, role, location] if p]
    return " | ".join(parts)


def parse_csv(path: Path) -> list[dict]:
    alumni = []
    skipped = 0

    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # skip header

        for row in reader:
            # Pad short rows
            while len(row) < 7:
                row.append("")

            profile_url = row[COL_PROFILE_URL].strip()
            name        = title_case_name(row[COL_NAME].strip())
            batch       = row[COL_BATCH].strip()
            raw_role    = row[COL_ROLE].strip()
            location    = row[COL_LOCATION].strip()

            # Skip rows without a name
            if not name:
                skipped += 1
                continue

            current_role = clean_role(raw_role)

            record = {
                "name":            name,
                "batch":           batch,
                "current_role":    current_role,
                "location":        location if location else None,
                "profile_url":     profile_url if profile_url else None,
                "searchable_text": make_searchable(name, batch, current_role, location),
            }
            alumni.append(record)

    if skipped:
        print(f"  Skipped {skipped} rows with no name.")

    return alumni


def compute_stats(alumni: list[dict]) -> dict:
    locations = Counter(
        a["location"] for a in alumni if a["location"]
    )
    batches = Counter(a["batch"] for a in alumni if a["batch"])

    no_role     = sum(1 for a in alumni if not a["current_role"])
    no_location = sum(1 for a in alumni if not a["location"])

    return {
        "total_alumni":          len(alumni),
        "with_role":             len(alumni) - no_role,
        "without_role":          no_role,
        "with_location":         len(alumni) - no_location,
        "without_location":      no_location,
        "top_10_locations":      dict(locations.most_common(10)),
        "batch_distribution":    dict(sorted(batches.items())),
    }


def main():
    if not INPUT_CSV.exists():
        print(f"ERROR: {INPUT_CSV} not found.")
        sys.exit(1)

    print(f"Parsing {INPUT_CSV.name} ...")
    alumni = parse_csv(INPUT_CSV)

    stats  = compute_stats(alumni)

    output = {
        "meta":   stats,
        "alumni": alumni,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    print(f"\nDone -> {OUTPUT_JSON.name}")
    print(f"\n{'─'*40}")
    print(f"  Total alumni      : {stats['total_alumni']}")
    print(f"  With role         : {stats['with_role']}")
    print(f"  Without role      : {stats['without_role']}")
    print(f"  With location     : {stats['with_location']}")
    print(f"  Without location  : {stats['without_location']}")
    print(f"\n  Top locations:")
    for loc, count in stats["top_10_locations"].items():
        print(f"    {loc:<25} {count}")
    print(f"\n  Batch breakdown:")
    for batch, count in stats["batch_distribution"].items():
        print(f"    {batch:<20} {count}")

    # Quick sanity-check sample
    print(f"\n{'─'*40}")
    print("  Sample (first 5 records):")
    for a in alumni[:5]:
        print(f"  - {a['name']:<30} | {a['batch']:<12} | {a['current_role'][:40]:<40} | {a['location']}")


if __name__ == "__main__":
    main()
