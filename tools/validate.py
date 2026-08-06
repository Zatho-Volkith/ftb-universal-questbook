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

import qid
import snbt

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

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
    chapters: dict[Path, dict] = field(default_factory=dict)
    reward_tables: dict[Path, dict] = field(default_factory=dict)


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

        module = LoadedModule(number=number, slug=module_dir.name,
                              manifest=manifest, root=version_root)

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
                encoded = qid.parse(ident)["module"]
                if encoded != module.number:
                    report.error(
                        where,
                        f"ID {ident} encodes module {encoded:02d} but lives in "
                        f"module {module.number:02d} ({module.slug})"
                    )
    return seen


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


def check_layout(modules: list[LoadedModule], report: Report) -> None:
    for module in modules:
        for path, tree in module.chapters.items():
            rel = str(path.relative_to(REPO_ROOT))
            positions: dict[tuple[float, float], str] = {}
            for quest in tree.get("quests") or []:
                if not isinstance(quest, dict):
                    continue
                x, y = quest.get("x"), quest.get("y")
                if x is None or y is None:
                    continue
                key = (
                    float(x.value if isinstance(x, snbt.Num) else x),
                    float(y.value if isinstance(y, snbt.Num) else y),
                )
                if key in positions:
                    report.warn(
                        f"{rel} :: quest {quest.get('id')}",
                        f"sits at the same position ({key[0]}, {key[1]}) as quest "
                        f"{positions[key]} -- they will overlap in the book"
                    )
                else:
                    positions[key] = str(quest.get("id"))


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
    check_chapter_filenames(modules, report)
    check_dependencies(modules, report)
    check_reward_tables(modules, report)
    check_layout(modules, report)
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
