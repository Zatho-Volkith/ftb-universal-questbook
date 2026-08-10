"""Install questbook modules into a Minecraft instance.

Copying chapter files by hand almost works. The part that does not is
`chapter_groups.snbt`: it is a single shared registry, so a chapter naming a
group that is not registered there renders in a broken state. That one file is
the reason installation needs a tool rather than a README paragraph.

The other thing this does is pick modules for you. Point it at an instance and
it reads the mod IDs out of `mods/*.jar`, then installs exactly the modules
whose requirements are met -- so a cozy pack gets the Farmer's Delight and
Croptopia chapters and skips the rest, with no list to maintain by hand.

usage:
    python3 install.py --instance ~/.minecraft --auto
    python3 install.py --instance ~/.minecraft --modules 00-core 01-getting-started
    python3 install.py --instance ~/.minecraft --auto --dry-run
    python3 install.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import compose
import qid
import snbt

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTS_SUBPATH = Path("config/ftbquests/quests")

# Every chapter-group ID this library has ever shipped. Groups the current
# install does not reference are pruned from the target, but only out of this
# fixed set -- an arbitrary "7B..." match could be someone else's group.
LIBRARY_GROUP_IDS = {f"7B000001000000{n:02X}" for n in range(1, 10)}

_MODID_LINE = re.compile(r'^\s*modId\s*=\s*"([^"]+)"')


def mod_ids_from_toml(text: str) -> set[str]:
    """Mod ids a `mods.toml` DECLARES, not the ones it depends on.

    Both use `modId = "..."`, so searching the whole file counts every
    dependency as an installed mod -- which would offer someone chapters for
    mods their pack merely mentions.
    """
    ids: set[str] = set()
    in_mods_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[["):
            in_mods_section = stripped.startswith("[[mods]]")
            continue
        if stripped.startswith("["):
            in_mods_section = False
            continue
        if in_mods_section:
            match = _MODID_LINE.match(line)
            if match:
                ids.add(match.group(1))
    return ids


@dataclass
class Module:
    slug: str
    number: int
    manifest: dict
    version_root: Path

    @property
    def requires_mods(self) -> list[str]:
        return list(self.manifest.get("requires_mods", []))

    @property
    def requires_modules(self) -> list[str]:
        return list(self.manifest.get("requires_modules", []))

    @property
    def is_required(self) -> bool:
        return bool(self.manifest.get("required", False))

    @property
    def extends(self) -> str | None:
        """Slug of the module this one is an addon for, if any."""
        return self.manifest.get("extends")

    @property
    def chapter_number(self) -> int:
        return self.manifest.get("chapter_number", 1)


# --------------------------------------------------------------------------

def load_modules(mc_version: str, repo_root: Path = REPO_ROOT,
                 ) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    modules_dir = repo_root / "modules"
    if not modules_dir.exists():
        return modules
    for module_dir in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
        manifest_path = module_dir / "module.json"
        version_root = module_dir / mc_version
        if not manifest_path.exists() or not version_root.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        modules[module_dir.name] = Module(
            slug=module_dir.name,
            number=manifest["module_number"],
            manifest=manifest,
            version_root=version_root,
        )
    return modules


def detect_mods(instance: Path) -> set[str]:
    """Read mod IDs out of an instance's mods folder."""
    found: set[str] = {"minecraft"}
    mods_dir = instance / "mods"
    if not mods_dir.is_dir():
        return found

    for jar in sorted(mods_dir.glob("*.jar")):
        try:
            with zipfile.ZipFile(jar) as archive:
                names = set(archive.namelist())
                if "fabric.mod.json" in names:
                    try:
                        payload = json.loads(
                            archive.read("fabric.mod.json").decode("utf-8-sig")
                        )
                        if isinstance(payload, dict) and payload.get("id"):
                            found.add(payload["id"])
                            continue
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                    if toml in names:
                        text = archive.read(toml).decode("utf-8", errors="replace")
                        found.update(mod_ids_from_toml(text))
                        break
        except (zipfile.BadZipFile, OSError):
            continue
    return found


