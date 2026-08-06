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
| `20` | *(reserved)* Farmer's Delight | `farmersdelight` | planned |
| `21` | *(reserved)* Croptopia | `croptopia` | planned |
| `22` | *(reserved)* Productive Bees | `productivebees` | planned |
| `23` | *(reserved)* Sophisticated Storage | `sophisticatedstorage` | planned |
| `24` | *(reserved)* Waystones | `waystones` | planned |
| `25` | *(reserved)* Botany Pots | `botanypots` | planned |
| `26` | *(reserved)* Aquaculture | `aquaculture` | planned |
| `27` | *(reserved)* Supplementaries | `supplementaries` | planned |

## Allocated ranges

### Module `00` — core

| Object | ID | As long |
|--------|----|---------|
| chapter group `vanilla` | `7B00000100000001` | — |
| chapter group `mods` | `7B00000100000002` | — |
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
