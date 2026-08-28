"""Provision real self-hosted Dovecot mailboxes for every candidate.

Each candidate address `<local>@<domain>` (from backend/data/mail_addresses.json)
becomes a row in the shared mail server's `amasmail.virtual_users` table — the
same SQL-backed Dovecot model amaskills uses. No dovecot reload, no per-box
shell-out: the Maildir is auto-created by Dovecot on first delivery. Passwords are
SHA512-CRYPT hashed (Dovecot passdb) and the plaintext is kept in a gitignored
JOBFINDER file so the CRM can authenticate SMTP submission as each candidate.

    python -m backend.tools.provision_mailboxes            # provision all
    python -m backend.tools.provision_mailboxes --only ID  # just one candidate
    python -m backend.tools.provision_mailboxes --dry-run

Self-hosted: no Mailgun, no third party. The mail lands in /var/mail/vhosts.
"""
from __future__ import annotations

import argparse
import crypt
import json
import os
import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "backend" / "data" / "profiles.json"
ADDR_FILE = ROOT / "backend" / "data" / "mail_addresses.json"
PW_FILE = ROOT / "backend" / "data" / "mailbox_passwords.json"  # gitignored
DBPASS_FILE = "/home/projects/amaskills/crm/.dbpass"  # shared mail-server DB
MAILDIR_ROOT = "/var/mail/vhosts"


def _gen_password(n: int = 14) -> str:
    cs = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(cs) for _ in range(n))


def _hash(plain: str) -> str:
    return crypt.crypt(plain, crypt.mksalt(crypt.METHOD_SHA512))


def _maildir(local: str, domain: str) -> str:
    # UNSHARDED, directly under the domain dir — matches Dovecot mail_location %d/%n.
    return f"{MAILDIR_ROOT}/{domain}/{local}"


def _ensure_maildir(maildir: str) -> None:
    """Pre-create new/cur/tmp as vmail:mail 2770 so the CRM (group `mail`) can read
    it. Dovecot otherwise creates maildirs 0710 (group cannot traverse). Needs sudo."""
    for sub in ("new", "cur", "tmp"):
        subprocess.run(["sudo", "-n", "mkdir", "-p", f"{maildir}/{sub}"],
                       capture_output=True)
    subprocess.run(["sudo", "-n", "chown", "-R", "vmail:mail", maildir],
                   capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "-R", "2770", maildir],
                   capture_output=True)


def _sql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _rows() -> list[dict]:
    profiles = {p["id"]: p for p in json.loads(PROFILES.read_text())}
    addrs = json.loads(ADDR_FILE.read_text())
    out = []
    for pid, email in addrs.items():
        p = profiles.get(pid)
        if p is None or p.get("is_sample"):
            continue
        local, _, domain = email.partition("@")
        if not domain:
            continue
        out.append({
            "id": pid, "email": email.lower(), "local": local, "domain": domain,
            "full_name": p.get("full_name") or pid,
        })
    return out


def provision_email(email: str, full_name: str = "") -> dict:
    """Provision ONE mailbox into `amasmail.virtual_users` (+ password + group-readable
    Maildir). Idempotent (INSERT IGNORE — a re-click of the same demo persona is a no-op).
    Used by the /catalog demo fill so a synthetic persona's address is a LIVE, deliverable
    mailbox. Returns {email, ok, created, error}. Best-effort by design: the caller must not
    let a provisioning failure break the fill."""
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if not (local and domain):
        return {"email": email, "ok": False, "created": False, "error": "bad address"}
    passwords = {}
    if PW_FILE.exists():
        try:
            passwords = json.loads(PW_FILE.read_text())
        except Exception:
            passwords = {}
    pw = passwords.get(email) or _gen_password()
    maildir = _maildir(local, domain)
    sql = ("INSERT IGNORE INTO virtual_users "
           "(email,domain,full_name,password_hash,password_plain,maildir) VALUES "
           "('{e}','{d}','{n}','{h}','{p}','{m}'); SELECT ROW_COUNT();".format(
               e=_sql_escape(email), d=_sql_escape(domain),
               n=_sql_escape(full_name or local), h=_sql_escape(_hash(pw)),
               p=_sql_escape(pw), m=_sql_escape(maildir)))
    try:
        dbpass = Path(DBPASS_FILE).read_text().strip()
        proc = subprocess.run(["mysql", "-N", "-uamasmail", f"-p{dbpass}", "amasmail"],
                              input=sql, text=True, capture_output=True)
    except Exception as e:
        return {"email": email, "ok": False, "created": False, "error": f"{type(e).__name__}: {e}"}
    if proc.returncode != 0:
        return {"email": email, "ok": False, "created": False, "error": proc.stderr.strip()[:200]}
    created = (proc.stdout.strip().splitlines() or ["0"])[-1].strip() == "1"
    if created:                       # only write on a real insert (keeps the file stable)
        passwords[email] = pw
        try:
            PW_FILE.write_text(json.dumps(passwords, ensure_ascii=False, indent=2))
            os.chmod(PW_FILE, 0o600)
        except Exception:
            pass
        _ensure_maildir(maildir)
    return {"email": email, "ok": True, "created": created, "maildir": maildir}