def select_modules(modules: dict[str, Module], available_mods: set[str],
                   ) -> tuple[list[Module], list[tuple[Module, list[str]]]]:
    """Split modules into installable and skipped (with the missing mods)."""
    chosen, skipped = [], []
    for module in modules.values():
        missing = [m for m in module.requires_mods if m not in available_mods]
        if missing and not module.is_required:
            skipped.append((module, missing))
        else:
            chosen.append(module)
    return chosen, skipped


def resolve_module_deps(chosen: list[Module], modules: dict[str, Module],
                        ) -> tuple[list[Module], list[str]]:
    """Pull in required modules (e.g. 00-core) that were not named explicitly."""
    selected = {m.slug: m for m in chosen}
    problems: list[str] = []
    queue = list(chosen)
    while queue:
        module = queue.pop()
        for dep in module.requires_modules:
            if dep in selected:
                continue
            if dep not in modules:
                problems.append(
                    f"{module.slug} requires module {dep!r}, which is not present"
                )
                continue
            selected[dep] = modules[dep]
            queue.append(modules[dep])
    for slug, module in modules.items():
        if module.is_required and slug not in selected:
            selected[slug] = module
    return [selected[k] for k in sorted(selected)], problems


# --------------------------------------------------------------------------

def existing_ids(quests_dir: Path) -> dict[str, str]:
    """Every object ID already present in the target book."""
    found: dict[str, str] = {}
    if not quests_dir.exists():
        return found
    for path in quests_dir.rglob("*.snbt"):
        try:
            tree = snbt.load(str(path))
        except (snbt.SNBTError, OSError):
            continue

        def collect(node):
            if isinstance(node, dict):
                ident = node.get("id")
                if isinstance(ident, str) and qid.is_valid_id(ident):
                    found.setdefault(ident, str(path.name))
                for value in node.values():
                    collect(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    collect(value)

        collect(tree)
    return found


def merge_chapter_groups(quests_dir: Path,
                         incoming: list[dict]) -> tuple[int, int, int]:
    """Add our groups to the target's chapter_groups.snbt without touching theirs.

    Also drops library-owned groups this install no longer references --
    after chapters move between groups, the old heading would otherwise sit
    empty in the book forever. Only IDs from LIBRARY_GROUP_IDS are ever
    removed; anything else in the file belongs to the pack.
    """
    target = quests_dir / "chapter_groups.snbt"
    if target.exists():
        try:
            document = snbt.load(str(target))
        except snbt.SNBTError as exc:
            raise SystemExit(f"cannot merge into {target}: {exc}")
        if not isinstance(document, dict):
            raise SystemExit(f"{target} is not a compound; refusing to overwrite")
    else:
        document = {"chapter_groups": []}

    groups = document.setdefault("chapter_groups", [])
    by_id = {g.get("id"): g for g in groups if isinstance(g, dict)}

    added = 0
    refreshed = 0
    for group in incoming:
        existing = by_id.get(group["id"])
        if existing is None:
            groups.append(dict(group))
            added += 1
            continue
        # We own this group, so re-assert our fields. This matters because FTB
        # migrates group titles into lang/ and strips them from this file; once
        # we clear our stale lang entries there would otherwise be no title left
        # anywhere, and the group would render blank.
        if any(existing.get(k) != v for k, v in group.items()):
            existing.update(group)
            refreshed += 1

    keep_ids = {g["id"] for g in incoming}
    stale = [g for g in groups
             if isinstance(g, dict)
             and g.get("id") in LIBRARY_GROUP_IDS
             and g.get("id") not in keep_ids]
    for group in stale:
        groups.remove(group)

    if added or refreshed or stale:
        if target.exists():
            backup(target)
        snbt.dump(document, str(target))
    return added + refreshed, len(stale), len(groups)


def prune_lang_entries(quests_dir: Path, module_numbers: set[int]) -> int:
    """Drop translation entries belonging to the modules we are installing.

    FTB Quests does not leave authored files alone. On first load it migrates
    every `title`, `subtitle` and `description` out of the chapter file and into
    `lang/en_us.snbt`, keyed by object ID, then rewrites the chapter without
    them. That is fine until we ship an update: the new chapter file carries the
    new wording inline, but the stale lang entry still exists under the same ID,
    and a translation always wins over an object's inline default. The player
    would keep reading last version's text forever.

    So we clear our own entries and let the mod re-migrate from the fresh files.

    Scoped deliberately to the modules being installed. Pruning entries for a
    module we are *not* rewriting would delete text with nothing to restore it,
    because that module's installed chapter file no longer contains any.
    """
    lang_dir = quests_dir / "lang"
    if not lang_dir.is_dir():
        return 0

    removed_total = 0
    for lang_file in sorted(lang_dir.glob("*.snbt")):
        try:
            document = snbt.load(str(lang_file))
        except (snbt.SNBTError, OSError):
            continue
        if not isinstance(document, dict):
            continue

        doomed = []
        for key in document:
            # keys look like: quest.7B01010300010000.quest_desc
            parts = key.split(".")
            if len(parts) < 2 or not qid.is_library_id(parts[1]):
                continue
            if qid.parse(parts[1])["module"] in module_numbers:
                doomed.append(key)

        if not doomed:
            continue
        backup(lang_file)
        for key in doomed:
            del document[key]
        snbt.dump(document, str(lang_file))
        removed_total += len(doomed)

    return removed_total


def ensure_book_data(quests_dir: Path) -> bool:
    """Give a brand-new book a data.snbt so first launch behaves as authored.

    Without this, FTB Quests generates the file on first launch with
    progression_mode "linear" -- and this library's chapters branch, they do
    not form a line. The field set below is exactly what the mod generates
    (verified against a testbed server), with only the progression mode
    changed. An existing data.snbt belongs to the pack and is never touched.
    """
    target = quests_dir / "data.snbt"
    if target.exists():
        return False
    snbt.dump({
        "default_autoclaim_rewards": "disabled",
        "default_consume_items": False,
        "default_quest_disable_jei": False,
        "default_quest_shape": "circle",
        "default_reward_team": False,
        "detection_delay": 20,
        "disable_gui": False,
        "drop_book_on_death": False,
        "drop_loot_crates": False,
        "emergency_items_cooldown": 300,
        "fallback_locale": "en_us",
        "grid_scale": snbt.Num(0.5, "d"),
        "hide_excluded_quests": False,
        "lock_message": "",
        "loot_crate_no_drop": {"boss": 0, "monster": 600, "passive": 4000},
        "pause_game": False,
        "presets": {},
        "progression_mode": "flexible",
        "show_lock_icons": False,
        "verify_on_load": False,
        "version": 13,
    }, str(target))
    return True


def backup(path: Path) -> Path:
    """Copy a file aside before overwriting or removing it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, destination)
    return destination


# --------------------------------------------------------------------------

@dataclass
class PlannedFile:
    dest_rel: str      # path under the quests folder, e.g. "chapters/ae2.snbt"
    payload: bytes
    source_desc: str


@dataclass
class Plan:
    """Everything an install will do, computed before anything is written."""
    files: list[PlannedFile]
    superseded: dict[str, str]    # chapter filename -> the library chapter ID
                                  # that proves the file is ours to remove
    groups: list[dict]            # group entries the planned chapters reference
    module_numbers: set[int]
    merges: dict[str, list[str]]  # parent slug -> addon slugs merged in


def _as_int(value) -> int:
    return int(value.value if isinstance(value, snbt.Num) else value)


def build_plan(chosen: list[Module], modules: dict[str, Module]) -> Plan:
    """Compute the exact files an install will write.

    Addon modules do not become chapters of their own: each family's addons
    are merged into the parent's chapter file via compose.compose_family.
    The completionist chapter, if chosen, gets its quests generated here from
    every other chapter in the plan.
    """
    by_slug = {m.slug: m for m in chosen}
    families = compose.family_map(by_slug)
    addon_slugs = {slug for addons in families.values() for slug in addons}
    for parent in families:
        assert parent in by_slug, f"addon chosen without its parent {parent}"

    groups_available: list[dict] = []
    for module in chosen:
        groups_dir = module.version_root / "chapter_groups"
        if groups_dir.is_dir():
            for source in sorted(groups_dir.glob("*.snbt")):
                document = snbt.load(str(source))
                groups_available.extend(document.get("chapter_groups", []))
    group_order = {g["id"]: _as_int(g.get("order_index", 0))
                   for g in groups_available if isinstance(g, dict)}

    # (module, dest name, parsed tree, raw bytes or None). Raw bytes are kept
    # for chapters that install unchanged, so those copies stay byte-stable.
    chapters: list[tuple[Module, str, dict, bytes | None]] = []
    files: list[PlannedFile] = []

    for module in chosen:
        if module.slug in addon_slugs:
            continue  # its chapter rides along inside the parent's file
        chapters_dir = module.version_root / "chapters"
        if not chapters_dir.is_dir():
            continue
        for source in sorted(chapters_dir.glob("*.snbt")):
            tree = snbt.load(str(source))
            if module.slug in families:
                addon_trees = []
                for slug in families[module.slug]:
                    addon_dir = by_slug[slug].version_root / "chapters"
                    for addon_file in sorted(addon_dir.glob("*.snbt")):
                        addon_trees.append(snbt.load(str(addon_file)))
                tree = compose.compose_family(tree, addon_trees)
                chapters.append((module, source.name, tree, None))
            else:
                chapters.append((module, source.name, tree, source.read_bytes()))

    # The completionist tracks every other chapter in this very plan.
    for i, (module, name, tree, _raw) in enumerate(chapters):
        if module.slug != "09-completionist":
            continue
        targets = [
            compose.Target(
                chapter_id=other["id"],
                title=other["title"],
                icon=other["icon"],
                group_order=group_order.get(other.get("group"), 0),
                order_index=_as_int(other["order_index"]),
            )
            for m, _n, other, _r in chapters if m.slug != module.slug
        ]
        tree["quests"] = compose.completionist_quests(targets)
        chapters[i] = (module, name, tree, None)

    for module, name, tree, raw in chapters:
        payload = raw if raw is not None else (snbt.dumps(tree) + "\n").encode()
        files.append(PlannedFile(f"chapters/{name}", payload, module.slug))

    for module in chosen:
        tables_dir = module.version_root / "reward_tables"
        if not tables_dir.is_dir():
            continue
        for source in sorted(tables_dir.glob("*.snbt")):
            files.append(PlannedFile(f"reward_tables/{source.name}",
                                     source.read_bytes(), module.slug))

    # Chapters that a previous version of this library installed standalone
    # and this install supersedes: every addon chapter of every chosen parent,
    # chosen or not (a removed mod's chapter is exactly as stale).
    superseded: dict[str, str] = {}
    for module in chosen:
        if module.extends:
            continue
        for other in modules.values():
            if other.extends != module.slug:
                continue
            for source in (other.version_root / "chapters").glob("*.snbt"):
                superseded[source.name] = qid.qid(
                    other.number, other.chapter_number, "chapter", 0, 1)

    referenced = {tree.get("group") for _m, _n, tree, _r in chapters}
    groups = [g for g in groups_available
              if isinstance(g, dict) and g.get("id") in referenced]

    return Plan(
        files=files,
        superseded=superseded,
        groups=groups,
        module_numbers={m.number for m in chosen},
        merges={parent: list(addons) for parent, addons in families.items()},
    )


def apply_plan(quests_dir: Path, plan: Plan) -> dict[str, int]:
    """Write a plan into the target quests folder."""
    quests_dir.mkdir(parents=True, exist_ok=True)
    book_created = ensure_book_data(quests_dir)

    written = 0
    for planned in plan.files:
        destination = quests_dir / planned.dest_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() == planned.payload:
                continue  # already up to date
            backup(destination)
        destination.write_bytes(planned.payload)
        written += 1

    removed = 0
    for name, expected_id in plan.superseded.items():
        path = quests_dir / "chapters" / name
        if not path.exists():
            continue
        try:
            tree = snbt.load(str(path))
        except (snbt.SNBTError, OSError):
            continue
        if isinstance(tree, dict) and tree.get("id") == expected_id:
            backup(path)
            path.unlink()
            removed += 1

    merged, pruned_groups, total_groups = merge_chapter_groups(
        quests_dir, plan.groups)
    pruned_lang = prune_lang_entries(quests_dir, plan.module_numbers)

    return {
        "written": written,
        "removed": removed,
        "groups_merged": merged,
        "groups_pruned": pruned_groups,
        "groups_total": total_groups,
        "lang_pruned": pruned_lang,
        "book_created": int(book_created),
    }


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--instance", type=Path,
                        help="Minecraft instance root (the folder containing mods/)")
    parser.add_argument("--quests-dir", type=Path,
                        help="write directly to this quests folder instead")
    parser.add_argument("--modules", nargs="*", default=None,
                        help="module directory names to install")
    parser.add_argument("--auto", action="store_true",
                        help="pick modules by reading the instance's mods folder")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="list available modules and exit")
    parser.add_argument("--uncovered", action="store_true",
                        help="with --auto, name the installed mods this "
                             "library has no chapter for")
    args = parser.parse_args(argv)

    modules = load_modules(args.mc_version)
    if not modules:
        print(f"no modules available for Minecraft {args.mc_version}", file=sys.stderr)
        return 1

    if args.list:
        print(f"modules for Minecraft {args.mc_version}:\n")
        for slug, module in modules.items():
            needs = ", ".join(module.requires_mods) or "vanilla only"
            flag = " [required]" if module.is_required else ""
            print(f"  {slug}{flag}")
            print(f"      {module.manifest.get('description', '').strip()}")
            print(f"      requires: {needs}\n")
        return 0

    if not args.instance and not args.quests_dir:
        parser.error("one of --instance or --quests-dir is required")

    quests_dir = args.quests_dir or (args.instance / QUESTS_SUBPATH)

    # -- choose modules ----------------------------------------------------
    skipped: list[tuple[Module, list[str]]] = []
    if args.auto:
        if not args.instance:
            parser.error("--auto needs --instance so it can read mods/")
        available = detect_mods(args.instance)
        print(f"detected {len(available) - 1} mods in {args.instance / 'mods'}")
        chosen, skipped = select_modules(modules, available)

        # A mod nobody has written a chapter for appears in neither the
        # installed list nor the skipped one, so from the outside the tool
        # looks as though it failed to read the jar. Say so instead: a gap in
        # the library and a gap in detection are very different problems.
        covered = {mod for module in modules.values()
                   for mod in module.requires_mods}
        uncovered = sorted(available - covered - {"minecraft"})
        if uncovered:
            print(f"  {len(uncovered)} of them have no chapter in this library "
                  f"yet" + ("" if args.uncovered else "  (--uncovered to list)"))
            if args.uncovered:
                for name in uncovered:
                    print(f"    {name}")
    elif args.modules:
        unknown = [m for m in args.modules if m not in modules]
        if unknown:
            print(f"unknown module(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"available: {', '.join(modules)}", file=sys.stderr)
            return 1
        chosen = [modules[m] for m in args.modules]
    else:
        parser.error("pass --auto, or name modules with --modules, or use --list")

    chosen, problems = resolve_module_deps(chosen, modules)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1

    # -- collision check ---------------------------------------------------
    present = existing_ids(quests_dir)
    incoming_ids: dict[str, str] = {}
    for module in chosen:
        for path in module.version_root.rglob("*.snbt"):
            try:
                tree = snbt.load(str(path))
            except snbt.SNBTError as exc:
                print(f"error: {path} failed to parse: {exc}", file=sys.stderr)
                return 1

            def collect(node):
                if isinstance(node, dict):
                    ident = node.get("id")
                    if isinstance(ident, str) and qid.is_library_id(ident):
                        incoming_ids[ident] = f"{module.slug}/{path.name}"
                    for value in node.values():
                        collect(value)
                elif isinstance(node, (list, tuple)):
                    for value in node:
                        collect(value)

            collect(tree)

    plan = build_plan(chosen, modules)

    # Ours are 7B-prefixed and computed, so a clash normally just means this
    # library is already installed and we are about to overwrite our own files.
    # It is only worth reporting when the ID turns up in a file we are *not*
    # replacing -- that means someone copied our content somewhere unexpected.
    will_replace = {Path(planned.dest_rel).name for planned in plan.files}
    will_replace.update(plan.superseded)
    # chapter_groups.snbt is merged rather than replaced, but our group IDs
    # living there is expected, not a collision.
    will_replace.add("chapter_groups.snbt")
    clashes = {i: present[i] for i in incoming_ids if i in present}
    foreign_clashes = {
        i: where for i, where in clashes.items() if where not in will_replace
    }

    # -- report ------------------------------------------------------------
    print(f"\ntarget: {quests_dir}")
    print(f"installing {len(chosen)} module(s) as {len(plan.files)} file(s):")
    merged_into = {slug: parent
                   for parent, addons in plan.merges.items() for slug in addons}
    for module in chosen:
        if module.slug in merged_into:
            print(f"  {module.slug:<28} merged into {merged_into[module.slug]}")
            continue
        counts = {
            kind: len(list((module.version_root / kind).glob("*.snbt")))
            for kind in ("chapters", "reward_tables", "chapter_groups")
            if (module.version_root / kind).is_dir()
        }
        detail = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        print(f"  {module.slug:<28} {detail}")
    stale_present = [name for name in sorted(plan.superseded)
                     if (quests_dir / "chapters" / name).exists()]
    if stale_present:
        print(f"\nremoving {len(stale_present)} standalone add-on chapter(s) "
              f"from an earlier version (their quests now live in the parent "
              f"chapter):")
        for name in stale_present:
            print(f"  chapters/{name}")
    if skipped:
        print(f"\nskipping {len(skipped)} module(s) whose mods are not installed:")
        for module, missing in skipped:
            print(f"  {module.slug:<28} needs {', '.join(missing)}")
    if clashes:
        print(f"\n{len(clashes)} object(s) already present -- these will be replaced "
              f"(this is normal when reinstalling or upgrading)")
    if foreign_clashes:
        print("\nwarning: some IDs collide with objects this library did not write:")
        for ident, where in list(foreign_clashes.items())[:5]:
            print(f"  {ident} already in {where}")

    if args.dry_run:
        print("\ndry run -- nothing written.")
        return 0

    # -- write -------------------------------------------------------------
    stats = apply_plan(quests_dir, plan)

    print(f"\nwrote {stats['written']} file(s)")
    if stats["removed"]:
        print(f"removed {stats['removed']} superseded add-on chapter file(s)")
    print(f"chapter groups: {stats['groups_merged']} added, "
          f"{stats['groups_total']} now registered"
          + (f", {stats['groups_pruned']} stale removed"
             if stats["groups_pruned"] else ""))
    if stats["lang_pruned"]:
        print(f"cleared {stats['lang_pruned']} stale translation entr(ies) so "
              f"the updated text takes effect")

    if stats["book_created"]:
        print("created data.snbt for the new book "
              "(progression_mode \"flexible\")")
    else:
        try:
            data = snbt.load(str(quests_dir / "data.snbt"))
            mode = data.get("progression_mode")
            if mode != "flexible":
                print(f"\nnote: this instance uses progression_mode {mode!r}. "
                      f"These chapters are authored for \"flexible\"; with a stricter "
                      f"mode some quests will be locked in an order they were not "
                      f"designed for. data.snbt was left untouched.")
        except snbt.SNBTError:
            pass

    print("\ndone. Launch the game and open the quest book to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
