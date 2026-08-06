"""Build an item-ID manifest by reading mod jars.

The single most common way a shared questbook breaks is a mistyped item ID.
`farmersdelight:rice_panicle` versus `farmersdelight:rice_panicles` is invisible
in review and shows up in game as a barrier block where a reward should be.

Mod jars do not ship a machine-readable registry dump, but they do ship
`assets/<namespace>/lang/en_us.json`, and every registered item needs a
translation key there. Reading those keys back out gives us a very good
approximation of the mod's item list -- good enough to catch typos, which is
the whole point.

Two known limits, both acceptable for a linter:
  * an item whose translation key is shared or generated may be missed, so the
    validator treats an unknown ID as a warning, never a hard error
  * blocks without an item form still appear here; you can name them in a quest
    and it will resolve, so that direction is harmless

Vanilla items are not in any mod jar. Those come from a committed snapshot --
see fetch_vanilla_items.py -- so validation works with no network access.

usage:
    python3 extract_item_ids.py <dir-of-jars> [more dirs...] -o manifest.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

LANG_PATH = re.compile(r"^assets/([a-z0-9_.\-]+)/lang/en_us\.json$")
# Recipe JSON is a second, better source. A lang key only proves a translation
# exists -- Nether's Delight has `block.mynethersdelight.blazier_block` but the
# item is actually `mynethersdelight:blazier`, so lang alone will happily
# approve an ID the game rejects. Anything named in a recipe genuinely exists.
RECIPE_PATH = re.compile(r"^data/([a-z0-9_.\-]+)/recipes?/.*\.json$")
NAMESPACED_ID = re.compile(r'"([a-z0-9_.\-]+):([a-z0-9_/]+)"')
# Item and block paths in Minecraft are always lowercase alphanumeric with
# underscores. Anything else is a tooltip or subtitle key, not an item.
VALID_PATH = re.compile(r"^[a-z0-9_]+$")
ENTRY_KEY = re.compile(r"^(item|block)\.([a-z0-9_.\-]+)\.(.+)$")


#: Recipe fields that name something other than an item. `type` is the recipe
#: id itself; the rest are entities, mod ids and bookkeeping.
_NON_ITEM_KEYS = {
    "type", "parent1", "parent2", "offspring", "bee", "entity", "entity_type",
    "mob", "modid", "category", "group", "advancement", "recipe_book_group",
    "neoforge:conditions", "forge:conditions", "conditions", "fluid",
    "dimension", "biome", "structure", "block",
    # Component data carries entity NBT -- a spawn egg's `components` names the
    # bee inside it. The item's own id is a sibling of `components`, never
    # within it, so skipping the whole block loses nothing real.
    "components", "minecraft:entity_data", "entity_data",
}


def _item_ids_in(node) -> list[str]:
    """Ids that are unambiguously items.

    Only values of an `item:` or `id:` key count. A bare namespaced string can
    be anything -- Productive Bees' `bee_conversion` recipes carry bee entity
    types in plain `source` and `result` fields, and harvesting those yielded
    `productivebees:iron`, which passed the manifest and then failed in game.

    The cost is missing the occasional 1.21 shorthand ingredient written as a
    plain string, but lang scanning usually covers those. A missed item is a
    warning; a phantom one is a barrier block in somebody's world.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _NON_ITEM_KEYS:
                continue
            if key in ("item", "id") and isinstance(value, str) and ":" in value:
                if not value.startswith("#"):
                    found.append(value)
                continue
            found.extend(_item_ids_in(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_item_ids_in(value))
    return found


def ids_from_jar(jar_path: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Return {namespace: {item ids}} and any warnings for one jar."""
    found: dict[str, set[str]] = {}
    warnings: list[str] = []

    try:
        archive = zipfile.ZipFile(jar_path)
    except zipfile.BadZipFile:
        return found, [f"{jar_path.name}: not a readable zip archive"]

    with archive:
        lang_files = [n for n in archive.namelist() if LANG_PATH.match(n)]
        if not lang_files:
            warnings.append(f"{jar_path.name}: no en_us.json lang file found")

        for name in lang_files:
            try:
                raw = archive.read(name).decode("utf-8-sig")
                entries = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                warnings.append(f"{jar_path.name}:{name}: unreadable ({exc})")
                continue
            if not isinstance(entries, dict):
                continue

            for key in entries:
                match = ENTRY_KEY.match(key)
                if not match:
                    continue
                _kind, namespace, path = match.groups()
                if not VALID_PATH.match(path):
                    continue  # tooltip / subtitle / description key
                found.setdefault(namespace, set()).add(f"{namespace}:{path}")

        # Second pass: IDs named in recipe files. These are authoritative --
        # the game would reject the recipe otherwise -- and they catch items
        # whose registry name differs from their translation key.
        for name in archive.namelist():
            if not RECIPE_PATH.match(name):
                continue
            try:
                text = archive.read(name).decode("utf-8-sig")
            except (UnicodeDecodeError, KeyError):
                continue
            # Walk the parsed recipe rather than regexing the text, so we can
            # skip fields that name things which are not items. Productive
            # Bees' `bee_breeding` recipes carry entity types in parent1 /
            # parent2 / offspring, and harvesting those yielded ids like
            # `productivebees:iron` -- which passed a manifest check and then
            # failed in game, the exact failure this manifest exists to stop.
            try:
                recipe = json.loads(text)
            except json.JSONDecodeError:
                continue
            for ident in _item_ids_in(recipe):
                namespace, path = ident.split(":", 1)
                if "/" in path or not VALID_PATH.match(path):
                    continue  # tag, recipe type or group reference
                found.setdefault(namespace, set()).add(ident)

    return found, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an item-ID manifest from a directory of mod jars."
    )
    parser.add_argument("dirs", nargs="+", type=Path,
                        help="directories to scan for .jar files (searched recursively)")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="path to write the JSON manifest")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    jars: list[Path] = []
    for directory in args.dirs:
        if not directory.exists():
            print(f"error: {directory} does not exist", file=sys.stderr)
            return 1
        jars.extend(sorted(directory.rglob("*.jar")))

    if not jars:
        print("error: no .jar files found", file=sys.stderr)
        return 1

    all_ids: dict[str, set[str]] = {}
    provenance: dict[str, str] = {}
    warnings: list[str] = []

    for jar in jars:
        found, jar_warnings = ids_from_jar(jar)
        warnings.extend(jar_warnings)
        for namespace, ids in found.items():
            all_ids.setdefault(namespace, set()).update(ids)
            provenance.setdefault(namespace, jar.name)

    manifest = {
        "_comment": "Generated by extract_item_ids.py. Do not edit by hand.",
        "_source_jars": [j.name for j in jars],
        "namespaces": {
            namespace: {
                "from_jar": provenance.get(namespace, "?"),
                "count": len(ids),
                "items": sorted(ids),
            }
            for namespace, ids in sorted(all_ids.items())
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if not args.quiet:
        total = sum(len(v) for v in all_ids.values())
        print(f"scanned {len(jars)} jars -> {len(all_ids)} namespaces, {total} ids")
        for namespace, ids in sorted(all_ids.items(), key=lambda kv: -len(kv[1]))[:15]:
            print(f"  {namespace:<28} {len(ids):>5}  ({provenance.get(namespace, '?')})")
        if len(all_ids) > 15:
            print(f"  ... and {len(all_ids) - 15} more namespaces")
        for warning in warnings:
            print(f"  warning: {warning}", file=sys.stderr)
        print(f"wrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
