"""Negative tests: inject one fault at a time, confirm the validator catches it.

A linter that reports "clean" on everything is worse than no linter, because
it buys false confidence. Each case below breaks the library in exactly one
way and asserts the specific message fires.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHAPTER = "modules/01-getting-started/1.21.1/chapters/getting_started.snbt"
TABLE = "modules/00-core/1.21.1/reward_tables/common.snbt"
MANIFEST = "modules/01-getting-started/module.json"


def edit(path: Path, old: str, new: str, count: int = 1):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} not found in {path.name}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


CASES = []


def case(name, expect):
    def register(fn):
        CASES.append((name, expect, fn))
        return fn
    return register


@case("duplicate ID", r"is already used by")
def _(root):
    # give quest 2 the same ID as quest 1
    edit(root / CHAPTER, '"7B01010300020000"', '"7B01010300010000"')


@case("malformed ID", r"is not 16 uppercase hex")
def _(root):
    edit(root / CHAPTER, '"7B01010300030000"', '"7b0101030003000"')


@case("foreign ID (no library marker)", r"does not start with the library marker")
def _(root):
    edit(root / CHAPTER, '"7B01010300040000"', '"A1B2C3D4E5F60718"')


@case("ID in the wrong module", r"encodes module 0x7A but lives in module 0x01")
def _(root):
    # 0x7A is deliberately a module number nothing owns, so this fires the
    # wrong-module check alone rather than also colliding with a real ID.
    edit(root / CHAPTER, '"7B01010300050000"', '"7B7A010300050000"')


@case("cross-chapter dependency", r"which is not in this chapter")
def _(root):
    edit(root / CHAPTER, 'dependencies: ["7B01010300020000"]',
         'dependencies: ["7B0202030009000A"]')


@case("dangling reward table reference", r"does not match any reward table")
def _(root):
    edit(root / CHAPTER, "table_id: 8863084092434939905L",
         "table_id: 1234567890123456L")


@case("chapter filename mismatch", r"but the file is")
def _(root):
    edit(root / CHAPTER, 'filename: "getting_started"', 'filename: "gettingstarted"')


@case("typo'd item ID", r"is not in any manifest")
def _(root):
    edit(root / CHAPTER, 'id: "minecraft:crafting_table"',
         'id: "minecraft:crafting_tables"')


@case("undeclared mod namespace", r'not listed in module\.json "requires_mods"')
def _(root):
    edit(root / CHAPTER, 'id: "minecraft:shield"', 'id: "farmersdelight:skillet"')


@case("overlapping quests", r"sits at the same position")
def _(root):
    # move quest 8 on top of quest 7
    edit(root / CHAPTER, "x: 3.0d\n\t\t\ty: 1.5d", "x: 4.5d\n\t\t\ty: 1.5d")


@case("dependency line drawn under a quest", r"passes under")
def _(root):
    # Park 'Somewhere to Sleep' exactly halfway along the line from
    # 'A Place to Work' to 'Something to Eat'.
    edit(root / CHAPTER, "x: 1.5d\n\t\t\ty: 3.0d", "x: 2.25d\n\t\t\ty: 0.75d")


@case("redundant dependency", r"already requires")
def _(root):
    # 'The Stone Age' needs 'First Tools', which needs 'A Place to Work'.
    # Naming the latter as well adds nothing but a line across the chapter.
    edit(root / CHAPTER,
         'dependencies: ["7B01010300030000"]',
         'dependencies: ["7B01010300030000", "7B01010300020000"]')


@case("broken SNBT", r"could not parse")
def _(root):
    path = root / TABLE
    path.write_text(path.read_text(encoding="utf-8").replace("{", "", 1), encoding="utf-8")


@case("reward table entry with a bad item", r"is not in any manifest")
def _(root):
    edit(root / TABLE, 'id: "minecraft:torch"', 'id: "minecraft:torches"')


ADDON = "modules/20a-expanded-delight/module.json"
ADDON_CHAPTER = "modules/20a-expanded-delight/1.21.1/chapters/expanded_delight.snbt"
ADDON2 = "modules/20b-nethers-delight/module.json"


@case("two modules claiming the same chapter slot", r"already used by")
def _(root):
    # 20b normally owns chapter 3; point it at 20a's chapter 2
    edit(root / ADDON2, '"chapter_number": 3', '"chapter_number": 2')


@case("addon not sharing its parent's module number", r"Addons share the parent's module number")
def _(root):
    edit(root / ADDON, '"module_number": 32', '"module_number": 33')


@case("addon not depending on its parent module", r"does not list it in\s+requires_modules")
def _(root):
    edit(root / ADDON, '"00-core",\n    "20-farmersdelight"', '"00-core"')


@case("addon not requiring its parent's mods", r"does not require its mods")
def _(root):
    edit(root / ADDON, '"farmersdelight",\n    "expandeddelight"', '"expandeddelight"')


@case("ID using a chapter the module does not own", r"owns chapter")
def _(root):
    edit(root / ADDON_CHAPTER, '"7B20020300010000"', '"7B20070300010000"')


MINING = "modules/02-mining-and-caving/1.21.1/chapters/mining_and_caving.snbt"


@case("unsupported task type (stage)", r"task type 'stage' is not usable here")
def _(root):
    # FTB accepts `stage`, silently rewrites it to type "custom", and the quest
    # becomes uncompletable. Verified in the testbed; the linter must refuse it.
    edit(root / MINING, 'type: "checkmark"', 'type: "stage"')


@case("unknown task type", r"unrecognised task type")
def _(root):
    edit(root / MINING, 'type: "checkmark"', 'type: "teleport"')


@case("bare '&' in a title", r"'&' followed by whitespace")
def _(root):
    # FTB Library reads & as a formatting code and prints an error over the
    # text. Nothing fails to load, so only in-game testing catches it -- which
    # is precisely why the linter has to.
    edit(root / MINING, 'title: "Mining \\\\& Caving"', 'title: "Mining & Caving"')


@case("'&' at end of text", r"text ends with '&'")
def _(root):
    edit(root / MINING, 'subtitle: "Down, and further down"', 'subtitle: "Down and further&"')


def run(root: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(root / "tools" / "validate.py")],
        capture_output=True, text=True, cwd=str(root),
    )
    return result.stdout + result.stderr


def main() -> int:
    baseline_root = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(REPO, baseline_root)
    baseline = run(baseline_root)
    if "clean." not in baseline:
        print("BASELINE IS NOT CLEAN -- fix that before trusting these tests")
        print(baseline)
        return 1
    print("baseline: clean\n")

    failures = 0
    for name, expect, mutate in CASES:
        work = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(REPO, work)
        mutate(work)
        output = run(work)
        caught = re.search(expect, output) is not None
        print(f"  {'PASS' if caught else 'FAIL'}  {name}")
        if not caught:
            failures += 1
            print(f"        expected /{expect}/ in:")
            print("        " + "\n        ".join(output.strip().splitlines()[:12]))
        shutil.rmtree(work.parent, ignore_errors=True)

    shutil.rmtree(baseline_root.parent, ignore_errors=True)
    print(f"\n{len(CASES) - failures}/{len(CASES)} checks fire correctly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
