"""Lint the questbook library.

A drop-in module library fails in ways a single hand-authored pack does not,
and almost all of them are silent: the book loads, and something is just quietly
wrong twenty quests in. This checks the failure modes that matter:

  ERRORS -- these break someone's game
    * malformed SNBT
    * an ID that is not 16 uppercase hex characters
    * the same ID used twice anywhere in the library
    * an ID whose encoded module number disagrees with the module it lives in
    * a quest depending on a quest in another chapter (breaks droppability)
    * a reward pointing at a reward table that does not exist
    * a chapter whose `filename` field disagrees with its actual filename

  WARNINGS -- these are probably mistakes
    * an item ID absent from every manifest (usually a typo)
    * a mod namespace used without being declared in module.json
    * two quests occupying the same grid position

The cross-chapter dependency check is the one doing the most work. FTB Quests
resolves dependencies by raw ID with no notion of which file they came from, so
nothing stops you writing one -- and it works perfectly in your dev instance,
where every chapter is present. It only breaks for the person who installs the
one chapter they wanted, which is exactly the audience this library exists for.

usage:
    python3 validate.py                     # check everything
    python3 validate.py --module 01-getting-started
    python3 validate.py --mc-version 1.21.1 --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import compose
import qid
import snbt

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

#: Shapes FTB Quests ships with. Anything else needs a theme file the
#: installing pack will not have.
QUEST_SHAPES = {"", "default", "circle", "square", "rsquare", "diamond",
                "pentagon", "hexagon", "octagon", "heart", "gear"}

ERROR, WARNING = "error", "warning"


@dataclass
class Finding:
    level: str
    where: str
    message: str

    def __str__(self) -> str:
        tag = "ERROR  " if self.level == ERROR else "warning"
        return f"  {tag}  {self.where}\n           {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.findings.append(Finding(ERROR, where, message))

    def warn(self, where: str, message: str) -> None:
        self.findings.append(Finding(WARNING, where, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]


# --------------------------------------------------------------------------
# manifests
# --------------------------------------------------------------------------

def load_manifests(mc_version: str) -> tuple[set[str], dict[str, str]]:
    """Return every known item ID, plus {namespace: source description}."""
    known: set[str] = set()
    sources: dict[str, str] = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        if mc_version not in path.name:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for namespace, info in payload.get("namespaces", {}).items():
            known.update(info.get("items", []))
            sources.setdefault(namespace, info.get("from_jar", path.name))
    return known, sources


# --------------------------------------------------------------------------
# traversal helpers
# --------------------------------------------------------------------------

def walk(node, trail: str = ""):
    """Yield (trail, node) for every dict in the tree."""
    if isinstance(node, dict):
        yield trail or "<root>", node
        for key, value in node.items():
            yield from walk(value, f"{trail}.{key}" if trail else key)
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from walk(value, f"{trail}[{i}]")


def item_refs(node, trail: str = ""):
    """Yield (trail, item_id) for every item reference in the tree.

    Item references appear as `item: {id: ..., count: ...}` and as
    `icon: {id: ...}`. Custom icons carry a texture path under the
    ftbquests namespace rather than an item, so those are skipped.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else key
            if key in ("item", "icon"):
                if isinstance(value, str) and ":" in value:
                    if not value.startswith("ftbquests:"):
                        yield here, value
                elif isinstance(value, dict):
                    ident = value.get("id")
                    if isinstance(ident, str) and ":" in ident:
                        if not ident.startswith("ftbquests:"):
                            yield here, ident
            yield from item_refs(value, here)
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from item_refs(value, f"{trail}[{i}]")


# --------------------------------------------------------------------------
# module loading
# --------------------------------------------------------------------------

@dataclass
class LoadedModule:
    number: int
    slug: str
    manifest: dict
    root: Path
    #: Chapter byte for this module's objects. Addons share their parent's
    #: module number and are distinguished by this instead, so an addon costs
    #: a chapter slot rather than one of the 256 module numbers.
    chapter_number: int = 1
    chapters: dict[Path, dict] = field(default_factory=dict)
    reward_tables: dict[Path, dict] = field(default_factory=dict)

    @property
    def extends(self) -> str | None:
        return self.manifest.get("extends")


