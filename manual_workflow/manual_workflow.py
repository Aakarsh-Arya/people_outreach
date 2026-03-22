"""
Manual Workflow CLI — parallel manual path for IIMU alumni outreach.

Subcommands:
  export   — read PENDING rows from sheet tabs, write to research_queue.md
  ingest   — parse ingest_queue.md, write profiles/emails to the sheet
    query    — filter sheet rows and print them in multiple formats
  rebatch  — reorganise existing batches in research_queue.md
    reconcile-email — mark manually verified email rows back to PENDING
  review   — inspect and manage review_queue.md
  reconcile — reconcile queue files against the sheet
  status   — show current workflow state
  reset    — reset rows back to PENDING (dry run by default)
    reset-email — reset EMAIL_DONE rows back to RESEARCH_DONE
    reset-research — reset RESEARCH_DONE rows back to PENDING
"""

# TODO LIST — updated 2026-03-15
# ─────────────────────────────────────────
# [ ] Run Phase 1 for cohort_2021: python main.py phase1 iimu_2021.csv
# [ ] Run Phase 1 for cohort_2022: python main.py phase1 iimu_2022.csv
# [ ] Run Phase 1 for cohort_2023: python main.py phase1 iimu_2023.csv
# [ ] Run one-time confidence cleanup: python manual_workflow/cleanup_confidence.py --all --dry-run first
# [ ] Fix Dr. Madhupa Bakshi: add Graduation_Year manually in sheet (cohort_2013, Row 27)
# [ ] Investigate and resolve Phase 2A automated path (Gemini grounded search not wired into automated pipeline)
# [x] Moonshot support ticket sent (2026-03-14)
# [x] Ingest hardening pass complete (2026-03-15)

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import sheets_helper

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKFLOW_DIR = Path(__file__).resolve().parent
_RESEARCH_QUEUE = _WORKFLOW_DIR / "research_queue.md"
_INGEST_QUEUE = _WORKFLOW_DIR / "ingest_queue.md"
_REVIEW_QUEUE = _WORKFLOW_DIR / "review_queue.md"
_EMAIL_QUEUE = _WORKFLOW_DIR / "email_queue.md"
_MANUAL_OVERRIDES = _WORKFLOW_DIR / "manual_overrides.md"
_LOGS_DIR = _WORKFLOW_DIR / "logs"
STATUS_SKIP_GUESSED_EMAIL = "SKIP_GUESSED_EMAIL"
GUESSED_SOURCES = {"", "guessed", "ambiguous"}
VERIFIED_EMAIL_SOURCES = {"people_api", config.EMAIL_SOURCE_MANUAL_VERIFIED}
INGEST_QUEUE_TEMPLATE = """# Ingest Queue
_Pending ingest._

Paste Kimi/Qwen output below, between --- separators:

---


---
"""
DETAIL_STATUSES = [
    config.STATUS_PENDING,
    config.STATUS_RESEARCH_DONE,
    config.STATUS_FAILED_PARSE,
    STATUS_SKIP_GUESSED_EMAIL,
    config.STATUS_EMAIL_DONE,
    config.STATUS_SENT,
]
DEFAULT_DETAIL_STATUSES = [
    config.STATUS_PENDING,
    config.STATUS_RESEARCH_DONE,
    config.STATUS_FAILED_PARSE,
    STATUS_SKIP_GUESSED_EMAIL,
]
_RECONCILE_REMOVE_STATUSES = {
    config.STATUS_PROCESSING,
    config.STATUS_RESEARCH_DONE,
    config.STATUS_EMAIL_DONE,
    config.STATUS_SENT,
    STATUS_SKIP_GUESSED_EMAIL,
    config.STATUS_FAILED_PARSE,
    config.STATUS_FAILED,
}

# ---------------------------------------------------------------------------
# Valid PRIMARY_DOMAIN labels
# ---------------------------------------------------------------------------

_VALID_PRIMARY_DOMAINS = {
    "SALES, MARKETING & COMMERCIAL",
    "CONSULTING & STRATEGY",
    "FINANCE & INVESTMENTS",
    "OPERATIONS & EXCELLENCE",
    "ANALYTICS & DATA",
    "PRODUCT MANAGEMENT",
    "HUMAN RESOURCES & PEOPLE",
    "ENTREPRENEURSHIP",
}

# ---------------------------------------------------------------------------
# Confidence helpers
# ---------------------------------------------------------------------------

def _normalize_confidence(value: str) -> str:
    v = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    v = " ".join(v.split())
    if v == "very high":
        return "very_high"
    if v in {"high", "medium", "low", "unconfirmed"}:
        return v
    return "unconfirmed"


def confidence_rank(value: str) -> int:
    return {
        "very_high": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
        "unconfirmed": 0,
    }.get((value or "").strip().lower(), 0)


# ---------------------------------------------------------------------------
# RECONCILE_ID helpers
# ---------------------------------------------------------------------------

def build_reconcile_id(row: dict) -> tuple[str, str] | None:
    """Build (RECONCILE_ID, ID_TYPE) from a sheet row. Returns None if year missing."""
    email = (row.get("Email") or "").strip()
    email_source = (row.get("Email_Source") or "").strip()
    year = (row.get("Graduation_Year") or "").strip()
    name = (row.get("Name") or "").strip()

    if not year or not re.match(r"^\d{4}$", year):
        return None

    if email:
        if email_source in VERIFIED_EMAIL_SOURCES:
            return f"{email}|YEAR:{year}", "verified_email"
        else:
            return f"{email}|YEAR:{year}|GUESSED", "guessed_email"
    else:
        return f"NAME:{name}|YEAR:{year}", "name_year"


def parse_reconcile_id(rid: str) -> dict:
    """Parse a RECONCILE_ID string into components."""
    rid = rid.strip()
    year = None
    is_guessed = False

    year_match = re.search(r"\|YEAR:(\d{4})", rid)
    if year_match:
        year = year_match.group(1)
        rid_clean = rid[: year_match.start()] + rid[year_match.end() :]
    else:
        rid_clean = rid

    if "|GUESSED" in rid_clean:
        is_guessed = True
        rid_clean = rid_clean.replace("|GUESSED", "")

    if rid_clean.startswith("NAME:"):
        name = rid_clean[5:]
        return {"type": "name_year", "name": name, "year": year, "is_guessed": False}
    else:
        email = rid_clean
        return {"type": "email", "email": email, "year": year, "is_guessed": is_guessed}


# ---------------------------------------------------------------------------
# Name similarity
# ---------------------------------------------------------------------------

def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content to a temp file then replace atomically."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ManualWorkflowLogger:
    def __init__(self, prefix: str):
        self.log_path: Path | None = None
        self._disabled = False
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            self.log_path = _LOGS_DIR / f"{prefix}_{timestamp}.log"
        except Exception as exc:
            self._disabled = True
            print(f"[Log] WARNING: Could not create logs directory: {exc}")

    def log(self, level: str, operation: str, message: str) -> None:
        if self._disabled or not self.log_path:
            return
        line = f"[{datetime.now().isoformat(timespec='seconds')}] [{level}] [{operation}] {message}\n"
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception as exc:
            self._disabled = True
            print(f"[Log] WARNING: Could not write log file: {exc}")


def _reconcile_tier_score(reconcile_id: str) -> int:
    if reconcile_id.startswith("NAME:"):
        return 1
    if reconcile_id.endswith("|GUESSED"):
        return 2
    return 3


