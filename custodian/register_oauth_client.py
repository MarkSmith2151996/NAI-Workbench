#!/usr/bin/env python3
"""Register or rotate a persistent OAuth client for Custodian."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custodian.oauth_provider import create_or_rotate_named_client


def _is_valid_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http":
        return host in {"localhost", "127.0.0.1", "::1"}
    return True


async def _run(name: str, rotate_secret: bool, redirect_uris: list[str] | None) -> int:
    client_id, client_secret, created, status = await create_or_rotate_named_client(
        name,
        rotate_secret=rotate_secret,
        redirect_uris=redirect_uris,
    )

    if client_secret is None:
        print(f"client_id: {client_id}")
        print(f"created_at: {status}")
        print("client exists; use --rotate-secret to issue new secret.")
        return 0

    action = "created" if created else "rotated"
    print(f"[{action}] OAuth client credentials")
    print(f"name: {name}")
    print(f"client_id: {client_id}")
    print(f"client_secret: {client_secret}")
    print("Save this client secret now. It is not shown again.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register a Custodian OAuth client",
        epilog=(
            "Example:\n"
            "  register_oauth_client.py --name claude-desktop "
            "--redirect-uri https://claude.ai/api/mcp/auth_callback"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="Logical client name")
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help="Rotate the secret for an existing client",
    )
    parser.add_argument(
        "--redirect-uri",
        action="append",
        default=None,
        help="Redirect URI to store for the client. Repeatable.",
    )
    args = parser.parse_args()

    if args.redirect_uri is not None:
        invalid = [uri for uri in args.redirect_uri if not _is_valid_redirect_uri(uri)]
        if invalid:
            parser.error(f"invalid redirect URI(s): {', '.join(invalid)}")

    return asyncio.run(_run(args.name, args.rotate_secret, args.redirect_uri))


if __name__ == "__main__":
    raise SystemExit(main())
