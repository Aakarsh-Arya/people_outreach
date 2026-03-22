"""Duplicate email detection for alumni rows."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher


def detect_duplicates(rows: list[dict[str, str]]) -> dict[str, list[int]]:
    """Return a map of email -> [row_indices] for emails appearing more than once.

    Row indices are 0-based (matching SheetCache row indices).
    """
    email_to_indices: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        email = (row.get("Email", "") or "").strip().lower()
        if email:
            email_to_indices[email].append(index)
    return {email: indices for email, indices in email_to_indices.items() if len(indices) > 1}


def detect_fuzzy_name_duplicates(
    rows: list[dict[str, str]],
    threshold: float = 0.85,
) -> list[tuple[int, int, float]]:
    """Return pairs of row indices whose names are suspiciously similar.

    Each tuple is (index_a, index_b, similarity_ratio).  Only pairs at or
    above *threshold* are returned.  Designed for small-ish datasets (< 500
    rows per cohort) where an O(n^2) comparison is acceptable.
    """
    names: list[tuple[int, str]] = []
    for index, row in enumerate(rows):
        name = (row.get("Name", "") or "").strip().lower()
        if name:
            names.append((index, name))

    duplicates: list[tuple[int, int, float]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ratio = SequenceMatcher(None, names[i][1], names[j][1]).ratio()
            if ratio >= threshold:
                print(
                    f"  [Dedupe] Fuzzy match: rows {names[i][0] + 2} & {names[j][0] + 2} "
                    f"'{names[i][1]}' ~ '{names[j][1]}' (similarity={ratio:.3f})"
                )
                duplicates.append((names[i][0], names[j][0], round(ratio, 3)))
    return duplicates


