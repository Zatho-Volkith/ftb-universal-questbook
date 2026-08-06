"""Build a distributable bundle of the questbook.

Produces a folder (and a zip beside it) containing:

  Install Questbook (Windows).bat   double-click launchers for the interactive
  install-questbook.sh              wizard -- paste a path, tick chapters, done

  installer/   the wizard and the command-line installer, plus the modules.
               Merges into an existing questbook rather than replacing it.

  quests/      a ready-made quest folder, for anyone who would rather just
               copy files in. Only safe when the pack has no questbook yet.

    python3 tools/package.py                       # -> dist/
    python3 tools/package.py --output ~/Desktop
    python3 tools/package.py --modules 01-getting-started 02-mining-and-caving
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_NAME = "FTB-Universal-Questbook"

README = """FTB Universal Questbook
=======================

Drop-in quest chapters for FTB Quests.

  Minecraft {mc}   |   NeoForge and Fabric (same files work on both)
  FTB Quests 2101.1.x, plus FTB Library, FTB Teams and Architectury

Contents
--------
The installer carries {module_count} chapters in total, including chapters for
specific mods and their add-ons. It reads your pack and offers only the ones
your mods support -- anything you do not have is greyed out.

The `quests` folder, for copying in by hand, holds the {chapter_count}
vanilla-only chapters ({quest_count} quests, {table_count} reward tables):

{contents}

Those use vanilla items exclusively, so they work in any {mc} pack. Mod
chapters are installer-only for exactly that reason -- copied in blind they
would be a wall of barrier blocks for anyone without the mod.


Installing -- the easy way
--------------------------
Run the installer and paste the path to your modpack when it asks.

    Windows        double-click  "Install Questbook (Windows).bat"
    Linux / macOS  double-click  install-questbook.sh
                   or run:       ./install-questbook.sh

It will:

  1. ask for your instance folder (you can drag the folder onto the window)
  2. read the questbook already in that pack, if any
  3. let you tick exactly which chapters you want
  4. show you what it is about to do -- nothing is written until you confirm
  5. merge everything into config/ftbquests/quests/

It MERGES rather than replaces, so a pack's own quests and chapter groups
survive. Replaced files are backed up as `<name>.snbt.bak-<timestamp>` first.

Requires Python 3.9 or newer. Nothing to pip install.
On Windows, get it from python.org and tick "Add python.exe to PATH".


Installing -- by hand
---------------------
If the pack has NO questbook yet, you can just copy the `quests` folder into:

    <instance>/config/ftbquests/

so you end up with `config/ftbquests/quests/chapters/...`

If the pack ALREADY has a questbook, do NOT do this. `chapter_groups.snbt` is
a shared registry, and overwriting it deletes the pack's own chapter groups.
Use the installer above, which merges that file instead.


Command line
------------
The installer also works without prompts:

    python3 installer/tools/install.py --instance /path/to/pack --auto --dry-run
    python3 installer/tools/install.py --instance /path/to/pack --auto
    python3 installer/tools/install.py --list

`--auto` reads the pack's mods folder and installs the chapters that fit.
`--dry-run` shows what would happen without writing anything.


Servers
-------
Install to the server folder too, using the same steps -- quest definitions
come from the server, not the client. Player progress lives separately in
`world/ftbquests/` and is not touched by installing or updating.


Updating later
--------------
Always use the installer to update, even if you copied the files by hand the
first time.

FTB Quests moves quest text out of the chapter files into
`config/ftbquests/quests/lang/en_us.snbt` the first time a world loads. A
translation entry beats the text in the chapter file, so simply copying newer
chapters over the top leaves players reading the OLD wording. The installer
clears the stale entries so the new text takes effect.


Progression
-----------
These chapters branch rather than running in a fixed line, and are authored for
`progression_mode: "flexible"` in `config/ftbquests/quests/data.snbt`. A
stricter mode will lock quests in an order they were not designed for. The
installer will tell you if your setting differs; it never edits that file.


Troubleshooting
---------------
A quest showing a BARRIER BLOCK instead of an icon means that item does not
exist in your pack. If it is in a vanilla chapter, that is a bug -- please
report it. If it is in a mod chapter, check that mod is actually installed.

Nothing appears in the book: check that the chapter groups were registered in
`config/ftbquests/quests/chapter_groups.snbt`.


