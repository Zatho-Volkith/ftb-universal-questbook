"""Bring a chapter back out of a Minecraft instance and into the library.

This is the inverse of `install.py`, and it exists because the best place to
write a quest chapter is inside the game: you can see the layout, pick items
out of the real registry, and test a task the moment you write it.

What stops that being a straight file copy is the ID scheme. The in-game editor
mints random 16-hex IDs, while everything in this library has IDs *computed*
from where the object sits (see qid.py). So an imported chapter has to be
renumbered, and every internal reference rewritten to match, before it is a
module anyone else can safely install.

The renumbering is deliberately conservative: an ID that is already correct for
the target module and chapter is kept exactly as it is. That means re-importing
a chapter you have extended renumbers only the quests you added, and everyone
already playing keeps their completion state for the rest.

    # what is in the instance, and which of it the library already owns
    python3 tools/import_chapter.py --instance ~/.minecraft --list

    # pull edits to a chapter back into the module it came from
    python3 tools/import_chapter.py --instance ~/.minecraft \
        --chapter refined_storage --into 29-refinedstorage

    # adopt a brand new chapter as a module (see ID-REGISTRY.md for the number)
    python3 tools/import_chapter.py --instance ~/.minecraft \
        --chapter mekanism --new 2b-mekanism --module 43 \
        --name "Mekanism" --group automation --requires mekanism
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qid
import snbt

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTS_SUBPATH = Path("config") / "ftbquests" / "quests"

#: Rewards are numbered from here so that a quest's tasks and its rewards can
#: never land on the same ID, whatever order they were authored in.
REWARD_INDEX_BASE = 10

#: Refused on the way in rather than at validate time, because the failure they
#: cause in game is silent. See README, "Task types".
BANNED_TASK_TYPES = {"stage", "location"}

_SLUG_RE = re.compile(r"^[0-9a-z]{2}[a-z]?-[a-z0-9-]+$")


class Problem(Exception):
    """Something that must be fixed before the chapter can be imported."""


# --------------------------------------------------------------------------
# reading the library's own registries
# --------------------------------------------------------------------------

def load_library() -> dict:
    return json.loads((REPO_ROOT / "library.json").read_text(encoding="utf-8"))


def known_modules(mc_version: str) -> dict[str, dict]:
    """slug -> module.json, for every module that has content for this version."""
    out: dict[str, dict] = {}
    for manifest_path in sorted((REPO_ROOT / "modules").glob("*/module.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mc_version in manifest.get("mc_versions", []):
            out[manifest_path.parent.name] = manifest
    return out


def chapter_number_of(manifest: dict) -> int:
    """Modules that own a single chapter may leave this out; it means 1."""
    return manifest.get("chapter_number", 1)


# --------------------------------------------------------------------------
# walking a chapter
# --------------------------------------------------------------------------

def quests_of(tree: dict) -> list[dict]:
    return [q for q in (tree.get("quests") or []) if isinstance(q, dict)]


def ident(node: dict) -> str:
    value = node.get("id")
    return str(value) if value is not None else ""


def owned_by(value: str, module: int, chapter: int, kind: str) -> dict | None:
    """Parse `value` if it is already this library's ID for this slot."""
    if not qid.is_library_id(value):
        return None
    try:
        parts = qid.parse(value)
    except ValueError:
        return None
    if parts["module"] != module or parts["chapter"] != chapter:
        return None
    if parts["type"] != kind:
        return None
    return parts


def assign_owners(quests: list[dict], module: int, chapter: int) -> dict[int, int]:
    """Give every quest its owner number, keeping the ones already correct.

    Owner numbers are an allocation order, not a document order -- a quest
    added later keeps the number it was first given even if it is written
    higher up the file. Preserving them is what keeps player progress intact
    across a re-import.
    """
    owner_of: dict[int, int] = {}
    used: set[int] = set()

    for quest in quests:
        parts = owned_by(ident(quest), module, chapter, "quest")
        if parts and parts["owner"] and parts["owner"] not in used:
            owner_of[id(quest)] = parts["owner"]
            used.add(parts["owner"])

    nxt = 1
    for quest in quests:
        if id(quest) in owner_of:
            continue
        while nxt in used:
            nxt += 1
        owner_of[id(quest)] = nxt
        used.add(nxt)
    return owner_of