def discover_modules(modules_dir: Path, mc_version: str, only: str | None,
                     report: Report) -> list[LoadedModule]:
    loaded: list[LoadedModule] = []
    if not modules_dir.exists():
        return loaded

    for module_dir in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
        if only and module_dir.name != only:
            continue
        manifest_path = module_dir / "module.json"
        if not manifest_path.exists():
            report.error(str(module_dir.relative_to(REPO_ROOT)),
                         "module.json is missing")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.error(str(manifest_path.relative_to(REPO_ROOT)),
                         f"module.json is not valid JSON: {exc}")
            continue

        number = manifest.get("module_number")
        if not isinstance(number, int) or not 0 <= number <= 255:
            report.error(str(manifest_path.relative_to(REPO_ROOT)),
                         f"module_number must be an integer 0-255, got {number!r}")
            continue

        version_root = module_dir / mc_version
        if not version_root.exists():
            continue  # module does not target this MC version; not an error

        chapter_number = manifest.get("chapter_number", 1)
        if not isinstance(chapter_number, int) or not 1 <= chapter_number <= 255:
            report.error(str(manifest_path.relative_to(REPO_ROOT)),
                         f"chapter_number must be an integer 1-255, "
                         f"got {chapter_number!r}")
            continue

        module = LoadedModule(number=number, slug=module_dir.name,
                              manifest=manifest, root=version_root,
                              chapter_number=chapter_number)

        for kind, bucket in (("chapters", module.chapters),
                             ("reward_tables", module.reward_tables)):
            for snbt_path in sorted((version_root / kind).glob("*.snbt")):
                try:
                    bucket[snbt_path] = snbt.load(str(snbt_path))
                except (snbt.SNBTError, OSError) as exc:
                    report.error(str(snbt_path.relative_to(REPO_ROOT)),
                                 f"could not parse: {exc}")
        loaded.append(module)

    return loaded


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_ids(modules: list[LoadedModule], report: Report) -> dict[str, str]:
    """Verify every ID is well-formed, unique, and in its module's range."""
    seen: dict[str, str] = {}

    for module in modules:
        for path, tree in list(module.chapters.items()) + list(module.reward_tables.items()):
            rel = str(path.relative_to(REPO_ROOT))
            for trail, node in walk(tree):
                ident = node.get("id")
                if not isinstance(ident, str):
                    continue
                # `id` also names items and icons ("minecraft:stone"); those
                # are not object IDs and are checked separately.
                if ":" in ident:
                    continue
                where = f"{rel} :: {trail}"

                if not qid.is_valid_id(ident):
                    report.error(where, f"{ident!r} is not 16 uppercase hex characters")
                    continue
                if ident in seen:
                    report.error(where, f"ID {ident} is already used by {seen[ident]}")
                    continue
                seen[ident] = where

                if not qid.is_library_id(ident):
                    report.error(
                        where,
                        f"ID {ident} does not start with the library marker "
                        f"{qid.LIBRARY_MARKER!r}. Every ID must come from qid.py so "
                        f"modules cannot collide -- see ID-REGISTRY.md."
                    )
                    continue
                parsed = qid.parse(ident)
                if parsed["module"] != module.number:
                    report.error(
                        where,
                        f"ID {ident} encodes module 0x{parsed['module']:02X} but "
                        f"lives in module 0x{module.number:02X} ({module.slug})"
                    )
                    continue
                # Chapter byte 0 means module-scoped (reward tables, groups);
                # anything else must be this module's own chapter slot.
                if parsed["chapter"] not in (0, module.chapter_number):
                    report.error(
                        where,
                        f"ID {ident} encodes chapter 0x{parsed['chapter']:02X} but "
                        f"{module.slug} owns chapter 0x{module.chapter_number:02X}. "
                        f"Addons share their parent's module number and must use "
                        f"their own chapter_number -- see docs/MODULE-SPEC.md."
                    )
    return seen


