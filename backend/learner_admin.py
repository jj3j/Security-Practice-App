from __future__ import annotations

import argparse
import sys
from pathlib import Path

from learning_db import LearningDataError, LearningRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize learner state and manage the approved OIDC identity allowlist."
    )
    parser.add_argument("--database", type=Path, required=True, help="Learner SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or validate the learner database schema.")

    approve_parser = subparsers.add_parser("approve", help="Approve or re-enable an OIDC identity.")
    approve_parser.add_argument("--issuer", required=True, help="Exact OIDC issuer URL.")
    approve_parser.add_argument("--subject", required=True, help="Exact OIDC subject claim.")
    approve_parser.add_argument("--label", help="Optional administrative label.")
    approve_parser.add_argument(
        "--role",
        choices=("learner", "owner"),
        default="learner",
        help="Application role. Bootstrap the maintainer with owner.",
    )

    disable_parser = subparsers.add_parser("disable", help="Disable an approved OIDC identity.")
    disable_parser.add_argument("--issuer", required=True, help="Exact OIDC issuer URL.")
    disable_parser.add_argument("--subject", required=True, help="Exact OIDC subject claim.")

    subparsers.add_parser("list", help="List allowlist entries without exposing tokens or credentials.")
    subparsers.add_parser(
        "pending",
        help="List validated Google identities awaiting explicit approval.",
    )
    args = parser.parse_args(argv)
    repository = LearningRepository(args.database)

    try:
        repository.initialize()
        if args.command == "init":
            print(f"Learner database ready: {repository.database_path}")
        elif args.command == "approve":
            repository.approve_identity(args.issuer, args.subject, args.label, args.role)
            print("OIDC identity approved.")
        elif args.command == "disable":
            disabled = repository.disable_identity(args.issuer, args.subject)
            if not disabled:
                print("OIDC identity was not enabled.", file=sys.stderr)
                return 1
            print("OIDC identity disabled.")
        elif args.command == "list":
            for entry in repository.list_approved_identities():
                state = "enabled" if entry["enabled"] else "disabled"
                label = f" label={entry['label']}" if entry["label"] else ""
                print(
                    f"issuer={entry['issuer']} subject={entry['subject']} "
                    f"role={entry['role']} state={state}{label}"
                )
        else:
            for entry in repository.list_pending_identities():
                email = entry["email"] or "<not provided>"
                name = entry["display_name"] or "<not provided>"
                print(
                    f"issuer={entry['issuer']} subject={entry['subject']} "
                    f"email={email} name={name} last_seen={entry['last_seen_at']}"
                )
    except LearningDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
