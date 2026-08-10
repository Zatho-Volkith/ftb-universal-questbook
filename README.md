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

# which of your mods this library has no chapter for yet
python3 tools/install.py --instance ~/.minecraft --auto --dry-run --uncovered
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

A consequence worth stating plainly: a mod nobody has written a chapter for
appears in neither the installed list nor the skipped one, so silence looks
exactly like a failure to read the jar. `--auto` therefore reports the gap, and
`--uncovered` names the mods:

```
detected 89 mods in .../mods
  74 of them have no chapter in this library yet  (--uncovered to list)
```

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
  import_chapter.py           pull a chapter back out of an instance
  wizard.py                   interactive installer shipped in the bundle
  validate.py                 lint the library
  testbed.py                  build and run a throwaway server to verify
  package.py                  build a distributable bundle + zip
  derive_chapter.py           draft a chapter from a mod's recipe graph
  qid.py                      compute and explain IDs
  snbt.py                     SNBT reader/writer in FTB's dialect
  extract_item_ids.py         build an item manifest from mod jars
  fetch_vanilla_items.py      snapshot a vanilla item registry
  data/*.json                 committed item manifests
tests/
  test_validate.py            asserts each validator check actually fires
  test_import.py              asserts importing is stable and idempotent
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
| `02-mining-and-caving` | Mining & Caving | 15 | vanilla |
| `03-farming-and-food` | Farming & Food | 15 | vanilla |
| `04-the-nether` | The Nether | 14 | vanilla |
| `05-the-end` | The End | 13 | vanilla |
| `06-building` | Building & Decoration | 13 | vanilla |
| `07-enchanting-and-brewing` | Enchanting & Brewing | 13 | vanilla |
| `20-farmersdelight` | Farmer's Delight | 12 | `farmersdelight` |
| `20a-expanded-delight` | └ Expanded Delight | 6 | `+ expandeddelight` |
| `20b-nethers-delight` | └ Nether's Delight | 6 | `+ mynethersdelight` |
| `20c-oceans-delight` | └ Ocean's Delight | 5 | `+ oceansdelight` |
| `20d-enders-delight` | └ Ender's Delight | 5 | `+ endersdelight` |
| `22-productivebees` | Productive Bees | 12 | `productivebees` |
| `22a-bee-breeding` | └ Bee Breeding | 16 | `(same mod)` |
| `28-umapyoi` | Umapyoi | 12 | `umapyoi` |
| `29-refinedstorage` | Refined Storage | 19 | `refinedstorage` |
| `2a-ae2` | Applied Energistics 2 | 20 | `ae2` |
| `2aa-megacells` | └ MEGA Cells | 5 | `+ megacells` |
| `2ab-extendedae` | └ Extended AE | 5 | `+ extendedae` |
| `2ac-advanced-ae` | └ Advanced AE | 6 | `+ advanced_ae` |
| `2ad-appflux` | └ Applied Flux | 5 | `+ appflux` |
| `30-quality-of-life` | Quality of Life | 8 | vanilla |
| `30a-waystones` | └ Waystones | 4 | `+ waystones` |
| `30b-explorers-compass` | └ Explorer's Compass | 2 | `+ explorerscompass` |
| `30c-toolbelt` | └ Tool Belt | 3 | `+ toolbelt` |
| `30d-sol-carrot` | └ Spice of Life: Carrot Edition | 2 | `+ solcarrot` |
| `30e-crafting-on-a-stick` | └ Crafting on a Stick | 4 | `+ crafting_on_a_stick` |
| `30f-torchmaster` | └ Torchmaster | 3 | `+ torchmaster` |
| `30g-pylons` | └ Pylons | 4 | `+ pylons` |
| `30h-lootr` | └ Lootr | 3 | `+ lootr` |
| `30i-easy-villagers` | └ Easy Villagers | 5 | `+ easy_villagers` |

**286 quests across 15 in-game chapters on a full install.** Add-on modules do
not appear as chapters of their own: the installer merges each one's quests
into its parent's chapter, in a cluster below the parent's tree, and only for
the add-on mods actually present. Two chapters are special: **Welcome** (six
checkmark quests on how the book works) sits ungrouped at the top, and
**Completionist** is generated at install time — one quest per installed
chapter that completes itself when that chapter does, plus a finale for
finishing everything. 107 quests are vanilla-only and work in any 1.21.1
pack; the rest install only when their mods are present.

### Task types

A chapter made only of item turn-ins reads like a shopping list. These nine
types are used throughout, and all nine were verified by loading them in a real
server and reading back what FTB Quests normalised them to:

| Type | Used for |
|------|----------|
| `item` | hand in or hold an item |
| `checkmark` | an information node with no requirement — click to acknowledge |
| `observation` | look at a block or entity for a few seconds |
| `stat` | a vanilla statistic (animals bred, play time, mob kills) |
| `biome` / `structure` / `dimension` | be somewhere |
| `kill` | defeat a specific entity |
| `advancement` | earn a vanilla advancement |
| `xp` | reach an experience level |

Two are deliberately **not** used, and `validate.py` rejects both:

- **`stage`** — FTB accepts it, silently rewrites it to `type: "custom"`, and
  the quest becomes uncompletable. It needs a game-stage mod or KubeJS.
- **`location`** — works, but pins a quest to fixed coordinates in one world,
  which cannot be portable content.

The `stage` behaviour is the nastier of the two: nothing errors, the book
loads, the quest count is right, and the quest simply can never be finished.

### Deriving a chapter from a mod

A mod's recipes *are* its progression, and that is a fact about the mod rather
than anyone's creative work — so a chapter skeleton can be computed:

```bash
python3 tools/derive_chapter.py --mod productivebees --analyse
python3 tools/derive_chapter.py --mod productivebees --module 34 -o draft.snbt
```

It reads the jar, resolves the mod's item tags, drops recipes gated behind
mods you do not have, collapses variant families (Productive Bees ships 600-odd
beehives named after other mods' wood types), ranks what is left by depth and
centrality, and emits a chapter with dependencies already wired.

The output is a **draft**: structure derived, prose yours. Always run
`--analyse` first — it warns when a mod is tiered enough that the default
variant-collapsing would hide its progression, in which case rerun with
`--no-collapse`.

| Mod shape | Mode | Why |
|-----------|------|-----|
| Material variants (Productive Bees, the Delights) | default | 45 beehive variants collapse to one node |
| Tiered tech (Mekanism, EnderIO) | `--no-collapse` | basic → advanced → elite → ultimate are four goals, not one |
| AE2 | default | its `1k`/`4k`/`64k` tiers are already protected |
| Own tier vocabulary (Mystical Agriculture) | `--tier-words inferium prudentium …` | no built-in list can anticipate a mod's invented tier names |

Two things it taught us, both worth knowing before pointing it at a new mod:

- **A mod's progression is often not in its crafting recipes.** Productive Bees
  looks almost flat until you read its 107 `bee_breeding` recipes
  (`parent1` + `parent2` → `offspring`), which reveal an eight-tier tree.
- **Not every node is an item.** Bees are entities; you cannot ask a player to
  hand one in. Those nodes become `checkmark` tasks with a note, so the
  structure survives and you choose the real objective.

### Writing chapters in the game, and importing them back

The best place to write a quest is inside the game — you can see the layout,
pick items out of the real registry, and test a task the moment you write it.
`import_chapter.py` is the way back:

```bash
# what is in the instance, and which of it the library already owns
python3 tools/import_chapter.py --instance ~/.minecraft --list

# pull your edits back into the module they came from
python3 tools/import_chapter.py --instance ~/.minecraft \
    --chapter refined_storage --into 29-refinedstorage

# adopt a brand new chapter as a module
python3 tools/import_chapter.py --instance ~/.minecraft \
    --chapter mekanism --new 2b-mekanism --module 43 \
    --name "Mekanism" --group automation
```

What makes this more than a file copy is the ID scheme. The in-game editor
mints **random** hex IDs; everything here is **computed** from where the object
sits. So an imported chapter is renumbered, and every reference to a renumbered
ID — dependencies especially — is rewritten to match.

The renumbering is deliberately conservative. An ID that is already correct for
the target module and chapter is left exactly as it is, so re-importing a
chapter you extended in game touches only the quests you added:

```
quests : 11
  minted 1 new quest ID(s); 10 kept the ones they had
```

That matters because a quest's ID is what player progress is keyed on.
Renumbering the whole chapter on every import would silently reset everyone's
completion state the next time they updated.

Four things are refused outright rather than left for `validate.py`, because
each one produces a module that is broken for its *installer* rather than for
you:

| Refused | Why |
|---------|-----|
| a dependency on a quest in another chapter | works in your instance, breaks for anyone installing the module alone |
| `stage` or `location` tasks | see [Task types](#task-types) — `stage` fails silently |
| a reward drawing from a table the library does not ship | the reward resolves to nothing |
| a module number another module already owns | IDs would collide |

`requires_mods` is inferred from the item namespaces the chapter actually uses,
and `module.json` is written for you. Run `validate.py` and `testbed.py`
afterwards — the tool prints both commands when it finishes.

### Add-ons

Mods with their own ecosystems — Farmer's Delight especially — get add-on
modules that hang off the parent. The installer detects which are present,
offers exactly those, and **merges their quests into the parent's chapter**
rather than adding chapters of their own — one Farmer's Delight chapter with
an Expanded Delight cluster inside it, not five sidebar entries:

```
[✓] 8   Farmer's Delight             12  farmersdelight
[✓] 9     └ Expanded Delight          6  + expandeddelight
[ ] 10    └ Nether's Delight          6  + mynethersdelight   (needs mynethersdelight)
[✓] 11    └ Ocean's Delight           5  + oceansdelight
```

The merge only moves quest positions; every ID installs byte-identical to the
repo, so player progress survives updates, and an upgrade from an older
split-chapter install cleans up the standalone add-on chapters it replaced.
Add-ons share their parent's module number and are told apart by chapter
number, so a mod can have dozens without eating into the 256 module slots.
See [ID-REGISTRY.md](ID-REGISTRY.md#addons-chapter-slots-not-module-numbers).

---

## Working on the library

```bash
python3 tools/validate.py            # lint everything
python3 tools/validate.py --strict   # warnings fail too
python3 tests/test_validate.py       # confirm the linter still catches faults
python3 tests/test_import.py         # confirm importing is stable
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

Two of them are about whether the chapter can be *read*, which matters more
than it sounds — a wide chapter is where players give up:

- **a dependency line drawn under an unrelated quest.** FTB draws a straight
  line between quest centres, so in a chapter more than a few columns wide a
  long edge will pass behind whatever is in the way, and the quest it passes
  under reads as part of the chain. Fix it by moving one of the two, or by
  giving the chapter another row.
- **a redundant dependency** — a quest naming a prerequisite that another of
  its own prerequisites already requires. It changes nothing about when the
  quest unlocks, and costs a line across the chapter.

Both were found by writing the check: Refined Storage and AE2 had 19 crossings
between them, against 0–4 everywhere else, because both were laid out on three
rows and up to 20 quests wide. Five rows and a spine on `y=0` fixed both.

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

One trap is worth knowing about, because it fails in the dangerous direction.
A lang key is `item.<namespace>.<path>`, and paths routinely carry dotted
suffixes for tooltips — `item.minecraft.diamond_sword.desc`. If the namespace
is allowed to contain dots, a greedy parse reads
`item.mekanismtools.bronze_shield.black` as namespace
`mekanismtools.bronze_shield` holding an item called `black`. That invented
370 phantom namespaces and 821 phantom items, and a mistyped ID landing in one
of them **passed** the typo check, because the manifest appeared to confirm it.
The namespace is therefore matched dot-free, and anything left with a dot in
its path is treated as the tooltip key it is.

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