def check_chapter_slots(modules: list[LoadedModule], report: Report) -> None:
    """No two modules may claim the same (module number, chapter number).

    Addons deliberately reuse their parent's module number, so the chapter byte
    is the only thing keeping their IDs apart. A duplicate here would silently
    collide every ID in both modules.
    """
    claimed: dict[tuple[int, int], str] = {}
    for module in modules:
        slot = (module.number, module.chapter_number)
        if slot in claimed:
            report.error(
                f"modules/{module.slug}/module.json",
                f"claims module 0x{slot[0]:02X} chapter 0x{slot[1]:02X}, already "
                f"used by {claimed[slot]}. Give this module its own "
                f"chapter_number."
            )
        else:
            claimed[slot] = module.slug


def check_addon_parents(modules: list[LoadedModule], report: Report) -> None:
    """An addon must name a parent that exists and must depend on it."""
    by_slug = {m.slug: m for m in modules}
    for module in modules:
        parent_slug = module.extends
        if not parent_slug:
            continue
        parent = by_slug.get(parent_slug)
        if parent is None:
            report.error(f"modules/{module.slug}/module.json",
                         f"extends {parent_slug!r}, which is not a module here")
            continue
        if parent.number != module.number:
            report.error(
                f"modules/{module.slug}/module.json",
                f"extends {parent_slug} but uses module number "
                f"0x{module.number:02X} instead of the parent's "
                f"0x{parent.number:02X}. Addons share the parent's module number."
            )
        if parent_slug not in module.manifest.get("requires_modules", []):
            report.error(
                f"modules/{module.slug}/module.json",
                f"extends {parent_slug} but does not list it in "
                f"requires_modules, so installing this alone would leave it "
                f"without the chapter it extends"
            )
        parent_mods = set(parent.manifest.get("requires_mods", []))
        own_mods = set(module.manifest.get("requires_mods", []))
        if not parent_mods <= own_mods:
            report.error(
                f"modules/{module.slug}/module.json",
                f"extends {parent_slug} but does not require its mods "
                f"({', '.join(sorted(parent_mods - own_mods))}), so auto-detection "
                f"could install it into a pack the parent cannot run in"
            )


def check_chapter_filenames(modules: list[LoadedModule], report: Report) -> None:
    for module in modules:
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            declared = tree.get("filename")
            if declared is None:
                report.error(rel, "chapter is missing the `filename` field")
            elif declared != path.stem:
                report.error(
                    rel,
                    f"`filename` is {declared!r} but the file is {path.stem!r}; "
                    f"FTB Quests will rename the file on first save"
                )


def check_dependencies(modules: list[LoadedModule], report: Report) -> None:
    """No quest may depend on a quest outside its own chapter file."""
    for module in modules:
        allowed_external = set(module.manifest.get("allowed_external_dependencies", []))
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            quests = tree.get("quests") or []
            local = {q.get("id") for q in quests if isinstance(q, dict)}

            for index, quest in enumerate(quests):
                if not isinstance(quest, dict):
                    continue
                for dependency in quest.get("dependencies") or []:
                    if dependency in local or dependency in allowed_external:
                        continue
                    report.error(
                        f"{rel} :: quests[{index}] ({quest.get('id')})",
                        f"depends on {dependency}, which is not in this chapter. "
                        f"Cross-chapter dependencies break drop-in installation -- "
                        f"if this is deliberate, list it in module.json under "
                        f"\"allowed_external_dependencies\"."
                    )


