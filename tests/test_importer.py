from amagi_crm.importer import map_headers, read_contacts_csv


def test_common_export_headers_map():
    mapping, ignored = map_headers(["First Name", "LAST_NAME", "E-mail Address", "Mobile Phone",
                                    "Business Name", "Tags", "Lead Score"])
    assert mapping == {"First Name": "first_name", "LAST_NAME": "last_name", "E-mail Address": "email",
                       "Mobile Phone": "phone", "Business Name": "company", "Tags": "tags"}
    assert ignored == ["Lead Score"]


def test_second_column_for_the_same_field_is_ignored():
    mapping, ignored = map_headers(["Email", "Primary Email"])
    assert mapping == {"Email": "email"} and ignored == ["Primary Email"]


def test_full_name_split_tags_split_and_bom():
    text = "﻿Name,Email,Tags,Notes\nAmara Osei,amara@example.com,\"vip; client|referral\",\n,,,\nPrince,,,walk-in\n"
    rows, ignored = read_contacts_csv(text)
    assert ignored == []
    assert rows[0] == (2, {"first_name": "Amara", "last_name": "Osei", "email": "amara@example.com",
                           "tags": ["vip", "client", "referral"]})
    assert rows[1] == (4, {"first_name": "Prince", "notes": "walk-in"})  # blank row 3 skipped
    assert len(rows) == 2


def test_empty_file():
    assert read_contacts_csv("") == ([], [])
