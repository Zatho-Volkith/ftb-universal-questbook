# FTB Universal Questbook

A library of drop-in quest chapters, reward tables and progression for
[FTB Quests](https://www.curseforge.com/minecraft/mc-mods/ftb-quests-forge).

Most questbooks are welded to one modpack. This one is built so you can take
the three chapters you actually want, drop them into any pack, and have them
work — without hand-editing hex IDs or breaking the book you already have.

**Minecraft 1.21.1** · **NeoForge and Fabric** (the same files serve both) ·
FTB Quests `2101.1.x`

---

## Quick start

```bash
# see what's available
python3 tools/install.py --list

# read the instance's mods folder and install everything that fits
python3 tools/install.py --instance ~/.minecraft --auto

# or pick specific modules
python3 tools/install.py --instance ~/.minecraft --modules 01-getting-started

# see what would happen without writing anything
python3 tools/install.py --instance ~/.minecraft --auto --dry-run
```

Requires Python 3.9+ and nothing else. No pip install, no network access.

---

## Why this needs a tool at all

Three properties of FTB Quests make a *shared* questbook harder than a
*personal* one. Everything here exists to handle them.

**Every object ID is global.** Chapters, quests, tasks, rewards and reward
tables all live in one flat namespace of 16-character hex strings, and the
in-game editor generates them randomly. Two modules authored months apart could
collide, silently corrupting both. So this library does not generate IDs
randomly — it *computes* them from the module and chapter an object belongs to,
which makes collisions impossible by construction. See
[ID-REGISTRY.md](ID-REGISTRY.md).

**There is no conditional loading.** FTB Quests has no "only if Croptopia is
installed" flag. A quest referencing a missing item just renders broken. So
universality comes from *file granularity*: a vanilla-only core that works in
any pack, plus one optional module per mod. `--auto` reads your mods folder and
picks the ones that fit.

**`chapter_groups.snbt` is a shared registry.** Installing a chapter is not
purely additive — its group has to be registered in a file the target pack also
owns. `install.py` merges into it rather than overwriting, so a pack author's
own groups survive.

---

## Repository layout

```
library.json                  library-wide IDs: chapter groups, reward tables
ID-REGISTRY.md                module number allocation — read before adding one
modules/
  00-core/                    required: tiered reward tables + chapter groups
    module.json
    1.21.1/
      reward_tables/*.snbt
      chapter_groups/groups.snbt
  01-getting-started/         vanilla: wood → stone → iron, in ten quests
    module.json
    1.21.1/
      chapters/getting_started.snbt
tools/
  install.py                  install modules into an instance
  wizard.py                   interactive installer shipped in the bundle
  validate.py                 lint the library
  testbed.py                  build and run a throwaway server to verify
  package.py                  build a distributable bundle + zip
  qid.py                      compute and explain IDs
  snbt.py                     SNBT reader/writer in FTB's dialect
  extract_item_ids.py         build an item manifest from mod jars
  fetch_vanilla_items.py      snapshot a vanilla item registry
  data/*.json                 committed item manifests
tests/
  test_validate.py            asserts each validator check actually fires
```

Module content sits under a Minecraft version folder (`1.21.1/`) so a 1.20.1
tree can be added later without restructuring anything.

---

## The reward tables

`00-core` ships five tiered tables built **entirely from vanilla items**, so
they work in any 1.21.1 pack. Any module can draw from them.

| Tier | Rolls | Flavour |
|------|-------|---------|
| `common` | 1 | torches, logs, coal, food, basic materials |
| `uncommon` | 1 | iron, redstone, lapis, ender pearls, first diamond |
| `rare` | 1 | diamonds, emeralds, enchanted books, totems |
| `epic` | 1 | netherite scrap, elytra, tridents, beacons |
| `legendary` | 2 | netherite, nether stars, conduits, shulker boxes |

Quests reference a table by its **signed 64-bit long**, not its hex ID — this
catches everyone out at least once:

```
rewards: [{ id: "...", type: "random", table_id: 8863084092434939905L }]
```

`python3 tools/qid.py <hex-id>` prints the long for any ID. The current values
are also in `library.json` and every module's `module.json`.

---

## What's in the library

| Module | Chapter | Quests | Requires |
|--------|---------|--------|----------|
| `00-core` | *(reward tables and chapter groups)* | — | vanilla |
| `01-getting-started` | Getting Started | 10 | vanilla |
| `02-mining-and-caving` | Mining & Caving | 12 | vanilla |
| `03-farming-and-food` | Farming & Food | 12 | vanilla |
| `04-the-nether` | The Nether | 11 | vanilla |
| `05-the-end` | The End | 10 | vanilla |
| `06-building` | Building & Decoration | 10 | vanilla |
| `07-enchanting-and-brewing` | Enchanting & Brewing | 10 | vanilla |

75 quests, all vanilla-only, so every chapter works in any 1.21.1 pack.

---

## Working on the library

```bash
python3 tools/validate.py            # lint everything
python3 tools/validate.py --strict   # warnings fail too
python3 tests/test_validate.py       # confirm the linter still catches faults
```

### Testing against a real server

Static checking only goes so far. `testbed.py` builds a throwaway NeoForge
server — portable JDK and all, nothing installed system-wide — and asks the
actual mod whether the book is good:

```bash
python3 tools/testbed.py setup --accept-eula   # one-time, ~700 MB
python3 tools/testbed.py run                   # install, boot, report
python3 tools/testbed.py run --expect-quests 75
```

This catches the one class of bug static checking cannot. A quest with a
typo'd item ID loads *successfully* — FTB Quests substitutes a placeholder and
still reports "Loaded 75 quests" — and shows a barrier block in game. The only
signal is a line from Minecraft itself:

```
[Server thread/ERROR] [minecraft/ItemStack]: Tried to load invalid item:
  'Unknown registry key ...: minecraft:crafting_tables'
```

`testbed.py` greps for exactly that and fails the run.

Running a server means accepting the
[Minecraft EULA](https://aka.ms/MinecraftEULA); `setup` will not write
`eula.txt` unless you pass `--accept-eula`.

`validate.py` errors on anything that breaks someone's game — malformed SNBT,
duplicate or malformed IDs, an ID in the wrong module, a reward pointing at a
table that doesn't exist, a chapter whose `filename` field disagrees with its
own filename, and **cross-chapter dependencies**.

That last one is the rule that keeps modules droppable. FTB Quests resolves
dependencies by raw ID with no notion of which file they came from, so a quest
in chapter A can depend on one in chapter B and it will work perfectly in your
dev instance where every chapter is present. It breaks only for the person who
installs the one chapter they wanted — which is the entire audience for this
library. If you need one deliberately, declare it in `module.json` under
`allowed_external_dependencies`.

Warnings cover things that are probably mistakes: an item ID absent from every
manifest (almost always a typo), a mod namespace used without being declared in
`requires_mods`, and two quests sitting on the same grid position.

To add a module, see [docs/MODULE-SPEC.md](docs/MODULE-SPEC.md).

---

## Item manifests

Validation checks every item ID against a committed manifest, so a typo like
`farmersdelight:rice_panicles` is caught before anyone loads a broken book.

Vanilla items come from a snapshot of the 1.21.1 registry. Mod items are read
out of `assets/<namespace>/lang/en_us.json` inside the jars themselves — every
registered item needs a translation key, so the lang file is a good proxy for
the registry. It is not perfect, which is why an unknown ID is a warning rather
than an error.

```bash
# rebuild the mod manifest after adding jars
python3 tools/extract_item_ids.py path/to/mods -o tools/data/mod-items-1.21.1.json

# snapshot vanilla for a new Minecraft version
python3 tools/fetch_vanilla_items.py --mc-version 1.21.1
```

---

## Licensing

**[MIT](LICENSE)**, covering both the tooling and the quest content. Use it,
change it, ship it inside your own modpack, commercial or not — just keep the
copyright notice.

Do not commit mod jars to this repository. FTB Quests is distributed
All Rights Reserved; the `.gitignore` excludes `*.jar` for that reason.