def check_reward_tables(modules: list[LoadedModule], report: Report) -> None:
    """Every table_id must resolve to a reward table shipped by the library."""
    table_longs: dict[int, str] = {}
    for module in modules:
        for path, tree in module.reward_tables.items():
            ident = tree.get("id")
            if isinstance(ident, str) and qid.is_valid_id(ident):
                table_longs[qid.to_long(ident)] = f"{module.slug}/{path.name}"

    for module in modules:
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            for trail, node in walk(tree):
                if "table_id" not in node:
                    continue
                raw = node["table_id"]
                value = raw.value if isinstance(raw, snbt.Num) else raw
                if not isinstance(value, int):
                    report.error(f"{rel} :: {trail}",
                                 f"table_id must be a long, got {raw!r}")
                elif value not in table_longs:
                    report.error(
                        f"{rel} :: {trail}",
                        f"table_id {value}L does not match any reward table in the "
                        f"library. Reward tables are referenced by their signed "
                        f"64-bit long, not their hex ID -- use "
                        f"`python3 tools/qid.py <hex-id>` to get the right value."
                    )


def check_items(modules: list[LoadedModule], known: set[str],
                sources: dict[str, str], report: Report) -> None:
    for module in modules:
        declared = set(module.manifest.get("requires_mods", [])) | {"minecraft"}
        for path, tree in list(module.chapters.items()) + list(module.reward_tables.items()):
            rel = str(path.relative_to(REPO_ROOT))
            for trail, item in item_refs(tree):
                namespace = item.split(":", 1)[0]
                if namespace not in declared:
                    report.warn(
                        f"{rel} :: {trail}",
                        f"uses {item} from namespace {namespace!r}, which is not "
                        f"listed in module.json \"requires_mods\". Anyone without "
                        f"that mod will see a broken entry."
                    )
                if item not in known:
                    hint = ""
                    if namespace in sources:
                        near = [k for k in known
                                if k.startswith(namespace + ":")
                                and abs(len(k) - len(item)) <= 3
                                and k[-4:] == item[-4:]]
                        if near:
                            hint = f" Did you mean {near[0]}?"
                    report.warn(
                        f"{rel} :: {trail}",
                        f"item {item} is not in any manifest -- likely a typo.{hint}"
                    )


#: Task types this library uses, verified by loading each one in a real
#: server and reading back what FTB Quests normalised it to.
#:
#: `stage` is deliberately absent: FTB accepts it, silently rewrites it to
#: `type: "custom"`, and the quest becomes uncompletable. It needs a game-stage
#: mod or KubeJS to mean anything.
#:
#: `location` is absent for a different reason -- it works, but it pins a quest
#: to fixed coordinates in a specific world, which cannot be portable.
SUPPORTED_TASK_TYPES = {
    "item", "checkmark", "observation", "stat", "biome", "structure",
    "dimension", "kill", "advancement", "xp",
}
UNSUPPORTED_TASK_TYPES = {
    "stage": "FTB rewrites this to type \"custom\" and the quest can never be "
             "completed; it needs a game-stage mod or KubeJS",
    "location": "pins the quest to fixed coordinates in one world, so it cannot "
                "work as portable content",
    "custom": "requires a KubeJS script the installing pack will not have",
    "energy": "requires a mod that provides energy",
    "fluid": "requires a mod that provides fluid handling",
}


def check_task_types(modules: list[LoadedModule], report: Report) -> None:
    for module in modules:
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            for index, quest in enumerate(tree.get("quests") or []):
                if not isinstance(quest, dict):
                    continue
                for task in quest.get("tasks") or []:
                    if not isinstance(task, dict):
                        continue
                    kind = task.get("type")
                    where = f"{rel} :: quests[{index}] task {task.get('id')}"
                    if kind in UNSUPPORTED_TASK_TYPES:
                        report.error(
                            where,
                            f"task type {kind!r} is not usable here: "
                            f"{UNSUPPORTED_TASK_TYPES[kind]}"
                        )
                    elif kind not in SUPPORTED_TASK_TYPES:
                        report.warn(
                            where,
                            f"unrecognised task type {kind!r} -- if this is real, "
                            f"add it to SUPPORTED_TASK_TYPES after checking it "
                            f"loads in the testbed"
                        )


#: Formatting codes FTB Library's TextComponentParser accepts after '&'.
#: 0-9a-f are colours, k-o are styles, r resets.
_FORMAT_CODES = set("0123456789abcdefklmnorABCDEFKLMNOR")

