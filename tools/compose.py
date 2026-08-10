"""Compose install-time chapters: merged families and the completionist.

Addon modules are authored as standalone chapters -- each a self-contained
quest tree rooted at (0, 0) on the 1.5-unit grid. In the installed book they
do not appear as chapters of their own: the installer merges each addon's
quests into its parent's chapter, offset into a clear region below the
parent's tree. This file owns that composition. Everything here is a pure
function over parsed SNBT trees; callers do all I/O.

Why merging cannot break the layout invariants validate.py cares about:
dependency edges only ever connect quests inside one source tree (the
library has no cross-chapter dependencies, and composition adds none), and
a line between two points of a bounding box stays inside that box. Boxes
are placed at least GAP = 3.0 apart, which is more than twice the 0.55-unit
icon clearance the validator enforces -- so composition can introduce
neither coincident positions nor a line passing under a foreign quest.

The completionist chapter is also composed here: one quest per installed
chapter, each depending on the chapter's own ID, so FTB Quests completes it
when the chapter completes. Quest IDs encode the target chapter in the
owner field (module byte << 8 | chapter byte), which makes them a pure
function of the target -- the same install set or a different one always
mints the same ID for the same chapter, and player progress survives.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import qid
import snbt

# Grid distance kept clear between the parent's tree and each addon band,
# and between neighbouring addon trees inside a band. Two rows.
GAP = 3.0

# A band packs addon trees left to right and wraps when the next tree would
# extend past the parent's right edge -- but a narrow parent would then force
# a uselessly tall single column, so every band is at least this wide.
ROW_MIN_WIDTH = 12.0

COMPLETIONIST_MODULE = 9
COMPLETIONIST_CHAPTER = 1
COMPLETIONIST_FINAL_OWNER = 0xFFFF

_EPIC_TABLE = snbt.L(8863084092434939908)
_LEGENDARY_TABLE = snbt.L(8863084092434939909)


def _coord(value) -> float:
    return float(value.value if isinstance(value, snbt.Num) else value)


def bounding_box(tree: dict) -> tuple[float, float, float, float]:
    """(min_x, max_x, min_y, max_y) over a chapter's quests."""
    xs = [_coord(q["x"]) for q in tree.get("quests", [])]
    ys = [_coord(q["y"]) for q in tree.get("quests", [])]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def plan_offsets(parent_tree: dict,
                 addon_trees: list[dict]) -> list[tuple[float, float]]:
    """One (dx, dy) per addon tree, in the order given.

    Bands stack below the parent (positive y is down). Inside a band, trees
    pack left to right with GAP between them; the band wraps when the next
    tree would pass the right edge. All inputs sit on the 1.5 grid, and GAP
    is a multiple of 1.5, so every offset keeps the grid intact.
    """
    left, parent_right, _, parent_bottom = bounding_box(parent_tree)
    right = max(parent_right, left + ROW_MIN_WIDTH)

    offsets: list[tuple[float, float]] = []
    band_top = parent_bottom + GAP
    cursor = left
    band_height = 0.0
    for tree in addon_trees:
        a_left, a_right, a_top, a_bottom = bounding_box(tree)
        width = a_right - a_left
        height = a_bottom - a_top
        if cursor > left and cursor + width > right:
            band_top += band_height + GAP
            cursor = left
            band_height = 0.0
        offsets.append((cursor - a_left, band_top - a_top))
        cursor += width + GAP
        band_height = max(band_height, height)
    return offsets


def _shifted(items: list[dict], dx: float, dy: float) -> list[dict]:
    out = []
    for item in items:
        moved = copy.deepcopy(item)
        moved["x"] = snbt.d(_coord(moved["x"]) + dx)
        moved["y"] = snbt.d(_coord(moved["y"]) + dy)
        out.append(moved)
    return out