def _visible_row_number(row_index: int) -> int:
    return row_index + 2


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_INGEST_RUN_COMPLETE_RE = re.compile(
    r"^---\nINGEST_RUN_COMPLETE:\s*.+\nProcessed:\s*.+\n---\s*\n?",
    re.MULTILINE,
)
_EMAIL_BATCH_HEADER_RE = re.compile(r"^##\s*BATCH-(\d+)\s*\|\s*(\d+) names\s*\|\s*(cohort_[^\n]+)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# research_queue.md helpers
# ---------------------------------------------------------------------------

_BATCH_HEADER_RE = re.compile(r"━━━\s*BATCH-(\d+)\s*\|")
_BATCH_BLOCK_RE = re.compile(
    r"(━━━\s*BATCH-\d+\s*\|[^\n]*━━━\n)(.*?\n)(━━━\s*END\s+BATCH-\d+\s*━━━)",
    re.DOTALL,
)
_ID_LINE_RE = re.compile(r"\[ID:([^\]]+)\]")
_SIGNATURE_START_RE = re.compile(r"(?i)(best regards,?|best,|warm regards,?|regards,?)")


def _read_file_safe(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _existing_reconcile_ids(text: str) -> set[str]:
    """Extract all RECONCILE_IDs already present in research_queue.md."""
    return set(_ID_LINE_RE.findall(text))


def _email_queue_header() -> str:
    return (
        "# Email Queue\n"
        "# Auto-generated by: python manual_workflow/manual_workflow.py export-email\n"
        "# Do not edit manually. Paste <EMAIL> output from LLM into ingest_queue.md.\n"
    )


def _last_ingest_marker_match(text: str):
    matches = list(_INGEST_RUN_COMPLETE_RE.finditer(text))
    return matches[-1] if matches else None


def _get_rows_for_tab(tab_name: str, row_cache: dict[str, list[dict]]) -> list[dict]:
    if tab_name not in row_cache:
        row_cache[tab_name] = sheets_helper.read_all_rows(tab_name=tab_name)
    return row_cache[tab_name]


def _lookup_row_by_reconcile_id(
    reconcile_id: str,
    tab_name: str | None = None,
    row_cache: dict[str, list[dict]] | None = None,
) -> tuple[int | None, dict | None, str, str | None]:
    row_cache = row_cache if row_cache is not None else {}
    try:
        parsed = parse_reconcile_id(reconcile_id)
    except Exception as exc:
        return None, None, "PARSE_ERROR", str(exc)

    target_tab = tab_name
    if not target_tab:
        year = parsed.get("year")
        if not year:
            return None, None, "NO_YEAR", None
        target_tab = config.cohort_tab_name(year)

    try:
        rows = _get_rows_for_tab(target_tab, row_cache)
    except Exception as exc:
        return None, None, "API_ERROR", str(exc)

    row_idx, matched_row, _confidence, match_issue = _find_matching_row(parsed, rows)
    if matched_row is None:
        return None, None, match_issue or "NOT_FOUND", None
    return row_idx, matched_row, "MATCH", target_tab


def _render_research_queue(header_section: str, batches: list[dict]) -> str:
    now_iso = _timestamp_utc()
    rendered_batches = []
    total_names = 0
    for batch in batches:
        entries = batch["entries"]
        if not entries:
            continue
        total_names += len(entries)
        body_lines = list(batch["context_lines"])
        if body_lines and body_lines[-1] != "":
            body_lines.append("")
        body_lines.extend(entry["raw"] for entry in entries)
        body = "\n".join(body_lines).rstrip("\n") + "\n"
        header = re.sub(r"\|\s*\d+ names\s*━━━$", f"| {len(entries)} names ━━━", batch["header"])
        rendered_batches.append(f"{header}\n{body}{batch['end']}")

    base = header_section or "# Research Queue\n"
    if "_Last updated:" in base:
        base = re.sub(r"_Last updated:.*?_", f"_Last updated: {now_iso}_", base, count=1)
    if "_Pending batches:" in base:
        base = re.sub(
            r"_Pending batches:.*?_",
            f"_Pending batches: {len(rendered_batches)} | Total names pending: {total_names}_",
            base,
            count=1,
        )
    base = base.rstrip()
    if rendered_batches:
        return base + "\n\n" + "\n\n".join(rendered_batches) + "\n"
    return base + "\n"


def _parse_email_queue(text: str) -> tuple[str, list[dict]]:
    matches = list(_EMAIL_BATCH_HEADER_RE.finditer(text))
    header = text[: matches[0].start()] if matches else (text if text.strip() else _email_queue_header())
    batches = []

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        entries = []
        current_lines: list[str] = []
        current_rid = None
        for line in body.splitlines():
            if line.startswith("[ID:"):
                if current_lines and current_rid:
                    entries.append({"rid": current_rid, "raw": "\n".join(current_lines).strip()})
                current_rid = line[4:-1] if line.endswith("]") else line[4:]
                current_lines = [line]
            elif line.strip() == "---":
                if current_lines and current_rid:
                    entries.append({"rid": current_rid, "raw": "\n".join(current_lines).strip()})
                current_lines = []
                current_rid = None
            elif current_lines or line.strip():
                current_lines.append(line)
        if current_lines and current_rid:
            entries.append({"rid": current_rid, "raw": "\n".join(current_lines).strip()})

        batches.append(
            {
                "number": int(match.group(1)),
                "tab": match.group(3).strip(),
                "entries": entries,
            }
        )

    return header, batches


def _render_email_queue(header: str, batches: list[dict]) -> str:
    base = header.strip() if header.strip() else _email_queue_header().strip()
    rendered_batches = []
    for batch in batches:
        entries = batch["entries"]
        if not entries:
            continue
        lines = [f"## BATCH-{batch['number']:03d} | {len(entries)} names | {batch['tab']}", ""]
        for entry in entries:
            lines.append(entry["raw"].strip())
            lines.append("")
        lines.append("---")
        rendered_batches.append("\n".join(lines).rstrip())
    if rendered_batches:
        return base + "\n\n" + "\n\n".join(rendered_batches) + "\n"
    return base + "\n"


def _extract_ingest_items(text: str) -> tuple[str, list[dict]]:
    items = []
    for match in _INGEST_RUN_COMPLETE_RE.finditer(text):
        items.append({"type": "marker", "start": match.start(), "end": match.end(), "raw": match.group(0)})
    for block in _extract_all_blocks(text):
        items.append({"type": "block", "start": block["start"], "end": block["end"], "raw": block["raw"], "block": block})
    items.sort(key=lambda item: item["start"])
    prefix = text[: items[0]["start"]] if items else text
    return prefix, items


def _render_ingest_items(prefix: str, items: list[dict]) -> str:
    parts = []
    prefix_clean = prefix.strip()
    if prefix_clean:
        parts.append(prefix_clean)
    for item in items:
        parts.append(item["raw"].strip())
    return "\n\n".join(part for part in parts if part) + "\n"


def _strip_em_dashes(text: str) -> str:
    """Replace em/en dashes and multi-hyphens with ' - ' and collapse double spaces."""
    if not text:
        return text
    result = re.sub(r"\s*(?:\u2013|\u2014|-{2,})\s*", " - ", text)
    while "  " in result:
        result = result.replace("  ", " ")
    return result


def _sanitize_email_block_text(block_text: str) -> str:
    if not block_text:
        return block_text

    def _subject_repl(match: re.Match) -> str:
        return f"{match.group(1)}{_strip_em_dashes(match.group(2))}"

    def _body_repl(match: re.Match) -> str:
        return f"{match.group(1)}{_strip_em_dashes(match.group(2))}"

    updated = _SUBJECT_LINE_RE.sub(_subject_repl, block_text, count=1)
    updated = _BODY_RE.sub(_body_repl, updated, count=1)
    return updated


# Regex for detecting a collapsed single-line signature
_COLLAPSED_SIG_RE = re.compile(
    r"(?i)(best regards,?|warm regards,?|regards,?)\s+"
    r"(Aakarsh Arya)\s+"
    r"(IIM Udaipur[^\d]*?)\s*"
    r"(\d{10})\s+"
    r"(linkedin\.com\S*)",
)


# Input:  "...Best regards,\nAakarsh Arya\nIIM Udaipur, PGP\n2025 8360833126\nlinkedin.com/..."
# Output: "...Best regards,\nAakarsh Arya\nIIM Udaipur, PGP 2025\n8360833126\nlinkedin.com/..."
def _normalize_email_signature(body: str) -> str:
    if not body:
        return body
    signature_match = _SIGNATURE_START_RE.search(body)
    if not signature_match:
        return body

    before_signature = body[: signature_match.start()].rstrip()
    sign_off_block = body[signature_match.start() :]

    # Detect collapsed single-line signature and split it before normal handling
    collapsed = _COLLAPSED_SIG_RE.search(sign_off_block)
    if collapsed:
        sign_off_block = "\n".join([
            collapsed.group(1).strip(),
            collapsed.group(2).strip(),
            collapsed.group(3).strip(),
            collapsed.group(4).strip(),
            collapsed.group(5).strip(),
        ])
        print("[SIG_COLLAPSE_DETECTED] repaired collapsed signature")

    # Strip "phone number:" label and trailing whitespace on lines
    flat = re.sub(r"phone number:\s*", "", sign_off_block, flags=re.IGNORECASE)
    flat = re.sub(r"[ \t]+\n", "\n", flat)
    lines = [l.strip() for l in flat.splitlines() if l.strip()]
    if len(lines) < 2:
        return body

    # Line 1: sign-off phrase
    signoff = lines[0]
    rest = lines[1:]

    # Extract LinkedIn URL
    linkedin = ""
    for i, line in enumerate(rest):
        if re.search(r"(?i)linkedin\.com", line):
            linkedin = line
            rest = rest[:i] + rest[i + 1 :]
            break
    if not linkedin:
        return body

    # Extract phone (10 consecutive digits)
    phone = ""
    for i, line in enumerate(rest):
        m = re.search(r"\d{10}", line)
        if m:
            phone = m.group(0)
            leftover = (line[: m.start()] + line[m.end() :]).strip(" ,")
            if leftover:
                rest[i] = leftover
            else:
                rest = rest[:i] + rest[i + 1 :]
            break
    if not phone:
        return body

    # Reconstruct institution vs sender name
    institution_parts = []
    name_parts = []
    for line in rest:
        if re.search(r"(?i)IIM|PGP|\b20\d{2}\b", line):
            institution_parts.append(line)
        else:
            name_parts.append(line)

    sender_name = " ".join(name_parts).strip()
    institution = " ".join(institution_parts).strip()
    # Fix split year: "IIM Udaipur, PGP" + "2025" -> "IIM Udaipur, PGP 2025"
    institution = re.sub(r"PGP\s*,?\s*(\d{4})", r"PGP \1", institution)
    institution = re.sub(r"\s+", " ", institution).strip()

    if not sender_name or not institution:
        return body

    result = "\n".join([signoff, sender_name, institution, phone, linkedin])
    if before_signature:
        return before_signature + "\n\n" + result
    return result


_TITLE_RE = re.compile(r"^(?:Dr\.?|Prof\.?|Mr\.?|Ms\.?)\s+", re.IGNORECASE)


def _ensure_greeting(body: str, name_in_sheet: str) -> str:
    """Prepend 'Hi {first_name},' if the body doesn't already start with it."""
    if not name_in_sheet or not name_in_sheet.strip():
        return body
    first_name = _TITLE_RE.sub("", name_in_sheet.strip()).split()[0]
    if not first_name:
        return body
    if re.match(rf"(?i)^Hi\s+{re.escape(first_name)}", (body or "").lstrip()):
        return body
    return f"Hi {first_name},\n\n{body}"


def _reconcile_queue_file(
    filepath: Path,
    tab_name: str | None,
    log: ManualWorkflowLogger | None,
) -> dict[str, int | str]:
    text = _read_file_safe(filepath)
    row_cache: dict[str, list[dict]] = {}
    removed = 0
    kept = 0

    def _ingest_reconcile_decision(block_type: str | None, sheet_status: str) -> tuple[bool, str]:
        normalized_type = (block_type or "").upper()
        normalized_status = (sheet_status or "").strip()
        if normalized_type == "PROFILE":
            if normalized_status in {
                config.STATUS_RESEARCH_DONE,
                config.STATUS_EMAIL_DONE,
                config.STATUS_SENT,
                STATUS_SKIP_GUESSED_EMAIL,
            }:
                return True, f"PROFILE block — already {normalized_status}"
            return False, f"PROFILE block — {normalized_status or config.STATUS_PENDING}, research still needed"
        if normalized_type == "EMAIL":
            if normalized_status in {config.STATUS_EMAIL_DONE, config.STATUS_SENT}:
                return True, f"EMAIL block — already {normalized_status}"
            if normalized_status == config.STATUS_RESEARCH_DONE:
                return False, "EMAIL block — RESEARCH_DONE, email still needed"
            return False, f"EMAIL block — {normalized_status or config.STATUS_PENDING}, email still needed"
        return False, "Unknown block type — kept for manual review"

    def _log_keep(reconcile_id: str, reason: str):
        if log:
            log.log("INFO", "RECONCILE_KEPT", f"File: {filepath.name} | RECONCILE_ID: {reconcile_id} | reason={reason}")

    def _log_remove(reconcile_id: str, reason: str):
        if log:
            log.log("INFO", "RECONCILE_REMOVED", f"File: {filepath.name} | RECONCILE_ID: {reconcile_id} | reason={reason}")

    if filepath == _RESEARCH_QUEUE:
        first_batch = text.find("━━━ BATCH-")
        header_section = text[:first_batch] if first_batch != -1 else text
        batches = []
        for match in _BATCH_BLOCK_RE.finditer(text):
            body_lines = match.group(2).splitlines()
            entries = []
            context_lines = []
            for line in body_lines:
                id_match = _ID_LINE_RE.search(line)
                if id_match:
                    rid = id_match.group(1)
                    row_idx, row, status, extra = _lookup_row_by_reconcile_id(rid, tab_name=tab_name, row_cache=row_cache)
                    if status == "MATCH":
                        sheet_status = (row.get("STATUS") or "").strip()
                        if sheet_status in _RECONCILE_REMOVE_STATUSES:
                            removed += 1
                            _log_remove(rid, sheet_status)
                            continue
                        kept += 1
                        _log_keep(rid, sheet_status or config.STATUS_PENDING)
                    elif status == "API_ERROR":
                        kept += 1
                        _log_keep(rid, f"api_error:{extra}")
                    else:
                        kept += 1
                        _log_keep(rid, status.lower())
                    entries.append({"rid": rid, "raw": line})
                else:
                    context_lines.append(line)
            batches.append({"header": match.group(1).rstrip("\n"), "context_lines": context_lines, "entries": entries, "end": match.group(3)})
        _atomic_write(filepath, _render_research_queue(header_section, batches))
    elif filepath == _EMAIL_QUEUE:
        header, batches = _parse_email_queue(text)
        for batch in batches:
            kept_entries = []
            for entry in batch["entries"]:
                rid = entry["rid"]
                row_idx, row, status, extra = _lookup_row_by_reconcile_id(rid, tab_name=tab_name, row_cache=row_cache)
                if status == "MATCH":
                    sheet_status = (row.get("STATUS") or "").strip()
                    if sheet_status in _RECONCILE_REMOVE_STATUSES:
                        removed += 1
                        _log_remove(rid, sheet_status)
                        continue
                    kept += 1
                    _log_keep(rid, sheet_status or config.STATUS_PENDING)
                elif status == "API_ERROR":
                    kept += 1
                    _log_keep(rid, f"api_error:{extra}")
                else:
                    kept += 1
                    _log_keep(rid, status.lower())
                kept_entries.append(entry)
            batch["entries"] = kept_entries
        _atomic_write(filepath, _render_email_queue(header, batches))
    elif filepath == _INGEST_QUEUE:
        prefix, items = _extract_ingest_items(text)
        kept_items = []
        for item in items:
            if item["type"] == "marker":
                kept_items.append(item)
                continue
            block = item["block"]
            rid = block.get("reconcile_id")
            if not rid:
                kept += 1
                _log_keep("missing", "Unknown block type — kept for manual review")
                kept_items.append(item)
                continue
            row_idx, row, status, extra = _lookup_row_by_reconcile_id(rid, tab_name=tab_name, row_cache=row_cache)
            if status == "MATCH":
                sheet_status = (row.get("STATUS") or "").strip()
                should_remove, reason = _ingest_reconcile_decision(block.get("type"), sheet_status)
                if should_remove:
                    removed += 1
                    _log_remove(rid, reason)
                    continue
                kept += 1
                _log_keep(rid, reason)
            elif status == "API_ERROR":
                kept += 1
                _log_keep(rid, f"api_error:{extra}")
            else:
                kept += 1
                _log_keep(rid, status.lower())
            kept_items.append(item)
        _atomic_write(filepath, _render_ingest_items(prefix, kept_items))

    summary = f"[RECONCILE_DONE] File: {filepath} | Removed: {removed} | Kept: {kept}"
    if log:
        log.log("INFO", "RECONCILE_DONE", f"File: {filepath} | Removed: {removed} | Kept: {kept}")
    return {"removed": removed, "kept": kept, "summary": summary}


def _highest_batch_number(text: str) -> int:
    matches = _BATCH_HEADER_RE.findall(text)
    if not matches:
        return 0
    return max(int(m) for m in matches)


def _build_display_line(row: dict, reconcile_id: str) -> str:
    name = (row.get("Name") or "").strip()
    company = (row.get("AlmaConnect_Company") or "").strip() or "Unknown"
    location = (row.get("last_known_location", "") or row.get("Location", "") or "").strip()
    linkedin = (row.get("LinkedIn_URL") or "").strip()

    parts = [f"[ID:{reconcile_id}] {name} | AlmaConnect: {company}"]
    if location:
        parts.append(f"Location: {location}")
    if linkedin:
        parts.append(f"LinkedIn: {linkedin}")
    return " | ".join(parts)


def _apply_row_filters(
    rows: list[dict],
    *,
    domain_filter: str = "",
    name_filter: list[str] | None = None,
    column_filters: dict[str, list[str]] | None = None,
    status_filter: str | None = None,
) -> list[dict]:
    """
    AND logic across all active filters.
    Within column_filters, each column's values are OR logic.
    domain_filter is a shorthand for PRIMARY_DOMAIN contains match.
    name_filter is fuzzy match at threshold 0.8.
    status_filter is exact STATUS match.
    """
    result = rows

    if status_filter:
        result = [r for r in result if (r.get("STATUS") or "").strip() == status_filter]

    if domain_filter:
        domain_value = domain_filter.strip().lower()
        result = [
            r for r in result if domain_value in (r.get("PRIMARY_DOMAIN") or "").strip().lower()
        ]

    if name_filter:
        result = [
            r
            for r in result
            if any(_name_similarity((r.get("Name") or "").strip(), name) >= 0.8 for name in name_filter)
        ]

    if column_filters:
        for column, values in column_filters.items():
            values_lower = [value.strip().lower() for value in values]
            result = [
                r
                for r in result
                if (r.get(column) or "").strip().lower() in values_lower
            ]

    return result


def _parse_column_filters(filter_args: list[str] | None) -> dict[str, list[str]]:
    """
    Parses --filter COL=VALUE1,VALUE2 into {COL: [VALUE1, VALUE2]}.
    Multiple --filter flags are AND logic across columns.
    """
    if not filter_args:
        return {}
    result: dict[str, list[str]] = {}
    for item in filter_args:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"--filter must be COL=VALUE, got: {item}")
        column, raw_values = item.split("=", 1)
        column = column.strip()
        values = [value.strip() for value in raw_values.split(",") if value.strip()]
        if not column or not values:
            raise argparse.ArgumentTypeError(f"--filter must be COL=VALUE, got: {item}")
        result.setdefault(column, []).extend(values)
    return result


def _format_research_batches(
    entries: list[tuple[str, str, str]], batch_size: int, start_batch_num: int = 1
) -> list[str]:
    if not entries:
        return []

    batches: list[tuple[str, list[tuple[str, str, str]]]] = []
    current_chunk: list[tuple[str, str, str]] = []
    current_tab = entries[0][0]

    for entry in entries:
        tab, rid, display = entry
        if tab != current_tab:
            for i in range(0, len(current_chunk), batch_size):
                batches.append((current_tab, current_chunk[i : i + batch_size]))
            current_chunk = []
            current_tab = tab
        current_chunk.append((tab, rid, display))

    if current_chunk:
        for i in range(0, len(current_chunk), batch_size):
            batches.append((current_tab, current_chunk[i : i + batch_size]))

    new_blocks = []
    for idx, (tab, batch_entries) in enumerate(batches):
        batch_num = start_batch_num + idx
        batch_label = f"BATCH-{batch_num:03d}"
        year = tab.replace("cohort_", "") if "cohort_" in tab else tab
        header = f"━━━ {batch_label} | {tab} | {len(batch_entries)} names ━━━"
        context = f"BATCH CONTEXT: IIM Udaipur alumni, PGP {year} passouts"
        lines = [header, context, ""]
        for _, rid, display in batch_entries:
            lines.append(display)
        lines.append(f"━━━ END {batch_label} ━━━")
        new_blocks.append("\n".join(lines))

    return new_blocks


# ---------------------------------------------------------------------------
# EXPORT subcommand
# ---------------------------------------------------------------------------

def cmd_export(args):
    logger = ManualWorkflowLogger("export")
    batch_size = max(1, min(50, args.batch_size))
    try:
        column_filters = _parse_column_filters(args.col_filters)
    except argparse.ArgumentTypeError as exc:
        print(f"[Export] ERROR: {exc}")
        return

    reconcile_summary = _reconcile_queue_file(_RESEARCH_QUEUE, None, logger)
    print(reconcile_summary["summary"])

    names_filter = None
    if args.names:
        names_filter = [n.strip() for n in args.names.split(",") if n.strip()]
    if names_filter and not column_filters:
        batch_size = 1

    # Determine which tabs to export
    if args.all_pending:
        manifest_path = Path(config.COHORTS_MANIFEST_FILE)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tabs = [c["tab_name"] for c in manifest.get("cohorts", [])]
        else:
            print("[Export] ERROR: manifest.json not found and --all-pending specified.")
            return
    elif args.tab:
        tabs = args.tab
    else:
        print("[Export] ERROR: Specify --tab TAB or --all-pending.")
        return

    # Read existing queue to avoid duplicates and get batch numbering
    existing_text = _read_file_safe(_RESEARCH_QUEUE)
    existing_ids = _existing_reconcile_ids(existing_text)
    next_batch_num = _highest_batch_number(existing_text) + 1

    all_new_entries: list[tuple[str, str, str]] = []  # (tab_name, reconcile_id, display_line)

    for tab in tabs:
        try:
            rows = sheets_helper.read_all_rows(tab_name=tab)
        except Exception as e:
            print(f"[Export] ERROR reading tab '{tab}': {e}")
            continue

        pending = _apply_row_filters(
            rows,
            domain_filter=args.domain or "",
            name_filter=names_filter,
            column_filters=column_filters,
            status_filter=config.STATUS_PENDING,
        )
        print(f"[Export] Tab: {tab} | PENDING rows found: {len(pending)}")

        already_in_queue = 0
        newly_added = 0

        for row in pending:
            result = build_reconcile_id(row)
            if result is None:
                # NOTE: Rows skipped here require a manual fix directly in the Google Sheet.
                # Add the correct Graduation_Year value in the sheet, then re-run export.
                # Known affected rows as of 2026-03-15: Dr. Madhupa Bakshi (cohort_2013, Row 27)
                print(f"[Export] SKIPPED (no Graduation_Year): {row.get('Name', 'unknown')}")
                continue
            rid, _ = result
            if rid in existing_ids:
                already_in_queue += 1
                continue
            display = _build_display_line(row, rid)
            all_new_entries.append((tab, rid, display))
            existing_ids.add(rid)
            newly_added += 1

        print(f"[Export] Already in queue: {already_in_queue} | Newly added: {newly_added}")

    if not all_new_entries:
        print("[Export] No new entries to add.")
        return

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_blocks = _format_research_batches(all_new_entries, batch_size, next_batch_num)
    total_new_names = len(all_new_entries)

    # Build or update the file
    if not existing_text.strip():
        # Create fresh file
        header_section = (
            "# Research Queue\n"
            f"_Last updated: {now_iso}_\n"
            f"_Pending batches: {len(new_blocks)} | Total names pending: {total_new_names}_\n"
            "\n---\n\n"
            "## HOW TO USE\n"
            "1. Copy one full BATCH block (between the ━━━ markers, inclusive)\n"
            "2. Open system_prompt_research.md and paste its contents as your first message to Kimi/Qwen\n"
            "3. Then paste the batch block as your next message\n"
            "4. Copy the full output from Kimi/Qwen\n"
            "5. Open ingest_queue.md and paste the output after the last --- separator\n"
            "6. Run: python manual_workflow/manual_workflow.py ingest\n"
            "\n---\n\n"
        )
        content = header_section + "\n\n".join(new_blocks) + "\n"
    else:
        # Update header counters
        existing_batches_count = len(_BATCH_HEADER_RE.findall(existing_text))
        # Count existing pending names
        existing_id_lines = _ID_LINE_RE.findall(existing_text)
        total_pending = len(existing_id_lines) + total_new_names
        total_batches = existing_batches_count + len(new_blocks)

        # Update the header line
        updated = re.sub(
            r"_Last updated:.*?_",
            f"_Last updated: {now_iso}_",
            existing_text,
            count=1,
        )
        updated = re.sub(
            r"_Pending batches:.*?_",
            f"_Pending batches: {total_batches} | Total names pending: {total_pending}_",
            updated,
            count=1,
        )

        # Append new blocks at the end
        content = updated.rstrip() + "\n\n" + "\n\n".join(new_blocks) + "\n"

    _atomic_write(_RESEARCH_QUEUE, content)
    print(f"[Export] Batches written: {len(new_blocks)} (batch size: {batch_size})")
    print("[Export] research_queue.md updated.")


def cmd_query(args):
    query_column_filters: dict[str, list[str]] = {}
    if args.status:
        query_column_filters["STATUS"] = [value.strip() for value in args.status.split(",") if value.strip()]
    if args.email_source:
        query_column_filters["Email_Source"] = [value.strip() for value in args.email_source.split(",") if value.strip()]
    if args.confidence:
        query_column_filters["Confidence_Level"] = [value.strip() for value in args.confidence.split(",") if value.strip()]
    if args.gender:
        query_column_filters["GENDER"] = [value.strip() for value in args.gender.split(",") if value.strip()]
    if args.batch_year:
        query_column_filters["Graduation_Year"] = [value.strip() for value in args.batch_year.split(",") if value.strip()]
    name_filter = [value.strip() for value in (args.name or "").split(",") if value.strip()]

    if args.all_tabs:
        manifest_path = Path(config.COHORTS_MANIFEST_FILE)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tabs = [cohort["tab_name"] for cohort in manifest.get("cohorts", [])]
        else:
            tabs = [config.cohort_tab_name(year) for year in config.COHORT_YEARS]
    elif args.tab:
        tabs = [args.tab]
    else:
        tabs = [config.SHEET_NAME]

    matches: list[tuple[str, int, dict]] = []
    available_headers = set(config.SHEET_HEADERS) | {"STATUS"}

    for tab in tabs:
        try:
            rows = sheets_helper.read_all_rows(tab_name=tab)
        except Exception as exc:
            print(f"[Query] ERROR reading tab '{tab}': {exc}")
            continue
        rows_with_meta = []
        for row_index, row in enumerate(rows):
            available_headers.update(row.keys())
            rows_with_meta.append({**row, "__row_index": row_index})

        filtered_rows = _apply_row_filters(
            rows_with_meta,
            domain_filter=args.domain or "",
            name_filter=name_filter,
            column_filters=query_column_filters,
        )
        for row in filtered_rows:
            row_index = int(row["__row_index"])
            matches.append((tab, row_index, row))

    if not matches:
        print("[Query] No matching rows found.")
        return

    output_mode = args.output or "table"
    if output_mode == "table":
        print("Row\tName\tStatus\tEmail_Source\tConfidence\tEmail")
        for tab, row_index, row in matches:
            print(
                "\t".join(
                    [
                        str(_visible_row_number(row_index)),
                        (row.get("Name") or "").strip(),
                        (row.get("STATUS") or "").strip(),
                        (row.get("Email_Source") or "").strip(),
                        (row.get("Confidence_Level") or "").strip(),
                        (row.get("Email") or "").strip(),
                    ]
                )
            )
        return

    if output_mode == "fields":
        requested_fields = [(value or "").strip() for value in (args.fields or "").split(",") if value.strip()]
        if not requested_fields:
            print("[Query] ERROR: --output fields requires --fields COL1,COL2,...")
            return
        header_map = {header.lower(): header for header in available_headers}
        resolved_fields = []
        missing_fields = []
        for field in requested_fields:
            resolved = header_map.get(field.lower())
            if resolved:
                resolved_fields.append(resolved)
            else:
                missing_fields.append(field)
        if missing_fields:
            print(f"[Query] ERROR: Unknown columns: {', '.join(missing_fields)}")
            return
        for _, _, row in matches:
            print("\t".join((row.get(field) or "").strip() for field in resolved_fields))
        return

    if output_mode == "research":
        entries = []
        for tab, _, row in matches:
            if not (row.get("Email") or "").strip():
                continue
            result = build_reconcile_id(row)
            if result is None:
                continue
            rid, _ = result
            entries.append((tab, rid, _build_display_line(row, rid)))
        if not entries:
            print("[Query] No email-bearing rows matched for research output.")
            return
        blocks = _format_research_batches(entries, max(1, args.batch_size or 3), 1)
        print("\n\n".join(blocks))
        return

    if output_mode == "reconcile":
        for tab, _, row in matches:
            name = (row.get("Name") or "").replace('"', '\\"').strip()
            print(f'python manual_workflow/manual_workflow.py reconcile-email --tab {tab} --name "{name}"')
        return

    print(f"[Query] ERROR: Unknown output mode '{output_mode}'.")


def cmd_reset_email(args):
    try:
        column_filters = _parse_column_filters(args.col_filters)
    except argparse.ArgumentTypeError as exc:
        print(f"[ResetEmail] ERROR: {exc}")
        return

    name_filter = [value.strip() for value in (args.name or "").split(",") if value.strip()]

    try:
        rows = sheets_helper.read_all_rows(tab_name=args.tab)
    except Exception as exc:
        print(f"[ResetEmail] ERROR reading tab '{args.tab}': {exc}")
        return

    rows_with_meta = [{**row, "__row_index": row_index} for row_index, row in enumerate(rows)]
    matched_rows = _apply_row_filters(
        rows_with_meta,
        domain_filter=args.domain or "",
        name_filter=name_filter or None,
        column_filters=column_filters,
    )

    sent_count = sum(1 for row in matched_rows if (row.get("STATUS") or "").strip() == config.STATUS_SENT)
    candidates = [row for row in matched_rows if (row.get("STATUS") or "").strip() == config.STATUS_EMAIL_DONE]

    if sent_count > 0:
        print(f"[ResetEmail] WARNING: {sent_count} SENT rows skipped — permanently immutable.")

    if not candidates:
        print(f"[ResetEmail] No EMAIL_DONE rows matched in {args.tab}.")
        return

    row_numbers = [str(_visible_row_number(int(row["__row_index"]))) for row in candidates]
    print(f"[ResetEmail] {'DRY RUN — ' if not args.write else ''}Rows to reset: {len(candidates)}")
    print(f"[ResetEmail] Sheet rows: {', '.join(row_numbers)}")

    if not args.write:
        print("[ResetEmail] Dry run complete. Pass --write to apply changes.")
        return

    for row in candidates:
        assert (row.get("STATUS") or "").strip() != config.STATUS_SENT, (
            f"FATAL: attempted to overwrite SENT row at sheet row {_visible_row_number(int(row['__row_index']))} — aborting before any write."
        )

    updates = [
        (
            int(row["__row_index"]),
            {"STATUS": config.STATUS_RESEARCH_DONE, "Subject": "", "Body": ""},
            args.tab,
        )
        for row in candidates
    ]
    sheets_helper.batch_write_rows(updates)
    print(f"[ResetEmail] {len(candidates)} rows reset to RESEARCH_DONE.")


def cmd_reset_research(args):
    try:
        column_filters = _parse_column_filters(args.col_filters)
    except argparse.ArgumentTypeError as exc:
        print(f"[ResetResearch] ERROR: {exc}")
        return

    name_filter = [value.strip() for value in (args.name or "").split(",") if value.strip()]

    try:
        rows = sheets_helper.read_all_rows(tab_name=args.tab)
    except Exception as exc:
        print(f"[ResetResearch] ERROR reading tab '{args.tab}': {exc}")
        return

    rows_with_meta = [{**row, "__row_index": row_index} for row_index, row in enumerate(rows)]
    matched_rows = _apply_row_filters(
        rows_with_meta,
        domain_filter=args.domain or "",
        name_filter=name_filter or None,
        column_filters=column_filters,
    )

    sent_count = sum(1 for row in matched_rows if (row.get("STATUS") or "").strip() == config.STATUS_SENT)
    candidates = [row for row in matched_rows if (row.get("STATUS") or "").strip() == config.STATUS_RESEARCH_DONE]

    if sent_count > 0:
        print(f"[ResetResearch] WARNING: {sent_count} SENT rows skipped — permanently immutable.")

    if not candidates:
        print(f"[ResetResearch] No RESEARCH_DONE rows matched in {args.tab}.")
        return

    row_numbers = [str(_visible_row_number(int(row["__row_index"]))) for row in candidates]
    print(f"[ResetResearch] {'DRY RUN — ' if not args.write else ''}Rows to reset: {len(candidates)}")
    print(f"[ResetResearch] Sheet rows: {', '.join(row_numbers)}")

    if not args.write:
        print("[ResetResearch] Dry run complete. Pass --write to apply changes.")
        return

    for row in candidates:
        assert (row.get("STATUS") or "").strip() != config.STATUS_SENT, (
            f"FATAL: attempted to overwrite SENT row at sheet row {_visible_row_number(int(row['__row_index']))} — aborting before any write."
        )

    # Downstream confidence upgrade behavior on re-ingest is intentional and already handled in cmd_ingest.
    updates = [
        (
            int(row["__row_index"]),
            {
                "STATUS": config.STATUS_PENDING,
                "Verified_Company": "",
                "Enrichment_Notes": "",
                "Enrichment_Source": "",
                "Confidence_Level": "",
                "LinkedIn_URL": "",
                "GENDER": "",
                "PRIMARY_DOMAIN": "",
                "CONTEXT": "",
            },
            args.tab,
        )
        for row in candidates
    ]
    sheets_helper.batch_write_rows(updates)
    print(f"[ResetResearch] {len(candidates)} rows reset to PENDING.")


def cmd_reconcile_email(args):
    tab = args.tab
    requested_names = [value.strip() for value in args.name.split(",") if value.strip()]
    if not requested_names:
        print("[reconcile-email] ERROR: Provide at least one name.")
        return
    allowed_statuses = {
        config.STATUS_PENDING,
        config.STATUS_RESEARCH_DONE,
        STATUS_SKIP_GUESSED_EMAIL,
    }
    if args.status not in allowed_statuses:
        valid = ", ".join(sorted(allowed_statuses))
        print(f"[reconcile-email] ERROR: Unknown status '{args.status}'. Valid values: {valid}")
        return

    try:
        rows = sheets_helper.read_all_rows(tab_name=tab)
    except Exception as exc:
        print(f"[reconcile-email] ERROR reading tab '{tab}': {exc}")
        return

    def _normalized_name_key(value: str) -> str:
        return " ".join((value or "").casefold().split())

    row_entries = []
    rows_by_name: dict[str, list[tuple[int, dict]]] = {}
    for row_index, row in enumerate(rows):
        row_name = (row.get("Name") or "").strip()
        if not row_name:
            continue
        row_entries.append((row_index, row, row_name))
        rows_by_name.setdefault(_normalized_name_key(row_name), []).append((row_index, row))

    matched_rows: dict[int, dict] = {}
    for candidate in requested_names:
        exact_matches = rows_by_name.get(_normalized_name_key(candidate), [])
        if exact_matches:
            for row_index, row in exact_matches:
                matched_rows[row_index] = row
            continue

        best_match: tuple[int, dict] | None = None
        best_score = 0.0
        for row_index, row, row_name in row_entries:
            score = _name_similarity(row_name, candidate)
            if score > best_score:
                best_score = score
                best_match = (row_index, row)
        if best_match and best_score >= 0.7:
            matched_rows[best_match[0]] = best_match[1]

    if not matched_rows:
        print("[reconcile-email] No matching rows found.")
        return

    print(f"[reconcile-email] Matches found in {tab}:")
    for row_index in sorted(matched_rows):
        row = matched_rows[row_index]
        print(
            f"  Row {_visible_row_number(row_index)}: {(row.get('Name') or '').strip()} | "
            f"{(row.get('Email') or '').strip()} | "
            f"{(row.get('Email_Source') or '').strip()} -> {config.EMAIL_SOURCE_MANUAL_VERIFIED}"
        )

    updates = [
        (
            row_index,
            {"Email_Source": config.EMAIL_SOURCE_MANUAL_VERIFIED, "STATUS": args.status},
            tab,
        )
        for row_index in sorted(matched_rows)
    ]
    sheets_helper.batch_write_rows(updates)

    header = (
        "# Manual Email Verification Overrides\n"
        "# Format: timestamp | tab | name | email | new_source\n"
    )
    existing = _read_file_safe(_MANUAL_OVERRIDES)
    if not existing.strip():
        existing = header
    if not existing.endswith("\n"):
        existing += "\n"
    timestamp = _timestamp_utc()
    new_lines = []
    for row_index in sorted(matched_rows):
        row = matched_rows[row_index]
        new_lines.append(
            f"{timestamp} | {tab} | {(row.get('Name') or '').strip()} | {(row.get('Email') or '').strip()} | {config.EMAIL_SOURCE_MANUAL_VERIFIED}"
        )
    _atomic_write(_MANUAL_OVERRIDES, existing + "\n".join(new_lines) + "\n")
    print(f"[reconcile-email] Updated {len(matched_rows)} rows and logged overrides to {_MANUAL_OVERRIDES.name}.")


# ---------------------------------------------------------------------------
# INGEST subcommand
# ---------------------------------------------------------------------------

_PROFILE_BLOCK_RE = re.compile(r"<PROFILE>(.*?)</PROFILE>", re.DOTALL)
_EMAIL_BLOCK_RE = re.compile(r"<EMAIL>(.*?)</EMAIL>", re.DOTALL)
_TAGGED_BLOCK_RE = re.compile(
    r"(?ms)^\s*\[ID:(?P<id>[^\]]+)\]\s*\r?\n(?P<block><(?P<tag>PROFILE|EMAIL)>.*?</(?P=tag)>)"
)
_RAW_BLOCK_OPEN_RE = re.compile(r"<(?P<tag>PROFILE|EMAIL)>")
_RECONCILE_LINE_RE = re.compile(r"^\s*RECONCILE_ID:\s*(.+)$", re.MULTILINE)
_NAME_IN_SHEET_RE = re.compile(r"^\s*NAME_IN_SHEET:\s*(.+)$", re.MULTILINE)
_SUBJECT_LINE_RE = re.compile(r"^(\s*SUBJECT:\s*)(.+)$", re.MULTILINE)
_BODY_RE = re.compile(r"^(\s*BODY:\s*)(.+)$", re.MULTILINE | re.DOTALL)
_REVIEW_ENTRY_RE = re.compile(
    r"(━━━\s*(REVIEW-\d+)\s*\|.*?\n.*?━━━\s*END\s+\2\s*━━━\s*)",
    re.DOTALL,
)
_REVIEW_REASON_RE = re.compile(r"^\[REVIEW REASON:\s*(.+?)\]\s*$", re.MULTILINE)
_REVIEW_RID_RE = re.compile(r"^\[RECONCILE_ID:\s*(.+?)\]\s*$", re.MULTILINE)
_PROFILE_NAME_RE = re.compile(r"^\s*NAME:\s*(.+)$", re.MULTILINE)


def _parse_profile_block_manual(block_text: str) -> dict:
    """Parse a <PROFILE> block into key-value dict (mirrors _extract_profile_block)."""
    result = {}
    for line in block_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip().upper()] = value.strip()
    return result