#: Fields whose contents get run through the text parser and shown to players.
_DISPLAY_FIELDS = ("title", "subtitle", "description", "text")


def check_text_formatting(modules: list[LoadedModule], report: Report) -> None:
    """Catch text FTB Library's parser will reject at render time.

    `&` introduces a formatting code, so a plain ampersand in a title -- as in
    "Mining & Caving" -- makes the parser give up and print

        Invalid formatting! You must escape whitespace after & with \\&!

    over the chapter name. Nothing fails to load and nothing appears in the
    server log; you only see it in-game, which is exactly the kind of bug this
    linter exists to catch first. A literal ampersand must be written `\\&`.

    `{` opens a substitution and has the same problem.
    """
    def inspect(text: str, where: str) -> None:
        i = 0
        while i < len(text):
            char = text[i]
            if char == "\\":
                i += 2          # escaped; skip whatever follows
                continue
            if char == "&":
                if i + 1 >= len(text):
                    report.error(where, "text ends with '&'; the parser needs a "
                                        "formatting code after it, or write '\\\\&'")
                elif text[i + 1] not in _FORMAT_CODES:
                    shown = text[i + 1]
                    label = "whitespace" if shown.isspace() else f"{shown!r}"
                    report.error(
                        where,
                        f"'&' followed by {label} -- FTB Library reads '&' as a "
                        f"formatting code and will print an error over this text. "
                        f"For a literal ampersand write '\\\\&' (which is "
                        f"\"\\\\\\\\&\" inside an SNBT string)."
                    )
            elif char == "{":
                report.error(where, "'{' opens a substitution in FTB's text "
                                    "parser; write '\\\\{' for a literal brace")
            i += 1

    for module in modules:
        for path, tree in list(module.chapters.items()) + list(module.reward_tables.items()):
            rel = str(path.relative_to(REPO_ROOT))
            for trail, node in walk(tree):
                for field_name in _DISPLAY_FIELDS:
                    value = node.get(field_name)
                    where = f"{rel} :: {trail}.{field_name}" if trail != "<root>" \
                        else f"{rel} :: {field_name}"
                    if isinstance(value, str):
                        inspect(value, where)
                    elif isinstance(value, list):
                        for i, line in enumerate(value):
                            if isinstance(line, str):
                                inspect(line, f"{where}[{i}]")


def check_chapter_groups(modules: list[LoadedModule], report: Report) -> None:
    """Chapter group, manifest and category registry must all agree.

    Three things have to line up: the `group:` hex ID inside the chapter file,
    the `chapter_group` key in module.json, and the group actually defined by
    00-core. A chapter naming a group nobody registered renders in a broken
    state, and it is easy to miss because nothing errors on load.
    """
    library_path = REPO_ROOT / "library.json"
    categories_path = DATA_DIR / "mod-categories.json"
    if not library_path.exists():
        return

    library = json.loads(library_path.read_text(encoding="utf-8"))
    groups = {g["key"]: g["id"] for g in library.get("chapter_groups", [])}
    if not groups:
        return

    registry = {}
    if categories_path.exists():
        payload = json.loads(categories_path.read_text(encoding="utf-8"))
        registry = {mod: payload["categories"][cat]["group"]
                    for mod, cat in payload.get("mods", {}).items()
                    if cat in payload.get("categories", {})}

    for module in modules:
        key = module.manifest.get("chapter_group")
        manifest_rel = f"modules/{module.slug}/module.json"

        if key is not None and key not in groups:
            report.error(manifest_rel,
                         f"chapter_group {key!r} is not defined in library.json "
                         f"(known: {', '.join(sorted(groups))})")
            continue

        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            declared = tree.get("group")
            if key is None:
                # No chapter_group means deliberately ungrouped -- the chapter
                # sits above every group heading. FTB's convention for that is
                # an empty group string.
                if declared != "":
                    report.error(
                        rel,
                        f"module has no chapter_group, so its chapter must "
                        f"declare group \"\" to install ungrouped -- it "
                        f"declares {declared!r}"
                    )
                continue
            if declared != groups[key]:
                report.error(
                    rel,
                    f"chapter's group is {declared!r} but module.json says "
                    f"{key!r} ({groups[key]}). Installing this would file the "
                    f"chapter under the wrong heading, or none at all."
                )

        # Cross-check against the category registry: the mod this module is
        # actually about should agree with the group it claims.
        own_mods = list(module.manifest.get("requires_mods") or [])
        parent_slug = module.extends
        if parent_slug:
            parent = next((m for m in modules if m.slug == parent_slug), None)
            if parent:
                parent_mods = set(parent.manifest.get("requires_mods") or [])
                own_mods = [m for m in own_mods if m not in parent_mods] or own_mods
        if own_mods and key:
            expected = registry.get(own_mods[0])
            if expected and expected != key:
                report.warn(
                    manifest_rel,
                    f"mod {own_mods[0]!r} is classified as {expected!r} in "
                    f"tools/data/mod-categories.json but this module uses "
                    f"chapter_group {key!r}. Update whichever is wrong."
                )
            elif not expected and own_mods[0] not in registry:
                report.warn(
                    manifest_rel,
                    f"mod {own_mods[0]!r} is not in mod-categories.json, so "
                    f"nothing can classify it automatically. Add it."
                )


