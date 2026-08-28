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
    db.set_password_hash(responsible["id"], password_hash)
    print(f"Password updated for {args.login}")
    if not args.password:
        print(f"Generated password: {password}")


def _set_active(login: str, active: bool) -> None:
    responsible = db.get_responsible_by_login(login)
    if not responsible:
        print(f"No such responsible: {login}", file=sys.stderr)
        raise SystemExit(1)
    db.set_active(responsible["id"], active)
    print(f"{'Reactivated' if active else 'Deactivated'} responsible {login}")


def cmd_deactivate(args: argparse.Namespace) -> None:
    _set_active(args.login, False)


def cmd_reactivate(args: argparse.Namespace) -> None:
    _set_active(args.login, True)


def cmd_link(args: argparse.Namespace) -> None:
    responsible = db.get_responsible_by_login(args.login)
    if not responsible:
        print(f"No such responsible: {args.login}", file=sys.stderr)
        raise SystemExit(1)
    db.set_telegram_chat(responsible["id"], args.chat_id)
    print(f"Linked {args.login} to telegram chat_id={args.chat_id}")


def cmd_setavail(args: argparse.Namespace) -> None:
    if not (0 <= args.dow <= 6):
        print(f"Invalid --dow {args.dow}: must be 0-6 (Monday=0 .. Sunday=6)",
              file=sys.stderr)
        raise SystemExit(1)
    responsible = db.get_responsible_by_login(args.login)
    if not responsible:
        print(f"No such responsible: {args.login}", file=sys.stderr)
        raise SystemExit(1)
    rid = responsible["id"]
    start_min = hhmm_to_min(args.start)
    end_min = hhmm_to_min(args.end)
    if start_min >= end_min:
        print(f"Invalid window {args.start}-{args.end}: start must be before end",
              file=sys.stderr)
        raise SystemExit(1)
    rows = db.get_availability(rid)
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

    p_deact = sub.add_parser("deactivate", help="Deactivate a responsible (revokes their session).")
    p_deact.add_argument("--login", required=True)
    p_deact.set_defaults(func=cmd_deactivate)

    p_react = sub.add_parser("reactivate", help="Reactivate a deactivated responsible.")
    p_react.add_argument("--login", required=True)
    p_react.set_defaults(func=cmd_reactivate)

    p_link = sub.add_parser("link", help="Link a responsible to a Telegram chat_id.")
    p_link.add_argument("--login", required=True)
    p_link.add_argument("--chat-id", type=int, required=True, dest="chat_id")
    p_link.set_defaults(func=cmd_link)

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
