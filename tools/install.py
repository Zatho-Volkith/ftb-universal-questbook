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

import qid
import snbt

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTS_SUBPATH = Path("config/ftbquests/quests")

NEOFORGE_MODID = re.compile(r'^\s*modId\s*=\s*"([^"]+)"', re.MULTILINE)


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


# --------------------------------------------------------------------------

def load_modules(mc_version: str) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    modules_dir = REPO_ROOT / "modules"
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
                        found.update(NEOFORGE_MODID.findall(text))
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


def merge_chapter_groups(quests_dir: Path, incoming: list[dict],
                         dry_run: bool) -> tuple[int, int]:
    """Add our groups to the target's chapter_groups.snbt without touching theirs."""
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

    if (added or refreshed) and not dry_run:
        if target.exists():
            backup(target)
        snbt.dump(document, str(target))
    return added + refreshed, len(groups)


def prune_lang_entries(quests_dir: Path, module_numbers: set[int],
                       dry_run: bool) -> int:
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
        if not dry_run:
            backup(lang_file)
            for key in doomed:
                del document[key]
            snbt.dump(document, str(lang_file))
        removed_total += len(doomed)

    return removed_total


def backup(path: Path, replacement: Path | None = None) -> Path | None:
    """Copy a file aside before overwriting it.

    Skipped when the incoming file is byte-identical, so reinstalling the same
    version repeatedly does not bury the quests folder in .bak files.
    """
    if replacement is not None and path.read_bytes() == replacement.read_bytes():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, destination)
    return destination


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

    # Ours are 7B-prefixed and computed, so a clash normally just means this
    # library is already installed and we are about to overwrite our own files.
    # It is only worth reporting when the ID turns up in a file we are *not*
    # replacing -- that means someone copied our content somewhere unexpected.
    will_replace = {
        path.name
        for module in chosen
        for kind in ("chapters", "reward_tables")
        for path in (module.version_root / kind).glob("*.snbt")
        if (module.version_root / kind).is_dir()
    }
    # chapter_groups.snbt is merged rather than replaced, but our group IDs
    # living there is expected, not a collision.
    will_replace.add("chapter_groups.snbt")
    clashes = {i: present[i] for i in incoming_ids if i in present}
    foreign_clashes = {
        i: where for i, where in clashes.items() if where not in will_replace
    }

    # -- report ------------------------------------------------------------
    print(f"\ntarget: {quests_dir}")
    print(f"installing {len(chosen)} module(s):")
    for module in chosen:
        counts = {
            kind: len(list((module.version_root / kind).glob("*.snbt")))
            for kind in ("chapters", "reward_tables", "chapter_groups")
            if (module.version_root / kind).is_dir()
        }
        detail = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        print(f"  {module.slug:<28} {detail}")
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
    copied = 0
    groups_to_add: list[dict] = []

    for module in chosen:
        for kind in ("chapters", "reward_tables"):
            source_dir = module.version_root / kind
            if not source_dir.is_dir():
                continue
            destination_dir = quests_dir / kind
            destination_dir.mkdir(parents=True, exist_ok=True)
            for source in sorted(source_dir.glob("*.snbt")):
                destination = destination_dir / source.name
                if destination.exists():
                    if destination.read_bytes() == source.read_bytes():
                        continue  # already up to date
                    backup(destination, source)
                shutil.copy2(source, destination)
                copied += 1

        groups_dir = module.version_root / "chapter_groups"
        if groups_dir.is_dir():
            for source in sorted(groups_dir.glob("*.snbt")):
                document = snbt.load(str(source))
                groups_to_add.extend(document.get("chapter_groups", []))

    quests_dir.mkdir(parents=True, exist_ok=True)
    added, total = merge_chapter_groups(quests_dir, groups_to_add, args.dry_run)
    pruned = prune_lang_entries(
        quests_dir, {m.number for m in chosen}, args.dry_run
    )

    print(f"\ncopied {copied} file(s)")
    print(f"chapter groups: {added} added, {total} now registered")
    if pruned:
        print(f"cleared {pruned} stale translation entr(ies) so the updated "
              f"text takes effect")

    data_file = quests_dir / "data.snbt"
    if not data_file.exists():
        print(f"\nnote: {data_file.name} does not exist yet. FTB Quests will create it "
              f"on first launch. For the loose branching progression this library is "
              f"designed around, set progression_mode to \"flexible\".")
    else:
        try:
            data = snbt.load(str(data_file))
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