def check_shapes(modules: list[LoadedModule], report: Report) -> None:
    """Every quest shape must be one FTB Quests ships with."""
    for module in modules:
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            shape = tree.get("default_quest_shape")
            if isinstance(shape, str) and shape not in QUEST_SHAPES:
                report.error(rel,
                             f"default_quest_shape {shape!r} is not a built-in "
                             f"shape (known: {', '.join(sorted(QUEST_SHAPES))})")
            for quest in tree.get("quests") or []:
                if not isinstance(quest, dict):
                    continue
                shape = quest.get("shape")
                if isinstance(shape, str) and shape not in QUEST_SHAPES:
                    report.error(f"{rel} :: quest {quest.get('id')}",
                                 f"shape {shape!r} is not a built-in shape")


def check_group_registry(mc_version: str, report: Report) -> None:
    """The three group registries must agree, field for field.

    library.json is the author-facing list, 00-core's groups.snbt is what
    actually installs, and 00-core's provides_chapter_groups documents the
    module. They have drifted before; any disagreement means some player
    sees a different heading than the docs promise.
    """
    library_path = REPO_ROOT / "library.json"
    core_manifest_path = REPO_ROOT / "modules" / "00-core" / "module.json"
    snbt_paths = list((REPO_ROOT / "modules" / "00-core" / mc_version
                       / "chapter_groups").glob("*.snbt"))
    if not (library_path.exists() and core_manifest_path.exists() and snbt_paths):
        return

    library = {g["id"]: g for g in
               json.loads(library_path.read_text(encoding="utf-8"))
               .get("chapter_groups", [])}
    provides = json.loads(core_manifest_path.read_text(encoding="utf-8")) \
        .get("provides_chapter_groups", {})
    shipped: dict[str, dict] = {}
    for path in snbt_paths:
        for group in snbt.load(str(path)).get("chapter_groups", []):
            if isinstance(group, dict):
                shipped[group.get("id")] = group

    lib_rel = "library.json"
    core_rel = "modules/00-core/module.json"

    provided_by_id = {info.get("id"): (key, info) for key, info in provides.items()}
    for ident, entry in library.items():
        if ident not in shipped:
            report.error(lib_rel, f"group {entry['key']!r} ({ident}) is not in "
                                  f"00-core's groups.snbt, so it never installs")
        elif shipped[ident].get("title") != entry["title"] or \
                _num(shipped[ident].get("order_index")) != entry.get("order_index"):
            report.error(lib_rel,
                         f"group {entry['key']!r} ({ident}) disagrees with "
                         f"00-core's groups.snbt on title or order_index")
        if ident not in provided_by_id:
            report.error(core_rel, f"provides_chapter_groups is missing group "
                                   f"{entry['key']!r} ({ident})")
        else:
            key, info = provided_by_id[ident]
            if key != entry["key"] or info.get("title") != entry["title"]:
                report.error(core_rel,
                             f"provides_chapter_groups entry {key!r} disagrees "
                             f"with library.json for {ident}")
    for ident in set(shipped) | set(provided_by_id):
        if ident not in library:
            report.error(lib_rel, f"group {ident} is shipped or documented by "
                                  f"00-core but absent from library.json")


