#!/usr/bin/env python3
"""Report how many content mods the library covers.

The number that matters for the roadmap: mods with quest content that are
neither libraries (never in requires_mods by design) nor quality-of-life
conveniences (mod-categories.json says "qol"). The 150-content-mod milestone
gates the Fabric edition.

usage:
    python3 tools/coverage.py            # the number, plus the breakdown
    python3 tools/coverage.py --list     # every covered content mod
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TARGET = 150


def covered_mods(mc_version: str) -> tuple[set[str], set[str]]:
    """(content, qol) mod ids appearing in any module's requires_mods."""
    import import_chapter as ic
    categories = json.loads(
        (HERE / "data" / "mod-categories.json").read_text(encoding="utf-8")
    )["mods"]
    covered: set[str] = set()
    for manifest in ic.known_modules(mc_version).values():
        covered |= set(manifest.get("requires_mods") or [])
    covered.discard("minecraft")
    qol = {m for m in covered if categories.get(m) == "qol"}
    return covered - qol, qol


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    content, qol = covered_mods(args.mc_version)
    print(f"content mods covered : {len(content)} / {TARGET} "
          f"({TARGET - len(content)} to the Fabric milestone)")
    print(f"qol mods covered     : {len(qol)}")
    if args.list:
        print()
        for mod in sorted(content):
            print(f"  {mod}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
