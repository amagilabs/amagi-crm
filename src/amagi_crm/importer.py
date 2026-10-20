"""CSV contact import with forgiving header matching.

Takes the column names most CRMs export (including GoHighLevel's contact
export: "First Name", "Last Name", "Email", "Phone", "Business Name", "Tags").
Unrecognized columns are reported and ignored.
"""

from __future__ import annotations

import csv
import io
import re

ALIASES = {
    "first_name": {"first name", "firstname", "first", "given name"},
    "last_name": {"last name", "lastname", "last", "surname", "family name"},
    "full_name": {"name", "full name", "contact name"},
    "email": {"email", "email address", "e-mail", "e-mail address", "primary email"},
    "phone": {"phone", "phone number", "mobile", "mobile phone", "cell", "primary phone"},
    "company": {"company", "company name", "business name", "organization", "organisation", "account name"},
    "tags": {"tags", "tag", "labels"},
    "notes": {"notes", "note", "description", "comments"},
}


def _norm(header: str) -> str:
    return re.sub(r"[\s_]+", " ", header.replace("﻿", "").strip().lower())


def map_headers(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return ({csv_header: field}, [ignored headers])."""
    mapping, ignored = {}, []
    taken: set[str] = set()
    for h in headers:
        field = next((f for f, names in ALIASES.items() if _norm(h) in names), None)
        if field and field not in taken:
            mapping[h] = field
            taken.add(field)
        else:
            ignored.append(h)
    return mapping, ignored


def read_contacts_csv(text: str) -> tuple[list[tuple[int, dict]], list[str]]:
    """Parse CSV text into ``[(line_number, contact_fields)]`` plus ignored headers."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], []
    mapping, ignored = map_headers(reader.fieldnames)
    rows = []
    for row in reader:
        fields: dict = {}
        for header, field in mapping.items():
            value = (row.get(header) or "").strip()
            if value:
                fields[field] = value
        full = fields.pop("full_name", None)
        if full and not (fields.get("first_name") or fields.get("last_name")):
            first, _, last = full.partition(" ")
            fields["first_name"] = first
            if last:
                fields["last_name"] = last
        if "tags" in fields:
            fields["tags"] = [t.strip() for t in re.split(r"[,;|]", fields["tags"]) if t.strip()]
        if fields:
            rows.append((reader.line_num, fields))
    return rows, ignored
