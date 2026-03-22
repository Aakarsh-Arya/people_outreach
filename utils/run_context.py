"""Runtime logging and checkpoint helpers for pipeline runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

_CURRENT_RUN: dict[str, Any] | None = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _logs_dir() -> Path:
    logs_dir = Path(config.LOGS_DIR)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _debug_logs_dir() -> Path:
    debug_logs_dir = Path(config.DEBUG_LOGS_DIR)
    debug_logs_dir.mkdir(parents=True, exist_ok=True)
    return debug_logs_dir


def start_run(phase: str, cohort_file: str = "", tab_name: str = "") -> dict[str, Any]:
    """Create a per-run log file and cache its metadata."""
    global _CURRENT_RUN

    run_id = _run_stamp()
    log_path = _logs_dir() / f"run_{run_id}.log"
    debug_log_path = _debug_logs_dir() / f"debug_{run_id}.jsonl"
    _CURRENT_RUN = {
        "run_id": run_id,
        "phase": phase,
        "cohort_file": cohort_file,
        "tab_name": tab_name,
        "log_file": str(log_path),
        "debug_log_file": str(debug_log_path),
        "current_row_number": None,
        "current_alumni_name": "",
    }
    log_event(
        phase=phase,
        api_called="RUN_START",
        error_type="INFO",
        raw_response_snippet=f"cohort_file={cohort_file}; tab_name={tab_name}",
    )
    return dict(_CURRENT_RUN)


def get_current_run() -> dict[str, Any]:
    """Return current run metadata, starting a generic run if needed."""
    if _CURRENT_RUN is None:
        return start_run(phase="UNKNOWN")
    return dict(_CURRENT_RUN)


def set_current_row(*, row_number: int | None, alumni_name: str = "") -> None:
    """Set row context for downstream log entries that do not provide explicit row info."""
    global _CURRENT_RUN

    if _CURRENT_RUN is None:
        start_run(phase="UNKNOWN")

    if _CURRENT_RUN is None:
        return

    _CURRENT_RUN["current_row_number"] = row_number
    _CURRENT_RUN["current_alumni_name"] = alumni_name


def clear_current_row() -> None:
    """Clear row context after a row finishes processing."""
    if _CURRENT_RUN is None:
        return

    _CURRENT_RUN["current_row_number"] = None
    _CURRENT_RUN["current_alumni_name"] = ""


def log_event(
    *,
    phase: str,
    row_number: int | None = None,
    alumni_name: str = "",
    api_called: str = "",
    http_status: int | str | None = None,
    error_type: str = "",
    raw_response_snippet: str = "",
) -> None:
    """Append a structured log line for the active run."""
    run = get_current_run() if _CURRENT_RUN is None else _CURRENT_RUN
    if run is None:
        return

    entry = {
        "timestamp": _utc_timestamp(),
        "phase": phase,
        "row_number": row_number if row_number is not None else run.get("current_row_number"),
        "alumni_name": alumni_name or run.get("current_alumni_name", ""),
        "api_called": api_called,
        "http_status": "" if http_status is None else http_status,
        "error_type": error_type,
        "raw_response_snippet": (raw_response_snippet or "")[:500],
    }

    log_path = Path(run["log_file"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def log_retry_attempt(
    *,
    function_name: str,
    attempt_number: int,
    http_status: int | str | None,
    response_snippet: str,
) -> None:
    """Append a retry-focused event using the active run log."""
    log_event(
        phase="RETRY",
        api_called=function_name,
        http_status=http_status,
        error_type=f"retry_attempt_{attempt_number}",
        raw_response_snippet=response_snippet[:200],
    )


def log_raw_llm_response(
    *,
    phase: str,
    api_called: str,
    raw_response: str,
    tokens_used: int | str | None = None,
    alumni_name: str = "",
) -> None:
    """Append raw LLM output to the per-run debug JSONL file."""
    def _sanitize_pii(text: str) -> str:
        return re.sub(
            r"(?i)(name|email|linkedin|company|phone)\s*:\s*[^\n]+",
            r"\1: [REDACTED]",
            text,
        )

    run = get_current_run() if _CURRENT_RUN is None else _CURRENT_RUN
    if run is None:
        return

    entry = {
        "timestamp": _utc_timestamp(),
        "phase": phase,
        "alumni_name": alumni_name or run.get("current_alumni_name", ""),
        "api_called": api_called,
        "tokens_used": "" if tokens_used is None else tokens_used,
        "raw_response": _sanitize_pii(raw_response or "")[:10000],
    }

    debug_log_path = Path(run.get("debug_log_file") or (_debug_logs_dir() / f"debug_{run['run_id']}.jsonl"))
    debug_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(debug_log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def load_progress() -> dict[str, Any] | None:
    """Load progress checkpoint if it exists."""
    progress_path = Path(config.PROGRESS_FILE)
    if not progress_path.exists():
        return None

    try:
        with open(progress_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, ValueError):
        print(f"WARNING: Corrupt checkpoint file {progress_path} — ignoring.")
        return None


def save_progress(*, last_row_completed: int, cohort_file: str, tab_name: str) -> None:
    """Update progress checkpoint after a completed row."""
    run = get_current_run()
    payload = {
        "last_row_completed": last_row_completed,
        "timestamp": _utc_timestamp(),
        "cohort_file": cohort_file,
        "tab_name": tab_name,
        "run_id": run["run_id"],
        "log_file": run["log_file"],
    }
    progress_path = Path(config.PROGRESS_FILE)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(progress_path.parent), suffix=".tmp", prefix="progress_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
        os.replace(tmp_path, str(progress_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
