"""Probe the transient PROCESSING status on a single cohort_2017 row."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from sheets_helper import initialize_sheet, read_all_rows, update_row_multiple

TAB_NAME = "cohort_2017"
SHEET_ROW = 108
ROW_INDEX = SHEET_ROW - 2
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "debug_logs" / "phase2_processing_probe_108.txt"

RESET_UPDATES = {
    "STATUS": "PENDING",
    "Subject": "",
    "Body": "",
    "Verified_Company": "",
    "Enrichment_Notes": "",
    "Enrichment_Source": "",
    "Confidence_Level": "",
    "Tavily_Raw": "",
    "Tavily_Metadata": "",
}


def main() -> None:
    initialize_sheet(TAB_NAME)
    update_row_multiple(ROW_INDEX, RESET_UPDATES, tab_name=TAB_NAME)
    print(f"reset_row={SHEET_ROW} status=PENDING")

    command = [
        str(Path(__file__).resolve()),
    ]
    python_exe = str(Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe")
    batch_command = [
        python_exe,
        "main.py",
        "phase2-sheet",
        "1",
        str(SHEET_ROW),
        "--tab",
        TAB_NAME,
        "--force",
    ]

    with LOG_PATH.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            batch_command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )

    seen_processing = False
    seen_research_done = False
    seen_email_done = False
    history: list[str] = []

    while proc.poll() is None:
        rows = read_all_rows(tab_name=TAB_NAME)
        status = rows[ROW_INDEX].get("STATUS", "")
        history.append(status)
        print(f"poll_status={status}")
        if status == "PROCESSING":
            seen_processing = True
        if status == "RESEARCH_DONE":
            seen_research_done = True
        if status == "EMAIL_DONE":
            seen_email_done = True
        time.sleep(0.5)

    rows = read_all_rows(tab_name=TAB_NAME)
    final_status = rows[ROW_INDEX].get("STATUS", "")
    print(f"process_exit={proc.returncode}")
    print(f"seen_processing={seen_processing}")
    print(f"seen_research_done={seen_research_done}")
    print(f"seen_email_done={seen_email_done}")
    print(f"final_status={final_status}")
    print("history=" + ",".join(history))


if __name__ == "__main__":
    main()