"""End-to-end CLI runs in a scratch folder, the way a user would run them."""

import os
import sqlite3
import subprocess
import sys

import pytest

from amagi_crm.cli import main


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("AMAGI_CRM_") or name == "DATABASE_URL":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_help_runs_as_a_module():
    r = subprocess.run([sys.executable, "-m", "amagi_crm", "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "usage: amagi-crm" in r.stdout and "rotate-keys" in r.stdout


def test_refuses_to_run_without_keys(workdir, capsys):
    assert main(["contacts"]) == 2
    assert "AMAGI_CRM_ENCRYPTION_KEYS" in capsys.readouterr().err


def test_init_rejects_a_bad_timezone_and_writes_nothing(workdir, capsys):
    assert main(["init", "--timezone", "Mars/Base"]) == 1
    assert not (workdir / ".env").exists()


def test_full_session(workdir, fixture_csv, capsys):
    assert main(["init", "--timezone", "America/New_York"]) == 0
    out = capsys.readouterr().out
    assert "acrm_" in out and (workdir / ".env").exists()
    assert main(["init"]) == 0
    assert "Already set up" in capsys.readouterr().out

    assert main(["import", str(fixture_csv), "--dry-run"]) == 0
    assert "Dry run: 10 rows" in capsys.readouterr().out
    assert main(["import", str(fixture_csv)]) == 0
    out = capsys.readouterr().out
    assert "Imported 8 contacts" in out and "1 duplicate emails skipped, 1 invalid rows" in out

    assert main(["contacts", "--search", "roof"]) == 0
    assert "Lee Park" in capsys.readouterr().out
    assert main(["contacts", "add", "--first-name", "Dana", "--email", "dana@example.com", "--tags", "vip"]) == 0
    assert main(["contacts", "add", "--first-name", "Dana", "--email", "DANA@example.com"]) == 1
    assert "already exists" in capsys.readouterr().err

    assert main(["deals", "add", "Website rebuild", "--amount", "$4,500", "--contact-email", "dana@example.com"]) == 0
    assert "($4,500)" in capsys.readouterr().out
    assert main(["deals", "move", "1", "Won"]) == 0
    assert main(["deals", "board"]) == 0
    assert "Won (1, $4,500)" in capsys.readouterr().out
    assert main(["deals", "summary", "--json"]) == 0

    assert main(["tasks", "add", "Send engagement letter", "--due", "tomorrow"]) == 0
    assert main(["tasks"]) == 0
    assert "Send engagement letter" in capsys.readouterr().out

    assert main(["book", "2030-11-04T10:00", "--title", "Kickoff"]) == 0
    assert main(["book", "2030-11-04T10:15", "--title", "Clash"]) == 1
    assert "overlaps" in capsys.readouterr().err
    assert main(["agenda", "--date", "2030-11-04"]) == 0
    out = capsys.readouterr().out
    assert "10:00-10:30  Kickoff" in out and "Open slots: 09:00, 09:30, 10:30" in out

    assert main(["export", "--out", "contacts.csv"]) == 0
    assert "dana@example.com" in (workdir / "contacts.csv").read_text(encoding="utf-8")
    assert main(["user", "add", "sam", "--role", "readonly"]) == 0
    assert main(["user", "list"]) == 0
    assert "readonly" in capsys.readouterr().out
    assert main(["rotate-keys"]) == 0

    assert main(["audit", "verify"]) == 0
    assert "Audit chain OK" in capsys.readouterr().out
    assert main(["audit", "tail", "-n", "3"]) == 0

    con = sqlite3.connect(workdir / "amagi_crm.db")
    con.execute("UPDATE audit_log SET action = 'read' WHERE id = 3")
    con.commit()
    con.close()
    assert main(["audit", "verify"]) == 1
    assert "BROKEN at entry 3" in capsys.readouterr().out


def test_keygen_prints_usable_secrets(workdir, capsys):
    assert main(["keygen", "--postgres"]) == 0
    lines = dict(l.split("=", 1) for l in capsys.readouterr().out.splitlines() if "=" in l and not l.startswith("#"))
    assert set(lines) == {"AMAGI_CRM_ENCRYPTION_KEYS", "AMAGI_CRM_SECRET_KEY", "POSTGRES_PASSWORD"}
    (workdir / ".env").write_text("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n", encoding="utf-8")
    assert main(["contacts"]) == 0  # the generated keys pass the fail-closed check
    assert "No contacts." in capsys.readouterr().out
    assert main(["keygen", "--key-only"]) == 0
    assert len(capsys.readouterr().out.strip()) == 44
