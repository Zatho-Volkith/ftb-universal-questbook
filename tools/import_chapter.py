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

Two other things happen on the way in. Text that FTB Quests migrated into the
instance's `lang/en_us.snbt` (it does this to every loaded book) is recovered
and re-inlined, so a chapter authored in a played world does not import
textless. And a merged family chapter -- the installed form, where add-on
quests live inside the parent's file -- is split back into its source modules
by ID ownership, so you can polish a whole family's layout in game and
re-import it in one step.

    # what is in the instance, and which of it the library already owns
    python3 tools/import_chapter.py --instance ~/.minecraft --list --free

    # pull edits to a chapter back into the module it came from
    python3 tools/import_chapter.py --instance ~/.minecraft \
        --chapter refined_storage --into 29-refinedstorage

    # adopt a brand new chapter as a module. The slug encodes the module
    # number (2b = 0x2B = 43), and the group is suggested from the mod.
    python3 tools/import_chapter.py --instance ~/.minecraft \
        --chapter mekanism --new 2b-mekanism --name "Mekanism"

For the guided version of all this, run `python3 tools/import_wizard.py`,
or `python3 tools/webui.py` for the browser version.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
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

#: Mod module numbers are allocated from here up; below is vanilla territory.
MOD_NUMBER_BASE = 0x20


class Problem(Exception):
    """Something that must be fixed before the chapter can be imported."""


# --------------------------------------------------------------------------
# reading the library's own registries
# --------------------------------------------------------------------------

def load_library(repo_root: Path = REPO_ROOT) -> dict:
    return json.loads((repo_root / "library.json").read_text(encoding="utf-8"))


