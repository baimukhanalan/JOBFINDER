"""Operator CLI to create/manage "responsible" people for the interview scheduler.

Run from the repo root: `python -m backend.interviews.admin_cli <subcommand> ...`.
Nothing else imports this module — it's a standalone admin tool over `backend.
interviews.db` (schema/queries) and `backend.interviews.auth` (password hashing).
"""
from __future__ import annotations

import argparse
import secrets
import sys

from backend.interviews import auth, db
from backend.tools import mail_db


def hhmm_to_min(hhmm: str) -> int:
    """'HH:MM' -> minutes since midnight, e.g. '09:30' -> 570."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def cmd_add(args: argparse.Namespace) -> None:
    password = args.password or secrets.token_urlsafe(12)
    password_hash = auth.hash_password(password)
    rid = db.add_responsible(args.login, password_hash, args.name, tz=args.tz)
    print(f"Created responsible id={rid} login={args.login} name={args.name} tz={args.tz}")
    if not args.password:
        print(f"Generated password: {password}")


def cmd_list(args: argparse.Namespace) -> None:
    roster = db.list_responsibles(active_only=False)
    if not roster:
        print("No responsibles.")
        return
    for r in roster:
        status = "active" if r.get("active") else "inactive"
        print(f"id={r['id']} login={r['login']} name={r['name']} "
              f"tz={r['tz']} {status}")


def cmd_passwd(args: argparse.Namespace) -> None:
    responsible = db.get_responsible_by_login(args.login)
    if not responsible:
        print(f"No such responsible: {args.login}", file=sys.stderr)
        raise SystemExit(1)
    password = args.password or secrets.token_urlsafe(12)
    password_hash = auth.hash_password(password)
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET password_hash=%s WHERE id=%s",
                    (password_hash, responsible["id"]))
    print(f"Password updated for {args.login}")
    if not args.password:
        print(f"Generated password: {password}")


def cmd_setavail(args: argparse.Namespace) -> None:
    responsible = db.get_responsible_by_login(args.login)
    if not responsible:
        print(f"No such responsible: {args.login}", file=sys.stderr)
        raise SystemExit(1)
    rid = responsible["id"]
    rows = db.get_availability(rid)
    start_min = hhmm_to_min(args.start)
    end_min = hhmm_to_min(args.end)
    for row in rows:
        if row["dow"] == args.dow:
            row["start_min"] = start_min
            row["end_min"] = end_min
            row["enabled"] = True
    db.set_availability(rid, rows)
    print(f"Availability set for {args.login}: dow={args.dow} "
          f"{args.start}-{args.end}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_cli", description="Manage interview-scheduler responsibles.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Create a new responsible.")
    p_add.add_argument("--login", required=True)
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--password", default=None)
    p_add.add_argument("--tz", default="UTC")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List responsibles.")
    p_list.set_defaults(func=cmd_list)

    p_passwd = sub.add_parser("passwd", help="Change a responsible's password.")
    p_passwd.add_argument("--login", required=True)
    p_passwd.add_argument("--password", default=None)
    p_passwd.set_defaults(func=cmd_passwd)

    p_setavail = sub.add_parser("setavail", help="Set one weekday's availability window.")
    p_setavail.add_argument("--login", required=True)
    p_setavail.add_argument("--dow", type=int, required=True)
    p_setavail.add_argument("--start", required=True)
    p_setavail.add_argument("--end", required=True)
    p_setavail.set_defaults(func=cmd_setavail)

    return parser


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    db.ensure_schema()
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