def _scan_raw_blocks(text: str) -> list[dict]:
    blocks = []
    cursor = 0
    while True:
        match = _RAW_BLOCK_OPEN_RE.search(text, cursor)
        if not match:
            break
        block_type = match.group("tag")
        close_tag = f"</{block_type}>"
        close_index = text.find(close_tag, match.end())
        if close_index == -1:
            end = len(text)
            malformed = True
        else:
            end = close_index + len(close_tag)
            malformed = False
        blocks.append(
            {
                "type": block_type,
                "start": match.start(),
                "end": end,
                "markup": text[match.start() : end],
                "malformed": malformed,
            }
        )
        cursor = end if end > match.start() else match.end()
    return blocks


def _span_overlaps(existing_spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < existing_end and end > existing_start for existing_start, existing_end in existing_spans)


def _build_extracted_block(
    block_type: str,
    block_markup: str,
    raw_text: str,
    start: int,
    end: int,
    *,
    tagged_reconcile_id: str | None = None,
    parse_mode: str,
    malformed: bool = False,
) -> dict:
    block_match = (_PROFILE_BLOCK_RE if block_type == "PROFILE" else _EMAIL_BLOCK_RE).search(block_markup)
    block_text = block_match.group(1) if block_match else ""
    rid_match = _RECONCILE_LINE_RE.search(block_text)
    name_match = _NAME_IN_SHEET_RE.search(block_text)

    sanitized_block_text = block_text
    if block_type == "EMAIL":
        sanitized_block_text = _sanitize_email_block_text(block_text)
        if sanitized_block_text != block_text:
            raw_text = raw_text.replace(block_text, sanitized_block_text, 1)

    block = {
        "type": block_type,
        "raw": raw_text,
        "raw_inner": sanitized_block_text,
        "start": start,
        "end": end,
        "reconcile_id": rid_match.group(1).strip() if rid_match else (tagged_reconcile_id.strip() if tagged_reconcile_id else None),
        "tagged_reconcile_id": tagged_reconcile_id.strip() if tagged_reconcile_id else None,
        "name_in_sheet": name_match.group(1).strip() if name_match else None,
        "parse_mode": parse_mode,
        "parse_issue": None,
    }

    if block_type == "PROFILE":
        block["parsed"] = _parse_profile_block_manual(block_text)
    else:
        subj_match = _SUBJECT_LINE_RE.search(sanitized_block_text)
        body_match = _BODY_RE.search(sanitized_block_text)
        block["subject"] = subj_match.group(2).strip() if subj_match else ""
        block["body"] = body_match.group(2).strip() if body_match else ""

    if malformed:
        block["parse_issue"] = "MALFORMED_BLOCK"
    elif not block["reconcile_id"]:
        block["parse_issue"] = "MISSING_RECONCILE_ID"

    return block


