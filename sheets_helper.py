"""
Google Sheets helper.
Read/write operations on the master Google Sheet, including schema migration,
safe empty-tab reads, and header-aware row materialization.
"""

from __future__ import annotations

import asyncio

import config
from google_auth_helper import get_sheets_service
from utils.retry import retry_with_backoff

_CACHE_BY_TAB: dict[str, "SheetCache"] = {}


def _tab_name(tab_name: str | None = None) -> str:
    return tab_name or config.SHEET_NAME


def _sheet_range(tab_name: str | None = None, cell_range: str = "") -> str:
    active_tab = _tab_name(tab_name)
    return f"{active_tab}!{cell_range}" if cell_range else active_tab


def _get_sheet():
    spreadsheet_id = config.require_google_sheet_id()
    service = get_sheets_service()
    return service.spreadsheets(), spreadsheet_id


def _find_sheet_properties(metadata, tab_name: str):
    for worksheet in metadata.get("sheets", []):
        props = worksheet.get("properties", {})
        if props.get("title") == tab_name:
            return props
    return None


def _get_sheet_properties(sheet, spreadsheet_id, tab_name: str | None = None):
    metadata = sheet.get(spreadsheetId=spreadsheet_id).execute()
    active_tab = _tab_name(tab_name)
    props = _find_sheet_properties(metadata, active_tab)
    if props:
        return props
    raise ValueError(f"Sheet '{active_tab}' was not found in the spreadsheet.")


