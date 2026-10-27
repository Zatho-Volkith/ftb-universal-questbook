# FTB Universal Questbook — agent guide

A library of drop-in FTB Quests chapters, reward tables and progression for
Minecraft **1.21.1** (FTB Quests `2101.1.x`; the same files serve NeoForge and
Fabric). Python tooling, no Gradle. Python 3.9+, no third-party dependencies.

## Shared knowledge lives in the Athenaeum

The knowledge *behind* this repo has been written up as chapters. Read them
from the Athenaeum, cloned alongside this one at `../athenaeum/`
([Zatho-Volkith/athenaeum](https://github.com/Zatho-Volkith/athenaeum)):

- [`../athenaeum/modpacks/00-index.md`](../athenaeum/modpacks/00-index.md) —
  the format and its silent failures, computed IDs, authoring and deriving
  chapters, distribution, validation.
- [`../athenaeum/CONVENTIONS.md`](../athenaeum/CONVENTIONS.md) — versioning and
  the rule that validation must actually be run.

Those chapters were written from this repo. If you change how the ID scheme,
the installer or the validators work, **update the chapter in the same change**
— otherwise the Athenaeum starts describing a version of this library that no
longer exists.

If something here contradicts the Athenaeum, this file wins.

## Specific to this repo

- **Never mint a random ID.** Every object ID is computed from its position.
  `ID-REGISTRY.md` is the allocation table — read it before adding a module and
  never reuse a number.
- `docs/MODULE-SPEC.md` is the contract a module must satisfy.
- Do not commit mod jars. FTB Quests is distributed All Rights Reserved, and
  `.gitignore` excludes `*.jar` for that reason. Item manifests under
  `tools/data/` are the committed substitute.
- `stage` and `location` tasks are refused outright. `stage` is the dangerous
  one: FTB rewrites it to `custom` and the quest becomes uncompletable with
  nothing erroring anywhere.
- Renumbering on import is conservative on purpose — player progress is keyed
  on the quest ID, so renumbering a whole chapter would reset everyone's
  completion.

## Validation

A change is not done until these pass:

```sh
python3 tools/validate.py --strict
python3 tests/test_validate.py
python3 tests/test_import.py
```

Static checking cannot catch a typo'd item ID — FTB substitutes a placeholder,
reports success, and shows a barrier block in game. For anything touching item
IDs, also run the real-server testbed:

```sh
python3 tools/testbed.py run
```

Never claim the library validates without having run it.
