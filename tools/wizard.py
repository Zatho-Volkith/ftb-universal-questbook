#!/usr/bin/env python3
"""Interactive installer for the FTB Universal Questbook.

Paste the path to a modpack instance, tick the chapters you want, done. It
reads the questbook already in that instance and merges into it rather than
replacing it, so a pack's own quests survive.

This is the front door for people who are not going to run a command with
flags. Everything it does is available through install.py; this just asks the
questions instead of expecting arguments.

Run it directly:

    python3 install-questbook.py

Design notes worth keeping in mind if you edit this:

  * The audience is on Windows more often than not, pasting a path copied from
    Explorer or dragged onto a terminal. That means surrounding quotes, a
    trailing space, and backslashes. All of it gets normalised.
  * People paste the wrong folder constantly -- the `mods` folder, or the
    `config` folder, or a launcher's profile root. Rather than rejecting those,
    we work out which one they meant.
  * Nothing is written until the review step is confirmed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The real logic lives next door in tools/.
HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE / "tools", HERE.parent / "tools"):
    if (candidate / "install.py").exists():
        sys.path.insert(0, str(candidate))
        break
else:
    print("error: cannot find install.py -- the bundle looks incomplete.")
    raise SystemExit(1)

import install  # noqa: E402
import qid      # noqa: E402
import snbt     # noqa: E402

MC_VERSION = "1.21.1"


def _chapter_groups() -> dict:
    """{key: (order_index, title)} from library.json, for display grouping."""
    for base in (HERE, HERE.parent):
        path = base / "library.json"
        if path.exists():
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            return {g["key"]: (g.get("order_index", 99), g["title"])
                    for g in data.get("chapter_groups", [])}
    return {}


GROUPS = _chapter_groups()


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

def _supports_colour() -> bool:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        try:  # enable ANSI on Windows 10+ consoles
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


COLOUR = _supports_colour()


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


def bold(text: str) -> str:
    return paint(text, "1")


def dim(text: str) -> str:
    return paint(text, "2")


def green(text: str) -> str:
    return paint(text, "32")


def yellow(text: str) -> str:
    return paint(text, "33")


def red(text: str) -> str:
    return paint(text, "31")


TICK, CROSS = ("✓", "✗") if sys.stdout.encoding and \
    "utf" in sys.stdout.encoding.lower() else ("+", "x")


def banner() -> None:
    line = "=" * 58
    print()
    print(bold(line))
    print(bold("  FTB Universal Questbook — Installer"))
    print(bold(line))


def heading(step: int, total: int, title: str) -> None:
    print()
    print(bold(f"Step {step} of {total} — {title}"))
    print(dim("-" * 58))


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled.")
        raise SystemExit(130)
    return answer or (default or "")


def confirm(prompt: str, default: bool = False) -> bool:
    options = "[Y/n]" if default else "[y/N]"
    while True:
        answer = ask(f"{prompt} {options}").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print(yellow("  please answer y or n"))


# --------------------------------------------------------------------------
# step 1: find the instance
# --------------------------------------------------------------------------

def normalise_path(raw: str) -> Path:
    """Turn whatever the user pasted into a usable path.

    Handles the three things people actually paste: a quoted path (Explorer's
    "Copy as path" adds them), a drag-and-dropped path with an escaped space,
    and a trailing separator.
    """
    text = raw.strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1]
    text = text.replace("\\ ", " ").strip()
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def resolve_instance(path: Path) -> tuple[Path | None, str]:
    """Work out the instance root from whatever folder was given."""
    if not path.exists():
        return None, f"that path does not exist:\n    {path}"
    if path.is_file():
        path = path.parent

    # Already the instance root?
    if (path / "mods").is_dir() or (path / "config").is_dir():
        return path, ""

    # Pasted mods/ or config/ -- they meant the parent.
    if path.name in ("mods", "config") and \
            ((path.parent / "mods").is_dir() or (path.parent / "config").is_dir()):
        return path.parent, ""

    # A launcher profile root that keeps the game in a subfolder.
    for nested in (".minecraft", "minecraft"):
        candidate = path / nested
        if (candidate / "mods").is_dir() or (candidate / "config").is_dir():
            return candidate, ""

    return None, (f"no 'mods' or 'config' folder in:\n    {path}\n"
                  f"  That usually means it is not the instance folder.")


def describe_instance(instance: Path) -> dict:
    quests_dir = instance / install.QUESTS_SUBPATH
    info = {
        "mods": len(list((instance / "mods").glob("*.jar"))) if (instance / "mods").is_dir() else 0,
        "mod_ids": install.detect_mods(instance),
        "quests_dir": quests_dir,
        "existing_chapters": 0,
        "existing_groups": 0,
        "ours_present": False,
    }
    chapters_dir = quests_dir / "chapters"
    if chapters_dir.is_dir():
        info["existing_chapters"] = len(list(chapters_dir.glob("*.snbt")))
    groups_file = quests_dir / "chapter_groups.snbt"
    if groups_file.is_file():
        try:
            document = snbt.load(str(groups_file))
            info["existing_groups"] = len(document.get("chapter_groups") or [])
        except snbt.SNBTError:
            pass
    if quests_dir.is_dir():
        # any(), not bool(genexp) -- a generator object is always truthy, which
        # made this claim the library was installed in every pack it saw.
        info["ours_present"] = any(
            qid.is_library_id(i) for i in install.existing_ids(quests_dir)
        )
    return info


def step_find_instance() -> tuple[Path, dict]:
    heading(1, 4, "Where is your modpack?")
    print("Paste the path to your instance folder — the one containing")
    print(dim("  'mods' and 'config'.  You can drag the folder onto this window."))
    print()
    print(dim("  CurseForge : ...\\Instances\\<pack name>"))
    print(dim("  Prism      : ...\\instances\\<pack name>\\minecraft"))
    print(dim("  Modrinth   : ...\\profiles\\<pack name>"))
    print(dim("  Server     : the folder containing 'mods' and 'world'"))
    print()

    while True:
        raw = ask("Instance folder")
        if not raw:
            print(yellow("  nothing entered — paste a path, or Ctrl+C to quit"))
            continue
        instance, problem = resolve_instance(normalise_path(raw))
        if not instance:
            print(red(f"  {CROSS} {problem}"))
            continue

        info = describe_instance(instance)
        print()
        print(green(f"  {TICK} instance: {instance}"))
        print(f"    mods            : {info['mods']} jars")

        if "ftbquests" in info["mod_ids"]:
            print(green(f"    FTB Quests      : {TICK} installed"))
        else:
            print(yellow(f"    FTB Quests      : {CROSS} not detected"))
            print(yellow("      The questbook will install, but nothing will read it"))
            print(yellow("      until FTB Quests (plus FTB Library, FTB Teams and"))
            print(yellow("      Architectury) is in the pack."))

        if info["existing_chapters"] or info["existing_groups"]:
            print(f"    existing book   : {info['existing_chapters']} chapters, "
                  f"{info['existing_groups']} chapter groups")
            if info["ours_present"]:
                print(dim("      (this library is already installed — it will be updated)"))
            else:
                print(dim("      (it will be merged, not replaced)"))
        else:
            print("    existing book   : none — a fresh one will be created")

        print()
        if confirm("Is that the right instance?", default=True):
            return instance, info
        print()


# --------------------------------------------------------------------------
# step 2: choose chapters
# --------------------------------------------------------------------------

def display_text(text: str) -> str:
    """Undo FTB's text escapes so titles read normally in a terminal.

    A literal ampersand is stored as `\\&` because FTB Library treats a bare
    `&` as a formatting code. That escape is for the game's parser, not for us.
    """
    return text.replace("\\&", "&").replace("\\{", "{")


def chapter_summary(module: install.Module) -> tuple[int, str]:
    """Quest count and chapter title for display."""
    total = 0
    titles = []
    chapters_dir = module.version_root / "chapters"
    if chapters_dir.is_dir():
        for path in sorted(chapters_dir.glob("*.snbt")):
            try:
                tree = snbt.load(str(path))
            except snbt.SNBTError:
                continue
            total += len(tree.get("quests") or [])
            titles.append(display_text(tree.get("title") or path.stem))
    name = display_text(module.manifest.get("name", module.slug))
    return total, (titles[0] if len(titles) == 1 else name)


def step_choose(modules: dict[str, install.Module], mod_ids: set[str]) -> list[install.Module]:
    heading(2, 4, "Which chapters do you want?")

    optional: list[install.Module] = []
    required: list[install.Module] = []
    for module in modules.values():
        (required if module.is_required else optional).append(module)
    optional.sort(key=lambda m: m.number)

    # Order so every addon sits directly under the module it extends. Addons
    # share their parent's module number, so sorting on (module, chapter)
    # already produces that grouping.
    by_slug = {m.slug: m for m in optional}
    optional.sort(key=lambda m: (m.number, m.chapter_number))

    rows = []
    for module in optional:
        missing = [m for m in module.requires_mods if m not in mod_ids]
        quests, title = chapter_summary(module)
        parent = by_slug.get(module.extends or "")
        # Show only the mods this addon needs beyond its parent's.
        extra = [m for m in module.requires_mods
                 if not parent or m not in parent.requires_mods]
        key = module.manifest.get("chapter_group") or "mods"
        order, label = GROUPS.get(key, (99, key.title()))
        rows.append({"module": module, "missing": missing, "quests": quests,
                     "title": title, "on": not missing,
                     "is_addon": parent is not None,
                     "parent_slug": module.extends, "extra_mods": extra,
                     "group_key": key, "group_order": order,
                     "group_label": display_text(label)})

    # book group first, then parent-before-addon within it
    rows.sort(key=lambda r: (r["group_order"], r["module"].number,
                             r["module"].chapter_number))

    index_of = {row["module"].slug: i for i, row in enumerate(rows)}

    def set_state(i: int, on: bool) -> None:
        """Toggle a row, keeping parents and addons consistent.

        Ticking an addon implies its parent (install.py would pull it in
        anyway); unticking a parent drops the addons that hang off it.
        """
        rows[i]["on"] = on
        row = rows[i]
        if on and row["parent_slug"] in index_of:
            parent_index = index_of[row["parent_slug"]]
            if not rows[parent_index]["missing"]:
                rows[parent_index]["on"] = True
        if not on:
            for j, other in enumerate(rows):
                if other["parent_slug"] == row["module"].slug:
                    other["on"] = False

    def render() -> None:
        # Grouped by the chapter group each one lands in, so the list mirrors
        # how the book will actually be organised in game.
        print()
        seen_group = None
        for i, row in enumerate(rows, start=1):
            group = row["group_label"]
            if group != seen_group:
                seen_group = group
                print()
                print(f"  {bold(group)}")
                print(dim("  " + "-" * 58))
            mark = f"[{TICK}]" if row["on"] else "[ ]"
            if row["is_addon"]:
                label = f"  └ {row['title']}"
                needs = "+ " + (", ".join(row["extra_mods"]) or "—")
            else:
                label = row["title"]
                needs = ", ".join(row["module"].requires_mods) or "vanilla"
            if len(label) > 29:          # keep the columns aligned
                label = label[:28] + "…"
            body = f"{i:<4}{label:<30}{row['quests']:>5}  {needs}"
            if row["missing"]:
                print(dim(f"  [ ] {body}   (needs {', '.join(row['missing'])})"))
            elif row["on"]:
                print(f"  {green(mark)} {body}")
            else:
                print(f"  {mark} {body}")
        chosen = sum(r["quests"] for r in rows if r["on"])
        count = sum(1 for r in rows if r["on"])
        print(dim("  " + "-" * 58))
        print(f"  selected: {bold(str(count))} chapters, {bold(str(chosen))} quests")
        addons_on = sum(1 for r in rows if r["on"] and r["is_addon"])
        if addons_on:
            print(dim(f"  ({addons_on} of those are add-ons detected in your pack)"))

    while True:
        render()
        print()
        print(dim("  Type numbers to toggle (e.g. '1 3 5'), 'a' = all, 'n' = none,"))
        answer = ask("  Enter = install the selection").lower().strip()

        if not answer:
            selected = [r["module"] for r in rows if r["on"]]
            if not selected:
                print(yellow("  nothing selected — pick at least one chapter"))
                continue
            blocked = [r for r in rows if r["on"] and r["missing"]]
            if blocked:
                print(yellow(f"  {len(blocked)} selected chapter(s) need mods this pack"))
                print(yellow("  does not have. Those quests will show barrier blocks."))
                if not confirm("  Install them anyway?", default=False):
                    continue
            return required + selected

        if answer == "a":
            for i in range(len(rows)):
                set_state(i, True)
            continue
        if answer == "n":
            for i in range(len(rows)):
                rows[i]["on"] = False
            continue

        for token in answer.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(rows):
                index = int(token) - 1
                set_state(index, not rows[index]["on"])
            else:
                print(yellow(f"  ignoring '{token}' — expected a number 1-{len(rows)}"))


# --------------------------------------------------------------------------
# step 3 + 4: review and install
# --------------------------------------------------------------------------

def step_review(instance: Path, chosen: list[install.Module], info: dict) -> bool:
    heading(3, 4, "Review")
    quests_dir = info["quests_dir"]

    print(f"  writing to : {quests_dir}")
    print()
    total = 0
    for module in sorted(chosen, key=lambda m: m.number):
        if module.is_required:
            continue
        quests, title = chapter_summary(module)
        total += quests
        print(f"    {TICK} {title:<30} {quests:>3} quests")
    print(dim(f"    + shared reward tables and chapter groups"))
    print()
    print(f"  {bold(str(total))} quests in total")

    if info["existing_chapters"]:
        print()
        print(f"  Your pack's existing {info['existing_chapters']} chapter(s) are kept.")
        print(dim("  Files that get replaced are backed up as .snbt.bak-<timestamp> first."))

    print()
    return confirm(bold("Install now?"), default=True)


def step_install(quests_dir: Path, chosen: list[install.Module]) -> None:
    heading(4, 4, "Installing")
    import shutil

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
                        continue
                    install.backup(destination, source)
                shutil.copy2(source, destination)
                copied += 1
        groups_dir = module.version_root / "chapter_groups"
        if groups_dir.is_dir():
            for source in sorted(groups_dir.glob("*.snbt")):
                groups_to_add.extend(snbt.load(str(source)).get("chapter_groups", []))

    # Only register groups the installed chapters actually use, so nobody ends
    # up with empty headings for content they did not install.
    referenced = set()
    for module in chosen:
        chapters_dir = module.version_root / "chapters"
        if chapters_dir.is_dir():
            for path in sorted(chapters_dir.glob("*.snbt")):
                group = snbt.load(str(path)).get("group")
                if isinstance(group, str) and group:
                    referenced.add(group)
    groups_to_add = [g for g in groups_to_add if g.get("id") in referenced]

    quests_dir.mkdir(parents=True, exist_ok=True)
    added, total = install.merge_chapter_groups(quests_dir, groups_to_add, False)
    pruned = install.prune_lang_entries(
        quests_dir, {m.number for m in chosen}, False
    )

    print(f"  {green(TICK)} copied {copied} quest file(s)")
    print(f"  {green(TICK)} chapter groups: {added} written, {total} registered")
    if pruned:
        print(f"  {green(TICK)} cleared {pruned} stale text entr(ies) so updates show")

    data_file = quests_dir / "data.snbt"
    if data_file.exists():
        try:
            mode = snbt.load(str(data_file)).get("progression_mode")
            if mode and mode != "flexible":
                print()
                print(yellow(f"  Note: this pack uses progression_mode \"{mode}\"."))
                print(yellow("  These chapters branch rather than unlocking in a fixed"))
                print(yellow("  line, and are written for \"flexible\". Your data.snbt was"))
                print(yellow("  left untouched — change it yourself if you want."))
        except snbt.SNBTError:
            pass


def main() -> int:
    banner()
    modules = install.load_modules(MC_VERSION)
    if not modules:
        print(red(f"\nno modules found for Minecraft {MC_VERSION} — bundle incomplete."))
        return 1

    quests = 0
    for module in modules.values():
        quests += chapter_summary(module)[0]
    print(f"  Minecraft {MC_VERSION} · {len(modules) - 1} chapters · {quests} quests")
    print(dim("  Nothing is written until you confirm."))

    instance, info = step_find_instance()
    chosen = step_choose(modules, info["mod_ids"])
    chosen, problems = install.resolve_module_deps(chosen, modules)
    for problem in problems:
        print(red(f"error: {problem}"))
    if problems:
        return 1

    if not step_review(instance, chosen, info):
        print("\ncancelled — nothing was written.")
        return 0

    step_install(info["quests_dir"], chosen)

    print()
    print(green(bold("  Done.")))
    print()
    print("  Launch the pack and open the quest book.")
    print(dim("  On a server, install to the server folder as well — quest"))
    print(dim("  definitions come from the server, not the client."))
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ncancelled.")
        raise SystemExit(130)
