"""Install-time composition tests: merging, completionist, cleanup.

install.py no longer copies files -- it composes them. Each test builds a
plan against the real repo, applies it to a throwaway quests folder, and
asserts on what actually landed on disk. No server, no network.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import compose  # noqa: E402
import install  # noqa: E402
import snbt     # noqa: E402

MC = "1.21.1"

FAMILIES = {
    "farmers_delight.snbt": ("20-farmersdelight", 34),
    "productive_bees.snbt": ("22-productivebees", 28),
    "applied_energistics.snbt": ("2a-ae2", 70),
    "quality_of_life.snbt": ("30-quality-of-life", 38),
    "croptopia.snbt": ("21-croptopia", 22),
    "umapyoi.snbt": ("28-umapyoi", 18),
}
MERGED_BOXES = {
    "farmers_delight.snbt": (0.0, 9.0, -3.0, 13.5),
    "productive_bees.snbt": (0.0, 6.0, -1.5, 12.0),
    "applied_energistics.snbt": (-1.5, 12.0, -3.0, 33.0),
    "quality_of_life.snbt": (0.0, 12.0, -1.5, 21.0),
    "croptopia.snbt": (0.0, 7.5, -3.0, 9.0),
    "umapyoi.snbt": (0.0, 6.0, -1.5, 7.5),
}
def full_chapter_count() -> int:
    """One installed chapter file per non-addon module that ships chapters.

    Computed, not hardcoded: drafted modules (tools/import_chapter.py
    draft_module) land in modules/ between commits, and a fixed number would
    fail on every draft. The committed library ships at least 23.
    """
    modules = install.load_modules(MC)
    count = sum(len(list((m.version_root / "chapters").glob("*.snbt")))
                for m in modules.values() if not m.extends)
    assert count >= 23, count
    return count


def fresh_quests_dir() -> Path:
    return Path(tempfile.mkdtemp()) / "quests"


def do_install(quests_dir: Path, slugs: list[str] | None = None) -> dict:
    modules = install.load_modules(MC)
    chosen = list(modules.values()) if slugs is None else \
        [modules[s] for s in slugs]
    chosen, problems = install.resolve_module_deps(chosen, modules)
    assert not problems, problems
    plan = install.build_plan(chosen, modules)
    return {"plan": plan, "stats": install.apply_plan(quests_dir, plan)}


def coords(tree: dict) -> list[tuple[float, float]]:
    return [(compose._coord(q["x"]), compose._coord(q["y"]))
            for q in tree["quests"]]


def quest_ids(tree: dict) -> set[str]:
    return {q["id"] for q in tree["quests"]}


def source_tree(slug: str) -> dict:
    (path,) = (REPO / "modules" / slug / MC / "chapters").glob("*.snbt")
    return snbt.load(str(path))


def test_full_install_shape():
    quests = fresh_quests_dir()
    do_install(quests)
    chapters = sorted(p.name for p in (quests / "chapters").glob("*.snbt"))
    assert len(chapters) == full_chapter_count(), chapters
    addon_files = {"waystones.snbt", "mega_cells.snbt", "expanded_delight.snbt",
                   "bee_breeding.snbt", "applied_flux.snbt"}
    assert not addon_files & set(chapters), "addon chapter installed standalone"
    for name, (_slug, count) in FAMILIES.items():
        tree = snbt.load(str(quests / "chapters" / name))
        assert len(tree["quests"]) == count, (name, len(tree["quests"]))
        pos = coords(tree)
        assert len(pos) == len(set(pos)), f"{name}: overlapping quests"
        assert all(round(v * 2 / 3, 9) == round(v * 2 / 3) for xy in pos
                   for v in xy), f"{name}: quest off the 1.5 grid"
        box = compose.bounding_box(tree)
        assert box == MERGED_BOXES[name], (name, box)


def test_merged_ids_are_source_ids():
    quests = fresh_quests_dir()
    do_install(quests)
    for name, (slug, _count) in FAMILIES.items():
        merged = quest_ids(snbt.load(str(quests / "chapters" / name)))
        modules = install.load_modules(MC)
        family = [slug] + compose.family_map(modules).get(slug, [])
        expected = set()
        for member in family:
            expected |= quest_ids(source_tree(member))
        assert merged == expected, f"{name}: quest IDs drifted in the merge"


def test_partial_family():
    quests = fresh_quests_dir()
    do_install(quests, ["20-farmersdelight", "20b-nethers-delight",
                        "08-welcome", "09-completionist"])
    tree = snbt.load(str(quests / "chapters" / "farmers_delight.snbt"))
    assert len(tree["quests"]) == 18  # 12 parent + 6 nethers delight
    source = quest_ids(source_tree("20b-nethers-delight"))
    assert source <= quest_ids(tree)
    # The only chosen addon lands in the first band slot regardless of the
    # chapter numbers of the addons that were not chosen.
    addon_pos = [(x, y) for q in tree["quests"]
                 for x, y in [(compose._coord(q["x"]), compose._coord(q["y"]))]
                 if q["id"] in source]
    assert min(x for x, _ in addon_pos) == 0.0
    assert min(y for _, y in addon_pos) == 4.5
    assert max(x for x, _ in addon_pos) == 3.0
    comp = snbt.load(str(quests / "chapters" / "completionist.snbt"))
    # welcome + farmers delight tracked, plus the finale
    assert len(comp["quests"]) == 3


def test_reinstall_is_idempotent():
    quests = fresh_quests_dir()
    do_install(quests)
    second = do_install(quests)
    assert second["stats"]["written"] == 0
    assert second["stats"]["removed"] == 0
    backups = list(quests.rglob("*.bak-*"))
    assert not backups, backups


def test_superseded_addon_chapter_removed():
    quests = fresh_quests_dir()
    (quests / "chapters").mkdir(parents=True)
    (src,) = (REPO / "modules/30a-waystones" / MC / "chapters").glob("*.snbt")
    shutil.copy2(src, quests / "chapters" / "waystones.snbt")
    do_install(quests)
    assert not (quests / "chapters" / "waystones.snbt").exists()
    assert list((quests / "chapters").glob("waystones.snbt.bak-*"))


def test_foreign_file_with_addon_name_is_kept():
    quests = fresh_quests_dir()
    (quests / "chapters").mkdir(parents=True)
    foreign = quests / "chapters" / "waystones.snbt"
    foreign.write_text('{\n\tid: "A1B2C3D4E5F60718"\n\tquests: [ ]\n}\n',
                       encoding="utf-8")
    do_install(quests)
    assert foreign.exists(), "removed a chapter this library does not own"


def test_completionist_ids_and_dependencies():
    quests = fresh_quests_dir()
    do_install(quests)
    tree = snbt.load(str(quests / "chapters" / "completionist.snbt"))
    by_id = {q["id"]: q for q in tree["quests"]}
    ae2 = by_id["7B0901032A010000"]
    assert ae2["dependencies"] == ["7B2A010200000001"]
    assert ae2["tasks"] == [{"id": "7B0901042A010001", "type": "checkmark"}]
    final = by_id["7B090103FFFF0000"]
    assert len(final["dependencies"]) == full_chapter_count() - 1
    assert set(final["dependencies"]) == set(by_id) - {final["id"]}


def test_completionist_ids_stable_across_install_sets():
    small = fresh_quests_dir()
    do_install(small, ["20-farmersdelight", "09-completionist"])
    tree = snbt.load(str(small / "chapters" / "completionist.snbt"))
    fd = [q for q in tree["quests"] if q["id"] == "7B09010320010000"]
    assert fd and fd[0]["dependencies"] == ["7B20010200000001"]


def test_fresh_book_gets_data_snbt():
    quests = fresh_quests_dir()
    result = do_install(quests, ["01-getting-started"])
    assert result["stats"]["book_created"] == 1
    data = snbt.load(str(quests / "data.snbt"))
    assert data["progression_mode"] == "flexible"
    # An existing data.snbt belongs to the pack: reinstalling must not touch it.
    data["progression_mode"] = "linear"
    snbt.dump(data, str(quests / "data.snbt"))
    second = do_install(quests, ["01-getting-started"])
    assert second["stats"]["book_created"] == 0
    assert snbt.load(str(quests / "data.snbt"))["progression_mode"] == "linear"


def test_group_merge_and_prune():
    quests = fresh_quests_dir()
    quests.mkdir(parents=True)
    snbt.dump({"chapter_groups": [
        {"id": "1234567890ABCDEF", "title": "Pack's Own Group"},
        {"id": "7B00000100000007", "title": "Universal · Storage"},
    ]}, str(quests / "chapter_groups.snbt"))
    do_install(quests)
    doc = snbt.load(str(quests / "chapter_groups.snbt"))
    ids = [g["id"] for g in doc["chapter_groups"]]
    assert "1234567890ABCDEF" in ids, "foreign group was dropped"
    assert "7B00000100000007" not in ids, "retired library group survived"
    assert len(ids) == 6  # foreign + vanilla, mods, food, tech, qol


def test_lang_prune_scoped_to_installed_modules():
    quests = fresh_quests_dir()
    (quests / "lang").mkdir(parents=True)
    snbt.dump({
        "quest.7B30020300010000.title": "stale waystones text",
        "quest.AAAABBBBCCCCDDDD.title": "someone else's quest",
    }, str(quests / "lang" / "en_us.snbt"))
    do_install(quests, ["30-quality-of-life", "30a-waystones"])
    doc = snbt.load(str(quests / "lang" / "en_us.snbt"))
    assert "quest.7B30020300010000.title" not in doc
    assert "quest.AAAABBBBCCCCDDDD.title" in doc


def test_dry_run_writes_nothing():
    quests = fresh_quests_dir()
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "install.py"),
         "--quests-dir", str(quests), "--modules", "01-getting-started",
         "--dry-run"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout
    assert not quests.exists(), "dry run created files"


def main() -> int:
    failures = 0
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_"))
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} install checks pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
