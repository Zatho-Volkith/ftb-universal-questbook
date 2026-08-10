# ID Registry

Every object ID in this library is computed, never random. This file is the
allocation table. **Read it before adding a module**, and never reuse a number.

## Why

FTB Quests identifies chapters, quests, tasks, rewards and reward tables by a
16-character uppercase hex string, in one flat global namespace. The in-game
editor generates them randomly, which is fine for a single pack authored in one
place. For a library people install piecemeal into books that already exist, it
is a collision waiting to happen — and an ID collision in FTB Quests does not
throw an error, it silently merges or clobbers two unrelated objects.

Computing IDs from position makes collisions impossible by construction, and
makes every ID readable at a glance.

## Layout

```
7B  MM  CC  TT  AAAA BBBB
--  --  --  --  ---- ----
|   |   |   |   |    +--- index within the owner
|   |   |   |   +-------- owner index (which quest, or which reward table)
|   |   |   +------------ object type
|   |   +---------------- chapter number within the module (00 = module-wide)
|   +-------------------- module number
+------------------------ library marker, always 7B
```

### Why the marker is `7B`

Quests reference reward tables by their **signed 64-bit long**, not their hex
string — real packs contain `table_id: 4875026022556716679L`. Any ID whose
first hex digit is `8` or higher has the high bit set and serialises as a
*negative* long. That may work fine, but it is an avoidable edge case to build
a library on. Keeping the leading digit at `7` or below guarantees every ID we
mint is a clean positive long.

### Type codes (`TT`)

| Code | Object |
|------|--------|
| `01` | chapter group |
| `02` | chapter |
| `03` | quest |
| `04` | task |
| `05` | reward |
| `06` | reward table |
| `07` | reward table entry |
| `08` | loot crate |
| `09` | image |
| `0A` | quest link |

These are permanent. Changing one renumbers already-shipped content and breaks
every book that has it installed.

## Module numbers (`MM`)

`00` is reserved. `01`–`1F` are vanilla-only content. `20`+ are mod modules.

| `MM` | Module | Requires | Status |
|------|--------|----------|--------|
| `00` | `00-core` — reward tables, chapter groups | vanilla | shipped |
| `01` | `01-getting-started` | vanilla | shipped |
| `02` | `02-mining-and-caving` | vanilla | shipped |
| `03` | `03-farming-and-food` | vanilla | shipped |
| `04` | `04-the-nether` | vanilla | shipped |
| `05` | `05-the-end` | vanilla | shipped |
| `06` | `06-building` | vanilla | shipped |
| `07` | `07-enchanting-and-brewing` | vanilla | shipped |
| `08` | `08-welcome` — how the book works | vanilla | shipped |
| `09` | `09-completionist` — quests generated at install | vanilla | shipped |
| `20` | `20-farmersdelight` | `farmersdelight` | shipped |
| `21` | *(reserved)* Croptopia | `croptopia` | planned |
| `22` | `22-productivebees` + `22a-bee-breeding` | `productivebees` | shipped |
| `23` | *(reserved)* Sophisticated Storage | `sophisticatedstorage` | planned |
| `24` | *(reserved)* Waystones | `waystones` | planned |
| `25` | *(reserved)* Botany Pots | `botanypots` | planned |
| `26` | *(reserved)* Aquaculture | `aquaculture` | planned |
| `27` | *(reserved)* Supplementaries | `supplementaries` | planned |
| `28` | `28-umapyoi` | `umapyoi` | shipped |
| `29` | `29-refinedstorage` | `refinedstorage` | shipped |
| `2A` | `2a-ae2` + 4 add-ons | `ae2` | shipped |
| `30` | `30-quality-of-life` + 9 add-ons | vanilla parent | shipped |

## Addons merge into their parent's chapter at install time

An addon module still owns its chapter slot and every ID inside it, but the
installed book does not contain a chapter object for it. `install.py` merges
each addon's quests into the parent's chapter file, offset into a clear band
below the parent's tree (`tools/compose.py`). The quest, task and reward IDs
are installed byte-identical to the repo files -- only positions shift -- so
player progress keyed on those IDs survives, and the addon's chapter object
(`7B MM CC 02 …`) is simply never shipped.