def known_modules(mc_version: str, repo_root: Path = REPO_ROOT) -> dict[str, dict]:
    """slug -> module.json, for every module that has content for this version."""
    out: dict[str, dict] = {}
    for manifest_path in sorted((repo_root / "modules").glob("*/module.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mc_version in manifest.get("mc_versions", []):
            out[manifest_path.parent.name] = manifest
    return out


def chapter_number_of(manifest: dict) -> int:
    """Modules that own a single chapter may leave this out; it means 1."""
    return manifest.get("chapter_number", 1)


def parse_slug(slug: str) -> tuple[int, int]:
    """The module and chapter number a directory name encodes.

    The prefix is the module number in lowercase hex; a trailing letter is a
    chapter slot (a = chapter 2, b = 3, ...). `2b-mekanism` -> (43, 1);
    `2ae-ae2wtlib` -> (42, 6). This is the guard against the classic mistake
    of passing a decimal --module that does not match the hex in the name.
    """
    if not _SLUG_RE.match(slug):
        raise Problem(f"{slug!r} is not a module directory name -- expected "
                      f"something like '2b-mekanism' or '2ae-ae2wtlib'")
    prefix = slug.split("-", 1)[0]
    module = int(prefix[:2], 16)
    chapter = 1 if len(prefix) == 2 else ord(prefix[2]) - ord("a") + 2
    return module, chapter


def free_module_numbers(modules: dict[str, dict],
                        repo_root: Path = REPO_ROOT) -> dict[int, str]:
    """Status of every interesting module number.

    On-disk modules mark numbers taken; ID-REGISTRY.md rows containing
    "reserved" or "released" mark the rest. The registry is a hand-written
    document, so the parse is deliberately tolerant: a row that does not
    match contributes nothing, and a missing file means disk-only truth.
    """
    statuses: dict[int, str] = {}
    for slug, manifest in modules.items():
        number = manifest["module_number"]
        if number not in statuses or not manifest.get("extends"):
            statuses[number] = f"taken: {slug}"

    registry = repo_root / "ID-REGISTRY.md"
    if registry.exists():
        row = re.compile(r"^\|\s*`([0-9A-Fa-f]{2})`\s*\|\s*(.*?)\s*\|")
        for line in registry.read_text(encoding="utf-8").splitlines():
            match = row.match(line)
            if not match:
                continue
            number = int(match.group(1), 16)
            cell = match.group(2)
            if number in statuses:
                continue
            lowered = cell.lower()
            if "released" in lowered:
                statuses[number] = "released: never reuse"
            elif "reserved" in lowered or "planned" in line.lower():
                statuses[number] = f"reserved: {cell}"
    return statuses


def next_free_module_number(statuses: dict[int, str],
                            start: int = MOD_NUMBER_BASE) -> int:
    number = start
    while number in statuses:
        number += 1
    if number > 0xFF:
        raise Problem("no free module numbers left")
    return number


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
    walk(tree.get("icon") or {})
    return {ns for ns in found if ns not in ("minecraft", "neoforge", "forge")}


# --------------------------------------------------------------------------
# lang recovery
# --------------------------------------------------------------------------

def recover_lang(quests_dir: Path, tree: dict) -> tuple[dict, list[str]]:
    """Re-inline text FTB Quests migrated into the instance's lang file.

    On first load the mod moves every title, subtitle and description out of
    the chapter file into `lang/en_us.snbt`, keyed by object ID. A chapter
    exported from a played world is therefore textless, and importing it as-is
    would silently ship a book of blank quests. This puts the text back, by
    the object IDs the instance used, so it must run *before* renumbering.

    Text still present inline always wins over the lang entry.
    """
    lang_dir = quests_dir / "lang"
    lang_file = lang_dir / "en_us.snbt"
    if not lang_file.exists():
        candidates = sorted(lang_dir.glob("*.snbt")) if lang_dir.is_dir() else []
        if not candidates:
            return tree, []
        lang_file = candidates[0]
    try:
        lang = snbt.load(str(lang_file))
    except (snbt.SNBTError, OSError):
        return tree, []
    if not isinstance(lang, dict):
        return tree, []

    # object ID -> (object, which lang fields map to which tree fields)
    text_fields = {"title": "title", "subtitle": "subtitle",
                   "quest_desc": "description", "description": "description"}
    objects: dict[str, dict] = {}
    if ident(tree):
        objects[ident(tree)] = tree
    for quest in quests_of(tree):
        objects[ident(quest)] = quest
        for child in (quest.get("tasks") or []) + (quest.get("rewards") or []):
            if isinstance(child, dict) and ident(child):
                objects[ident(child)] = child

    recovered = {"title": 0, "subtitle": 0, "description": 0}
    for key, value in lang.items():
        parts = key.split(".")
        if len(parts) != 3:
            continue
        _kind, object_id, lang_field = parts
        target = objects.get(object_id)
        field_name = text_fields.get(lang_field)
        if target is None or field_name is None:
            continue
        if target.get(field_name):
            continue                       # inline text wins
        if field_name == "description" and isinstance(value, str):
            value = [value]
        target[field_name] = value
        recovered[field_name] += 1

    notes: list[str] = []
    if any(recovered.values()):
        notes.append(f"recovered from lang/{lang_file.name}: "
                     f"{recovered['title']} title(s), "
                     f"{recovered['subtitle']} subtitle(s), "
                     f"{recovered['description']} description(s)")
    textless = [str(q.get("id")) for q in quests_of(tree) if not q.get("title")]
    if textless:
        notes.append(f"warning: {len(textless)} quest(s) still have no title "
                     f"even after lang recovery -- they will render blank")
    return tree, notes


# --------------------------------------------------------------------------
# scanning and suggesting
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    stem: str
    title: str
    quest_count: int
    chapter_id: str
    owner_slug: str | None      # library module that owns this chapter, if any
    origin: str                 # human description of where it came from
    parse_error: str | None = None


def scan(quests_dir: Path, mc_version: str,
         repo_root: Path = REPO_ROOT) -> list[Candidate]:
    """Every chapter in the instance, and who owns it."""
    owners: dict[tuple[int, int], str] = {}
    for slug, manifest in known_modules(mc_version, repo_root).items():
        owners[(manifest["module_number"], chapter_number_of(manifest))] = slug

    out: list[Candidate] = []
    for path in sorted((quests_dir / "chapters").glob("*.snbt")):
        try:
            tree = snbt.load(str(path))
        except Exception as exc:                       # noqa: BLE001
            out.append(Candidate(path.stem, path.stem, 0, "", None,
                                 "could not parse", str(exc)))
            continue
        chapter_id = ident(tree)
        owner_slug = None
        origin = "authored in game"
        if qid.is_library_id(chapter_id):
            try:
                parts = qid.parse(chapter_id)
                owner_slug = owners.get((parts["module"], parts["chapter"]))
                origin = f"library: {owner_slug}" if owner_slug else (
                    f"library ID, module 0x{parts['module']:02X} "
                    f"(no such module here)")
            except ValueError:
                pass
        out.append(Candidate(path.stem, str(tree.get("title") or path.stem),
                             len(quests_of(tree)), chapter_id, owner_slug,
                             origin))
    return out


@dataclass
class Suggestion:
    inferred_mods: list[str]
    primary_mod: str | None
    group: str                                  # chapter group key
    reimport_slug: str | None                   # chapter already belongs here
    extends_candidates: list[tuple[str, int]]   # (parent slug, free chapter no)
    module_number: int | None                   # proposal for a new module
    slug: str | None                            # proposal for a new module


def suggest_slot(tree: dict, modules: dict[str, dict],
                 repo_root: Path = REPO_ROOT) -> Suggestion:
    """Work out where a chapter most plausibly belongs.

    Returns data, never decides: the wizard and web UI present it, the CLI
    uses it only for defaults the user did not override.
    """
    counts: dict[str, int] = {}

    def tally(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("id", "item", "entity") and isinstance(value, str) \
                        and ":" in value and not qid.is_valid_id(value):
                    ns = value.split(":", 1)[0]
                    if ns not in ("minecraft", "neoforge", "forge", "ftbquests"):
                        counts[ns] = counts.get(ns, 0) + 1
                else:
                    tally(value)
        elif isinstance(node, list):
            for item in node:
                tally(item)

    tally(tree.get("quests") or [])
    inferred = sorted(mods_used(tree))
    primary = max(counts, key=counts.get) if counts else None

    group = "mods"
    categories_path = repo_root / "tools" / "data" / "mod-categories.json"
    if primary and categories_path.exists():
        payload = json.loads(categories_path.read_text(encoding="utf-8"))
        category = payload.get("mods", {}).get(primary)
        info = payload.get("categories", {}).get(category)
        if info and info.get("group"):
            group = info["group"]

    reimport_slug = None
    chapter_id = ident(tree)
    if qid.is_library_id(chapter_id):
        try:
            parts = qid.parse(chapter_id)
            for slug, manifest in modules.items():
                if manifest["module_number"] == parts["module"] and \
                        chapter_number_of(manifest) == parts["chapter"]:
                    reimport_slug = slug
        except ValueError:
            pass

    # A module whose mods are a subset of this chapter's mods could adopt it
    # as an addon chapter. Offer the next free chapter slot in that family.
    extends_candidates: list[tuple[str, int]] = []
    inferred_set = set(inferred)
    for slug, manifest in modules.items():
        if manifest.get("extends"):
            continue
        required = set(manifest.get("requires_mods") or [])
        if required and required < inferred_set:
            used_chapters = {chapter_number_of(m) for m in modules.values()
                             if m is manifest or m.get("extends") == slug}
            nxt = 2
            while nxt in used_chapters:
                nxt += 1
            extends_candidates.append((slug, nxt))

    statuses = free_module_numbers(modules, repo_root)
    module_number = next_free_module_number(statuses)
    slug = None
    if primary:
        slug = f"{module_number:02x}-{primary.replace('_', '-')}"

    return Suggestion(inferred, primary, group, reimport_slug,
                      sorted(extends_candidates), module_number, slug)


# --------------------------------------------------------------------------
# planning and applying
# --------------------------------------------------------------------------

@dataclass
class ImportPlan:
    slug: str
    module_number: int
    chapter_number: int
    group_key: str
    extends: str | None
    requires: list[str]
    name: str
    title: str
    filename: str
    #: slug -> (target path, renumbered tree). One entry normally; several
    #: when a merged family chapter was split back into its modules.
    trees: dict[str, tuple[Path, dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    inferred_mods: list[str] = field(default_factory=list)
    quest_count: int = 0
    manifest: dict = field(default_factory=dict)
    manifest_path: Path | None = None
    registry_row: str | None = None


def _coord(value) -> float:
    return float(value.value if isinstance(value, snbt.Num) else value)


def _shift(quests: list[dict], dx: float, dy: float) -> None:
    for quest in quests:
        quest["x"] = snbt.Num(_coord(quest["x"]) + dx, "d")
        quest["y"] = snbt.Num(_coord(quest["y"]) + dy, "d")


def _split_family(tree: dict, module_number: int, chapter_number: int,
                  modules: dict[str, dict], mc_version: str,
                  repo_root: Path) -> tuple[dict[int, tuple[str, dict]], list[str]]:
    """Route quests of a merged family chapter back to their own modules.

    The installer merges addon quests into the parent's chapter, moving them
    by a uniform translation (compose.plan_offsets). Reversing that means:
    group quests by the chapter byte their library IDs carry, anchor each
    group against the addon's repo chapter to recover the translation, and
    subtract it. Quests with no library ID stay with the target chapter.
    """
    notes: list[str] = []
    slot_of = {}
    for slug, manifest in modules.items():
        if manifest["module_number"] == module_number:
            slot_of[chapter_number_of(manifest)] = slug

    groups: dict[int, list[dict]] = {}
    remaining: list[dict] = []
    for quest in quests_of(tree):
        routed = False
        value = ident(quest)
        if qid.is_library_id(value):
            try:
                parts = qid.parse(value)
                cc = parts["chapter"]
                if parts["module"] == module_number and cc != chapter_number \
                        and parts["type"] == "quest" and cc in slot_of:
                    groups.setdefault(cc, []).append(quest)
                    routed = True
            except ValueError:
                pass
        if not routed:
            remaining.append(quest)

    tree["quests"] = remaining
    split: dict[int, tuple[str, dict]] = {}
    for cc, quests in sorted(groups.items()):
        slug = slot_of[cc]
        chapter_files = sorted((repo_root / "modules" / slug / mc_version
                                / "chapters").glob("*.snbt"))
        if not chapter_files:
            # No shell to rebuild the chapter from; leave the quests where
            # they are and let renumber treat them as the target's.
            tree["quests"].extend(quests)
            notes.append(f"warning: {len(quests)} quest(s) carry chapter "
                         f"0x{cc:02X} IDs but {slug} has no chapter file -- "
                         f"kept in the main chapter")
            continue
        shell = snbt.load(str(chapter_files[0]))
        repo_pos = {ident(q): (_coord(q["x"]), _coord(q["y"]))
                    for q in quests_of(shell)}
        deltas = [(_coord(q["x"]) - repo_pos[ident(q)][0],
                   _coord(q["y"]) - repo_pos[ident(q)][1])
                  for q in quests if ident(q) in repo_pos]
        if deltas:
            dx, dy = max(set(deltas), key=deltas.count)
        else:
            xs = [_coord(q["x"]) for q in quests]
            ys = [_coord(q["y"]) for q in quests]
            dx = min(xs)
            dy = round((min(ys) + max(ys)) / 2 / 1.5) * 1.5
        _shift(quests, -dx, -dy)
        shell["quests"] = quests
        split[cc] = (slug, shell)
        notes.append(f"split {len(quests)} quest(s) back into {slug} "
                     f"(undoing merge offset {dx:+g}, {dy:+g})")
    return split, notes


def build_manifest(slug: str, module_number: int, chapter_number: int,
                   name: str, filename: str, title: str, group: str | None,
                   requires: list[str], extends: str | None,
                   mc_version: str, repo_root: Path = REPO_ROOT) -> dict:
    path = repo_root / "modules" / slug / "module.json"
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
            "chapters": [],
            "allowed_external_dependencies": [],
        }
        if group is not None:
            manifest["chapter_group"] = group
        if extends:
            manifest["extends"] = extends
            if extends not in manifest["requires_modules"]:
                manifest["requires_modules"].append(extends)

    entries = [c for c in manifest.get("chapters", [])
               if c.get("number") != chapter_number]
    entries.append({"number": chapter_number, "file": filename, "title": title})
    manifest["chapters"] = sorted(entries, key=lambda c: c["number"])
    return manifest


def plan_import(quests_dir: Path, chapter_stem: str, *,
                into: str | None = None, new_slug: str | None = None,
                module_number: int | None = None,
                chapter_number: int | None = None,
                group: str | None = None, requires: list[str] | None = None,
                extends: str | None = None, name: str | None = None,
                mc_version: str = "1.21.1",
                repo_root: Path = REPO_ROOT) -> ImportPlan:
    """Everything an import will do, computed before anything is written."""
    if bool(into) == bool(new_slug):
        raise Problem("give exactly one of a target module or a new slug")

    source = quests_dir / "chapters" / f"{chapter_stem}.snbt"
    if not source.exists():
        raise Problem(f"{source} does not exist")
    try:
        tree = snbt.load(str(source))
    except Exception as exc:                           # noqa: BLE001
        raise Problem(f"could not parse {source.name}: {exc}")

    modules = known_modules(mc_version, repo_root)
    groups = {g["key"]: g for g in load_library(repo_root)["chapter_groups"]}
    notes: list[str] = []

    # -- resolve the slot --------------------------------------------------
    if into:
        if into not in modules:
            raise Problem(f"no module {into!r}; have {', '.join(sorted(modules))}")
        manifest = modules[into]
        slug = into
        module_number = manifest["module_number"]
        chapter_number = chapter_number_of(manifest)
        group = manifest.get("chapter_group")
        allowed = set(manifest.get("allowed_external_dependencies") or [])
        extends = manifest.get("extends")
        registry_row = None
    else:
        slug = new_slug
        slug_module, slug_chapter = parse_slug(slug)
        existing = modules.get(slug)
        if existing:
            # The manifest is the authority for an already-created module;
            # contradicting flags are a mistake, not an instruction.
            for label, given, actual in (
                    ("--module", module_number, existing["module_number"]),
                    ("--chapter-number", chapter_number,
                     chapter_number_of(existing))):
                if given is not None and given != actual:
                    raise Problem(f"{slug} already exists with "
                                  f"{label.lstrip('-').replace('-', '_')}="
                                  f"{actual}; {label} {given} contradicts it")
            module_number = existing["module_number"]
            chapter_number = chapter_number_of(existing)
            group = group or existing.get("chapter_group")
            extends = extends or existing.get("extends")
            notes.append(f"{slug} already exists -- using its manifest's slot")
        else:
            if module_number is not None and module_number != slug_module:
                raise Problem(f"slug prefix {slug.split('-', 1)[0]!r} encodes "
                              f"module {slug_module} (0x{slug_module:02X}), "
                              f"but --module says {module_number}")
            if chapter_number is not None and chapter_number != slug_chapter:
                raise Problem(f"slug {slug!r} encodes chapter {slug_chapter}, "
                              f"but --chapter-number says {chapter_number}")
            module_number = slug_module
            chapter_number = slug_chapter
            taken = {m["module_number"]: s for s, m in modules.items()
                     if not m.get("extends")}
            if module_number in taken and extends != taken[module_number]:
                raise Problem(
                    f"module number {module_number} (0x{module_number:02X}) is "
                    f"already {taken[module_number]}. Pick a free one (see "
                    f"--list --free), or pass --extends {taken[module_number]} "
                    f"to add a chapter to it.")
            statuses = free_module_numbers(modules, repo_root)
            status = statuses.get(module_number, "")
            if status.startswith("released"):
                raise Problem(f"module number 0x{module_number:02X} was "
                              f"released and must never be reused")
        allowed = set((existing or {}).get("allowed_external_dependencies") or [])
        registry_row = None  # filled in below once requires are known

    # -- text and structure recovery --------------------------------------
    tree, lang_notes = recover_lang(quests_dir, tree)
    notes.extend(lang_notes)

    suggestion = suggest_slot(tree, modules, repo_root)
    if group is None:
        group = suggestion.group
        notes.append(f"chapter group {group!r} suggested from the chapter's "
                     f"items (override with --group)")
    if group not in groups:
        raise Problem(f"unknown chapter group {group!r}; have "
                      f"{', '.join(sorted(groups))}")

    split, split_notes = _split_family(tree, module_number, chapter_number,
                                       modules, mc_version, repo_root)
    notes.extend(split_notes)

    title = str(tree.get("title") or name or slug)
    tree["filename"] = chapter_stem
    tree["group"] = groups[group]["id"]

    tree, renumber_notes = renumber(tree, module_number, chapter_number, allowed)
    notes.extend(renumber_notes)

    trees: dict[str, tuple[Path, dict]] = {
        slug: (repo_root / "modules" / slug / mc_version / "chapters"
               / f"{chapter_stem}.snbt", tree)
    }
    for cc, (addon_slug, addon_tree) in split.items():
        addon_manifest = modules[addon_slug]
        addon_allowed = set(addon_manifest.get(
            "allowed_external_dependencies") or [])
        addon_tree, addon_notes = renumber(addon_tree, module_number, cc,
                                           addon_allowed)
        notes.extend(f"{addon_slug}: {n}" for n in addon_notes)
        filename = addon_tree.get("filename") or addon_slug
        trees[addon_slug] = (repo_root / "modules" / addon_slug / mc_version
                             / "chapters" / f"{filename}.snbt", addon_tree)

    inferred = sorted(mods_used(tree))
    if requires is None:
        if into or (modules.get(slug) or {}).get("requires_mods"):
            requires = list((modules.get(slug) or {}).get("requires_mods")
                            or inferred)
        else:
            requires = inferred

    manifest = build_manifest(slug, module_number, chapter_number,
                              name or title, f"{chapter_stem}.snbt", title,
                              group, list(requires), extends, mc_version,
                              repo_root)
    if not into and slug not in modules:
        registry_row = (f"| `{module_number:02X}` | `{slug}` | "
                        f"`{', '.join(requires) or 'vanilla'}` | shipped |")

    return ImportPlan(
        slug=slug, module_number=module_number, chapter_number=chapter_number,
        group_key=group, extends=extends, requires=list(requires),
        name=name or title, title=title, filename=chapter_stem,
        trees=trees, notes=notes, inferred_mods=inferred,
        quest_count=sum(len(quests_of(t)) for _p, t in trees.values()),
        manifest=manifest,
        manifest_path=repo_root / "modules" / slug / "module.json",
        registry_row=registry_row,
    )


def insert_registry_row(repo_root: Path, row: str, module_number: int) -> bool:
    """Insert a module row into ID-REGISTRY.md's allocation table, sorted.

    Returns False when the table cannot be located; the caller should then
    print the row for the human to place. Never guesses.
    """
    registry = repo_root / "ID-REGISTRY.md"
    if not registry.exists():
        return False
    lines = registry.read_text(encoding="utf-8").splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines)
                   if line.startswith("| `MM` | Module |")), None)
    if header is None:
        return False
    row_re = re.compile(r"^\|\s*`([0-9A-Fa-f]{2})`\s*\|")
    insert_at = None
    i = header + 2                      # skip the |---| separator
    while i < len(lines):
        match = row_re.match(lines[i])
        if not match:
            break
        if int(match.group(1), 16) > module_number and insert_at is None:
            insert_at = i
        i += 1
    lines.insert(insert_at if insert_at is not None else i, row + "\n")
    registry.write_text("".join(lines), encoding="utf-8")
    return True


def apply_import(plan: ImportPlan, repo_root: Path = REPO_ROOT,
                 update_registry: bool = False) -> list[Path]:
    """Write a plan's files. Returns the paths written."""
    written: list[Path] = []
    for _slug, (target, tree) in plan.trees.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        snbt.dump(tree, str(target))
        written.append(target)

    plan.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    plan.manifest_path.write_text(json.dumps(plan.manifest, indent=2) + "\n",
                                  encoding="utf-8")
    written.append(plan.manifest_path)

    if plan.registry_row and update_registry:
        if insert_registry_row(repo_root, plan.registry_row,
                               plan.module_number):
            written.append(repo_root / "ID-REGISTRY.md")
        else:
            plan.notes.append("could not locate the module table in "
                              "ID-REGISTRY.md -- add this row by hand:\n  "
                              + plan.registry_row)
    return written


# --------------------------------------------------------------------------
# drafting a chapter from nothing
# --------------------------------------------------------------------------

def draftable_mods(instance: Path, mc_version: str = "1.21.1",
                   repo_root: Path = REPO_ROOT) -> list[str]:
    """Mods in the instance the library has no chapter for yet."""
    import install
    modules = known_modules(mc_version, repo_root)
    covered = {mod for manifest in modules.values()
               for mod in manifest.get("requires_mods") or []}
    return sorted(install.detect_mods(instance) - covered - {"minecraft"})


def _group_key_for(mod: str, repo_root: Path) -> str:
    categories_path = repo_root / "tools" / "data" / "mod-categories.json"
    if categories_path.exists():
        payload = json.loads(categories_path.read_text(encoding="utf-8"))
        category = payload.get("mods", {}).get(mod)
        info = payload.get("categories", {}).get(category)
        if info and info.get("group"):
            return info["group"]
    return "mods"


def draft_module(instance: Path, mod: str, *, max_quests: int = 14,
                 collapse: bool = True, mc_version: str = "1.21.1",
                 repo_root: Path = REPO_ROOT,
                 install_into_instance: bool = True) -> dict:
    """Create a library module for `mod` with nothing pre-authored.

    The chapter is drafted from the mod's own recipe graph (derive_chapter),
    written into the library under the next free module number, and -- by
    default -- also installed into the instance's questbook, so the next
    launch shows it in game ready to polish. The draft mints computed IDs,
    so a later re-import of the polished chapter keeps them all.
    """
    import derive_chapter as dc

    jar = dc.find_jar(mod, [instance / "mods"])
    if jar is None:
        raise Problem(f"no jar in {instance / 'mods'} declares mod id {mod!r}")
    recipes, tags = dc.read_jar(jar, mod)
    if not recipes:
        raise Problem(
            f"{mod} has no recipes producing its own items, so there is no "
            f"progression to draft a chapter from. Library mods and "
            f"client-side utilities usually look like this.")

    modules = known_modules(mc_version, repo_root)
    number = next_free_module_number(free_module_numbers(modules, repo_root))
    slug = f"{number:02x}-{mod.replace('_', '-')}"
    group_key = _group_key_for(mod, repo_root)
    groups = {g["key"]: g for g in load_library(repo_root)["chapter_groups"]}
    group = groups[group_key]

    deps, depth, dropped = dc.build_graph(recipes, tags, mod, collapse)
    chosen = dc.pick(deps, depth, max_quests, collapse)
    if not chosen:
        raise Problem(f"{mod}'s recipe graph collapsed to nothing usable")
    tree = dc.build_chapter(chosen, deps, depth, number, 1, mod, group["id"])

    # Sit after the group's existing chapters rather than on top of them.
    peers = [_as_int(t.get("order_index", 0))
             for slug_ in modules
             for path in (repo_root / "modules" / slug_ / mc_version
                          / "chapters").glob("*.snbt")
             for t in [snbt.load(str(path))] if t.get("group") == group["id"]]
    tree["order_index"] = (max(peers) + 1) if peers else 0

    chapter_path = (repo_root / "modules" / slug / mc_version / "chapters"
                    / f"{mod}.snbt")
    chapter_path.parent.mkdir(parents=True)
    snbt.dump(tree, str(chapter_path))
    manifest = build_manifest(slug, number, 1, tree["title"], f"{mod}.snbt",
                              tree["title"], group_key, [mod], None,
                              mc_version, repo_root)
    manifest["description"] = (f"{tree['title']}. Drafted from the mod's "
                               f"recipe graph -- polish before shipping.")
    manifest_path = repo_root / "modules" / slug / "module.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                             encoding="utf-8")

    installed_to = None
    if install_into_instance:
        import install
        quests_dir = instance / QUESTS_SUBPATH
        (quests_dir / "chapters").mkdir(parents=True, exist_ok=True)
        installed_to = quests_dir / "chapters" / f"{mod}.snbt"
        snbt.dump(tree, str(installed_to))
        install.merge_chapter_groups(quests_dir, [
            {"id": group["id"], "order_index": group["order_index"],
             "title": group["title"]}])

    return {
        "slug": slug,
        "module_number": number,
        "quest_count": len(tree["quests"]),
        "dropped_recipes": dropped,
        "written": [chapter_path, manifest_path],
        "installed_to": installed_to,
        "registry_row": (f"| `{number:02X}` | `{slug}` | `{mod}` | "
                         f"draft |"),
    }


def _as_int(value) -> int:
    return int(value.value if isinstance(value, snbt.Num) else value)


# --------------------------------------------------------------------------

def do_list(quests_dir: Path, mc_version: str, show_free: bool = False,
            repo_root: Path = REPO_ROOT) -> int:
    candidates = scan(quests_dir, mc_version, repo_root)
    if not candidates:
        print(f"no chapters found in {quests_dir / 'chapters'}", file=sys.stderr)
        return 1

    print(f"chapters in {quests_dir / 'chapters'}:\n")
    for c in candidates:
        if c.parse_error:
            print(f"  {c.stem:<28} (could not parse: {c.parse_error})")
        else:
            print(f"  {c.stem:<28} {c.quest_count:>3} quests   {c.origin}")

    if show_free:
        statuses = free_module_numbers(known_modules(mc_version, repo_root),
                                       repo_root)
        print("\nmodule numbers 0x20+:")
        number = MOD_NUMBER_BASE
        shown = 0
        while shown < 12 and number <= 0xFF:
            status = statuses.get(number, "free")
            print(f"  0x{number:02X} ({number:>3})  {status}")
            if status == "free":
                shown += 1
            number += 1
    print("\nImport one with --chapter <name> --into <module> (or --new).")
    return 0


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
                        help="adopt as a new module with this directory name; "
                             "the slug prefix encodes the module number")
    parser.add_argument("--module", type=int,
                        help="module number for --new (decimal); optional -- "
                             "the slug encodes it, and they must agree")
    parser.add_argument("--chapter-number", type=int, default=None)
    parser.add_argument("--name", help="human name for --new")
    parser.add_argument("--group", default=None,
                        help="chapter group key from library.json "
                             "(suggested from the chapter's items if omitted)")
    parser.add_argument("--requires", nargs="*", default=None,
                        help="mod ids the chapter needs (inferred if omitted)")
    parser.add_argument("--extends", help="parent module slug, for an add-on")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--list", action="store_true",
                        help="list the instance's chapters and exit")
    parser.add_argument("--free", action="store_true",
                        help="with --list, also show free module numbers")
    parser.add_argument("--update-registry", action="store_true",
                        help="insert the new module's row into ID-REGISTRY.md")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    quests_dir = args.quests_dir or (args.instance / QUESTS_SUBPATH)
    if not quests_dir.is_dir():
        print(f"error: no quests folder at {quests_dir}", file=sys.stderr)
        return 1

    if args.list:
        return do_list(quests_dir, args.mc_version, args.free)

    if not args.chapter:
        parser.error("--chapter is required (or use --list)")
    if bool(args.into) == bool(args.new):
        parser.error("give exactly one of --into or --new")

    try:
        plan = plan_import(
            quests_dir, args.chapter, into=args.into, new_slug=args.new,
            module_number=args.module, chapter_number=args.chapter_number,
            group=args.group, requires=args.requires, extends=args.extends,
            name=args.name, mc_version=args.mc_version,
        )
    except Problem as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"source : {quests_dir / 'chapters' / (args.chapter + '.snbt')}")
    for slug, (target, tree) in plan.trees.items():
        print(f"target : {target.relative_to(REPO_ROOT)}  "
              f"({len(quests_of(tree))} quests)")
    print(f"module : {plan.slug}  (0x{plan.module_number:02X} / chapter "
          f"0x{plan.chapter_number:02X})")
    print(f"group  : {plan.group_key}")
    if plan.inferred_mods:
        print(f"mods   : {', '.join(plan.inferred_mods)}")
    for note in plan.notes:
        print(f"  {note}")
    if plan.registry_row and not args.update_registry:
        print(f"\nID-REGISTRY.md row (add it, or pass --update-registry):")
        print(f"  {plan.registry_row}")

    if args.dry_run:
        print("\ndry run -- nothing written.")
        return 0

    written = apply_import(plan, update_registry=args.update_registry)
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print("\nNow run:  python3 tools/validate.py")
    print("and then: python3 tools/testbed.py run --modules 00-core " + plan.slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
