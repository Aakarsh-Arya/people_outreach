"""Manifest-backed cohort runner for multi-cohort Phase 1/2 processing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import config
from sheets_helper import read_all_rows, tab_exists

MANIFEST_VERSION = 2
COHORT_STATUS_PENDING = "pending"
COHORT_STATUS_PHASE1_DONE = "phase1_done"
COHORT_STATUS_PHASE2_IN_PROGRESS = "phase2_in_progress"
COHORT_STATUS_PHASE2_DONE = "phase2_done"
COHORT_STATUS_SENDING = "sending"
COHORT_STATUS_QUOTA_REACHED = "quota_reached"
COHORT_STATUS_SENT = "sent"
RUNNABLE_COHORT_STATUSES = {
    COHORT_STATUS_PENDING,
    COHORT_STATUS_PHASE1_DONE,
    COHORT_STATUS_PHASE2_IN_PROGRESS,
}
_COHORT_CSV_HEADER = "Name,Graduation_Year,AlmaConnect_Company,LinkedIn_URL\n"


def _cohorts_dir() -> Path:
    path = Path(config.COHORTS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path() -> Path:
    return Path(config.COHORTS_MANIFEST_FILE)


def _csv_relative_path(year: str) -> str:
    return f"cohorts/{year}.csv"


def _csv_absolute_path(year: str) -> Path:
    return Path(config.BASE_DIR) / _csv_relative_path(year)


def _default_cohort_entry(year: str) -> dict[str, Any]:
    return {
        "year": year,
        "csv_path": _csv_relative_path(year),
        "tab_name": config.cohort_tab_name(year),
        "status": COHORT_STATUS_PENDING,
        "total_rows": 0,
        "rows_phase2_done": 0,
        "rows_sent": 0,
    }


def bootstrap_cohort_workspace() -> dict[str, Any]:
    """Ensure cohort placeholders and the manifest file exist."""
    _cohorts_dir()

    for year in config.COHORT_YEARS:
        csv_path = _csv_absolute_path(year)
        if not csv_path.exists():
            csv_path.write_text(_COHORT_CSV_HEADER, encoding="utf-8")

    manifest_path = _manifest_path()
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"version": MANIFEST_VERSION, "cohorts": []}

    existing = {
        str(entry.get("year", "")): entry
        for entry in manifest.get("cohorts", [])
        if entry.get("year")
    }

    cohorts = []
    for year in config.COHORT_YEARS:
        entry = dict(_default_cohort_entry(year))
        entry.update(existing.get(year, {}))
        entry["year"] = year
        entry["csv_path"] = entry.get("csv_path") or _csv_relative_path(year)
        entry["tab_name"] = entry.get("tab_name") or config.cohort_tab_name(year)
        entry["status"] = (entry.get("status") or "").strip() or COHORT_STATUS_PENDING
        entry["total_rows"] = int(entry.get("total_rows", 0) or 0)
        entry["rows_phase2_done"] = int(entry.get("rows_phase2_done", 0) or 0)
        entry["rows_sent"] = int(entry.get("rows_sent", 0) or 0)
        cohorts.append(entry)

    manifest = {"version": MANIFEST_VERSION, "cohorts": cohorts}
    save_manifest(manifest)
    return manifest


def load_manifest() -> dict[str, Any]:
    bootstrap_cohort_workspace()
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Corrupt manifest file {_manifest_path()}. "
            f"Delete it and re-run bootstrap, or restore from backup. Original error: {exc}"
        ) from exc


def save_manifest(manifest: dict[str, Any]) -> None:
    target = _manifest_path()
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), suffix=".tmp", prefix="manifest_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=True)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _resolve_csv_path(csv_path: str) -> Path:
    path = Path(csv_path)
    if not path.is_absolute():
        path = Path(config.BASE_DIR) / path
    return path


def _select_cohort(manifest: dict[str, Any], cohort_year: str | None = None) -> dict[str, Any] | None:
    cohorts = manifest.get("cohorts", [])
    if cohort_year:
        target = str(cohort_year)
        for cohort in cohorts:
            if str(cohort.get("year")) == target:
                return cohort
        raise ValueError(f"Cohort '{cohort_year}' was not found in the manifest.")

    for cohort in cohorts:
        if cohort.get("status", "").strip() in RUNNABLE_COHORT_STATUSES:
            return cohort
    return None


def _count_row_stats(rows: list[dict[str, str]]) -> tuple[int, int]:
    done_statuses = {config.STATUS_EMAIL_DONE, config.STATUS_SENT}
    rows_done = 0
    rows_sent = 0
    for row in rows:
        status = row.get("STATUS", "").strip()
        sent = row.get("Sent", "").strip() == "YES"
        if status in done_statuses or sent:
            rows_done += 1
        if sent or status == config.STATUS_SENT:
            rows_sent += 1
    return rows_done, rows_sent


def _derive_cohort_status(
    current_status: str,
    total_rows: int,
    rows_phase2_done: int,
    rows_sent: int,
) -> str:
    if total_rows <= 0:
        return COHORT_STATUS_PENDING
    if rows_sent >= total_rows:
        return COHORT_STATUS_SENT
    if rows_sent > 0:
        if current_status == COHORT_STATUS_QUOTA_REACHED:
            return COHORT_STATUS_QUOTA_REACHED
        return COHORT_STATUS_SENDING
    if rows_phase2_done >= total_rows:
        return COHORT_STATUS_PHASE2_DONE
    if rows_phase2_done > 0:
        return COHORT_STATUS_PHASE2_IN_PROGRESS
    return COHORT_STATUS_PHASE1_DONE


def _find_next_start_row(tab_name: str, rows: list[dict[str, str]] | None = None) -> int | None:
    if rows is None:
        rows = read_all_rows(tab_name=tab_name)
    done_statuses = {config.STATUS_EMAIL_DONE, config.STATUS_SENT}
    for row_index, row in enumerate(rows, start=2):
        if row.get("STATUS", "").strip() not in done_statuses and row.get("Sent", "").strip() != "YES":
            return row_index
    return None


def _update_cohort_progress(cohort: dict[str, Any]) -> tuple[int, int, int, list[dict[str, str]]]:
    rows = read_all_rows(tab_name=cohort["tab_name"])
    rows_done, rows_sent = _count_row_stats(rows)
    total_rows = len(rows)
    cohort["rows_phase2_done"] = rows_done
    cohort["rows_sent"] = rows_sent
    cohort["total_rows"] = total_rows
    cohort["status"] = _derive_cohort_status(
        str(cohort.get("status") or COHORT_STATUS_PENDING).strip(),
        total_rows,
        rows_done,
        rows_sent,
    )
    return rows_done, rows_sent, total_rows, rows


def sync_manifest(cohort_year: str | None = None) -> dict[str, Any]:
    """Recompute manifest progress from the current sheet tabs."""
    manifest = load_manifest()
    synced_cohorts = []

    for cohort in manifest.get("cohorts", []):
        if cohort_year and str(cohort.get("year")) != str(cohort_year):
            continue
        cohort_tab_name = cohort.get("tab_name", "")
        if not tab_exists(cohort_tab_name):
            print(f"[Manifest Sync] Skipping {cohort_tab_name} — tab does not exist")
            continue
        rows_done, rows_sent, total_rows = _update_cohort_progress(cohort)
        synced_cohorts.append(
            {
                "year": str(cohort.get("year")),
                "tab_name": cohort.get("tab_name", ""),
                "status": cohort.get("status", COHORT_STATUS_PENDING),
                "total_rows": total_rows,
                "rows_phase2_done": rows_done,
                "rows_sent": rows_sent,
            }
        )

    if cohort_year and not synced_cohorts:
        raise ValueError(f"Cohort '{cohort_year}' was not found in the manifest.")

    save_manifest(manifest)

    print("[Manifest Sync] Updated cohort progress from sheet tabs:")
    for cohort in synced_cohorts:
        print(
            "  - "
            f"{cohort['year']} ({cohort['tab_name']}): "
            f"status={cohort['status']}, "
            f"rows_phase2_done={cohort['rows_phase2_done']}/{cohort['total_rows']}, "
            f"rows_sent={cohort['rows_sent']}"
        )

    return manifest


def run_cohort(cohort_year: str | None = None) -> dict[str, Any] | None:
    """Run Phase 1 and Phase 2 for a selected manifest cohort."""
    from phase1_contact_resolution import run_phase1
    from phase2_orchestrator import run_phase2

    manifest = load_manifest()
    cohort = _select_cohort(manifest, cohort_year=cohort_year)
    if cohort is None:
        print("[Cohort Runner] No runnable cohort found. All cohorts are phase2_done or sent.")
        return None

    year = str(cohort["year"])
    tab_name = cohort["tab_name"]
    print(f"[Cohort Runner] Selected cohort {year} -> tab {tab_name}")

    if cohort.get("status", "").strip() == COHORT_STATUS_SENT:
        print(f"[Cohort Runner] Cohort {year} is already marked sent.")
        return dict(cohort)

    if cohort.get("status", "").strip() == COHORT_STATUS_PENDING:
        run_phase1(csv_path=str(_resolve_csv_path(cohort["csv_path"])), tab_name=tab_name)
        cohort["total_rows"] = len(read_all_rows(tab_name=tab_name))
        cohort["rows_phase2_done"] = 0
        cohort["rows_sent"] = 0
        cohort["status"] = COHORT_STATUS_PHASE1_DONE
        save_manifest(manifest)
        if cohort["total_rows"] == 0:
            print(f"[Cohort Runner] Cohort {year} has no rows after Phase 1. Skipping Phase 2.")
            cohort["status"] = COHORT_STATUS_PHASE2_DONE
            save_manifest(manifest)
            return dict(cohort)

    if cohort.get("status", "").strip() == COHORT_STATUS_PHASE2_DONE:
        print(f"[Cohort Runner] Cohort {year} already completed Phase 2.")
        return dict(cohort)

    while cohort.get("status", "").strip() in {COHORT_STATUS_PHASE1_DONE, COHORT_STATUS_PHASE2_IN_PROGRESS}:
        rows_done, _, total_rows, rows = _update_cohort_progress(cohort)
        save_manifest(manifest)

        if total_rows and rows_done >= total_rows:
            print(f"[Cohort Runner] Cohort {year} is fully processed for Phase 2.")
            break

        start_row = _find_next_start_row(tab_name, rows=rows)
        if start_row is None:
            cohort["status"] = COHORT_STATUS_PHASE2_DONE
            save_manifest(manifest)
            print(f"[Cohort Runner] Cohort {year} has no remaining Phase 2 rows.")
            break

        before_done = rows_done
        cohort["status"] = COHORT_STATUS_PHASE2_IN_PROGRESS
        save_manifest(manifest)
        print(
            f"[Cohort Runner] Running Phase 2 for cohort {year}: "
            f"tab={tab_name}, start_row={start_row}, count={config.COHORT_PHASE2_BATCH_SIZE}"
        )
        run_phase2(
            source="sheet",
            limit=config.COHORT_PHASE2_BATCH_SIZE,
            start_row=start_row,
            tab_name=tab_name,
        )

        rows_done, _, total_rows, _rows = _update_cohort_progress(cohort)
        save_manifest(manifest)
        if total_rows and rows_done >= total_rows:
            print(f"[Cohort Runner] Cohort {year} completed Phase 2.")
            break

        if rows_done == before_done:
            print(
                f"[Cohort Runner] No Phase 2 progress detected for cohort {year}; "
                "leaving manifest at phase2_in_progress for manual follow-up."
            )
            break

    return dict(cohort)


if __name__ == "__main__":
    import sys

    _year = sys.argv[1] if len(sys.argv) > 1 else None
    run_cohort(cohort_year=_year)