def assign_children(children: list, module: int, chapter: int, kind: str,
                    owner: int, base: int) -> list[str]:
    """Number a quest's tasks or rewards, keeping any already-correct IDs."""
    keep: dict[int, int] = {}
    used: set[int] = set()
    for position, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        parts = owned_by(ident(child), module, chapter, kind)
        if parts and parts["owner"] == owner and parts["index"] not in used:
            keep[position] = parts["index"]
            used.add(parts["index"])

    out: list[str] = []
    nxt = base + 1
    for position, _child in enumerate(children):
        if position in keep:
            index = keep[position]
        else:
            while nxt in used:
                nxt += 1
            index = nxt
            used.add(index)
        out.append(qid.qid(module, chapter, kind, owner, index))
    return out


# --------------------------------------------------------------------------
# the import itself
# --------------------------------------------------------------------------

def renumber(tree: dict, module: int, chapter: int,
             allowed_external: set[str]) -> tuple[dict, list[str]]:
    """Rewrite every ID in the chapter, and every reference to one.

    Returns the notes worth showing the user; raises Problem for anything that
    would ship a broken module.
    """
    notes: list[str] = []
    quests = quests_of(tree)
    if not quests:
        raise Problem("that chapter has no quests in it")

    tree["id"] = qid.qid(module, chapter, "chapter", 0, 1)

    owner_of = assign_owners(quests, module, chapter)
    remap: dict[str, str] = {}
    minted = 0

    for quest in quests:
        owner = owner_of[id(quest)]
        old = ident(quest)
        new = qid.qid(module, chapter, "quest", owner, 0)
        if old and old != new:
            remap[old] = new
            minted += 1
        quest["id"] = new

        for kind, key, base in (("task", "tasks", 0),
                                ("reward", "rewards", REWARD_INDEX_BASE)):
            children = quest.get(key) or []
            for child, new_id in zip(children,
                                     assign_children(children, module, chapter,
                                                     kind, owner, base)):
                if isinstance(child, dict):
                    child["id"] = new_id

        for task in quest.get("tasks") or []:
            if isinstance(task, dict) and str(task.get("type", "")) in BANNED_TASK_TYPES:
                raise Problem(
                    f"quest {str(quest.get('title', owner))!r} uses task type "
                    f"{str(task.get('type'))!r}, which cannot be shipped -- see "
                    f"README, 'Task types'"
                )

    # -- references --------------------------------------------------------
    # Dependencies are raw quest IDs with no notion of which file they came
    # from, so one pointing outside this chapter works in the instance it was
    # authored in and breaks for everyone who installs the module alone.
    external: set[str] = set()
    for quest in quests:
        deps = quest.get("dependencies")
        if not deps:
            continue
        rewritten = []
        for dep in deps:
            key = str(dep)
            if key in remap:
                rewritten.append(remap[key])
            elif owned_by(key, module, chapter, "quest"):
                rewritten.append(key)          # already correct, left alone
            else:
                external.add(key)
                rewritten.append(key)
        quest["dependencies"] = rewritten

    unexpected = external - allowed_external
    if unexpected:
        raise Problem(
            "these dependencies point outside the chapter, which breaks the "
            "module for anyone installing it on its own:\n    "
            + "\n    ".join(sorted(unexpected))
            + "\n  If one is deliberate, list it in module.json under "
              "allowed_external_dependencies."
        )

    # -- reward tables -----------------------------------------------------
    tables = {info["table_id"]: name
              for name, info in load_library()["reward_tables"].items()}
    unknown: set[int] = set()
    for quest in quests:
        for reward in quest.get("rewards") or []:
            if not isinstance(reward, dict) or "table_id" not in reward:
                continue
            value = reward["table_id"]
            number = int(getattr(value, "value", value))
            if number not in tables:
                unknown.add(number)
    if unknown:
        raise Problem(
            "these rewards draw from reward tables this library does not "
            "ship:\n    " + "\n    ".join(str(v) + "L" for v in sorted(unknown))
            + "\n  Point them at one of 00-core's tables (see library.json) "
              "before importing."
        )

    if minted == len(quests):
        notes.append(f"renumbered all {minted} quests -- none carried a library ID")
    elif minted:
        notes.append(f"minted {minted} new quest ID(s); "
                     f"{len(quests) - minted} kept the ones they had")
    else:
        notes.append("every quest already had a correct ID -- nothing renumbered")
    return tree, notes


