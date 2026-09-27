"""Create .env from .env.example with a fresh random secret for every CHANGE_ME value.

Used by CI and by developers setting up a clone. Refuses to overwrite an existing .env.
Secrets are URL-safe (they are embedded in database URLs) and never printed.

Usage: python scripts/make_env.py [--output .env]
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if args.output.exists():
        print(f"{args.output} already exists; not overwriting", file=sys.stderr)
        return 1
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    content, count = re.subn(r"CHANGE_ME_\w+", lambda _: secrets.token_urlsafe(24), template)
    args.output.write_text(content, encoding="utf-8")
    print(f"wrote {args.output} with {count} generated secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
