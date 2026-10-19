"""Local operator commands for private-demo invitations and tester access.

Run only in a trusted terminal connected to the intended DATABASE_PATH. This
module is not mounted as an HTTP API and never prints stored token hashes.
"""

import argparse
import sqlite3
import sys

from .invitations import create_invitation, disable_account, enable_account, revoke_invitation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Memory Atlas private demo access")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite", help="create a one-time invitation")
    invite.add_argument("--expires-days", type=int, default=7)
    revoke = commands.add_parser("revoke-invite", help="revoke an unused invitation by its ID")
    revoke.add_argument("invitation_id")
    disable = commands.add_parser("disable-user", help="disable login and revoke all sessions")
    disable.add_argument("username")
    enable = commands.add_parser("enable-user", help="re-enable a previously disabled account")
    enable.add_argument("username")
    args = parser.parse_args(argv)

    try:
        if args.command == "invite":
            invitation_id, token = create_invitation(args.expires_days)
            print(f"Invitation ID: {invitation_id}")
            print("Copy this one-time code now; it will not be shown again:")
            print(token)
        elif args.command == "revoke-invite":
            if not revoke_invitation(args.invitation_id):
                print("No unused invitation with that ID was changed.", file=sys.stderr)
                return 1
            print("Unused invitation revoked.")
        elif args.command == "disable-user":
            if not disable_account(args.username):
                print("No account with that username exists.", file=sys.stderr)
                return 1
            print("Account disabled and its active sessions revoked. Account data was preserved.")
        elif args.command == "enable-user":
            if not enable_account(args.username):
                print("No disabled account with that username exists.", file=sys.stderr)
                return 1
            print("Account enabled. The user must sign in again.")
    except (ValueError, sqlite3.OperationalError) as exc:
        print(f"Could not complete the command: {exc}", file=sys.stderr)
        print("Confirm DATABASE_PATH points to the initialized demo database.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
