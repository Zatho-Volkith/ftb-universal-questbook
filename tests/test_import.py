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


def _temp_instance(chapter_tree, lang=None):
    """A throwaway quests dir holding one chapter (and optionally a lang file)."""
    quests = Path(tempfile.mkdtemp()) / "quests"
    (quests / "chapters").mkdir(parents=True)
    snbt.dump(chapter_tree, str(quests / "chapters" / "imported.snbt"))
    if lang is not None:
        (quests / "lang").mkdir()
        snbt.dump(lang, str(quests / "lang" / "en_us.snbt"))
    return quests


def test_lang_recovery():
    tree = load()
    quest = tree["quests"][0]
    qid_ = str(quest["id"])
    stripped_title = quest.pop("title")
    stripped_desc = quest.pop("description")
    quest["subtitle"] = "kept inline"
    lang = {
        f"quest.{qid_}.title": stripped_title,
        f"quest.{qid_}.quest_desc": stripped_desc,
        f"quest.{qid_}.subtitle": "should lose to inline",
    }
    quests = _temp_instance(tree, lang)
    recovered = snbt.load(str(quests / "chapters" / "imported.snbt"))
    recovered, notes = import_chapter.recover_lang(quests, recovered)
    got = recovered["quests"][0]
    check("lang title recovered", got.get("title") == stripped_title,
          str(got.get("title")))
    check("lang description recovered", got.get("description") == stripped_desc)
    check("inline text beats lang", got.get("subtitle") == "kept inline",
          str(got.get("subtitle")))
    check("recovery is noted", any("recovered" in n for n in notes), str(notes))

    no_lang = _temp_instance(load())
    tree2 = snbt.load(str(no_lang / "chapters" / "imported.snbt"))
    _, notes2 = import_chapter.recover_lang(no_lang, tree2)
    check("no lang dir is a silent no-op", notes2 == [], str(notes2))


def test_parse_slug():
    check("plain slug encodes module",
          import_chapter.parse_slug("2b-mekanism") == (0x2B, 1))
    check("suffix letter encodes chapter",
          import_chapter.parse_slug("2ae-ae2wtlib") == (0x2A, 6))
    try:
        import_chapter.parse_slug("Mekanism!")
        check("bad slug refused", False)
    except import_chapter.Problem:
        check("bad slug refused", True)


def test_slug_module_mismatch_refused():
    quests = _temp_instance(load())
    try:
        import_chapter.plan_import(quests, "imported",
                                   new_slug="7a-nonesuch", module_number=99)
        check("slug/module mismatch refused", False)
    except import_chapter.Problem as exc:
        check("slug/module mismatch refused", "encodes module" in str(exc),
              str(exc))


def test_group_suggested_from_items():
    tree = load()
    tree["quests"][0]["tasks"][0]["item"] = {
        "count": snbt.Num(1), "id": "sophisticatedstorage:barrel"}
    modules = import_chapter.known_modules("1.21.1")
    suggestion = import_chapter.suggest_slot(tree, modules)
    check("storage mod suggests group tech", suggestion.group == "tech",
          suggestion.group)
    tree2 = load()
    tree2["quests"][0]["tasks"][0]["item"] = {
        "count": snbt.Num(1), "id": "croptopia:frying_pan"}
    suggestion2 = import_chapter.suggest_slot(tree2, modules)
    check("food mod suggests group food", suggestion2.group == "food",
          suggestion2.group)


def test_chapter_number_preserved_on_reimport():
    """--new on an existing add-on must keep its chapter slot, not reset to 1."""
    addon = snbt.load(str(
        REPO / "modules/20a-expanded-delight/1.21.1/chapters/expanded_delight.snbt"))
    quests = _temp_instance(addon)
    plan = import_chapter.plan_import(quests, "imported",
                                      new_slug="20a-expanded-delight")
    check("existing addon keeps chapter 2", plan.chapter_number == 2,
          str(plan.chapter_number))
    check("existing addon keeps module 32", plan.module_number == 32)
    try:
        import_chapter.plan_import(quests, "imported",
                                   new_slug="20a-expanded-delight",
                                   chapter_number=1)
        check("contradicting chapter number refused", False)
    except import_chapter.Problem as exc:
        check("contradicting chapter number refused",
              "contradicts" in str(exc), str(exc))


def test_family_split_back():
    """A merged family chapter routes quests home with positions restored."""
    parent = snbt.load(str(
        REPO / "modules/30-quality-of-life/1.21.1/chapters/quality_of_life.snbt"))
    addon = snbt.load(str(
        REPO / "modules/30a-waystones/1.21.1/chapters/waystones.snbt"))
    for quest in addon["quests"]:
        quest["x"] = snbt.Num(float(quest["x"].value) + 0.0, "d")
        quest["y"] = snbt.Num(float(quest["y"].value) + 6.0, "d")
    parent["quests"] = parent["quests"] + addon["quests"]
    quests = _temp_instance(parent)

    plan = import_chapter.plan_import(quests, "imported",
                                      into="30-quality-of-life")
    check("addon routed to its module", "30a-waystones" in plan.trees,
          str(sorted(plan.trees)))
    _target, split_tree = plan.trees["30a-waystones"]
    original = snbt.load(str(
        REPO / "modules/30a-waystones/1.21.1/chapters/waystones.snbt"))
    want = {str(q["id"]): (float(q["x"].value), float(q["y"].value))
            for q in original["quests"]}
    got = {str(q["id"]): (float(q["x"].value), float(q["y"].value))
           for q in split_tree["quests"]}
    check("split positions restored", got == want, f"{got} != {want}")
    check("parent kept its own quests",
          len(import_chapter.quests_of(plan.trees["30-quality-of-life"][1])) == 8)


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
