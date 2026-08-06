# Installing

## Requirements

- Minecraft **1.21.1**, NeoForge or Fabric
- **FTB Quests** `2101.1.x`, plus its dependencies: **FTB Library**, **FTB Teams**
  and **Architectury**
- **Python 3.9+** to run the installer (nothing to `pip install`)

Quest content is platform-agnostic, so the same files work on NeoForge and
Fabric with no changes.

## The easy way — the interactive installer

If you downloaded a release bundle, just run the launcher and paste the path to
your modpack when it asks:

| Platform | Do this |
|----------|---------|
| Windows | double-click **Install Questbook (Windows).bat** |
| Linux / macOS | double-click **install-questbook.sh**, or `./install-questbook.sh` |

It finds your instance, reads the questbook already there, lets you tick the
chapters you want, and shows you everything before writing a single file. You
can drag the instance folder onto the window instead of typing the path.

Working from a clone of the repository instead? Same thing:

```bash
python3 tools/wizard.py
```

## From the command line

Point the installer at your instance. It reads `mods/`, works out which modules
fit, and installs those.

```bash
python3 tools/install.py --instance /path/to/instance --auto
```

Where "instance" is the folder containing `mods/` and `config/`:

| Launcher | Path |
|----------|------|
| Vanilla / MultiMC | `~/.minecraft` or `instances/<name>/.minecraft` |
| CurseForge | `~/curseforge/minecraft/Instances/<name>` |
| Prism | `~/.local/share/PrismLauncher/instances/<name>/minecraft` |
| Modrinth | `~/.modrinth/profiles/<name>` |
| Server | the folder containing `mods/` and `world/` |

Always look before you leap:

```bash
python3 tools/install.py --instance /path/to/instance --auto --dry-run
```

## Picking modules yourself

```bash
python3 tools/install.py --list
python3 tools/install.py --instance /path/to/instance \
    --modules 01-getting-started
```

Dependencies resolve automatically — `00-core` holds the reward tables and
chapter groups, so it is always pulled in whether or not you name it.

## Manual installation

If you would rather not run the script, copy files into
`config/ftbquests/quests/` in the instance:

```
modules/<module>/1.21.1/chapters/*.snbt        ->  quests/chapters/
modules/<module>/1.21.1/reward_tables/*.snbt   ->  quests/reward_tables/
```

Then **merge** `modules/00-core/1.21.1/chapter_groups/groups.snbt` into
`quests/chapter_groups.snbt`, adding its entries to the existing
`chapter_groups` list rather than replacing the file. Chapters whose group is
not registered there will not display correctly. This merge step is the main
reason the installer exists.

## After installing

Existing files are backed up as `<name>.snbt.bak-<timestamp>` before being
replaced, and files that are already identical are left alone.

These chapters are authored for **flexible** progression — quests branch rather
than unlocking in a fixed line. The installer will tell you if your `data.snbt`
uses a stricter `progression_mode`, but it never edits that file, since it is
your pack's setting to make.

Launch the game and open the quest book. If a quest shows a **barrier block**
where its icon should be, that item does not exist in your pack — please open
an issue with the pack and the quest name.

## Uninstalling

Delete the module's files from `quests/chapters/` and `quests/reward_tables/`,
and remove its group entries from `quests/chapter_groups.snbt`.

Player progress lives separately, in `world/ftbquests/`, keyed by team UUID.
Removing quest files does not touch it — and reinstalling later restores
progress, because the IDs are computed and therefore stable across installs.