def mods_used(tree: dict) -> set[str]:
    """Namespaces this chapter's items and entities come from."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("id", "item", "entity", "biome", "dimension",
                           "structure", "advancement", "block") and isinstance(value, str):
                    if ":" in value and not qid.is_valid_id(value):
                        found.add(value.split(":", 1)[0])
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree.get("quests") or [])
    return {ns for ns in found if ns not in ("minecraft", "neoforge", "forge")}


# --------------------------------------------------------------------------

def do_list(quests_dir: Path, mc_version: str) -> int:
    chapters = sorted((quests_dir / "chapters").glob("*.snbt"))
    if not chapters:
        print(f"no chapters found in {quests_dir / 'chapters'}", file=sys.stderr)
        return 1

    owners: dict[tuple[int, int], str] = {}
    for slug, manifest in known_modules(mc_version).items():
        owners[(manifest["module_number"], chapter_number_of(manifest))] = slug

    print(f"chapters in {quests_dir / 'chapters'}:\n")
    for path in chapters:
        try:
            tree = snbt.load(str(path))
        except Exception as exc:                       # noqa: BLE001
            print(f"  {path.stem:<28} (could not parse: {exc})")
            continue
        count = len(quests_of(tree))
        chapter_id = ident(tree)
        origin = "authored in game"
        if qid.is_library_id(chapter_id):
            try:
                parts = qid.parse(chapter_id)
                slug = owners.get((parts["module"], parts["chapter"]))
                origin = f"library: {slug}" if slug else (
                    f"library ID, module 0x{parts['module']:02X} "
                    f"(no such module here)")
            except ValueError:
                pass
        print(f"  {path.stem:<28} {count:>3} quests   {origin}")
    print("\nImport one with --chapter <name> --into <module> (or --new).")
    return 0


def write_manifest(slug: str, module_number: int, chapter_number: int,
                   name: str, filename: str, title: str, group: str,
                   requires: list[str], extends: str | None,
                   mc_version: str) -> Path:
    path = REPO_ROOT / "modules" / slug / "module.json"
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    else:
        manifest = {
            "id": slug,
            "module_number": module_number,
            "chapter_number": chapter_number,
            "name": name,
            "description": f"{name}. Imported from a live instance.",
            "version": "1.0.0",
            "mc_versions": [mc_version],
            "requires_mods": requires,
            "requires_modules": ["00-core"],
            "chapter_group": group,
            "chapters": [],
            "allowed_external_dependencies": [],
        }
        if extends:
            manifest["extends"] = extends
            if extends not in manifest["requires_modules"]:
                manifest["requires_modules"].append(extends)

    entries = [c for c in manifest.get("chapters", [])
               if c.get("number") != chapter_number]
    entries.append({"number": chapter_number, "file": filename, "title": title})
    manifest["chapters"] = sorted(entries, key=lambda c: c["number"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", type=Path, required=True,
                        help="Minecraft instance root (the folder containing mods/)")
    parser.add_argument("--quests-dir", type=Path,
                        help="read this quests folder instead of the instance's")
    parser.add_argument("--chapter", help="chapter filename, without .snbt")
    parser.add_argument("--into", help="slug of the module this chapter belongs to")
    parser.add_argument("--new", metavar="SLUG",
                        help="adopt as a new module with this directory name")
    parser.add_argument("--module", type=int,
                        help="module number for --new; see ID-REGISTRY.md")
    parser.add_argument("--chapter-number", type=int, default=1)
    parser.add_argument("--name", help="human name for --new")
    parser.add_argument("--group", default="mods",
                        help="chapter group key from library.json")
    parser.add_argument("--requires", nargs="*", default=None,
                        help="mod ids the chapter needs (inferred if omitted)")
    parser.add_argument("--extends", help="parent module slug, for an add-on")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--list", action="store_true",
                        help="list the instance's chapters and exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    quests_dir = args.quests_dir or (args.instance / QUESTS_SUBPATH)
    if not quests_dir.is_dir():
        print(f"error: no quests folder at {quests_dir}", file=sys.stderr)
        return 1

    if args.list:
        return do_list(quests_dir, args.mc_version)

    if not args.chapter:
        parser.error("--chapter is required (or use --list)")
    if bool(args.into) == bool(args.new):
        parser.error("give exactly one of --into or --new")

    source = quests_dir / "chapters" / f"{args.chapter}.snbt"
    if not source.exists():
        print(f"error: {source} does not exist", file=sys.stderr)
        return 1

    modules = known_modules(args.mc_version)
    library = load_library()
    groups = {g["key"]: g for g in library["chapter_groups"]}

    # -- work out where it is going ----------------------------------------
    if args.into:
        if args.into not in modules:
            print(f"error: no module {args.into!r}; have "
                  f"{', '.join(sorted(modules))}", file=sys.stderr)
            return 1
        manifest = modules[args.into]
        slug = args.into
        module_number = manifest["module_number"]
        chapter_number = chapter_number_of(manifest)
        group_key = manifest.get("chapter_group", "mods")
        allowed = set(manifest.get("allowed_external_dependencies") or [])
        extends = manifest.get("extends")
    else:
        slug = args.new
        if not _SLUG_RE.match(slug):
            print(f"error: {slug!r} is not a module directory name -- expected "
                  f"something like '2b-mekanism'", file=sys.stderr)
            return 1
        if args.module is None:
            print("error: --new needs --module (see ID-REGISTRY.md)", file=sys.stderr)
            return 1
        taken = {m["module_number"]: s for s, m in modules.items()}
        if args.module in taken and taken[args.module] != slug:
            other = taken[args.module]
            if not modules.get(slug, {}).get("extends") and args.extends != other:
                print(f"error: module number {args.module} (0x{args.module:02X}) "
                      f"is already {other}. Pick a free one from ID-REGISTRY.md, "
                      f"or pass --extends {other} to add a chapter to it.",
                      file=sys.stderr)
                return 1
        module_number = args.module
        chapter_number = args.chapter_number
        group_key = args.group
        allowed = set()
        extends = args.extends

    if group_key not in groups:
        print(f"error: unknown chapter group {group_key!r}; have "
              f"{', '.join(sorted(groups))}", file=sys.stderr)
        return 1

    # -- transform ---------------------------------------------------------
    try:
        tree = snbt.load(str(source))
    except Exception as exc:                           # noqa: BLE001
        print(f"error: could not parse {source.name}: {exc}", file=sys.stderr)
        return 1

    title = str(tree.get("title") or args.name or slug)
    filename = args.chapter
    tree["filename"] = filename
    tree["group"] = groups[group_key]["id"]

    try:
        tree, notes = renumber(tree, module_number, chapter_number, allowed)
    except Problem as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    inferred = sorted(mods_used(tree))
    requires = args.requires if args.requires is not None else inferred
    if extends and args.into:
        requires = modules[slug].get("requires_mods", requires)

    target = (REPO_ROOT / "modules" / slug / args.mc_version / "chapters"
              / f"{filename}.snbt")

    print(f"source : {source}")
    print(f"target : {target.relative_to(REPO_ROOT)}")
    print(f"module : {slug}  (0x{module_number:02X} / chapter "
          f"0x{chapter_number:02X})")
    print(f"quests : {len(quests_of(tree))}")
    print(f"group  : {groups[group_key]['title']}")
    if inferred:
        print(f"mods   : {', '.join(inferred)}")
    for note in notes:
        print(f"  {note}")

    if args.dry_run:
        print("\ndry run -- nothing written.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    snbt.dump(tree, str(target))
    manifest_path = write_manifest(slug, module_number, chapter_number,
                                   args.name or title, f"{filename}.snbt",
                                   title, group_key, list(requires), extends,
                                   args.mc_version)

    print(f"\nwrote {target.relative_to(REPO_ROOT)}")
    print(f"wrote {manifest_path.relative_to(REPO_ROOT)}")
    print("\nNow run:  python3 tools/validate.py")
    print("and then: python3 tools/testbed.py run --modules 00-core " + slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