def _get_headers(sheet, spreadsheet_id, tab_name: str | None = None):
    result = sheet.values().get(
        spreadsheetId=spreadsheet_id,
        range=_sheet_range(tab_name, "1:1"),
    ).execute()
    return result.get("values", [[]])[0]


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _add_sheet_tab(sheet, spreadsheet_id, tab_name: str):
    sheet.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": tab_name,
                        }
                    }
                }
            ]
        },
    ).execute()


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _set_headers(sheet, spreadsheet_id, headers, tab_name: str | None = None):
    sheet.values().update(
        spreadsheetId=spreadsheet_id,
        range=_sheet_range(tab_name, "A1"),
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()


def _column_letter(index):
    index += 1
    letters = []
    while index:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _insert_column(sheet, spreadsheet_id, sheet_gid, column_index):
    sheet.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_gid,
                            "dimension": "COLUMNS",
                            "startIndex": column_index,
                            "endIndex": column_index + 1,
                        },
                        "inheritFromBefore": column_index > 0,
                    }
                }
            ]
        },
    ).execute()


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _batch_update_values(sheet, spreadsheet_id, data):
    sheet.values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _append_values(sheet, spreadsheet_id, tab_name: str, values):
    sheet.values().append(
        spreadsheetId=spreadsheet_id,
        range=_sheet_range(tab_name, "A1"),
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _clear_values(sheet, spreadsheet_id, range_name: str):
    sheet.values().clear(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        body={},
    ).execute()


def _resolve_insert_index(current_headers, desired_header):
    desired_index = config.SHEET_HEADERS.index(desired_header)
    for next_header in config.SHEET_HEADERS[desired_index + 1:]:
        if next_header in current_headers:
            return current_headers.index(next_header)
    return len(current_headers)


class SheetCache:
    """In-memory cache of a sheet tab with write-through helpers."""

    def __init__(self, tab_name: str | None = None, *, create_if_missing: bool = True):
        self.tab_name = _tab_name(tab_name)
        self.headers: list[str] = []
        self.rows: dict[int, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._create_if_missing = create_if_missing

    def load(self, tab_name: str | None = None) -> "SheetCache":
        if tab_name:
            self.tab_name = _tab_name(tab_name)

        ensure_tab_exists(self.tab_name, create_if_missing=self._create_if_missing)

        sheet, spreadsheet_id = _get_sheet()
        result = sheet.values().get(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(self.tab_name),
        ).execute()

        values = result.get("values", [])
        self.headers = values[0] if values else config.SHEET_HEADERS[:]
        self.rows = {}

        if len(values) < 2:
            return self

        for row_index, row_data in enumerate(values[1:]):
            padded = row_data + [""] * (len(self.headers) - len(row_data))
            self.rows[row_index] = dict(zip(self.headers, padded))
        return self

    def get_row(self, row_index: int) -> dict[str, str]:
        if row_index not in self.rows:
            self.rows[row_index] = {header: "" for header in self.headers}
        return self.rows[row_index]

    def set_row_status(self, row_index: int, status: str) -> None:
        self.write_row(row_index, {"STATUS": status})

    def write_row(self, row_index: int, data_dict: dict[str, str]) -> None:
        sheet, spreadsheet_id = _get_sheet()
        if not self.headers:
            self.load(self.tab_name)

        data = []
        for col_name, value in data_dict.items():
            if col_name not in self.headers:
                raise ValueError(f"Column '{col_name}' was not found in the sheet headers.")
            col_idx = self.headers.index(col_name)
            col_letter = _column_letter(col_idx)
            cell = f"{self.tab_name}!{col_letter}{row_index + 2}"
            data.append({"range": cell, "values": [[value]]})

        _batch_update_values(sheet, spreadsheet_id, data)
        row = self.get_row(row_index)
        row.update({key: "" if value is None else str(value) for key, value in data_dict.items()})


def _invalidate_cache(tab_name: str | None = None) -> None:
    _CACHE_BY_TAB.pop(_tab_name(tab_name), None)


def _get_default_cache(tab_name: str | None = None, *, force_reload: bool = False) -> SheetCache:
    active_tab = _tab_name(tab_name)
    cache = _CACHE_BY_TAB.get(active_tab)
    if cache is None or force_reload:
        cache = SheetCache(active_tab).load(active_tab)
        _CACHE_BY_TAB[active_tab] = cache
    return cache


def initialize_sheet(tab_name: str | None = None, create_if_missing: bool = True):
    """Ensure the expected headers exist and migrate old schema in place when possible."""
    active_tab = _tab_name(tab_name)
    ensure_tab_exists(active_tab, create_if_missing=create_if_missing)
    sheet, spreadsheet_id = _get_sheet()
    existing = _get_headers(sheet, spreadsheet_id, active_tab)

    if not existing:
        _set_headers(sheet, spreadsheet_id, config.SHEET_HEADERS, active_tab)
        print(f"[Sheets] Headers written: {config.SHEET_HEADERS}")
        _invalidate_cache(active_tab)
        return

    headers = existing[:]
    changed = False

    if "Current_Company" in headers and "AlmaConnect_Company" not in headers:
        idx = headers.index("Current_Company")
        headers[idx] = "AlmaConnect_Company"
        changed = True
        print("[Sheets] Migrated header: Current_Company -> AlmaConnect_Company")

    if changed:
        _set_headers(sheet, spreadsheet_id, headers, active_tab)

    missing_headers = [header for header in config.SHEET_HEADERS if header not in headers]
    if missing_headers:
        props = _get_sheet_properties(sheet, spreadsheet_id, active_tab)
        sheet_gid = props["sheetId"]

        for header in missing_headers:
            insert_index = _resolve_insert_index(headers, header)
            _insert_column(sheet, spreadsheet_id, sheet_gid, insert_index)
            cell = f"{active_tab}!{_column_letter(insert_index)}1"
            _batch_update_values(sheet, spreadsheet_id, [{"range": cell, "values": [[header]]}])
            headers.insert(insert_index, header)

            if header == "Verified_Company":
                print("[Sheets] Migrated header: added Verified_Company column")
            elif header == "Confidence_Level":
                print("[Sheets] Migrated header: added Confidence_Level column")
            else:
                print(f"[Sheets] Migrated header: added {header} column")

    current_headers = _get_headers(sheet, spreadsheet_id, active_tab)
    if current_headers[: len(config.SHEET_HEADERS)] == config.SHEET_HEADERS:
        print("[Sheets] Headers already present.")
    elif current_headers == config.SHEET_HEADERS:
        print("[Sheets] Headers already present.")
    else:
        print(f"[Sheets] Active headers: {current_headers}")

    _invalidate_cache(active_tab)


def tab_exists(tab_name: str) -> bool:
    """Return True if the named tab exists in the spreadsheet, without creating it."""
    sheet, spreadsheet_id = _get_sheet()
    metadata = sheet.get(spreadsheetId=spreadsheet_id).execute()
    return _find_sheet_properties(metadata, tab_name) is not None


def ensure_tab_exists(tab_name: str | None = None, *, create_if_missing: bool = True):
    """Create the sheet tab if it does not already exist."""
    active_tab = _tab_name(tab_name)
    sheet, spreadsheet_id = _get_sheet()
    metadata = sheet.get(spreadsheetId=spreadsheet_id).execute()
    props = _find_sheet_properties(metadata, active_tab)
    if props:
        return props

    if not create_if_missing:
        raise ValueError(f"Tab '{active_tab}' does not exist and create_if_missing=False")

    _add_sheet_tab(sheet, spreadsheet_id, active_tab)
    print(f"[Sheets] Created tab: {active_tab}")
    metadata = sheet.get(spreadsheetId=spreadsheet_id).execute()
    props = _find_sheet_properties(metadata, active_tab)
    if not props:
        raise ValueError(f"Sheet '{active_tab}' could not be created.")

    _invalidate_cache(active_tab)
    return props


def read_all_rows(tab_name: str | None = None):
    """Read all data rows from the sheet, excluding the header.

    Returns an empty list for empty tabs instead of raising.
    """
    cache = _get_default_cache(tab_name, force_reload=True)
    if not cache.rows:
        return []
    return [cache.get_row(index).copy() for index in sorted(cache.rows)]


def append_rows(rows_dicts, tab_name: str | None = None):
    """Append rows to the sheet using the sheet's active header order."""
    active_tab = _tab_name(tab_name)
    sheet, spreadsheet_id = _get_sheet()
    headers = _get_headers(sheet, spreadsheet_id, active_tab)
    if not headers:
        raise ValueError("Sheet headers are missing. Call initialize_sheet() before appending rows.")

    values = []
    for row in rows_dicts:
        values.append([row.get(header, "") for header in headers])

    _append_values(sheet, spreadsheet_id, active_tab, values)
    _invalidate_cache(active_tab)
    print(f"[Sheets] Appended {len(values)} rows.")


def clear_tab_data(tab_name: str | None = None) -> None:
    """Clear all data rows from a tab while preserving the header row."""
    active_tab = _tab_name(tab_name)
    sheet, spreadsheet_id = _get_sheet()
    headers = _get_headers(sheet, spreadsheet_id, active_tab)
    if not headers:
        return

    last_column = _column_letter(len(headers) - 1)
    _clear_values(sheet, spreadsheet_id, _sheet_range(active_tab, f"A2:{last_column}"))
    _invalidate_cache(active_tab)


def update_row_multiple(row_index, updates: dict, tab_name: str | None = None):
    """
    Update multiple columns in one row.
    updates: {column_name: value, ...}
    row_index: 0-based data row index.
    """
    cache = _get_default_cache(tab_name)
    cache.write_row(row_index, updates)


def batch_write_rows(updates_by_row: list[tuple[int, dict, str | None]]) -> None:
    """
    Write multiple rows in batched API calls grouped by tab.
    updates_by_row: list of (row_index, data_dict, tab_name) tuples.
    row_index: 0-based data row index (row 0 = sheet row 2).
    """
    if not updates_by_row:
        return

    grouped: dict[str, list[tuple[int, dict]]] = {}
    for row_index, data_dict, tab_name in updates_by_row:
        active_tab = _tab_name(tab_name)
        grouped.setdefault(active_tab, []).append((row_index, data_dict))

    sheet, spreadsheet_id = _get_sheet()
    total_rows = 0
    total_tabs = len(grouped)

    for active_tab, row_updates in grouped.items():
        cache = _get_default_cache(active_tab)
        if not cache.headers:
            cache.load(active_tab)

        data = []
        for row_index, data_dict in row_updates:
            for col_name, value in data_dict.items():
                if col_name not in cache.headers:
                    raise ValueError(f"Column '{col_name}' was not found in the sheet headers.")
                col_idx = cache.headers.index(col_name)
                col_letter = _column_letter(col_idx)
                cell = f"{active_tab}!{col_letter}{row_index + 2}"
                data.append({"range": cell, "values": [[value]]})

        _batch_update_values(sheet, spreadsheet_id, data)

        for row_index, data_dict in row_updates:
            row = cache.get_row(row_index)
            row.update({key: "" if value is None else str(value) for key, value in data_dict.items()})

        total_rows += len(row_updates)

    api_call_label = "1 API call" if total_tabs == 1 else f"{total_tabs} API calls"
    print(f"[Sheets] Batch wrote {total_rows} rows across {total_tabs} tabs in {api_call_label}.")