def _extract_all_blocks(text: str) -> list[dict]:
    """Extract PROFILE and EMAIL blocks from ingest text, returning list of parsed dicts."""
    blocks = []
    occupied_spans: list[tuple[int, int]] = []

    for match in _TAGGED_BLOCK_RE.finditer(text):
        block_type = match.group("tag")
        blocks.append(
            _build_extracted_block(
                block_type,
                match.group("block"),
                match.group(0),
                match.start(),
                match.end(),
                tagged_reconcile_id=match.group("id"),
                parse_mode="tagged",
            )
        )
        occupied_spans.append((match.start(), match.end()))

    # Fallback parser: recover raw <PROFILE>/<EMAIL> blocks when the leading [ID:...] line is missing.
    for block_scan in _scan_raw_blocks(text):
        if _span_overlaps(occupied_spans, block_scan["start"], block_scan["end"]):
            continue
        blocks.append(
            _build_extracted_block(
                block_scan["type"],
                block_scan["markup"],
                block_scan["markup"],
                block_scan["start"],
                block_scan["end"],
                parse_mode="fallback",
                malformed=block_scan["malformed"],
            )
        )

    blocks.sort(key=lambda item: item["start"])
    return blocks


def _find_matching_row(
    parsed_rid: dict, rows: list[dict]
) -> tuple[int | None, dict | None, str, str]:
    """Find the matching row in the sheet. Returns (index, row, confidence, reason)."""

    if parsed_rid["type"] == "email":
        email = parsed_rid["email"].lower()
        candidates = [
            (i, row)
            for i, row in enumerate(rows)
            if (row.get("Email") or "").strip().lower() == email
        ]
        if len(candidates) == 1:
            conf = "high" if not parsed_rid["is_guessed"] else "medium"
            return candidates[0][0], candidates[0][1], conf, ""
        if len(candidates) > 1:
            return None, None, "", "DUPLICATE_EMAIL_IN_SHEET"
        # Fall through to name matching if email not found
        # Need a name — try to extract from reconcile_id email prefix
        # This fallback is limited; if no match, report NO_ROW_MATCH
        pass

    # Name + year matching
    name = parsed_rid.get("name", "")
    year = parsed_rid.get("year", "")

    # For email type that fell through, we don't have a good name to match
    if not name and parsed_rid["type"] == "email":
        return None, None, "", "NO_ROW_MATCH"

    candidates = []
    for i, row in enumerate(rows):
        row_year = (row.get("Graduation_Year") or "").strip()
        if row_year != year:
            continue
        score = _name_similarity(name, row.get("Name", ""))
        if score >= 0.75:
            candidates.append((i, row, score))

    candidates.sort(key=lambda x: x[2], reverse=True)

    if not candidates:
        return None, None, "", "NO_ROW_MATCH"

    if len(candidates) == 1:
        i, row, score = candidates[0]
        if score >= 0.85:
            return i, row, "medium", ""
        else:
            return i, row, "low", "LOW_NAME_SIMILARITY"

    # Multiple matches — best must be clearly better
    if candidates[0][2] >= 0.85 and candidates[1][2] >= 0.85:
        return None, None, "", "AMBIGUOUS_NAME_MATCH"
    i, row, score = candidates[0]
    if score >= 0.85:
        return i, row, "medium", ""
    return i, row, "low", "LOW_NAME_SIMILARITY"