def compose_family(parent_tree: dict, addon_trees: list[dict]) -> dict:
    """The parent's chapter with every addon's quests merged in.

    Chapter identity -- id, filename, group, icon, title, subtitle,
    order_index -- stays the parent's. Inputs are never mutated. No
    dependency edges are added: addon trees keep floating free, exactly
    as they would in their own chapter.
    """
    merged = copy.deepcopy(parent_tree)
    for tree, (dx, dy) in zip(addon_trees, plan_offsets(parent_tree, addon_trees)):
        merged["quests"].extend(_shifted(tree.get("quests", []), dx, dy))
        merged.setdefault("images", []).extend(
            _shifted(tree.get("images", []), dx, dy))
        merged.setdefault("quest_links", []).extend(
            _shifted(tree.get("quest_links", []), dx, dy))
    return merged


def family_map(modules: dict) -> dict[str, list[str]]:
    """Parent slug -> addon slugs, sorted by chapter_number.

    Accepts any mapping of slug to an object with `extends` and
    `chapter_number` attributes (install.py's Module qualifies).
    """
    families: dict[str, list[str]] = {}
    for slug in sorted(modules):
        parent = modules[slug].extends
        if parent:
            families.setdefault(parent, []).append(slug)
    for parent, addons in families.items():
        addons.sort(key=lambda s: modules[s].chapter_number)
    return families


# -- completionist ---------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """One installed chapter the completionist tracks."""
    chapter_id: str
    title: str
    icon: dict
    group_order: int    # order of the chapter's group in the book; ungrouped = 0
    order_index: int    # the chapter's own order within its group


def _rewards(owner: int, xp: int, table) -> list[dict]:
    return [
        {
            "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                          "reward", owner, 0x0B),
            "type": "xp",
            "xp": xp,
        },
        {
            "exclude_from_claim_all": True,
            "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                          "reward", owner, 0x0C),
            "table_id": table,
            "type": "random",
        },
    ]


def completionist_quests(targets: list[Target]) -> list[dict]:
    """The full quest list for the completionist chapter.

    One quest per target, unlocking when the target chapter completes,
    plus a final quest depending on all of them. Each carries a single
    checkmark task -- the pattern the reference packs use for
    chapter-completion quests -- so claiming it is one click once the
    dependency is met. IDs depend only on each target's module and
    chapter bytes, never on the install set.
    """
    ordered = sorted(targets,
                     key=lambda t: (t.group_order, t.order_index, t.chapter_id))
    quests: list[dict] = []
    for i, target in enumerate(ordered):
        parts = qid.parse(target.chapter_id)
        owner = (parts["module"] << 8) | parts["chapter"]
        quests.append({
            "dependencies": [target.chapter_id],
            "description": [
                f"Every quest in {target.title}, finished.",
                "",
                "Unlocks the moment the chapter is done. Tick it off and",
                "take the loot.",
            ],
            "icon": copy.deepcopy(target.icon),
            "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                          "quest", owner),
            "rewards": _rewards(owner, 100, _EPIC_TABLE),
            "subtitle": "Complete every quest in this chapter",
            "tasks": [{
                "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                              "task", owner, 1),
                "type": "checkmark",
            }],
            "title": target.title,
            "x": snbt.d(1.5 * (i % 5)),
            "y": snbt.d(1.5 * (i // 5)),
        })

    if quests:
        last_row_y = 1.5 * ((len(ordered) - 1) // 5)
        quests.append({
            "dependencies": [q["id"] for q in quests],
            "description": [
                "Every chapter, every quest.",
                "",
                "There is nothing left in this book to do. Well played.",
            ],
            "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                          "quest", COMPLETIONIST_FINAL_OWNER),
            "rewards": _rewards(COMPLETIONIST_FINAL_OWNER, 500,
                                _LEGENDARY_TABLE),
            "subtitle": "Complete every chapter",
            "tasks": [{
                "id": qid.qid(COMPLETIONIST_MODULE, COMPLETIONIST_CHAPTER,
                              "task", COMPLETIONIST_FINAL_OWNER, 1),
                "type": "checkmark",
            }],
            "title": "Master of the Book",
            "x": snbt.d(3.0),
            "y": snbt.d(last_row_y + 3.0),
        })
    return quests
