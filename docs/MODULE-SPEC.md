# Adding a module

A module is one self-contained unit someone can install or skip. Usually that
means "the chapters for one mod", or "one themed area of vanilla progression".

## 1. Claim a module number

Add a row to [ID-REGISTRY.md](../ID-REGISTRY.md). Never reuse a number, even
for a module that was removed — someone out there still has it installed.

Vanilla content takes `01`–`1F`. Mod content takes `20`+.

## 2. Create the directory

```
modules/22-productivebees/
  module.json
  README.md                     optional: screenshots, design notes
  1.21.1/
    chapters/
      productive_bees.snbt
    reward_tables/              optional: only if the module adds its own
      bee_products.snbt
```

The Minecraft version folder matters. A module can ship `1.21.1/` and later
`1.20.1/` side by side; `install.py --mc-version` picks between them, and a
module with no folder for the requested version is skipped rather than erroring.

## 3. Write `module.json`

```json
{
  "id": "22-productivebees",
  "module_number": 34,
  "name": "Productive Bees",
  "description": "Bee breeding, hives and honey processing.",
  "version": "1.0.0",
  "mc_versions": ["1.21.1"],
  "requires_mods": ["productivebees"],
  "requires_modules": ["00-core"],
  "chapter_group": "mods",
  "chapters": [
    { "number": 1, "file": "productive_bees.snbt", "title": "Productive Bees" }
  ],
  "allowed_external_dependencies": []
}
```

`module_number` is decimal and must match the `MM` byte of every ID in the
module — `22-productivebees` uses hex `22`, which is decimal **34**. The
validator checks this, so a mismatch fails loudly rather than shipping.

`requires_mods` drives `--auto`. List every mod namespace the module's items
come from. Getting this wrong means someone installs a chapter full of barrier
blocks.

## 4. Mint your IDs

```bash
python3 tools/qid.py new 34 1 chapter 0 1     # the chapter itself
python3 tools/qid.py new 34 1 quest 1         # quest 1
python3 tools/qid.py new 34 1 task 1 1        # quest 1, task 1
python3 tools/qid.py new 34 1 reward 1 11     # quest 1, reward 1
```

Rewards start at index 11 by convention, so tasks and rewards for the same
quest never look alike at a glance.

## 5. Author the chapter

Titles and descriptions go **inline**, not in `lang/`. A module has to be one
droppable file; pulling strings into a shared `lang/en_us.snbt` would force
every install to merge a second file.

> **FTB Quests will rewrite what you author, and you need to expect it.**
> The first time a server loads the book, the mod migrates every `title`,
> `subtitle` and `description` out of your chapter file into
> `lang/en_us.snbt`, keyed by object ID:
>
> ```
> quest.7B02010300010000.title: "Into the Dark"
> quest.7B02010300010000.quest_desc: [ "..." ]
> ```
>
> and rewrites the chapter without them. That is normal and your file is not
> broken. It does mean two things:
>
> * Do not be alarmed when an installed chapter looks stripped. The repository
>   copy stays authoritative; only the installed copy is rewritten.
> * **Shipping revised wording is not just a file copy.** A translation always
>   beats an object's inline default, so a stale lang entry under the same ID
>   would keep showing the old text forever. `install.py` handles this by
>   clearing the lang entries belonging to the modules it is installing, so the
>   mod re-migrates from your fresh files. It deliberately scopes that to those
>   modules — clearing entries for a module it is *not* rewriting would delete
>   text with nothing left to restore it.
>
> The same applies to chapter group titles, which is why `install.py`
> re-asserts the fields of groups the library owns rather than skipping ones
> that already exist.

```
{
	default_hide_dependency_lines: false
	default_quest_shape: ""
	filename: "productive_bees"
	group: "7B00000100000002"
	icon: { id: "productivebees:bee_nest_oak" }
	id: "7B22010200000001"
	images: [ ]
	order_index: 0
	quest_links: [ ]
	quests: [
		{
			id: "7B22010300010000"
			title: "Your First Hive"
			subtitle: "Catch a bee"
			description: [
				"Bees will not come to you.",
				"",
				"Find a nest, and bring a smoker."
			]
			tasks: [{ id: "7B22010400010001", type: "item", item: { count: 1, id: "productivebees:bee_cage" } }]
			rewards: [
				{ id: "7B2201050001000B", type: "xp", xp: 10 }
				{ id: "7B2201050001000C", type: "random", table_id: 8863084092434939905L, exclude_from_claim_all: true }
			]
			x: 0.0d
			y: 0.0d
		}
	]
	subtitle: "Bees, hives and honey"
	title: "Productive Bees"
}
```

Notes that save time:

- **Tabs, not spaces.** Members are newline-separated; commas are optional.
- **Number suffixes are types.** `x: 0.0d` is a double, `weight: 5.0f` a float,
  `count: 8L` a long. Dropping a suffix can make a quest fail to load.
- **Reward tables are referenced by long, not hex.** `table_id: ...L`. Use
  `python3 tools/qid.py <hex-id>` to get the value.
- **`filename` must match the file's own name**, or FTB renames the file on
  first save.
- **Keys are written alphabetically.** FTB re-sorts on every in-game edit, so
  matching that keeps diffs readable. `snbt.py` does it automatically.
- **Only depend on quests in the same chapter.** This is the rule that keeps
  the module droppable; see the README for why. If you genuinely need an
  external dependency, list it in `allowed_external_dependencies`.

## 6. Update the item manifest, then validate

If the mod is new to the library, add its jar to your reference folder and
rebuild the manifest so the validator can check your item IDs:

```bash
python3 tools/extract_item_ids.py path/to/mods -o tools/data/mod-items-1.21.1.json
python3 tools/validate.py --module 22-productivebees --strict
```

## Adding an add-on chapter

When a mod has its own ecosystem — Farmer's Delight has several Delight
addons, Productive Bees has built-in support for a long list of worldgen mods —
each addon gets its own chapter that only installs when that mod is present.

An addon does **not** claim a new module number. It shares its parent's and
takes a different chapter number:

```json
{
  "id": "20a-expanded-delight",
  "module_number": 32,
  "chapter_number": 2,
  "extends": "20-farmersdelight",
  "requires_mods": ["farmersdelight", "expandeddelight"],
  "requires_modules": ["00-core", "20-farmersdelight"],
  "chapter_group": "mods"
}
```

Five rules, all enforced by `validate.py`:

1. `module_number` is the **parent's**, not a new one
2. `chapter_number` is unique within that module — it is the only thing keeping
   two addons' IDs apart, so a duplicate silently collides everything
3. `extends` names a module that exists
4. `requires_modules` includes the parent, so installing the addon alone still
   pulls in the chapter it extends
5. `requires_mods` repeats the parent's mods as well as its own, so
   auto-detection cannot install it into a pack the parent cannot run in

Mint IDs with the addon's own chapter number:

```bash
python3 tools/qid.py new 32 2 quest 1      # -> 7B20020300010000
```

The wizard picks this up automatically: addons render indented under their
parent, ticked when their mod is detected and greyed out when it is not.

## 7. Test in a real instance

The validator catches structural mistakes; it cannot tell you whether a chapter
is *good*. Install into a throwaway instance and open the book.

```bash
python3 tools/install.py --instance ~/test-instance --modules 22-productivebees
```

Check that quest icons resolve (a barrier block means a bad item ID), the
dependency lines look the way you intended, and nothing overlaps.