def _num(value):
    return int(value.value) if isinstance(value, snbt.Num) else value


#: Half-width of the box a quest icon occupies, in grid units. FTB draws a
#: default-shape quest about one unit across, so a dependency line passing
#: within this of another quest's centre visually runs underneath it.
_ICON_CLEARANCE = 0.55

#: How far along a dependency line to bother looking. Both ends are anchored
#: to a quest by definition, so only the middle stretch can be ambiguous.
_LINE_INTERIOR = (0.08, 0.92)


def _passes_under(start: tuple[float, float], end: tuple[float, float],
                  point: tuple[float, float]) -> bool:
    """Does the segment start->end run under a quest sitting at point?"""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if not length_sq:
        return False
    along = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
    if not _LINE_INTERIOR[0] < along < _LINE_INTERIOR[1]:
        return False
    return (abs(start[0] + along * dx - point[0]) < _ICON_CLEARANCE
            and abs(start[1] + along * dy - point[1]) < _ICON_CLEARANCE)


def _implied_by_siblings(quest_deps: list[str], target: str,
                         deps: dict[str, list[str]]) -> bool:
    """Is `target` already reachable through one of the quest's other deps?

    If so, FTB still draws the edge, so it is a line across the chapter that
    tells the player nothing they did not already know.
    """
    seen: set[str] = set()
    stack = [d for d in quest_deps if d != target]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(deps.get(node, []))
    return target in seen


def _layout_findings(rel: str, tree: dict, report: Report) -> None:
    """Layout checks for one chapter tree, authored or composed."""
    quests = [q for q in tree.get("quests") or [] if isinstance(q, dict)]
    # When the chapter hides dependency lines nothing is drawn, so line
    # geometry cannot mislead anyone -- only overlap still matters.
    check_lines = not tree.get("default_hide_dependency_lines", False)

    positions: dict[tuple[float, float], str] = {}
    place: dict[str, tuple[float, float]] = {}
    title: dict[str, str] = {}
    deps: dict[str, list[str]] = {}

    for quest in quests:
        qid = str(quest.get("id"))
        title[qid] = str(quest.get("title") or qid)
        deps[qid] = [str(d) for d in quest.get("dependencies") or []]

        x, y = quest.get("x"), quest.get("y")
        if x is None or y is None:
            continue
        key = (
            float(x.value if isinstance(x, snbt.Num) else x),
            float(y.value if isinstance(y, snbt.Num) else y),
        )
        place[qid] = key
        if key in positions:
            report.warn(
                f"{rel} :: quest {quest.get('id')}",
                f"sits at the same position ({key[0]}, {key[1]}) as quest "
                f"{positions[key]} -- they will overlap in the book"
            )
        else:
            positions[key] = str(quest.get("id"))

    if not check_lines:
        return

    # A dependency line drawn under an unrelated quest reads as though
    # that quest were part of the chain. In a wide chapter this is the
    # difference between a readable tree and a plate of spaghetti.
    for qid in title:
        local = [d for d in deps[qid] if d in title]
        for dep in local:
            if _implied_by_siblings(local, dep, deps):
                report.warn(
                    f"{rel} :: quest {title[qid]!r}",
                    f"depends on {title[dep]!r}, which another of its "
                    f"dependencies already requires -- the edge is "
                    f"redundant and draws a line across the chapter"
                )
            if qid not in place or dep not in place:
                continue
            for other in title:
                if other in (qid, dep) or other not in place:
                    continue
                if _passes_under(place[dep], place[qid], place[other]):
                    report.warn(
                        f"{rel} :: quest {title[qid]!r}",
                        f"its dependency line from {title[dep]!r} passes "
                        f"under {title[other]!r} -- move one of them"
                    )