Licence
-------
MIT -- see LICENSE. Use it, change it, ship it in your own pack, commercial or
not. Just keep the copyright notice.
"""


def build_quests_folder(destination: Path, modules: list[str], mc_version: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "install.py"),
         "--quests-dir", str(destination), "--mc-version", mc_version,
         "--modules", *modules],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout + result.stderr, file=sys.stderr)
        raise SystemExit("failed to build the quests folder")


def count_content(quests_dir: Path) -> tuple[int, int, int]:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import snbt  # noqa: E402  (path set above)

    chapters = sorted((quests_dir / "chapters").glob("*.snbt"))
    tables = sorted((quests_dir / "reward_tables").glob("*.snbt"))
    quests = 0
    for path in chapters:
        tree = snbt.load(str(path))
        quests += len(tree.get("quests") or [])
    return len(chapters), quests, len(tables)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--modules", nargs="*", default=None,
                        help="default: every module needing no mods")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)

    # Two different sets, deliberately.
    #
    # The installer ships EVERY module -- the wizard detects which mods a pack
    # has and offers exactly those, so withholding the mod chapters would just
    # make the bundle less useful.
    #
    # The drop-in `quests/` folder ships only the vanilla-only modules, because
    # it gets copied in blind with nothing checking what the pack contains. A
    # Farmer's Delight chapter in there would be a wall of barrier blocks for
    # anyone without the mod.
    all_modules, dropin_modules = [], []
    for manifest_path in sorted((REPO_ROOT / "modules").glob("*/module.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not (manifest_path.parent / args.mc_version).exists():
            continue
        slug = manifest_path.parent.name
        all_modules.append(slug)
        if not manifest.get("requires_mods"):
            dropin_modules.append(slug)

    if args.modules:
        all_modules = list(args.modules)
        dropin_modules = [m for m in dropin_modules if m in all_modules]
    modules = all_modules

    output_root = args.output.expanduser().resolve()
    bundle = output_root / BUNDLE_NAME
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    # 1. the drop-in quests folder
    quests_dir = bundle / "quests"
    build_quests_folder(quests_dir, dropin_modules, args.mc_version)

    # 2. the installer, for packs that already have a book.
    # install.py resolves modules as parent.parent, so mirror the repo shape:
    # installer/tools/install.py alongside installer/modules/.
    installer_dir = bundle / "installer"
    (installer_dir / "tools").mkdir(parents=True)
    for name in ("install.py", "snbt.py", "qid.py"):
        shutil.copy2(REPO_ROOT / "tools" / name, installer_dir / "tools" / name)
    shutil.copy2(REPO_ROOT / "tools" / "wizard.py",
                 installer_dir / "install-questbook.py")
    shutil.copy2(REPO_ROOT / "library.json", installer_dir / "library.json")
    for module in modules:
        shutil.copytree(REPO_ROOT / "modules" / module,
                        installer_dir / "modules" / module,
                        ignore=shutil.ignore_patterns("*.bak-*", "__pycache__"))

    # Double-click launchers. Most of the audience is on Windows and will not
    # open a terminal to type a command.
    windows_launcher = bundle / "Install Questbook (Windows).bat"
    windows_launcher.write_text(
        "@echo off\r\n"
        "title FTB Universal Questbook - Installer\r\n"
        "cd /d \"%~dp0\"\r\n"
        "where py >nul 2>nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "    py -3 \"installer\\install-questbook.py\"\r\n"
        "    goto done\r\n"
        ")\r\n"
        "where python >nul 2>nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "    python \"installer\\install-questbook.py\"\r\n"
        "    goto done\r\n"
        ")\r\n"
        "echo.\r\n"
        "echo Python 3 is required but was not found.\r\n"
        "echo Install it from https://www.python.org/downloads/\r\n"
        "echo and tick \"Add python.exe to PATH\" during setup.\r\n"
        ":done\r\n"
        "echo.\r\n"
        "pause\r\n",
        encoding="utf-8", newline="",
    )

    unix_launcher = bundle / "install-questbook.sh"
    unix_launcher.write_text(
        "#!/usr/bin/env bash\n"
        "# Double-click, or run ./install-questbook.sh\n"
        "cd \"$(dirname \"$0\")\" || exit 1\n"
        "for candidate in python3 python; do\n"
        "    if command -v \"$candidate\" >/dev/null 2>&1; then\n"
        "        exec \"$candidate\" installer/install-questbook.py\n"
        "    fi\n"
        "done\n"
        "echo 'Python 3 is required but was not found.'\n"
        "echo 'Install it with your package manager, or from python.org'\n"
        "read -r -p 'Press Enter to close.'\n",
        encoding="utf-8",
    )
    unix_launcher.chmod(0o755)
    (installer_dir / "install-questbook.py").chmod(0o755)

    # 3. docs and licence
    shutil.copy2(REPO_ROOT / "LICENSE", bundle / "LICENSE")
    chapters, quests, tables = count_content(quests_dir)
    contents = "\n".join(
        f"  - {p.stem.replace('_', ' ').title()}"
        for p in sorted((quests_dir / "chapters").glob("*.snbt"))
    )
    (bundle / "README.txt").write_text(
        README.format(mc=args.mc_version, contents=contents, quest_count=quests,
                      chapter_count=chapters, table_count=tables,
                      module_count=len(modules) - 1),
        encoding="utf-8",
    )

    # 4. prove the packaged installer actually runs before shipping it.
    # A bundle whose installer cannot import its own modules is worse than no
    # bundle, and the failure is invisible until a user hits it.
    check = subprocess.run(
        [sys.executable, str(installer_dir / "tools" / "install.py"), "--list"],
        capture_output=True, text=True,
    )
    if check.returncode != 0 or "00-core" not in check.stdout:
        print(check.stdout + check.stderr, file=sys.stderr)
        raise SystemExit("the packaged installer does not run -- bundle is broken")

    # And the wizard, which has its own path-finding to get wrong. Importing it
    # executes everything except main(), so a broken import shows up here.
    wizard_check = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util as u, sys;"
         "s = u.spec_from_file_location('w', sys.argv[1]);"
         "m = u.module_from_spec(s); s.loader.exec_module(m);"
         "print('modules:', len(m.install.load_modules('" + args.mc_version + "')))",
         str(installer_dir / "install-questbook.py")],
        capture_output=True, text=True,
    )
    if wizard_check.returncode != 0 or "modules:" not in wizard_check.stdout:
        print(wizard_check.stdout + wizard_check.stderr, file=sys.stderr)
        raise SystemExit("the packaged wizard does not load -- bundle is broken")

    # 5. zip it
    archive = None
    if not args.no_zip:
        archive = output_root / f"{BUNDLE_NAME}-{args.mc_version}.zip"
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(bundle.parent))

    print(f"bundle: {bundle}")
    print(f"  installer: {len(modules)} modules "
          f"({len(modules) - len(dropin_modules)} need mods)")
    print(f"  drop-in  : {len(dropin_modules)} vanilla-only modules")
    print(f"  content  : {chapters} chapters, {quests} quests, {tables} reward tables")
    if archive:
        print(f"  zip      : {archive} ({archive.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