## Completionist owner encoding

Module `09` mints its quests at install time, one per installed chapter. The
16-bit owner field encodes the target chapter: `owner = MM << 8 | CC` of the
target's ID. The quest tracking the AE2 chapter (`7B2A010200000001`) is
therefore always `7B0901032A010000`, no matter which modules are installed --
progress survives any reinstall. Owner `FFFF` is reserved for the final
"Master of the Book" quest. Rewards use the usual `000B`/`000C` indices;
each quest carries one checkmark task at index `0001` under its owner.

## Addons: chapter slots, not module numbers

A mod with addons — Farmer's Delight has five or six in common use — would burn
through the module numbers fast if each addon claimed one. It does not need to.

An addon **shares its parent's module number** and is distinguished by its
**chapter number** instead. There are 255 chapter slots inside every module, so
the ceiling is nowhere near.

| Directory | Module | Chapter | ID prefix | Requires |
|-----------|--------|---------|-----------|----------|
| `20-farmersdelight` | `20` | `01` | `7B2001…` | `farmersdelight` |
| `20a-expanded-delight` | `20` | `02` | `7B2002…` | `+ expandeddelight` |
| `20b-nethers-delight` | `20` | `03` | `7B2003…` | `+ mynethersdelight` |
| `20c-oceans-delight` | `20` | `04` | `7B2004…` | `+ oceansdelight` |
| `20d-enders-delight` | `20` | `05` | `7B2005…` | `+ endersdelight` |

The directory suffix letter tracks the chapter: `a` = chapter 2, `b` = 3, and
so on. It is a convention for humans; the authority is `chapter_number` in
`module.json`.

An addon's `module.json` must:

* set `module_number` to the **parent's** number
* set its own `chapter_number`, unique within that module
* name the parent in `extends`
* list the parent in `requires_modules`
* repeat the parent's `requires_mods` alongside its own

`validate.py` enforces every one of those, plus that no two modules claim the
same (module, chapter) slot — which matters more here than anywhere else,
since the chapter byte is the only thing keeping two addons' IDs apart.

## Allocated ranges

### Module `00` — core

| Object | ID | As long |
|--------|----|---------|
| chapter group `vanilla` | `7B00000100000001` | — |
| chapter group `mods` | `7B00000100000002` | — |
| chapter group `qol` | `7B00000100000003` | — |
| chapter group `food` | `7B00000100000004` | — |
| chapter group `tech` (formerly `automation`) | `7B00000100000006` | — |
| *(retired, reserved forever)* `exploration`, `storage`, `adventure`, `building` | `7B00000100000005`, `07`, `08`, `09` | — |

The retired group IDs must never be reissued: installs prune them from
players' books by exact ID (`install.LIBRARY_GROUP_IDS`), so a new group
minted under one of them would be deleted on its next install. The canonical
title for `mods` is **Universal · Mod Content**.
| reward table `common` | `7B00000600000001` | `8863084092434939905L` |
| reward table `uncommon` | `7B00000600000002` | `8863084092434939906L` |
| reward table `rare` | `7B00000600000003` | `8863084092434939907L` |
| reward table `epic` | `7B00000600000004` | `8863084092434939908L` |
| reward table `legendary` | `7B00000600000005` | `8863084092434939909L` |

### Module `01` — getting started

| Object | ID |
|--------|----|
| chapter `getting_started` | `7B01010200000001` |
| quests 1–10 | `7B010103000N0000` |
| tasks | `7B010104000N000i` |
| rewards | `7B010105000N001i` |

## Using the tool

Never write an ID by hand.

```bash
# mint one
python3 tools/qid.py new 3 1 quest 5
# -> 7B03010300050000

# mint a reward table and get the long you need for table_id
python3 tools/qid.py new 0 0 reward_table 0 6

# explain an ID you found in a file
python3 tools/qid.py 7B01010400030001
# -> module 01 / chapter 01 / task owner=3 index=1
```

`validate.py` enforces all of this: any ID that is malformed, duplicated,
missing the `7B` marker, or encoding a module number that disagrees with the
directory it lives in is an error.