def _append_to_review(
    reason: str,
    reconcile_id: str | None,
    raw_block: str,
    action_needed: str = "",
    extra_context: str = "",
    logger: ManualWorkflowLogger | None = None,
) -> None:
    """Append a failed/flagged entry to review_queue.md."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    existing = _read_file_safe(_REVIEW_QUEUE)
    raw_block = _sanitize_email_block_text(raw_block)

    # Find highest review number
    review_nums = re.findall(r"REVIEW-(\d+)", existing)
    next_num = max(int(n) for n in review_nums) + 1 if review_nums else 1

    label = f"REVIEW-{next_num:03d}"

    entry_lines = [
        f"\n━━━ {label} | {now_iso} ━━━",
        f"[REVIEW REASON: {reason}]",
    ]
    if reconcile_id:
        entry_lines.append(f"[RECONCILE_ID: {reconcile_id}]")
    if action_needed:
        entry_lines.append(f"[ACTION NEEDED: {action_needed}]")
    if extra_context:
        entry_lines.append(extra_context)
    entry_lines.append("")
    entry_lines.append(raw_block)
    entry_lines.append(f"━━━ END {label} ━━━\n")

    new_entry = "\n".join(entry_lines)

    if not existing.strip():
        content = (
            "# Review Queue\n"
            f"_Last updated: {now_iso}_\n"
            "_Items pending review: 1_\n"
            "\n---\n\n"
            "## HOW TO USE\n"
            "1. Inspect each entry below\n"
            "2. Fix the issue noted in [REVIEW REASON]\n"
            "3. Copy the corrected <PROFILE> or <EMAIL> block\n"
            "4. Paste into ingest_queue.md and run ingest again\n"
            "\n---\n"
            + new_entry
        )
    else:
        item_count = len(review_nums) + 1
        content = re.sub(
            r"_Items pending review:\s*\d+_",
            f"_Items pending review: {item_count}_",
            existing,
        )
        content = re.sub(
            r"_Last updated:.*?_",
            f"_Last updated: {now_iso}_",
            content,
            count=1,
        )
        content = content.rstrip() + "\n" + new_entry

    _atomic_write(_REVIEW_QUEUE, content)
    if logger:
        logger.log(
            "WARN",
            "REVIEW_QUEUE",
            f"Moved block to review_queue.md | reason={reason} | RECONCILE_ID: {reconcile_id or 'missing'}",
        )


def _extract_active_ingest_text(text: str, force: bool) -> tuple[str, str]:
    if force:
        return "", text

    last_match = _last_ingest_marker_match(text)
    if not last_match:
        return "", text

    return text[: last_match.end()], text[last_match.end() :]


def _profile_confidence_from_block(block: dict) -> str:
    if block["type"] != "PROFILE":
        return "unconfirmed"
    raw_confidence = block["parsed"].get("CONFIDENCE", "")
    confidence_clean = re.split(r"\s*[—\-]{1,2}\s*", raw_confidence, maxsplit=1)[0].strip()
    return _normalize_confidence(confidence_clean)


def _deduplicate_ingest_blocks(
    blocks: list[dict],
    logger: ManualWorkflowLogger,
) -> tuple[list[dict], int]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for index, block in enumerate(blocks, start=1):
        block["block_index"] = index
        block["normalized_confidence"] = _profile_confidence_from_block(block)
        block["dedup_winner"] = True
        rid = block.get("reconcile_id") or f"__missing__:{index}"
        grouped[rid].append(block)

    duplicate_groups = 0
    dedup_skipped = 0
    winners_by_identity: dict[int, bool] = {}

    for rid, group in grouped.items():
        if rid.startswith("__missing__:"):
            winners_by_identity[id(group[0])] = True
            continue
        if len(group) == 1:
            winners_by_identity[id(group[0])] = True
            continue

        duplicate_groups += 1
        winner = max(
            group,
            key=lambda item: (
                confidence_rank(item["normalized_confidence"]),
                _reconcile_tier_score(item["reconcile_id"]),
                item["block_index"],
            ),
        )
        winners_by_identity[id(winner)] = True
        logger.log(
            "INFO",
            "DEDUP_WINNER",
            "RECONCILE_ID: "
            f"{winner['reconcile_id']} | block={winner['block_index']} | confidence={winner['normalized_confidence']}",
        )

        for candidate in group:
            if candidate is winner:
                continue
            candidate["dedup_winner"] = False
            reason_parts = []
            candidate_rank = confidence_rank(candidate["normalized_confidence"])
            winner_rank = confidence_rank(winner["normalized_confidence"])
            if candidate_rank < winner_rank:
                reason_parts.append("lower confidence")
            elif _reconcile_tier_score(candidate["reconcile_id"]) < _reconcile_tier_score(winner["reconcile_id"]):
                reason_parts.append("lower tier")
            else:
                reason_parts.append("older paste order")
            logger.log(
                "WARN",
                "DEDUP_SKIPPED",
                "RECONCILE_ID: "
                f"{candidate['reconcile_id']} | block={candidate['block_index']} | reason={', '.join(reason_parts)}",
            )
            dedup_skipped += 1

    logger.log(
        "INFO",
        "DEDUP_PRECAN",
        f"{len(blocks)} blocks parsed, {len(grouped)} unique IDs, {duplicate_groups} duplicate groups found",
    )

    if duplicate_groups == 0:
        print("[DEDUP] No duplicates found in paste")
    else:
        print(
            f"[DEDUP] Parsed {len(blocks)} blocks, {len(grouped)} unique IDs, {duplicate_groups} duplicate groups"
        )

    winners = [block for block in blocks if winners_by_identity.get(id(block), False)]
    return winners, dedup_skipped


def _format_ingest_run_marker(summary: dict[str, int]) -> str:
    timestamp = _timestamp_utc()
    return (
        "---\n"
        f"INGEST_RUN_COMPLETE: {timestamp}\n"
        "Processed: "
        f"{summary['blocks_parsed']} blocks | Written: {summary['written']} | Skipped: {summary['skipped_confidence']} | "
        f"Review: {summary['review']} | Dedup_skipped: {summary['dedup_skipped']}\n"
        "---\n"
    )


def _parse_review_entries(text: str) -> list[dict]:
    entries = []
    for match in _REVIEW_ENTRY_RE.finditer(text):
        full_text = match.group(1).strip() + "\n"
        reason_match = _REVIEW_REASON_RE.search(full_text)
        rid_match = _REVIEW_RID_RE.search(full_text)
        name_match = _NAME_IN_SHEET_RE.search(full_text) or _PROFILE_NAME_RE.search(full_text)
        reconcile_id = rid_match.group(1).strip() if rid_match else ""
        display_name = name_match.group(1).strip() if name_match else ""
        if not display_name and reconcile_id.startswith("NAME:"):
            display_name = reconcile_id[5:].split("|YEAR:", 1)[0].strip()
        entries.append(
            {
                "label": match.group(2),
                "full_text": full_text,
                "reason": reason_match.group(1).strip() if reason_match else "UNKNOWN",
                "reconcile_id": reconcile_id,
                "display_name": display_name or "Unknown",
            }
        )
    return entries


def _render_review_queue(entries: list[dict]) -> str:
    now_iso = _timestamp_utc()
    header = (
        "# Review Queue\n"
        f"_Last updated: {now_iso}_\n"
        f"_Items pending review: {len(entries)}_\n"
        "\n---\n\n"
        "## HOW TO USE\n"
        "1. Inspect each entry below\n"
        "2. Fix the issue noted in [REVIEW REASON]\n"
        "3. Copy the corrected <PROFILE> or <EMAIL> block\n"
        "4. Paste into ingest_queue.md and run ingest again\n"
        "\n---\n"
    )
    if not entries:
        return header
    return header + "\n" + "\n".join(entry["full_text"].rstrip() for entry in entries) + "\n"


def _append_block_to_ingest_queue(raw_block: str) -> None:
    existing = _read_file_safe(_INGEST_QUEUE)
    last_marker = _last_ingest_marker_match(existing)
    block_text = _sanitize_email_block_text(raw_block).strip()
    # review --retry idempotency: append after the last marker so the block remains pending.
    if last_marker:
        before = existing[: last_marker.end()].rstrip()
        after = existing[last_marker.end() :].strip()
        parts = [before, block_text]
        if after:
            parts.append(after)
        content = "\n\n".join(part for part in parts if part) + "\n"
    else:
        existing_clean = existing.rstrip()
        content = (existing_clean + "\n\n" + block_text + "\n") if existing_clean else (block_text + "\n")
    _atomic_write(_INGEST_QUEUE, content)


def cmd_ingest(args):
    logger = ManualWorkflowLogger("ingest")
    logger.log("INFO", "INGEST_START", "cmd_ingest started")

    reconcile_summary = _reconcile_queue_file(_INGEST_QUEUE, None, logger)
    print(reconcile_summary["summary"])

    ingest_text = _read_file_safe(_INGEST_QUEUE)
    _historical_prefix, active_ingest_text = _extract_active_ingest_text(ingest_text, args.force)
    if not active_ingest_text.strip() or "<PROFILE>" not in active_ingest_text and "<EMAIL>" not in active_ingest_text:
        print("[Ingest] Nothing to ingest — ingest_queue.md is empty or has no blocks.")
        return

    parsed_blocks = _extract_all_blocks(active_ingest_text)
    if not parsed_blocks:
        print("[Ingest] No <PROFILE> or <EMAIL> blocks found in ingest_queue.md.")
        return
    ingest_offset = len(_historical_prefix)
    for block in parsed_blocks:
        block["start"] += ingest_offset
        block["end"] += ingest_offset

    fallback_parsed_count = sum(1 for block in parsed_blocks if block.get("parse_mode") == "fallback")
    if fallback_parsed_count:
        print(
            f"[Ingest] Fallback parser recovered {fallback_parsed_count} untagged blocks from raw <PROFILE>/<EMAIL> tags."
        )

    parsed_block_count = len(parsed_blocks)
    blocks, dedup_skipped_count = _deduplicate_ingest_blocks(parsed_blocks, logger)

    profile_count = sum(1 for b in blocks if b["type"] == "PROFILE")
    email_count = sum(1 for b in blocks if b["type"] == "EMAIL")
    print(f"[Ingest] Blocks found: {len(blocks)} ({profile_count} PROFILE, {email_count} EMAIL)")

    tab_cache: dict[str, list[dict]] = {}
    skipped_confidence_count = 0
    review_count = 0
    guessed_skip_count = 0
    idempotent_skip_count = 0
    review_reasons: dict[str, int] = {}
    processed_ids: set[str] = set()
    cleanup_remove_spans: set[tuple[int, int]] = set()
    pending_writes: list[tuple[int, dict, str | None]] = []
    pending_write_records: list[dict] = []
    pending_review_entries: list[tuple[str, dict, str, str]] = []

    for block in parsed_blocks:
        if not block.get("dedup_winner", True):
            cleanup_remove_spans.add((block["start"], block["end"]))

    def _get_rows(tab_name: str) -> list[dict]:
        if tab_name not in tab_cache:
            tab_cache[tab_name] = sheets_helper.read_all_rows(tab_name=tab_name)
        return tab_cache[tab_name]

    def _send_to_review(reason: str, block: dict, action: str = "", extra: str = ""):
        nonlocal review_count
        review_count += 1
        review_reasons[reason] = review_reasons.get(reason, 0) + 1
        pending_review_entries.append((reason, block, action, extra))

    for block in blocks:
        parse_issue = block.get("parse_issue")
        if parse_issue == "MALFORMED_BLOCK":
            _send_to_review(
                "MALFORMED_BLOCK",
                block,
                action="Fix the malformed block markup so it has a closing tag, then re-ingest.",
            )
            continue

        rid = block.get("reconcile_id")
        if not rid:
            _send_to_review(
                "MISSING_RECONCILE_ID",
                block,
                action="Add a RECONCILE_ID line inside this block or restore the leading [ID:...] tag, then re-ingest.",
            )
            continue

        if rid in processed_ids:
            idempotent_skip_count += 1
            cleanup_remove_spans.add((block["start"], block["end"]))
            logger.log("WARN", "IDEMPOTENT_SKIP", f"Already processed this RECONCILE_ID in this run: {rid}")
            print(f"[Ingest] IDEMPOTENT_SKIP: {rid} — already processed in this run.")
            continue

        parsed = parse_reconcile_id(rid)
        year = parsed.get("year")
        if not year:
            _send_to_review(
                "MISSING_YEAR",
                block,
                action="RECONCILE_ID has no YEAR component. Fix and re-ingest.",
            )
            continue

        tab_name = config.cohort_tab_name(year)

        try:
            rows = _get_rows(tab_name)
        except Exception as exc:
            _send_to_review(
                "TAB_READ_ERROR",
                block,
                action=f"Could not read tab '{tab_name}': {exc}",
            )
            continue

        row_idx, matched_row, _reconcile_confidence, match_issue = _find_matching_row(parsed, rows)

        if match_issue == "DUPLICATE_EMAIL_IN_SHEET":
            _send_to_review(
                "DUPLICATE_EMAIL_IN_SHEET",
                block,
                action="Multiple rows have the same email. Resolve manually.",
            )
            continue
        if match_issue == "AMBIGUOUS_NAME_MATCH":
            _send_to_review(
                "AMBIGUOUS_NAME_MATCH",
                block,
                action="Multiple close name matches found. Specify manually.",
            )
            continue
        if row_idx is None:
            _send_to_review(
                "NO_ROW_MATCH",
                block,
                action="No matching row found in the sheet. Verify RECONCILE_ID.",
            )
            continue

        name_in_sheet_from_block = block.get("name_in_sheet")
        sheet_name = (matched_row.get("Name") or "").strip()
        row_email_source = (matched_row.get("Email_Source") or "").strip()
        normalized_email_source = row_email_source.lower()
        if name_in_sheet_from_block:
            sim = _name_similarity(name_in_sheet_from_block, sheet_name)
            if sim < 0.70:
                _send_to_review(
                    f"NAME_MISMATCH | sheet={sheet_name} | profile={name_in_sheet_from_block}",
                    block,
                    action="Verify this is the same person. If yes, fix NAME_IN_SHEET and re-ingest.",
                )
                continue

        if match_issue == "LOW_NAME_SIMILARITY":
            print(f"[Ingest] WARNING: Low name similarity for {rid} — proceeding with caution.")

        # Status gate table: SENT and SKIP_GUESSED_EMAIL are terminal for manual ingest.
        # Email_Source values guessed, ambiguous, and blank are treated as guessed.
        current_status = (matched_row.get("STATUS") or "").strip()

        if current_status == config.STATUS_SENT:
            print(f"[Ingest] SKIP: {rid} — row already SENT.")
            cleanup_remove_spans.add((block["start"], block["end"]))
            continue

        if current_status == STATUS_SKIP_GUESSED_EMAIL:
            print(f"[Ingest] SKIP_GUESSED_EMAIL: {rid} — terminal manual status already set.")
            cleanup_remove_spans.add((block["start"], block["end"]))
            continue

        if current_status == config.STATUS_PROCESSING:
            print(f"[Ingest] SKIP_PROCESSING_IN_PROGRESS: {rid} — automated pipeline owns this row.")
            cleanup_remove_spans.add((block["start"], block["end"]))
            continue

        block_type = block["type"]

        if current_status == config.STATUS_FAILED and block_type == "EMAIL":
            _send_to_review(
                "FAILED_ROW_NEEDS_RESEARCH_FIRST",
                block,
                action="This row has STATUS=FAILED. It needs research (PROFILE) before email generation.",
            )
            continue

        target_status = None
        clear_email_due_to_upgrade = False
        new_conf = _profile_confidence_from_block(block)

        if block_type == "PROFILE":
            if current_status in (config.STATUS_PENDING, ""):
                target_status = config.STATUS_RESEARCH_DONE
            elif current_status == config.STATUS_RESEARCH_DONE:
                raw_existing_conf = matched_row.get("Confidence_Level", "")
                existing_conf = _normalize_confidence(raw_existing_conf)  # normalize read side — dirty sheet values
                if (raw_existing_conf or "").strip() and (raw_existing_conf or "").strip() != existing_conf:
                    logger.log(
                        "INFO",
                        "NORMALIZE_READ",
                        f"RECONCILE_ID: {rid} | original='{(raw_existing_conf or '').strip()}' | normalized='{existing_conf}'",
                    )
                if confidence_rank(new_conf) >= confidence_rank(existing_conf):
                    target_status = config.STATUS_RESEARCH_DONE
                    if confidence_rank(new_conf) == confidence_rank(existing_conf):
                        logger.log(
                            "INFO",
                            "DEDUP_OVERWRITE",
                            f"same confidence, newer paste wins | RECONCILE_ID: {rid}",
                        )
                    print(f"[Ingest] Upgrading confidence for {rid}: {existing_conf} → {new_conf}")
                else:
                    skipped_confidence_count += 1
                    cleanup_remove_spans.add((block["start"], block["end"]))
                    logger.log(
                        "INFO",
                        "WRITE_SKIPPED",
                        f"RECONCILE_ID: {rid} | existing_confidence={existing_conf} | new_confidence={new_conf}",
                    )
                    print(f"[Ingest] SKIP: {rid} — existing confidence ({existing_conf}) > new ({new_conf})")
                    continue
            elif current_status == config.STATUS_EMAIL_DONE:
                raw_existing_conf = matched_row.get("Confidence_Level", "")
                existing_conf = _normalize_confidence(raw_existing_conf)  # normalize read side — dirty sheet values
                if (raw_existing_conf or "").strip() and (raw_existing_conf or "").strip() != existing_conf:
                    logger.log(
                        "INFO",
                        "NORMALIZE_READ",
                        f"RECONCILE_ID: {rid} | original='{(raw_existing_conf or '').strip()}' | normalized='{existing_conf}'",
                    )
                if confidence_rank(new_conf) >= confidence_rank(existing_conf):
                    target_status = config.STATUS_RESEARCH_DONE
                    clear_email_due_to_upgrade = True
                    if confidence_rank(new_conf) == confidence_rank(existing_conf):
                        logger.log(
                            "INFO",
                            "DEDUP_OVERWRITE",
                            f"same confidence, newer paste wins | RECONCILE_ID: {rid}",
                        )
                else:
                    skipped_confidence_count += 1
                    cleanup_remove_spans.add((block["start"], block["end"]))
                    logger.log(
                        "INFO",
                        "WRITE_SKIPPED",
                        f"RECONCILE_ID: {rid} | existing_confidence={existing_conf} | new_confidence={new_conf}",
                    )
                    print(f"[Ingest] SKIP: {rid} — existing confidence ({existing_conf}) > new ({new_conf})")
                    continue
            elif current_status == config.STATUS_FAILED_PARSE:
                target_status = config.STATUS_RESEARCH_DONE
            elif current_status == config.STATUS_FAILED:
                target_status = config.STATUS_RESEARCH_DONE
                print(f"[Ingest] Manual recovery of FAILED row: {rid}")
            else:
                target_status = config.STATUS_RESEARCH_DONE
        else:
            if current_status in (config.STATUS_PENDING, ""):
                target_status = config.STATUS_EMAIL_DONE
            elif current_status == config.STATUS_RESEARCH_DONE:
                target_status = config.STATUS_EMAIL_DONE
            elif current_status == config.STATUS_EMAIL_DONE:
                target_status = config.STATUS_EMAIL_DONE
                print(f"[Ingest] Overwriting email for {rid}")
            elif current_status == config.STATUS_FAILED_PARSE:
                target_status = config.STATUS_EMAIL_DONE
            else:
                target_status = config.STATUS_EMAIL_DONE

        updates = {}

        if block_type == "PROFILE":
            parsed_profile = block["parsed"]
            updates["Verified_Company"] = parsed_profile.get("COMPANY", "")
            updates["Enrichment_Notes"] = block["raw"][:5000]
            updates["Enrichment_Source"] = "manual_kimi_research"
            updates["Confidence_Level"] = new_conf
            linkedin = parsed_profile.get("LINKEDIN_URL", "")
            if linkedin:
                updates["LinkedIn_URL"] = linkedin
            # New profile fields (V7)
            updates["GENDER"] = parsed_profile.get("GENDER", "")
            updates["BATCH_YEAR"] = parsed_profile.get("BATCH_YEAR", "")
            raw_domain = parsed_profile.get("PRIMARY_DOMAIN", "")
            if raw_domain in _VALID_PRIMARY_DOMAINS:
                updates["PRIMARY_DOMAIN"] = raw_domain
            else:
                if raw_domain:
                    print(f"[PRIMARY_DOMAIN_INVALID] value={raw_domain}")
                    logger.log("WARN", "PRIMARY_DOMAIN_INVALID", f"value={raw_domain} | RECONCILE_ID: {rid}")
                updates["PRIMARY_DOMAIN"] = ""
            raw_context = parsed_profile.get("CONTEXT", "")
            if len(raw_context) > 500:
                raw_context = raw_context[:500]
                print(f"[CONTEXT_TRUNCATED] RECONCILE_ID: {rid}")
                logger.log("WARN", "CONTEXT_TRUNCATED", f"RECONCILE_ID: {rid}")
            updates["CONTEXT"] = raw_context
            if clear_email_due_to_upgrade:
                updates["Subject"] = ""
                updates["Body"] = ""
                logger.log(
                    "INFO",
                    "STATUS_RESET",
                    f"Profile upgraded, email cleared. Was EMAIL_DONE → RESEARCH_DONE. RECONCILE_ID: {rid}",
                )
                print(
                    f"[STATUS_RESET] Profile upgraded, email cleared. Was EMAIL_DONE → RESEARCH_DONE. RECONCILE_ID: {rid}"
                )
            if target_status:
                updates["STATUS"] = target_status
            # TODO: Add system_prompt_email.md instruction block for guessed-email handling.
            # When a user requests the email prompt update, this status should be documented there.
            if normalized_email_source in GUESSED_SOURCES:
                guessed_skip_count += 1
                updates["STATUS"] = STATUS_SKIP_GUESSED_EMAIL
                if normalized_email_source == "ambiguous":
                    logger.log(
                        "WARN",
                        "GUESSED_SKIP",
                        f"Email_Source=ambiguous, treating as guessed. RECONCILE_ID: {rid}",
                    )
                    print(f"[GUESSED_SKIP] Email_Source=ambiguous, treating as guessed. RECONCILE_ID: {rid}")
                elif not row_email_source:
                    logger.log(
                        "WARN",
                        "GUESSED_SKIP",
                        f"Email_Source blank, treating as guessed. RECONCILE_ID: {rid}",
                    )
                    print(f"[GUESSED_SKIP] Email_Source blank, treating as guessed. RECONCILE_ID: {rid}")
                else:
                    logger.log(
                        "WARN",
                        "GUESSED_SKIP",
                        f"Email_Source=guessed. Status set to SKIP_GUESSED_EMAIL. Will not generate email. RECONCILE_ID: {rid}",
                    )
                    print(
                        f"[GUESSED_SKIP] Email_Source=guessed. Status set to SKIP_GUESSED_EMAIL. Will not generate email. RECONCILE_ID: {rid}"
                    )
        else:
            if normalized_email_source in GUESSED_SOURCES:
                guessed_skip_count += 1
                cleanup_remove_spans.add((block["start"], block["end"]))
                logger.log(
                    "WARN",
                    "GUESSED_SKIP",
                    f"Email block blocked because Email_Source is {normalized_email_source or 'blank'}. RECONCILE_ID: {rid}",
                )
                print(f"[GUESSED_SKIP] Email block skipped for guessed email row. RECONCILE_ID: {rid}")
                continue
            subject = _strip_em_dashes(block.get("subject", ""))
            updates["Subject"] = subject
            email_body = block.get("body", "")
            email_body = _ensure_greeting(email_body, block.get("name_in_sheet", ""))
            email_body = _strip_em_dashes(email_body)
            email_body = _normalize_email_signature(email_body)
            updates["Body"] = email_body
            if target_status:
                updates["STATUS"] = target_status

        assert current_status != config.STATUS_SENT, "STATUS=SENT rows must never be modified"
        pending_writes.append((row_idx, updates, tab_name))
        pending_write_records.append(
            {
                "reconcile_id": rid,
                "tab_name": tab_name,
                "row_idx": row_idx,
                "status": updates.get("STATUS", current_status),
                "block": block,
            }
        )
        processed_ids.add(rid)

    try:
        batch_api_calls = len({resolved_tab for _, _, resolved_tab in pending_writes if resolved_tab})
        sheets_helper.batch_write_rows(pending_writes)
        if pending_writes:
            api_call_phrase = "single API call" if batch_api_calls == 1 else f"{batch_api_calls} API calls"
            logger.log("INFO", "BATCH_WRITE", f"{len(pending_writes)} rows written in {api_call_phrase}.")
            print(f"[BATCH_WRITE] {len(pending_writes)} rows written in {api_call_phrase}.")
    except Exception as exc:
        logger.log("ERROR", "BATCH_WRITE_FAILED", f"Exception: {exc}. No rows were written.")
        print("Batch write failed — ingest_queue.md has NOT been cleared. Fix the error and re-run ingest.")
        raise

    success_count = len(pending_write_records)
    success_rids = {record["reconcile_id"] for record in pending_write_records}
    tabs_updated: dict[str, int] = {}
    affected_tabs: set[str] = set()
    for record in pending_write_records:
        rid = record["reconcile_id"]
        tab_name = record["tab_name"]
        tabs_updated[tab_name] = tabs_updated.get(tab_name, 0) + 1
        affected_tabs.add(tab_name)
        cleanup_remove_spans.add((record["block"]["start"], record["block"]["end"]))
        logger.log(
            "INFO",
            "WRITE_SUCCESS",
            f"RECONCILE_ID: {rid} | tab={tab_name} | row={_visible_row_number(record['row_idx'])} | status={record['status']}",
        )

    for reason, block, action, extra in pending_review_entries:
        cleanup_remove_spans.add((block["start"], block["end"]))
        _append_to_review(
            reason=reason,
            reconcile_id=block.get("reconcile_id"),
            raw_block=block["raw"],
            action_needed=action,
            extra_context=extra,
            logger=logger,
        )

    now_iso = _timestamp_utc()
    summary_counts = {
        "blocks_parsed": parsed_block_count,
        "written": success_count,
        "skipped_confidence": skipped_confidence_count,
        "review": review_count,
        "dedup_skipped": dedup_skipped_count,
    }
    marker = _format_ingest_run_marker(summary_counts)
    ingest_prefix, ingest_items = _extract_ingest_items(ingest_text)
    remaining_items = []
    failed_blocks_kept = 0
    removed_processed_blocks = 0
    for item in ingest_items:
        if item["type"] == "marker":
            remaining_items.append(item)
            continue
        span = (item["block"]["start"], item["block"]["end"])
        if span in cleanup_remove_spans:
            removed_processed_blocks += 1
            continue
        failed_blocks_kept += 1
        remaining_items.append(item)
    remaining_items.append({"type": "marker", "raw": marker})
    _atomic_write(_INGEST_QUEUE, _render_ingest_items(ingest_prefix, remaining_items))
    logger.log(
        "INFO",
        "INGEST_CLEANUP",
        f"Removed: {removed_processed_blocks} processed blocks. Kept: {failed_blocks_kept} failed blocks.",
    )

    rq_text = _read_file_safe(_RESEARCH_QUEUE)
    if rq_text and success_rids:
        for rid in success_rids:
            escaped = re.escape(f"[ID:{rid}]")
            rq_text = re.sub(rf"^.*{escaped}.*\n?", "", rq_text, flags=re.MULTILINE)

        def _clean_empty_batches(text: str) -> str:
            def is_batch_empty(match):
                body = match.group(2)
                return not bool(_ID_LINE_RE.search(body))

            while True:
                new_text = _BATCH_BLOCK_RE.sub(
                    lambda m: "" if is_batch_empty(m) else m.group(0), text
                )
                if new_text == text:
                    break
                text = new_text
            return text

        rq_text = _clean_empty_batches(rq_text)

        def _update_batch_counts(text: str) -> str:
            """Update the name count in each batch header to reflect remaining entries."""

            def _replace_count(match):
                header = match.group(1)
                body = match.group(2)
                end = match.group(3)
                remaining = len(_ID_LINE_RE.findall(body))
                updated_header = re.sub(r"\d+ names", f"{remaining} names", header)
                return updated_header + body + end

            return _BATCH_BLOCK_RE.sub(_replace_count, text)

        rq_text = _update_batch_counts(rq_text)
        rq_text = re.sub(r"\n{3,}", "\n\n", rq_text).strip() + "\n"

        remaining_ids = len(_ID_LINE_RE.findall(rq_text))
        remaining_batches = len(_BATCH_HEADER_RE.findall(rq_text))
        rq_text = re.sub(
            r"_Pending batches:.*?_",
            f"_Pending batches: {remaining_batches} | Total names pending: {remaining_ids}_",
            rq_text,
        )
        rq_text = re.sub(
            r"_Last updated:.*?_",
            f"_Last updated: {now_iso}_",
            rq_text,
            count=1,
        )

        _atomic_write(_RESEARCH_QUEUE, rq_text)
        print(f"[Ingest] research_queue.md: {len(success_rids)} entries cleared")

    refreshed_tabs = sorted(affected_tabs)
    if refreshed_tabs:
        refresh_result = _export_email_batches(refreshed_tabs, 5, logger, reconcile_first=True, is_auto_refresh=True)
        if refresh_result["new_rows"] > 0:
            logger.log("INFO", "EMAIL_QUEUE_REFRESH", f"Auto-refreshed email_queue.md for: {', '.join(refreshed_tabs)}")
            print(f"[EMAIL_QUEUE_REFRESH] Auto-refreshed email_queue.md for: {', '.join(refreshed_tabs)}")

    print(f"[Ingest] Successfully written to sheet: {success_count}")
    if review_count > 0:
        print(f"[Ingest] Moved to review_queue.md: {review_count}")
        for reason, count in sorted(review_reasons.items()):
            print(f"  - {reason}: {count}")
    if tabs_updated:
        tab_summary = ", ".join(f"{t} ({c} rows)" for t, c in sorted(tabs_updated.items()))
        print(f"[Ingest] Tabs updated: {tab_summary}")

    summary_time = datetime.now().isoformat(timespec="seconds")
    summary_lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Ingest complete — {summary_time}",
        f"  Blocks parsed:        {parsed_block_count:>2}",
        f"  Written to sheet:     {success_count:>2}",
        f"  Skipped (confidence): {skipped_confidence_count:>2}",
        f"  Dedup skipped:        {dedup_skipped_count:>2}",
        f"  Guessed email blocked:{guessed_skip_count:>3}",
        f"  Moved to review:      {review_count:>2}",
        f"  Idempotent skips:     {idempotent_skip_count:>2}",
        f"Log: {logger.log_path if logger.log_path else 'disabled'}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for line in summary_lines:
        print(line)
    logger.log(
        "INFO",
        "INGEST_COMPLETE",
        " | ".join(
            [
                f"blocks_parsed={parsed_block_count}",
                f"written={success_count}",
                f"skipped_confidence={skipped_confidence_count}",
                f"dedup_skipped={dedup_skipped_count}",
                f"guessed_email_blocked={guessed_skip_count}",
                f"review={review_count}",
                f"idempotent_skips={idempotent_skip_count}",
                f"log={logger.log_path if logger.log_path else 'disabled'}",
            ]
        ),
    )

    if success_count > 0 or review_count > 0:
        last_ingested_line = (
            f"_Last ingested: {now_iso} | Ingested: {success_count} | Failed: {review_count}_\n\n"
        )
        _atomic_write(_INGEST_QUEUE, last_ingested_line + INGEST_QUEUE_TEMPLATE)


def cmd_review(args):
    logger = ManualWorkflowLogger("review")
    review_text = _read_file_safe(_REVIEW_QUEUE)
    entries = _parse_review_entries(review_text)

    if args.list:
        if not entries:
            print("review_queue.md is empty.")
            return
        grouped: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            grouped[entry["reason"]].append(entry)
        print(f"review_queue.md — {len(entries)} entries")
        print()
        index = 1
        for reason in sorted(grouped):
            print(f"[{reason}]")
            for entry in grouped[reason]:
                print(f"  {index}. {entry['display_name']}  — RECONCILE_ID: {entry['reconcile_id']}")
                index += 1
            print()
        return

    if args.clear_all:
        removed = len(entries)
        _atomic_write(_REVIEW_QUEUE, _render_review_queue([]))
        logger.log("INFO", "REVIEW_QUEUE", f"Cleared all review entries | removed={removed}")
        print(f"Cleared {removed} entries from review_queue.md.")
        return

    query = (args.retry or args.clear or "").strip().lower()
    matches = [entry for entry in entries if query in entry["display_name"].lower()]
    if not matches:
        print("No matching review entry found.")
        return

    target = matches[0]
    remaining_entries = [entry for entry in entries if entry["label"] != target["label"]]

    if args.retry:
        raw_blocks = _extract_all_blocks(target["full_text"])
        if not raw_blocks:
            print("Selected review entry has no <PROFILE> or <EMAIL> block to retry.")
            return
        _append_block_to_ingest_queue(raw_blocks[0]["raw"])
        _atomic_write(_REVIEW_QUEUE, _render_review_queue(remaining_entries))
        logger.log(
            "INFO",
            "REVIEW_QUEUE",
            f"Moved {target['display_name']} back to ingest_queue.md | RECONCILE_ID: {target['reconcile_id']}",
        )
        print(f"Moved {target['display_name']} → ingest_queue.md. Run 'ingest' to process.")
        return

    if args.clear:
        _atomic_write(_REVIEW_QUEUE, _render_review_queue(remaining_entries))
        logger.log(
            "INFO",
            "REVIEW_QUEUE",
            f"Cleared {target['display_name']} from review_queue.md | RECONCILE_ID: {target['reconcile_id']}",
        )
        print(f"Cleared {target['display_name']} from review_queue.md.")
        return


# ---------------------------------------------------------------------------
# REBATCH subcommand
# ---------------------------------------------------------------------------

def cmd_rebatch(args):
    batch_size = max(1, min(50, args.batch_size))
    text = _read_file_safe(_RESEARCH_QUEUE)
    if not text.strip():
        print("[Rebatch] research_queue.md is empty.")
        return

    # Extract all name lines from all batches
    all_entries: list[tuple[str, str]] = []  # (tab_name, display_line)

    for match in _BATCH_BLOCK_RE.finditer(text):
        header = match.group(1)
        body = match.group(2)
        # Extract tab name from header
        tab_match = re.search(r"\|\s*(cohort_\w+)\s*\|", header)
        tab = tab_match.group(1) if tab_match else "unknown"

        for line in body.splitlines():
            line = line.strip()
            if line and _ID_LINE_RE.search(line):
                all_entries.append((tab, line))

    if not all_entries:
        print("[Rebatch] No name entries found in batches.")
        return

    # Group by tab, then chunk
    tab_groups: dict[str, list[str]] = {}
    for tab, line in all_entries:
        tab_groups.setdefault(tab, []).append(line)

    batches: list[tuple[str, list[str]]] = []
    for tab, lines in tab_groups.items():
        for i in range(0, len(lines), batch_size):
            batches.append((tab, lines[i : i + batch_size]))

    # Rebuild file
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_names = sum(len(entries) for _, entries in batches)

    # Preserve the header section (everything before first batch)
    first_batch_pos = text.find("━━━ BATCH-")
    if first_batch_pos == -1:
        header_section = text
    else:
        header_section = text[:first_batch_pos]

    # Update header counters
    header_section = re.sub(
        r"_Last updated:.*?_",
        f"_Last updated: {now_iso}_",
        header_section,
        count=1,
    )
    header_section = re.sub(
        r"_Pending batches:.*?_",
        f"_Pending batches: {len(batches)} | Total names pending: {total_names}_",
        header_section,
    )

    new_blocks = []
    for idx, (tab, entries) in enumerate(batches):
        batch_num = idx + 1
        batch_label = f"BATCH-{batch_num:03d}"
        year = tab.replace("cohort_", "") if "cohort_" in tab else tab
        header = f"━━━ {batch_label} | {tab} | {len(entries)} names ━━━"
        context = f"BATCH CONTEXT: IIM Udaipur alumni, PGP {year} passouts"
        block_lines = [header, context, ""]
        block_lines.extend(entries)
        block_lines.append(f"━━━ END {batch_label} ━━━")
        new_blocks.append("\n".join(block_lines))

    content = header_section.rstrip() + "\n\n" + "\n\n".join(new_blocks) + "\n"
    _atomic_write(_RESEARCH_QUEUE, content)
    print(f"[Rebatch] {total_names} names reorganised into {len(batches)} batches of {batch_size}")


def _export_email_batches(
    tabs: list[str],
    batch_size: int,
    logger: ManualWorkflowLogger,
    *,
    domain_filter: str = "",
    column_filters: dict[str, list[str]] | None = None,
    reconcile_first: bool = True,
    is_auto_refresh: bool = False,
) -> dict[str, int]:
    if reconcile_first:
        reconcile_summary = _reconcile_queue_file(_EMAIL_QUEUE, None, logger)
        if not is_auto_refresh:
            print(reconcile_summary["summary"])

    existing_text = _read_file_safe(_EMAIL_QUEUE)
    existing_ids = _existing_reconcile_ids(existing_text)
    header, existing_batches = _parse_email_queue(existing_text)
    next_batch_num = max((batch["number"] for batch in existing_batches), default=0) + 1
    new_batches = []
    total_new = 0

    for tab in tabs:
        try:
            rows = sheets_helper.read_all_rows(tab_name=tab)
        except Exception as exc:
            print(f"[ExportEmail] ERROR reading tab '{tab}': {exc}")
            continue

        research_done_rows = [r for r in rows if (r.get("STATUS") or "").strip() == config.STATUS_RESEARCH_DONE]
        research_done_rows = _apply_row_filters(
            research_done_rows,
            domain_filter=domain_filter,
            column_filters=column_filters,
        )
        already_in_queue = 0
        newly_added = 0
        batch_entries = []

        for row in research_done_rows:
            email_source = (row.get("Email_Source") or "").strip().lower()
            if email_source in GUESSED_SOURCES:
                logger.log("INFO", "EMAIL_EXPORT_SKIP", f"guessed email: {(row.get('Name') or '').strip()}")
                continue
            result = build_reconcile_id(row)
            if result is None:
                continue
            rid, _ = result
            if rid in existing_ids:
                already_in_queue += 1
                continue
            confidence = _normalize_confidence(row.get("Confidence_Level", ""))
            full_notes = (row.get("Enrichment_Notes") or "").strip()
            # Full PROFILE payload is required here so HOOK1, HOOK2, OUTREACH_NOTE, ROLE, and DOMAIN survive export.
            entry_lines = [
                f"[ID:{rid}]",
                f"Name: {(row.get('Name') or '').strip()}",
                f"Confidence: {confidence}",
                "Enrichment_Notes:",
                full_notes,
            ]
            batch_entries.append({"rid": rid, "raw": "\n".join(entry_lines).rstrip()})
            existing_ids.add(rid)
            newly_added += 1
            total_new += 1

        for index in range(0, len(batch_entries), batch_size):
            new_batches.append(
                {
                    "number": next_batch_num,
                    "tab": tab,
                    "entries": batch_entries[index : index + batch_size],
                }
            )
            next_batch_num += 1

        if not is_auto_refresh or newly_added > 0:
            print(
                f"[ExportEmail] Tab: {tab} | RESEARCH_DONE rows: {len(research_done_rows)} | Already in queue: {already_in_queue} | Newly added: {newly_added}"
            )

    if not new_batches:
        if not is_auto_refresh:
            print("[ExportEmail] No new entries to add.")
        return {"batches_written": 0, "new_rows": total_new}

    _atomic_write(_EMAIL_QUEUE, _render_email_queue(header, existing_batches + new_batches))
    if not is_auto_refresh:
        print(f"[ExportEmail] Batches written: {len(new_batches)} (batch size: {batch_size})")
    return {"batches_written": len(new_batches), "new_rows": total_new}


def cmd_export_email(args):
    logger = ManualWorkflowLogger("export_email")
    batch_size = max(1, min(50, args.batch_size))
    try:
        column_filters = _parse_column_filters(args.col_filters)
    except argparse.ArgumentTypeError as exc:
        print(f"[ExportEmail] ERROR: {exc}")
        return
    if args.all_pending:
        manifest_path = Path(config.COHORTS_MANIFEST_FILE)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tabs = [c["tab_name"] for c in manifest.get("cohorts", [])]
        else:
            print("[ExportEmail] ERROR: manifest.json not found and --all-pending specified.")
            return
    elif args.tab:
        tabs = [args.tab]
    else:
        print("[ExportEmail] ERROR: Specify --tab TAB or --all-pending.")
        return

    _export_email_batches(
        tabs,
        batch_size,
        logger,
        domain_filter=args.domain or "",
        column_filters=column_filters,
        reconcile_first=True,
        is_auto_refresh=False,
    )


def cmd_reconcile(args):
    logger = ManualWorkflowLogger("reconcile")
    queue_map = {
        "research": _RESEARCH_QUEUE,
        "ingest": _INGEST_QUEUE,
        "email": _EMAIL_QUEUE,
    }
    queue_names = [args.queue] if args.queue != "all" else ["research", "ingest", "email"]
    for queue_name in queue_names:
        summary = _reconcile_queue_file(queue_map[queue_name], None, logger)
        print(summary["summary"])


# ---------------------------------------------------------------------------
# STATUS subcommand
# ---------------------------------------------------------------------------

def cmd_status(args):
    if args.ingest_queue:
        ingest_text = _read_file_safe(_INGEST_QUEUE)
        blocks = _extract_all_blocks(ingest_text)
        last_marker = _last_ingest_marker_match(ingest_text)
        pending_blocks = []
        processed_blocks = []
        row_cache: dict[str, list[dict]] = {}
        for block in blocks:
            if last_marker and block["start"] < last_marker.start():
                processed_blocks.append(block)
            else:
                pending_blocks.append(block)

        print("ingest_queue.md — status")
        print("─────────────────────────────────────")
        print(f"Pending blocks (not yet ingested): {len(pending_blocks)}")
        for block in pending_blocks:
            rid = block.get("reconcile_id") or "missing"
            row_idx, row, status, extra = _lookup_row_by_reconcile_id(rid, row_cache=row_cache)
            if status == "MATCH":
                sheet_status = (row.get("STATUS") or "").strip() or config.STATUS_PENDING
                print(f"  [ID:{rid}]  → Sheet: {sheet_status}")
            elif status == "NOT_FOUND" or status == "NO_ROW_MATCH":
                print(f"  [ID:{rid}]  → Sheet: not found")
            else:
                print(f"  [ID:{rid}]  → Sheet: {status.lower()}")
        print()
        print(f"Already processed (above last marker): {len(processed_blocks)}")
        print()
        print(f"Last run: {last_marker.group(0).splitlines()[1].split(': ', 1)[1] if last_marker else 'none'}")
        print("─────────────────────────────────────")
        print("Tip: run 'reconcile --queue ingest' to remove already-done blocks.")
        return

    if args.detail:
        tab_name = args.detail
        try:
            rows = sheets_helper.read_all_rows(tab_name=tab_name)
        except Exception as exc:
            print(f"[Status] ERROR reading {tab_name}: {exc}")
            return

        bucket_order = [args.only] if args.only else DEFAULT_DETAIL_STATUSES
        buckets: dict[str, list[tuple[int, dict]]] = {status: [] for status in bucket_order}
        for row_index, row in enumerate(rows):
            status = (row.get("STATUS") or "").strip() or config.STATUS_PENDING
            email_source = (row.get("Email_Source") or "").strip().lower()
            if status == config.STATUS_PENDING and email_source in {"guessed", "ambiguous", ""}:
                status = STATUS_SKIP_GUESSED_EMAIL
            if status in buckets:
                buckets[status].append((row_index, row))

        if args.export_names:
            export_statuses = [args.only] if args.only else [config.STATUS_PENDING]
            for status in export_statuses:
                for _, row in buckets.get(status, []):
                    print((row.get("Name") or "").strip())
            return

        print(f"{tab_name} — detailed pending view")
        print("─────────────────────────────────────")
        total_displayed = 0
        labels = {
            config.STATUS_PENDING: "PENDING (research not started)",
            config.STATUS_RESEARCH_DONE: "RESEARCH_DONE (email not yet generated)",
            config.STATUS_FAILED_PARSE: "FAILED_PARSE",
            STATUS_SKIP_GUESSED_EMAIL: "SKIP_GUESSED_EMAIL",
            config.STATUS_EMAIL_DONE: "EMAIL_DONE",
            config.STATUS_SENT: "SENT",
        }
        for status in bucket_order:
            items = buckets.get(status, [])
            if not items:
                continue
            print(f"{labels[status]}:")
            for row_index, row in items:
                total_displayed += 1
                name = (row.get("Name") or "").strip()
                email = (row.get("Email") or "").strip()
                email_source = (row.get("Email_Source") or "").strip() or "guessed"
                line = f"  Row {_visible_row_number(row_index):<3} | {name:<22} | {email_source}: {email or '(blank)'}"
                if status == config.STATUS_RESEARCH_DONE:
                    line += f"  | Confidence: {_normalize_confidence(row.get('Confidence_Level', ''))}"
                print(line)
            print()
        print("─────────────────────────────────────")
        print(f"Total pending action: {total_displayed} rows")
        return

    print("══════════════════════════════════════════")
    print("MANUAL WORKFLOW STATUS")
    print("══════════════════════════════════════════")
    print()

    # research_queue.md
    rq_text = _read_file_safe(_RESEARCH_QUEUE)
    rq_batches = _BATCH_HEADER_RE.findall(rq_text)
    rq_names = len(_ID_LINE_RE.findall(rq_text))
    oldest_batch = ""
    if rq_batches:
        # Find oldest batch — look for date in the text near first batch
        first_batch_match = _BATCH_HEADER_RE.search(rq_text)
        if first_batch_match:
            oldest_batch = f"BATCH-{min(int(b) for b in rq_batches):03d}"
    print("research_queue.md")
    print(f"  Batches pending:    {len(rq_batches)}")
    print(f"  Names pending:      {rq_names}")
    if oldest_batch:
        print(f"  Oldest batch:       {oldest_batch}")
    print()

    # ingest_queue.md
    iq_text = _read_file_safe(_INGEST_QUEUE)
    iq_profiles = len(_PROFILE_BLOCK_RE.findall(iq_text))
    iq_emails = len(_EMAIL_BLOCK_RE.findall(iq_text))
    iq_total = iq_profiles + iq_emails
    print("ingest_queue.md")
    print(f"  Blocks waiting:     {iq_total}")
    if iq_total > 0:
        print("  (run 'ingest' to process)")
    print()

    eq_text = _read_file_safe(_EMAIL_QUEUE)
    eq_ids = len(_ID_LINE_RE.findall(eq_text))
    print("email_queue.md")
    print(f"  Blocks waiting:     {eq_ids}")
    print()

    # review_queue.md
    rv_text = _read_file_safe(_REVIEW_QUEUE)
    rv_items = len(re.findall(r"━━━\s*REVIEW-(\d+)\s*\|", rv_text))
    print("review_queue.md")
    print(f"  Items needing attention: {rv_items}")
    print()

    # Sheet state
    manifest_path = Path(config.COHORTS_MANIFEST_FILE)
    print("Sheet state (PENDING rows by tab):")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        total_pending = 0
        for cohort in manifest.get("cohorts", []):
            tab = cohort["tab_name"]
            try:
                rows = sheets_helper.read_all_rows(tab_name=tab)
                pending = sum(1 for r in rows if (r.get("STATUS") or "").strip() == config.STATUS_PENDING)
                total_row_count = len(rows)
                total_pending += pending
                if total_row_count > 0:
                    print(f"  {tab}:   {pending} pending / {total_row_count} total")
            except Exception:
                print(f"  {tab}:   (could not read)")
        print()
        if total_pending > 0:
            batch_size_for_estimate = 15
            est_batches = (total_pending + batch_size_for_estimate - 1) // batch_size_for_estimate
            print(f"Estimated batches to clear all pending (batch size {batch_size_for_estimate}): {est_batches}")
    else:
        print("  (manifest.json not found — cannot list cohort tabs)")

    print()
    print("══════════════════════════════════════════")


# ---------------------------------------------------------------------------
# RESET subcommand
# ---------------------------------------------------------------------------

_RESET_CLEARABLE_STATUSES = {
    config.STATUS_RESEARCH_DONE,
    config.STATUS_EMAIL_DONE,
    config.STATUS_FAILED,
    config.STATUS_FAILED_PARSE,
}

_RESET_COLUMNS = {
    "STATUS": config.STATUS_PENDING,
    "Verified_Company": "",
    "Enrichment_Notes": "",
    "Enrichment_Source": "",
    "Confidence_Level": "",
    "Subject": "",
    "Body": "",
}


def cmd_reset(args):
    tab = args.tab
    write = args.write
    names_filter = None
    if args.names:
        names_filter = [n.strip() for n in args.names.split(",") if n.strip()]

    try:
        rows = sheets_helper.read_all_rows(tab_name=tab)
    except Exception as e:
        print(f"[Reset] ERROR reading tab '{tab}': {e}")
        return

    candidates = []
    for i, row in enumerate(rows):
        status = (row.get("STATUS") or "").strip()
        if status not in _RESET_CLEARABLE_STATUSES:
            continue
        row_name = (row.get("Name") or "").strip()
        if names_filter:
            matched = any(
                SequenceMatcher(None, row_name.lower(), n.lower()).ratio() >= 0.8
                for n in names_filter
            )
            if not matched:
                continue
        candidates.append((i, row_name, status))

    if not candidates:
        print(f"[Reset] No resettable rows found in {tab}.")
        return

    print(f"[Reset] {'DRY RUN — ' if not write else ''}Tab: {tab}")
    print(f"[Reset] Rows to reset: {len(candidates)}")
    for row_idx, name, status in candidates:
        print(f"  Row {row_idx + 2}: {name} ({status} → PENDING)")

    if not write:
        print("[Reset] Dry run complete. Pass --write to apply changes.")
        return

    updates = [(row_idx, dict(_RESET_COLUMNS), tab) for row_idx, _, _ in candidates]
    sheets_helper.batch_write_rows(updates)
    print(f"[Reset] {len(candidates)} rows reset to PENDING.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manual Workflow CLI for IIMU alumni outreach pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # export
    export_parser = subparsers.add_parser("export", help="Export PENDING rows to research_queue.md")
    export_parser.add_argument("--tab", action="append", help="Cohort tab name (repeatable)")
    export_parser.add_argument("--batch-size", type=int, default=15, help="Names per batch (default: 15)")
    export_parser.add_argument("--all-pending", action="store_true", help="Export all pending across all tabs")
    export_parser.add_argument("--names", help="Comma-separated names to export (fuzzy match, batch-size=1 per name)")
    export_parser.add_argument("--domain", help="PRIMARY_DOMAIN contains match")
    export_parser.add_argument("--filter", dest="col_filters", action="append", help="Exact column filter in the form COL=VALUE or COL=VALUE1,VALUE2")

    # export-email
    export_email_parser = subparsers.add_parser("export-email", help="Export RESEARCH_DONE rows to email_queue.md")
    export_email_parser.add_argument("--tab", help="Cohort tab name")
    export_email_parser.add_argument("--all-pending", action="store_true", help="Export RESEARCH_DONE rows across all tabs")
    export_email_parser.add_argument("--batch-size", type=int, default=5, help="Names per batch (default: 5)")
    export_email_parser.add_argument("--domain", help="PRIMARY_DOMAIN contains match")
    export_email_parser.add_argument("--filter", dest="col_filters", action="append", help="Exact column filter in the form COL=VALUE or COL=VALUE1,VALUE2")

    # query
    query_parser = subparsers.add_parser("query", help="Filter rows by column values and output them in different formats")
    query_scope = query_parser.add_mutually_exclusive_group()
    query_scope.add_argument("--tab", help="Single cohort tab")
    query_scope.add_argument("--all-tabs", action="store_true", help="Query all cohort tabs from manifest")
    query_parser.add_argument("--status", help="Comma-separated STATUS values")
    query_parser.add_argument("--email-source", help="Comma-separated Email_Source values")
    query_parser.add_argument("--confidence", help="Comma-separated Confidence_Level values")
    query_parser.add_argument("--gender", help="Comma-separated GENDER values")
    query_parser.add_argument("--batch-year", help="Comma-separated Graduation_Year values")
    query_parser.add_argument("--domain", help="PRIMARY_DOMAIN contains match")
    query_parser.add_argument("--name", help="Comma-separated fuzzy name filter")
    query_parser.add_argument("--output", choices=["table", "fields", "research", "reconcile"], default="table")
    query_parser.add_argument("--batch-size", type=int, default=3, help="Batch size for --output research")
    query_parser.add_argument("--fields", help="Comma-separated fields for --output fields")

    # ingest
    ingest_parser = subparsers.add_parser("ingest", help="Parse ingest_queue.md and write to sheet")
    ingest_parser.add_argument("--force", action="store_true", help="Ignore INGEST_RUN_COMPLETE markers and re-process all blocks")

    # rebatch
    rebatch_parser = subparsers.add_parser("rebatch", help="Reorganise batches in research_queue.md")
    rebatch_parser.add_argument("--batch-size", type=int, required=True, help="New batch size")

    # review
    review_parser = subparsers.add_parser("review", help="Inspect and manage review_queue.md")
    review_group = review_parser.add_mutually_exclusive_group(required=True)
    review_group.add_argument("--list", action="store_true", help="List review_queue.md entries grouped by reason")
    review_group.add_argument("--retry", help="Move a review entry back to ingest_queue.md by name")
    review_group.add_argument("--clear", help="Remove a review entry from review_queue.md by name")
    review_group.add_argument("--clear-all", action="store_true", help="Clear all review_queue.md entries")

    # reconcile
    reconcile_parser = subparsers.add_parser("reconcile", help="Reconcile queue files against the sheet")
    reconcile_parser.add_argument("--queue", choices=["research", "ingest", "email", "all"], default="all", help="Queue to reconcile (default: all)")

    # reconcile-email
    reconcile_email_parser = subparsers.add_parser("reconcile-email", help="Mark manually verified email rows and set a target status")
    reconcile_email_parser.add_argument("--tab", required=True, help="Cohort tab name")
    reconcile_email_parser.add_argument("--name", required=True, help="Comma-separated names to reconcile")
    reconcile_email_parser.add_argument(
        "--status",
        default="PENDING",
        help="Target STATUS to set on reconciled rows (default: PENDING). Use RESEARCH_DONE if research is already complete.",
    )

    # status
    status_parser = subparsers.add_parser("status", help="Show current workflow status")
    status_parser.add_argument("--detail", help="Show per-row detail for a specific cohort tab")
    status_parser.add_argument("--ingest-queue", action="store_true", help="Show pending vs processed blocks in ingest_queue.md")
    status_parser.add_argument("--only", choices=DETAIL_STATUSES, help="Filter --detail view to one status bucket")
    status_parser.add_argument("--export-names", action="store_true", help="With --detail, print just the names for piping")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset rows back to PENDING (dry run by default)")
    reset_parser.add_argument("--tab", required=True, help="Cohort tab name")
    reset_parser.add_argument("--names", help="Comma-separated names to filter (fuzzy match)")
    reset_parser.add_argument("--write", action="store_true", help="Actually apply changes (default is dry run)")

    reset_email_parser = subparsers.add_parser("reset-email", help="Reset EMAIL_DONE rows back to RESEARCH_DONE")
    reset_email_parser.add_argument("--tab", required=True, help="Cohort tab name")
    reset_email_scope = reset_email_parser.add_mutually_exclusive_group(required=True)
    reset_email_scope.add_argument("--domain", help="PRIMARY_DOMAIN contains match")
    reset_email_scope.add_argument("--name", help="Comma-separated fuzzy name filter")
    reset_email_scope.add_argument("--filter", dest="col_filters", action="append", help="Exact column filter in the form COL=VALUE or COL=VALUE1,VALUE2")
    reset_email_parser.add_argument("--write", action="store_true", help="Actually apply changes (default is dry run)")

    reset_research_parser = subparsers.add_parser("reset-research", help="Reset RESEARCH_DONE rows back to PENDING")
    reset_research_parser.add_argument("--tab", required=True, help="Cohort tab name")
    reset_research_scope = reset_research_parser.add_mutually_exclusive_group(required=True)
    reset_research_scope.add_argument("--domain", help="PRIMARY_DOMAIN contains match")
    reset_research_scope.add_argument("--name", help="Comma-separated fuzzy name filter")
    reset_research_scope.add_argument("--filter", dest="col_filters", action="append", help="Exact column filter in the form COL=VALUE or COL=VALUE1,VALUE2")
    reset_research_parser.add_argument("--write", action="store_true", help="Actually apply changes (default is dry run)")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args)
    elif args.command == "export-email":
        cmd_export_email(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "rebatch":
        cmd_rebatch(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "reconcile":
        cmd_reconcile(args)
    elif args.command == "reconcile-email":
        cmd_reconcile_email(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "reset":
        cmd_reset(args)
    elif args.command == "reset-email":
        cmd_reset_email(args)
    elif args.command == "reset-research":
        cmd_reset_research(args)


if __name__ == "__main__":
    main()
