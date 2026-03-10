"""
Google Sheets helper.
Read/write operations on the master Google Sheet, including schema migration.
"""

import config
from google_auth_helper import get_sheets_service


def _get_sheet():
    spreadsheet_id = config.require_google_sheet_id()
    service = get_sheets_service()
    return service.spreadsheets(), spreadsheet_id


def _get_sheet_properties(sheet, spreadsheet_id):
    metadata = sheet.get(spreadsheetId=spreadsheet_id).execute()
    for worksheet in metadata.get("sheets", []):
        props = worksheet.get("properties", {})
        if props.get("title") == config.SHEET_NAME:
            return props
    raise ValueError(f"Sheet '{config.SHEET_NAME}' was not found in the spreadsheet.")


def _get_headers(sheet, spreadsheet_id):
    result = sheet.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{config.SHEET_NAME}!1:1",
    ).execute()
    return result.get("values", [[]])[0]


def _set_headers(sheet, spreadsheet_id, headers):
    sheet.values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{config.SHEET_NAME}!A1",
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


def _resolve_insert_index(current_headers, desired_header):
    desired_index = config.SHEET_HEADERS.index(desired_header)
    for next_header in config.SHEET_HEADERS[desired_index + 1:]:
        if next_header in current_headers:
            return current_headers.index(next_header)
    return len(current_headers)


def initialize_sheet():
    """Ensure the expected headers exist and migrate old schema in place when possible."""
    sheet, spreadsheet_id = _get_sheet()
    existing = _get_headers(sheet, spreadsheet_id)

    if not existing:
        _set_headers(sheet, spreadsheet_id, config.SHEET_HEADERS)
        print(f"[Sheets] Headers written: {config.SHEET_HEADERS}")
        return

    headers = existing[:]
    changed = False

    if "Current_Company" in headers and "AlmaConnect_Company" not in headers:
        idx = headers.index("Current_Company")
        headers[idx] = "AlmaConnect_Company"
        changed = True
        print("[Sheets] Migrated header: Current_Company -> AlmaConnect_Company")

    if changed:
        _set_headers(sheet, spreadsheet_id, headers)

    missing_headers = [header for header in config.SHEET_HEADERS if header not in headers]
    if missing_headers:
        props = _get_sheet_properties(sheet, spreadsheet_id)
        sheet_gid = props["sheetId"]

        for header in missing_headers:
            insert_index = _resolve_insert_index(headers, header)
            _insert_column(sheet, spreadsheet_id, sheet_gid, insert_index)
            cell = f"{config.SHEET_NAME}!{_column_letter(insert_index)}1"
            sheet.values().update(
                spreadsheetId=spreadsheet_id,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[header]]},
            ).execute()
            headers.insert(insert_index, header)

            if header == "Verified_Company":
                print("[Sheets] Migrated header: added Verified_Company column")
            elif header == "Confidence_Level":
                print("[Sheets] Migrated header: added Confidence_Level column")
            else:
                print(f"[Sheets] Migrated header: added {header} column")

    current_headers = _get_headers(sheet, spreadsheet_id)
    if current_headers[: len(config.SHEET_HEADERS)] == config.SHEET_HEADERS:
        print("[Sheets] Headers already present.")
    elif current_headers == config.SHEET_HEADERS:
        print("[Sheets] Headers already present.")
    else:
        print(f"[Sheets] Active headers: {current_headers}")


def read_all_rows():
    """Read all data rows from the sheet (excluding header). Returns list of dicts."""
    sheet, spreadsheet_id = _get_sheet()
    result = sheet.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{config.SHEET_NAME}",
    ).execute()

    values = result.get("values", [])
    if len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for row_data in values[1:]:
        padded = row_data + [""] * (len(headers) - len(row_data))
        rows.append(dict(zip(headers, padded)))
    return rows


def append_rows(rows_dicts):
    """Append rows to the sheet using the sheet's active header order."""
    sheet, spreadsheet_id = _get_sheet()
    headers = _get_headers(sheet, spreadsheet_id)
    if not headers:
        raise ValueError("Sheet headers are missing. Call initialize_sheet() before appending rows.")

    values = []
    for row in rows_dicts:
        values.append([row.get(header, "") for header in headers])

    sheet.values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{config.SHEET_NAME}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    print(f"[Sheets] Appended {len(values)} rows.")


def update_row(row_index, column_name, value):
    """
    Update a single cell. row_index is 0-based data row index (row 0 = sheet row 2).
    """
    sheet, spreadsheet_id = _get_sheet()
    headers = _get_headers(sheet, spreadsheet_id)
    if column_name not in headers:
        raise ValueError(f"Column '{column_name}' was not found in the sheet headers.")

    col_idx = headers.index(column_name)
    col_letter = _column_letter(col_idx)
    cell = f"{config.SHEET_NAME}!{col_letter}{row_index + 2}"

    sheet.values().update(
        spreadsheetId=spreadsheet_id,
        range=cell,
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def update_row_multiple(row_index, updates: dict):
    """
    Update multiple columns in one row.
    updates: {column_name: value, ...}
    row_index: 0-based data row index.
    """
    sheet, spreadsheet_id = _get_sheet()
    headers = _get_headers(sheet, spreadsheet_id)

    data = []
    for col_name, value in updates.items():
        if col_name not in headers:
            raise ValueError(f"Column '{col_name}' was not found in the sheet headers.")
        col_idx = headers.index(col_name)
        col_letter = _column_letter(col_idx)
        cell = f"{config.SHEET_NAME}!{col_letter}{row_index + 2}"
        data.append({"range": cell, "values": [[value]]})

    sheet.values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
