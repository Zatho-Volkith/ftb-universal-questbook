"""Deterministic ID allocation for the Universal Questbook library.

FTB Quests identifies every object -- chapter groups, chapters, quests, tasks,
rewards, reward tables -- by a 16-character uppercase hex string in a single
flat global namespace. The quest editor generates these randomly. That is fine
for one pack authored in one place, but it is fatal for a drop-in library:
two modules authored months apart could collide, or a module could collide with
IDs already present in the book someone is installing into.

So we do not generate randomly. Every ID in this library is *computed* from
where the object sits in the library, which makes collisions impossible by
construction and makes any ID readable at a glance.

    7B  MM  CC  TT  AAAA BBBB
    --  --  --  --  ---- ----
    |   |   |   |   |    +--- index within the owner (task/reward/entry number)
    |   |   |   |   +-------- owner index (which quest, or which table)
    |   |   |   +------------ object type (see TYPES below)
    |   |   +---------------- chapter number within the module (00 = module-wide)
    |   +-------------------- module number, 00-FF
    +------------------------ library marker, constant

Why the marker is 7B and not something more on-the-nose like FB:

FTB Quests references reward tables from quests by their *signed 64-bit long*
value, not by the hex string -- you see `table_id: 4875026022556716679L` in
shipping packs. Any ID whose first hex digit is 8 or above has the high bit
set and therefore serialises as a negative long. That may well work, but it is
an unnecessary edge case to bet a library on. Keeping the leading digit at 7 or
below guarantees every ID we mint is a clean positive long.
"""

from __future__ import annotations

import re

__all__ = [
    "LIBRARY_MARKER", "TYPES", "qid", "to_long", "parse", "is_library_id",
    "is_valid_id",
]

LIBRARY_MARKER = "7B"

#: Object type codes. Stable forever -- changing one renumbers shipped content.
TYPES = {
    "chapter_group": 0x01,
    "chapter":       0x02,
    "quest":         0x03,
    "task":          0x04,
    "reward":        0x05,
    "reward_table":  0x06,
    "table_entry":   0x07,
    "loot_crate":    0x08,
    "image":         0x09,
    "quest_link":    0x0A,
}

_TYPE_NAMES = {code: name for name, code in TYPES.items()}

_ID_RE = re.compile(r"^[0-9A-F]{16}$")


def qid(module: int, chapter: int, kind: str, owner: int = 0, index: int = 0) -> str:
    """Compute the canonical ID for one object.

    module  -- module number (0-255); see ID-REGISTRY.md for the allocation
    chapter -- chapter number within the module, or 0 for module-wide objects
               such as reward tables and chapter groups
    kind    -- a key of TYPES
    owner   -- for tasks and rewards, the index of the quest that owns them;
               for table entries, the index of the reward table
    index   -- position within that owner, 1-based

    >>> qid(1, 2, "quest", owner=5)
    '7B01020300050000'
    >>> qid(0, 0, "reward_table", index=3)
    '7B00000600000003'
    """
    if kind not in TYPES:
        raise KeyError(f"unknown object type {kind!r}; expected one of {sorted(TYPES)}")
    for name, value, limit in (
        ("module", module, 0xFF),
        ("chapter", chapter, 0xFF),
        ("owner", owner, 0xFFFF),
        ("index", index, 0xFFFF),
    ):
        if not 0 <= value <= limit:
            raise ValueError(f"{name} must be between 0 and {limit}, got {value}")

    return (
        f"{LIBRARY_MARKER}"
        f"{module:02X}{chapter:02X}{TYPES[kind]:02X}"
        f"{owner:04X}{index:04X}"
    )


def is_valid_id(value: str) -> bool:
    """True if this is a well-formed FTB Quests ID (ours or anyone's)."""
    return bool(_ID_RE.match(value))


def is_library_id(value: str) -> bool:
    """True if this ID was minted by this library."""
    return is_valid_id(value) and value.startswith(LIBRARY_MARKER)


def to_long(value: str) -> int:
    """Convert a hex ID to the signed 64-bit long FTB uses for `table_id`.

    Quests reference reward tables numerically, so every reward table we ship
    needs its long form written into the quests that draw from it.

    >>> to_long("7B00000600000001")
    8863084092434939905
    """
    if not is_valid_id(value):
        raise ValueError(f"not a valid 16-char hex ID: {value!r}")
    unsigned = int(value, 16)
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def parse(value: str) -> dict:
    """Decompose one of our IDs back into its fields, for diagnostics."""
    if not is_library_id(value):
        raise ValueError(f"{value!r} was not minted by this library")
    type_code = int(value[6:8], 16)
    return {
        "module": int(value[2:4], 16),
        "chapter": int(value[4:6], 16),
        "type": _TYPE_NAMES.get(type_code, f"unknown(0x{type_code:02X})"),
        "owner": int(value[8:12], 16),
        "index": int(value[12:16], 16),
        "long": to_long(value),
    }


def describe(value: str) -> str:
    """Human-readable one-liner for an ID."""
    try:
        parts = parse(value)
    except ValueError as exc:
        return f"{value}  ({exc})"
    return (
        f"{value}  module {parts['module']:02d} / chapter {parts['chapter']:02d} / "
        f"{parts['type']} owner={parts['owner']} index={parts['index']}"
    )


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("usage:")
        print("  qid.py <hex-id> [...]              explain existing IDs")
        print("  qid.py new <module> <chapter> <kind> [owner] [index]")
        raise SystemExit(2)

    if args[0] == "new":
        if len(args) < 4:
            raise SystemExit("usage: qid.py new <module> <chapter> <kind> [owner] [index]")
        module, chapter, kind = int(args[1]), int(args[2]), args[3]
        owner = int(args[4]) if len(args) > 4 else 0
        index = int(args[5]) if len(args) > 5 else 0
        minted = qid(module, chapter, kind, owner, index)
        print(minted)
        print(f"  as long (for table_id): {to_long(minted)}L")
    else:
        for value in args:
            print(describe(value.upper()))