def get_submission_password(email: str) -> str | None:
    """The mailbox's submission password from `virtual_users.password_plain` — the AUTHORITATIVE
    store (every provisioned row carries it). `mailbox_passwords.json` is only a best-effort cache
    that lost entries to an unlocked read-modify-write race, so sending must fall back to the DB
    (otherwise a candidate whose file entry was dropped can't reply — 'no submission password')."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    sql = ("SELECT password_plain FROM virtual_users WHERE email='{e}' "
           "AND password_plain IS NOT NULL AND password_plain<>'' LIMIT 1;".format(
               e=_sql_escape(email)))
    try:
        dbpass = Path(DBPASS_FILE).read_text().strip()
        proc = subprocess.run(["mysql", "-N", "-uamasmail", f"-p{dbpass}", "amasmail"],
                              input=sql, text=True, capture_output=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="provision just this profile id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = _rows()
    if args.only:
        rows = [r for r in rows if r["id"] == args.only]
    if not rows:
        print("no candidates to provision")
        return

    passwords = {}
    if PW_FILE.exists():
        try:
            passwords = json.loads(PW_FILE.read_text())
        except Exception:
            passwords = {}

    values = []
    for r in rows:
        pw = passwords.get(r["email"]) or _gen_password()
        passwords[r["email"]] = pw
        values.append(
            "('{e}','{d}','{n}','{h}','{p}','{m}')".format(
                e=_sql_escape(r["email"]), d=_sql_escape(r["domain"]),
                n=_sql_escape(r["full_name"]), h=_sql_escape(_hash(pw)),
                p=_sql_escape(pw), m=_sql_escape(_maildir(r["local"], r["domain"]))))

    print(f"candidates: {len(rows)} (domain(s): {sorted({r['domain'] for r in rows})})")
    if args.dry_run:
        print("dry-run — sample:", rows[0]["email"], "->", _maildir(rows[0]["local"], rows[0]["domain"]))
        return

    sql = ("INSERT IGNORE INTO virtual_users "
           "(email,domain,full_name,password_hash,password_plain,maildir) VALUES\n"
           + ",\n".join(values) + ";")

    dbpass = Path(DBPASS_FILE).read_text().strip()
    proc = subprocess.run(
        ["mysql", "-uamasmail", f"-p{dbpass}", "amasmail"],
        input=sql, text=True, capture_output=True)
    if proc.returncode != 0:
        print("MYSQL ERROR:", proc.stderr.strip()[:400])
        raise SystemExit(1)

    # persist plaintext passwords for the CRM's SMTP submission (gitignored)
    PW_FILE.write_text(json.dumps(passwords, ensure_ascii=False, indent=2))
    os.chmod(PW_FILE, 0o600)
    print(f"provisioned {len(rows)} mailboxes into virtual_users; passwords -> {PW_FILE.name}")

    # pre-create each Maildir group-readable so the CRM can read incoming mail
    for r in rows:
        _ensure_maildir(_maildir(r["local"], r["domain"]))
    print(f"pre-created {len(rows)} group-readable Maildirs under {MAILDIR_ROOT}")


if __name__ == "__main__":
    main()
