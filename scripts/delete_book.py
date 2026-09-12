"""
CLI for deleting one or more books from Lenny (S3 files + DB record).

Accepts bare OLIDs or OpenLibrary edition keys, mixed freely. One bad ID
never blocks the rest. Must run inside the lenny_api container.

    make delete-book olid=OL51008637M
    make delete-book olid="OL51008637M 37044623"
"""

import argparse
import sys

from lenny.core.api import LennyAPI
from lenny.core.briet import parse_olid

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete one or more books from Lenny")
    parser.add_argument("olids", nargs="+", help="Bare OLIDs or OpenLibrary edition keys (e.g. OL51008637M)")
    args = parser.parse_args()

    parsed = []
    invalid = []
    for raw in args.olids:
        olid = parse_olid(raw)
        if olid is None:
            invalid.append(raw)
        else:
            parsed.append(olid)

    for raw in invalid:
        print(f"[✗] '{raw}' is not a valid OLID or edition key — skipped")

    if not parsed:
        sys.exit(1)

    result = LennyAPI.delete_many(parsed)

    for olid in result["deleted"]:
        print(f"[✓] Deleted {olid}")
    for olid in result["not_found"]:
        print(f"[✗] {olid} not found")
    for olid, error in result["failed"].items():
        print(f"[✗] {olid} failed: {error}")

    sys.exit(1 if (result["not_found"] or result["failed"] or invalid) else 0)
