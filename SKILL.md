---
name: amagi-crm
description: Operate a self-hosted amagi-crm instance from the command line - set it up, import contacts from a CSV export (GoHighLevel-style headers work), add and move deals on the pipeline, add and complete tasks, book appointments without double-booking, read the day's agenda and open slots, print the weekly pipeline summary, verify the tamper-evident audit log, and rotate encryption keys. Use when the user wants to manage contacts, deals, tasks or appointments in amagi-crm, migrate contacts into it, check its audit log, or set it up locally or with Docker Compose.
---

# amagi-crm

A small-firm CRM that runs on your own machine or server: contacts, a Kanban deal pipeline,
tasks, and appointment booking, with a web UI, a REST API and this CLI. Contact details,
deal and task text and appointment details are encrypted in the database; every change, read
and export lands in an HMAC-chained audit log.

## When to use this skill

- "Set up a CRM for the office" / "import these contacts from our old CRM's export"
- "Add a deal for Harbor Dental, $4,500" / "move deal 3 to Won" / "what does the pipeline look like?"
- "Remind me to send the engagement letter tomorrow" / "what's due?"
- "Book a 30-minute consult Monday at 10" / "what's open on Thursday?"
- "Has anyone tampered with the audit log?" / "rotate the encryption key"

## Commands

Run from the folder that holds the instance's `.env` (or pass `--env-file PATH` before the
command). Every command except `init` and `keygen` refuses to run without the keys in `.env`.

```
amagi-crm init --timezone America/New_York --hours "mon-fri 09:00-17:00"   # writes .env, prints the admin token once
amagi-crm import contacts.csv --dry-run        # check the column mapping first
amagi-crm import contacts.csv                  # duplicates (by email) are skipped, bad rows reported by line
amagi-crm contacts --search harbor             # also --tag vip, --limit N
amagi-crm contacts add --first-name Dana --last-name Whitfield --email dana@example.com --tags vip
amagi-crm deals add "Website rebuild" --amount 4500 --contact-email dana@example.com
amagi-crm deals board                          # columns with counts and totals
amagi-crm deals move 3 Won                     # stage names are case-insensitive
amagi-crm deals summary                        # this week: new, won, lost, open pipeline (--json available)
amagi-crm tasks add "Send engagement letter" --due tomorrow --contact-email dana@example.com
amagi-crm tasks                                # open tasks, overdue flagged; --all includes done
amagi-crm tasks done 4
amagi-crm book 2026-11-02T10:00 --title "Consult" --minutes 30 --contact-email dana@example.com
amagi-crm agenda --date 2026-11-02             # appointments, tasks due, open slots
amagi-crm audit verify                         # exit 1 if any entry was edited, removed or reordered
amagi-crm audit tail -n 20
amagi-crm user add sam --role member           # roles: admin, member, readonly; prints the token once
amagi-crm user feed-token sam --base-url https://crm.example.com   # private .ics calendar link
amagi-crm serve                                # web UI + REST API on http://127.0.0.1:8000 (docs at /docs)
```

Times without an offset are read in the firm's time zone (`AMAGI_CRM_TIMEZONE`).

## Rules for the agent

- **Tokens are shown once.** When `init`, `user add` or `user feed-token` prints a token, pass it
  to the user and tell them to store it; don't write it into files, notes or commit messages.
- **Exports are decrypted.** `amagi-crm export` writes plain CSV. Only run it when the user asks,
  write it to a path they choose, and say that the file holds readable client data.
- **Never edit `.env` keys casually.** Removing a key from `AMAGI_CRM_ENCRYPTION_KEYS` makes any
  row still encrypted under it unreadable. To rotate: put a new key first
  (`amagi-crm keygen --key-only`), keep the old one after it, run `amagi-crm rotate-keys`, confirm
  it reports 0 on a second run, and only then drop the old key, after a backup.
- **A failed `audit verify` is an incident, not a glitch.** Report the entry number and reason
  verbatim and don't "fix" the log. Tell the user to keep the printed head hash somewhere off
  the server: the chain alone can't show that the newest entries were deleted.
- **Booking conflicts are real.** If `book` says the time overlaps, offer the open slots from
  `agenda` instead of retrying a nearby time.

## Docker Compose (PostgreSQL)

```
cp .env.example .env
docker build -t amagi-crm . && docker run --rm amagi-crm keygen --postgres >> .env
docker compose up -d
docker compose exec app amagi-crm user add admin --role admin
```

The app binds to 127.0.0.1. Put a TLS reverse proxy in front before anyone reaches it over a network.

## Known limitations (tell the user, don't hide them)

- One firm, one time zone, one shared calendar. No per-user calendars, no recurring appointments.
- Search runs over decrypted rows in the app, because encrypted columns can't be searched in SQL.
  Fine for tens of thousands of contacts, not millions.
- No email or SMS sending, no two-way calendar sync (the .ics feed is read-only), no voice layer.
- Encryption protects the database and its backups. It does not protect a server where an
  attacker can read `.env` or the running process's memory.
- Not a compliance certification. It supplies building blocks (encryption at rest, audit trail,
  roles); whether a deployment meets a given bar rule or regulation is for the firm to decide.
