"""Round-trip tests for import_chapter.py.

The property that matters is stability: importing a chapter that came out of
this library must give the same IDs back, and importing it again after you add
a quest in game must renumber only the quest you added. If that ever stops
being true, everyone playing loses their completion state on the next update,
which is the kind of bug you only find out about from a bug report.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import import_chapter  # noqa: E402
import snbt  # noqa: E402

CHAPTER = REPO / "modules/01-getting-started/1.21.1/chapters/getting_started.snbt"
MODULE, CHAPTER_NUMBER = 1, 1

FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def ids_of(tree):
    out = []
    for quest in import_chapter.quests_of(tree):
        out.append(str(quest["id"]))
        for key in ("tasks", "rewards"):
            for child in quest.get(key) or []:
                out.append(str(child["id"]))
    return out


def load():
    return snbt.load(str(CHAPTER))


def test_identity():
    """A shipped chapter re-imported unchanged keeps every ID."""
    before = ids_of(load())
    tree, _ = import_chapter.renumber(load(), MODULE, CHAPTER_NUMBER, set())
    after = ids_of(tree)
    check("shipped chapter re-imports byte-identically", before == after,
          f"{sum(1 for a, b in zip(before, after) if a != b)} IDs changed")


def test_added_quest():
    """Adding a quest mints exactly one new quest ID and disturbs nothing."""
    tree = load()
    before = ids_of(tree)
    existing = str(import_chapter.quests_of(tree)[0]["id"])
    tree["quests"].append({
        "dependencies": [existing],
        "id": "A3F19C2B7D4E5061",                 # what the editor would write
        "rewards": [{"id": "1B2C3D4E5F607182", "type": "xp", "xp": snbt.Num(50)}],
        "tasks": [{"id": "9F8E7D6C5B4A3928", "type": "checkmark"}],
        "title": "Added In Game",
        "x": snbt.Num(9.0, "d"), "y": snbt.Num(0.0, "d"),
    })
    result, notes = import_chapter.renumber(tree, MODULE, CHAPTER_NUMBER, set())
    after = ids_of(result)

    check("existing IDs survive an addition", after[:len(before)] == before)
    check("the added quest gets a computed ID",
          all(i.startswith("7B0101") for i in after[len(before):]),
          str(after[len(before):]))
    check("its dependency still resolves",
          str(result["quests"][-1]["dependencies"][0]) == existing)
    check("the report says one quest was minted",
          any("minted 1 new quest" in n for n in notes), str(notes))


def test_repeat_is_stable():
    """Importing twice is the same as importing once."""
    once, _ = import_chapter.renumber(load(), MODULE, CHAPTER_NUMBER, set())
    twice, _ = import_chapter.renumber(once, MODULE, CHAPTER_NUMBER, set())
    check("import is idempotent", ids_of(once) == ids_of(twice))


def test_cross_chapter_dependency_refused():
    tree = load()
    tree["quests"][-1]["dependencies"] = ["7B05010300030000"]   # a quest in The End
    try:
        import_chapter.renumber(tree, MODULE, CHAPTER_NUMBER, set())
    except import_chapter.Problem as exc:
        check("cross-chapter dependency refused", "point outside" in str(exc))
    else:
        check("cross-chapter dependency refused", False, "no Problem raised")


def test_allowed_external_dependency():
    tree = load()
    tree["quests"][-1]["dependencies"] = ["7B05010300030000"]
    try:
        import_chapter.renumber(tree, MODULE, CHAPTER_NUMBER,
                                {"7B05010300030000"})
    except import_chapter.Problem as exc:
        check("a declared external dependency is allowed", False, str(exc))
    else:
        check("a declared external dependency is allowed", True)


def test_banned_task_refused():
    tree = load()
    tree["quests"][-1]["tasks"] = [{"id": "AABBCCDDEEFF0011", "type": "stage",
                                    "stage": "chapter_two"}]
    try:
        import_chapter.renumber(tree, MODULE, CHAPTER_NUMBER, set())
    except import_chapter.Problem as exc:
        check("'stage' task refused", "cannot be shipped" in str(exc))
    else:
        check("'stage' task refused", False, "no Problem raised")


def test_foreign_reward_table_refused():
    tree = load()
    tree["quests"][-1]["rewards"] = [{"id": "AABBCCDDEEFF0012", "type": "random",
                                      "table_id": snbt.Num(4875026022556716679, "L")}]
    try:
        import_chapter.renumber(tree, MODULE, CHAPTER_NUMBER, set())
    except import_chapter.Problem as exc:
        check("unknown reward table refused", "does not ship" in str(exc))
    else:
        check("unknown reward table refused", False, "no Problem raised")


def test_mods_inferred():
    tree = load()
    check("vanilla chapter needs no mods", import_chapter.mods_used(tree) == set(),
          str(import_chapter.mods_used(tree)))
    tree["quests"][0]["tasks"][0]["item"] = {"count": snbt.Num(1),
                                             "id": "mekanism:ingot_osmium"}
    check("a modded item is picked up",
          "mekanism" in import_chapter.mods_used(tree))


def main():
    print("import_chapter round trip\n")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total}" if total else ""))
    for line in FAILURES:
        print(f"  {line}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