def check_layout(modules: list[LoadedModule], report: Report) -> None:
    for module in modules:
        for path, tree in module.chapters.items():
            _layout_findings(str(path.relative_to(REPO_ROOT)), tree, report)


def check_composed_families(modules: list[LoadedModule], ids: dict[str, str],
                            report: Report) -> None:
    """Hold what the installer will actually build to the authored standards.

    validate.py otherwise only sees repo files, but the installed book
    contains composed artifacts: each family merged into one chapter, and a
    generated completionist chapter. Simulate both here -- with every addon
    present, the worst case -- so a layout or ID problem surfaces in CI
    rather than in someone's pack.
    """
    by_slug = {m.slug: m for m in modules}
    for parent_slug, addon_slugs in compose.family_map(by_slug).items():
        parent = by_slug.get(parent_slug)
        if parent is None or not parent.chapters:
            continue
        parent_tree = next(iter(parent.chapters.values()))
        addon_trees = [next(iter(by_slug[slug].chapters.values()))
                       for slug in addon_slugs if by_slug[slug].chapters]
        merged = compose.compose_family(parent_tree, addon_trees)
        _layout_findings(f"<merged> {parent_slug} (+ {', '.join(addon_slugs)})",
                         merged, report)

    targets = [
        compose.Target(chapter_id=str(tree.get("id")),
                       title=str(tree.get("title") or module.slug),
                       icon=tree.get("icon") or {},
                       group_order=0, order_index=0)
        for module in modules
        if module.slug != "09-completionist"
        for tree in module.chapters.values()
    ]
    seen: set[str] = set()
    for quest in compose.completionist_quests(targets):
        for trail, node in walk(quest):
            ident = node.get("id")
            if not isinstance(ident, str) or ":" in ident:
                continue  # item references (icons) carry ids too; skip them
            where = f"<completionist> {trail}"
            if not qid.is_valid_id(ident):
                report.error(where, f"generated ID {ident!r} is malformed")
            elif ident in seen:
                report.error(where, f"generated ID {ident} is minted twice")
            elif ident in ids:
                report.error(where,
                             f"generated ID {ident} collides with an authored "
                             f"object in {ids[ident]}")
            seen.add(ident)


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--module", default=None,
                        help="check only this module directory name")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    args = parser.parse_args(argv)

    report = Report()
    known, sources = load_manifests(args.mc_version)
    if not known:
        report.warn("tools/data",
                    f"no item manifests found for {args.mc_version}; "
                    f"item checks will be skipped")

    modules = discover_modules(REPO_ROOT / "modules", args.mc_version,
                               args.module, report)

    if not modules:
        print(f"no modules found for Minecraft {args.mc_version}"
              + (f" matching {args.module!r}" if args.module else ""))
        return 1 if report.errors else 0

    ids = check_ids(modules, report)
    check_chapter_slots(modules, report)
    check_addon_parents(modules, report)
    check_chapter_filenames(modules, report)
    check_dependencies(modules, report)
    check_reward_tables(modules, report)
    check_task_types(modules, report)
    check_text_formatting(modules, report)
    check_chapter_groups(modules, report)
    check_group_registry(args.mc_version, report)
    check_shapes(modules, report)
    check_layout(modules, report)
    check_composed_families(modules, ids, report)
    if known:
        check_items(modules, known, sources, report)

    chapters = sum(len(m.chapters) for m in modules)
    tables = sum(len(m.reward_tables) for m in modules)
    print(f"Minecraft {args.mc_version}: {len(modules)} modules, "
          f"{chapters} chapters, {tables} reward tables, {len(ids)} unique IDs")

    if report.errors:
        print(f"\n{len(report.errors)} error(s):")
        for finding in report.errors:
            print(finding)
    if report.warnings:
        print(f"\n{len(report.warnings)} warning(s):")
        for finding in report.warnings:
            print(finding)

    if not report.findings:
        print("\nclean.")

    if report.errors:
        return 1
    return 1 if (args.strict and report.warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
