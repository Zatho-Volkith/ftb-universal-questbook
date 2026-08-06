"""Derive a draft quest chapter from a mod's own recipe graph.

A mod's recipes *are* its progression. If a centrifuge needs a comb and a comb
needs a hive, then that ordering is a fact about the mod, not an authoring
choice -- so the skeleton of a good chapter can be computed rather than
guessed at, and the result owes nothing to anyone else's questbook.

What it does:

  1. reads every recipe in the jar and works out result <- ingredients
  2. resolves the mod's own item tags so tag ingredients still link things up
  3. discards anything needing a third mod, which is what strips the 600-odd
     beehive variants Productive Bees ships for worldgen mods you do not have
  4. collapses cosmetic variant families (`expansion_box_oak`, `_birch`,
     `_cherry`...) to one representative, while protecting progression tiers
     (`basic`/`advanced`/`elite`, AE2's `1k`/`4k`/`64k`) from being merged
  5. ranks what is left by depth in the dependency graph
  6. emits a chapter laid out in tiers, with dependencies already wired

The output is a DRAFT. Titles are the item names and descriptions are stubs --
the structure is derived, the writing is yours. Run it, read it, cut what is
not interesting, then write the prose.

    python3 tools/derive_chapter.py --mod productivebees --analyse
    python3 tools/derive_chapter.py --mod mekanism --analyse --no-collapse
    python3 tools/derive_chapter.py --mod productivebees \\
        --module 34 --chapter 1 --max-quests 14 -o /tmp/draft.snbt

Run --analyse first, always. It reports the graph and warns when the mod is
tiered enough that collapsing would hide the progression, in which case rerun
with --no-collapse.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import qid
import snbt
from snbt import d, L

REPO_ROOT = Path(__file__).resolve().parent.parent

RECIPE_PATH = re.compile(r"^data/([a-z0-9_.\-]+)/recipes?/.*\.json$")
TAG_PATH = re.compile(r"^data/([a-z0-9_.\-]+)/tags/items?/(.*)\.json$")
VALID_PATH = re.compile(r"^[a-z0-9_]+$")

#: Output keys. Anything under these is a result, never an input. `offspring`
#: is here because a mod's real progression is often not in crafting at all:
#: Productive Bees ships 107 `bee_breeding` recipes of the form
#: parent1 + parent2 -> offspring, and that breeding tree *is* the mod's
#: progression. Crafting recipes alone make it look almost flat.
_RESULT_KEYS = {"result", "output", "results", "offspring"}

#: Condition blocks describe when a recipe applies; the ids inside them are
#: not ingredients.
_CONDITION_KEYS = {"neoforge:conditions", "forge:conditions", "conditions"}

# --------------------------------------------------------------------------
# reading the jar
# --------------------------------------------------------------------------

_MODID_LINE = re.compile(r'^\s*modId\s*=\s*"([^"]+)"')


def mod_ids_from_toml(text: str) -> set[str]:
    """Mod ids a `mods.toml` DECLARES, not the ones it depends on.

    Both use the same `modId = "..."` syntax, so a naive search over the whole
    file reports every dependency as though the jar provided it. Asking for
    `ae2` then matches AdvancedAE, which merely requires it. Only `[[mods]]`
    sections declare; `[[dependencies.*]]` sections do not.
    """
    ids: set[str] = set()
    in_mods_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[["):
            in_mods_section = stripped.startswith("[[mods]]")
            continue
        if stripped.startswith("["):
            in_mods_section = False
            continue
        if in_mods_section:
            match = _MODID_LINE.match(line)
            if match:
                ids.add(match.group(1))
    return ids


def declared_mod_ids(jar: Path) -> set[str]:
    """Mod ids a jar actually declares, from its own metadata."""
    ids: set[str] = set()
    try:
        with zipfile.ZipFile(jar) as archive:
            names = set(archive.namelist())
            if "fabric.mod.json" in names:
                try:
                    payload = json.loads(
                        archive.read("fabric.mod.json").decode("utf-8-sig"))
                    if isinstance(payload, dict) and payload.get("id"):
                        ids.add(payload["id"])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml in names:
                    text = archive.read(toml).decode("utf-8", errors="replace")
                    ids.update(mod_ids_from_toml(text))
                    break
    except (zipfile.BadZipFile, OSError):
        pass
    return ids


def find_jar(mod: str, search_dirs: list[Path]) -> Path | None:
    """Locate the jar that actually declares this mod id.

    Filename matching is not good enough once addons are involved: asking for
    `ae2` globs onto `ae2ct`, `ae2wtlib` and `ae2importexportcard` long before
    it reaches `appliedenergistics2-19.2.17.jar`, which is the one that
    declares `ae2`. Read the metadata and match what the jar says it is.
    """
    jars = [j for directory in search_dirs if directory.is_dir()
            for j in sorted(directory.rglob("*.jar"))]

    for jar in jars:                       # exact declared id wins outright
        if mod in declared_mod_ids(jar):
            return jar

    flat = mod.replace("_", "").replace("-", "")
    for jar in jars:                       # fall back to the filename
        name = jar.name.lower().replace("_", "").replace("-", "")
        if name.startswith(flat):
            return jar
    return next((j for j in jars
                 if flat in j.name.lower().replace("_", "").replace("-", "")), None)


def _ids_in(node) -> list[str]:
    """Every namespaced id mentioned anywhere in this subtree."""
    found: list[str] = []
    if isinstance(node, str):
        if ":" in node:
            found.append(node.lstrip("#"))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("item", "tag", "id"):
                found.extend(_ids_in(value))
            elif key in _CONDITION_KEYS or key in _RESULT_KEYS:
                continue
            else:
                found.extend(_ids_in(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_ids_in(value))
    return found


def required_mods(recipe: dict) -> set[str]:
    """Mods a recipe's conditions insist on, ignoring negated ones.

    `{"type": "neoforge:mod_loaded", "modid": "x"}` means this recipe only
    exists when x is installed. Wrapped in `neoforge:not` it means the
    opposite, so those must not count.
    """
    needed: set[str] = set()

    def walk(node, negated: bool):
        if isinstance(node, dict):
            kind = node.get("type", "")
            if kind.endswith(":not"):
                walk(node.get("value"), not negated)
                return
            if kind.endswith(":mod_loaded") and not negated:
                if isinstance(node.get("modid"), str):
                    needed.add(node["modid"])
            for key, value in node.items():
                if key != "type":
                    walk(value, negated)
        elif isinstance(node, list):
            for value in node:
                walk(value, negated)

    for key in _CONDITION_KEYS:
        if key in recipe:
            walk(recipe[key], False)
    return needed


def _result_of(recipe: dict) -> str | None:
    for key in ("result", "output", "offspring"):
        value = recipe.get(key)
        if isinstance(value, str) and ":" in value:
            return value
        if isinstance(value, dict):
            for inner in ("id", "item"):
                if isinstance(value.get(inner), str) and ":" in value[inner]:
                    return value[inner]
    return None


def read_jar(jar: Path, namespace: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (recipes: result -> ingredient ids, tags: tag id -> member ids)."""
    recipes: dict[str, set[str]] = {}
    tags: dict[str, set[str]] = defaultdict(set)

    with zipfile.ZipFile(jar) as archive:
        for name in archive.namelist():
            tag_match = TAG_PATH.match(name)
            if tag_match:
                ns, path = tag_match.groups()
                try:
                    payload = json.loads(archive.read(name).decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                for entry in payload.get("values", []):
                    value = entry.get("id") if isinstance(entry, dict) else entry
                    if isinstance(value, str):
                        tags[f"{ns}:{path}"].add(value.lstrip("#"))
                continue

            if not RECIPE_PATH.match(name):
                continue
            try:
                recipe = json.loads(archive.read(name).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(recipe, dict):
                continue
            result = _result_of(recipe)
            if not result or not result.startswith(namespace + ":"):
                continue
            gating = {m for m in required_mods(recipe)
                      if m not in (namespace, "minecraft", "neoforge", "forge")}
            if gating:
                continue        # this recipe only exists with another mod
            inputs = {i for i in _ids_in(recipe) if i != result}
            # the recipe type itself is not an ingredient
            inputs.discard(recipe.get("type", ""))
            # Each recipe is a separate ALTERNATIVE. Merging them would make an
            # item require the union of every way to make it, which is wrong
            # and inflates the graph badly.
            recipes.setdefault(result, []).append(inputs)

    return recipes, dict(tags)


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def resolve(ingredients: set[str], tags: dict[str, set[str]], namespace: str,
            ) -> tuple[set[str], set[str]]:
    """Split ingredients into (this mod's items, other namespaces seen)."""
    own: set[str] = set()
    foreign: set[str] = set()
    queue, seen = list(ingredients), set()
    while queue:
        ident = queue.pop()
        if ident in seen:
            continue
        seen.add(ident)
        if ident in tags:                       # a tag: follow its members
            queue.extend(tags[ident])
            continue
        ns = ident.split(":", 1)[0]
        if ns == namespace:
            own.add(ident)
        elif ns not in ("minecraft", "c", "neoforge", "forge"):
            foreign.add(ns)
    return own, foreign


#: Words that mark a progression tier rather than a cosmetic variant.
#: Collapsing across these throws away the actual progression -- Mekanism's
#: basic/advanced/elite/ultimate circuits are four separate goals, not four
#: colours of the same one.
_TIER_WORDS = {
    "basic", "advanced", "elite", "ultimate", "creative", "reinforced",
    "atomic", "infused", "tier1", "tier2", "tier3", "tier4", "tier5",
    "mk1", "mk2", "mk3", "mk4",
}

#: Numeric tiers: AE2 counts storage in 1k / 4k / 16k / 64k / 256k, Mekanism
#: and others just count. Same problem, different notation.
_TIER_PATTERN = re.compile(r"^\d+[kmgt]?$")


#: Extra tier vocabulary supplied per run. Every mod invents its own --
#: Mystical Agriculture tiers essences inferium -> prudentium -> tertium ->
#: imperium -> supremium, and no built-in list can anticipate that. Pass
#: --tier-words when --analyse shows a tier chain collapsing into one node.
_EXTRA_TIER_WORDS: set[str] = set()


def is_tier_token(token: str) -> bool:
    return (token in _TIER_WORDS or token in _EXTRA_TIER_WORDS
            or bool(_TIER_PATTERN.match(token)))


def build_families(items: list[str], threshold: int = 3,
                   collapse: bool = True) -> dict[str, str]:
    """Group items that differ only by a material or colour variant.

    Doing this from a hardcoded word list does not survive contact with a mod
    like Productive Bees, which names hives after wood types from a dozen
    worldgen mods -- aspen, grimwood, kousa, morado, hellbark, umbran. So work
    it out structurally instead: split each path on underscores, and try
    replacing every contiguous span of tokens with a wildcard. A span that
    yields the same pattern for several different items is a variant axis.

        advanced_oak_beehive             -> advanced_*_beehive
        advanced_atmospheric_aspen_beehive -> advanced_*_beehive
        expansion_box_cherry             -> expansion_box_*

    Items with no such pattern keep their own name and stand alone.
    """
    paths = {item: item.split(":", 1)[1] for item in items}
    if not collapse:
        return dict(paths)

    counts: dict[tuple, int] = defaultdict(int)
    candidates: dict[str, list[tuple]] = {}

    for item, path in paths.items():
        tokens = path.split("_")
        seen = []
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                if j - i == len(tokens):
                    continue                      # wildcard everything: useless
                # Never wildcard away a tier. Removing "basic" from
                # basic_control_circuit yields `*_control_circuit`, which
                # merges four distinct progression steps into one node.
                # `any`, not `all`: a span of ['1k','crafting'] still
                # destroys the tier if wildcarded away.
                if any(is_tier_token(tok) for tok in tokens[i:j]):
                    continue
                key = tuple(tokens[:i]) + ("*",) + tuple(tokens[j:])
                counts[key] += 1
                seen.append(key)
        candidates[item] = seen

    families: dict[str, str] = {}
    for item, keys in candidates.items():
        best, best_count = None, 0
        for key in keys:
            # Prefer the pattern covering the most items; on a tie prefer the
            # one that keeps more of the original name.
            score = counts[key]
            if score > best_count or (score == best_count and best is not None
                                      and len(key) > len(best)):
                best, best_count = key, score
        families[item] = "_".join(best) if best and best_count >= threshold \
            else paths[item]
    return families


def build_graph(recipes: dict[str, list[set[str]]], tags: dict[str, set[str]],
                namespace: str, collapse: bool = True,
                ) -> tuple[dict[str, set[str]], dict[str, int], list[str]]:
    """Return (deps, depth, dropped) for items craftable without other mods.

    Two things this has to get right, both learned the hard way on Productive
    Bees:

    * An item with several recipes needs ONE of them, not all. Depth is
      therefore a minimum over alternatives -- the earliest point the item
      becomes reachable.

    * Depth is computed over variant *families*, not items. Productive Bees
      lets you craft any advanced beehive from any other, so at item level the
      hives form a 60-link chain and the tiers become meaningless. Collapsing
      `advanced_oak_beehive` and `advanced_birch_beehive` into one node before
      ranking makes that chain vanish.
    """
    alternatives: dict[str, list[set[str]]] = {}
    dropped: list[str] = []
    for result, recipe_list in recipes.items():
        usable = []
        for ingredients in recipe_list:
            own, foreign = resolve(ingredients, tags, namespace)
            if not foreign:
                usable.append(own)
        if usable:
            alternatives[result] = usable
        else:
            dropped.append(result)      # every recipe needs a third mod

    # -- family-level graph ------------------------------------------------
    family_of = build_families(list(alternatives), collapse=collapse)
    family_alts: dict[str, list[set[str]]] = defaultdict(list)
    for item, options in alternatives.items():
        me = family_of[item]
        for option in options:
            mapped = {family_of[p] for p in option if p in family_of}
            mapped.discard(me)          # a family made from itself is a swap
            family_alts[me].append(mapped)

    family_depth: dict[str, int] = {}

    def compute(family: str, stack: frozenset) -> int:
        if family in family_depth:
            return family_depth[family]
        if family in stack:
            return 0                    # conversion cycle; treat as a root
        best = None
        for option in family_alts.get(family, [{}]):
            parents = [p for p in option if p in family_alts]
            cost = 0 if not parents else 1 + max(
                compute(p, stack | {family}) for p in parents
            )
            best = cost if best is None else min(best, cost)
        family_depth[family] = best or 0
        return family_depth[family]

    for family in list(family_alts):
        compute(family, frozenset())

    # -- project back onto items -------------------------------------------
    deps: dict[str, set[str]] = {}
    depth: dict[str, int] = {}
    for item, options in alternatives.items():
        depth[item] = family_depth.get(family_of[item], 0)
        # keep the cheapest recipe's own-mod ingredients as this item's parents
        best = min(options, key=lambda o: (
            max((family_depth.get(family_of[p], 0) for p in o if p in family_of),
                default=-1), len(o)))
        deps[item] = {p for p in best if p in alternatives
                      and family_of[p] != family_of[item]}

    # Mods ship two kinds of recipe that look like progression but are not:
    # sibling conversions (any advanced beehive crafts into any other) and
    # storage pairs (nine combs make a honeycomb block, and back again).
    # Same-family edges are already gone; drop reciprocal ones too, or the
    # pair props each other up and both look far more central than they are.
    for item, parents in list(deps.items()):
        deps[item] = {p for p in parents if item not in deps.get(p, ())}

    return deps, depth, dropped


def centrality(deps: dict[str, set[str]]) -> dict[str, int]:
    """How many other items are built from each one."""
    used: dict[str, int] = defaultdict(int)
    for ingredients in deps.values():
        for parent in ingredients:
            used[parent] += 1
    return used


def pick(deps: dict[str, set[str]], depth: dict[str, int], limit: int,
         collapse: bool = True) -> list[str]:
    """Choose one representative per variant family, spread across tiers.

    Ranking matters more than it looks. Sorting a tier alphabetically fills a
    chapter with whatever happens to start with 'a' -- for Productive Bees that
    is agate, apatite, benitoite, none of which anything else is built from. So
    rank by how central an item is: how many other items need it, then how much
    goes into making it. That surfaces the cages, hives and centrifuges the mod
    is actually about.
    """
    families = build_families(list(deps), collapse=collapse)
    used = centrality(deps)

    def rank(item: str) -> tuple:
        return (-used.get(item, 0), -len(deps.get(item, ())), item)

    by_family: dict[str, str] = {}
    for item in deps:
        family = families[item]
        current = by_family.get(family)
        if current is None or (depth[item], *rank(item)) < (depth[current], *rank(current)):
            by_family[family] = item

    candidates = sorted(by_family.values(), key=lambda i: (depth[i], *rank(i)))
    if len(candidates) <= limit:
        return candidates

    # Keep the shape of the tree: take proportionally from each tier, but the
    # most central items within each.
    tiers: dict[int, list[str]] = defaultdict(list)
    for item in candidates:
        tiers[depth[item]].append(item)
    chosen: list[str] = []
    for tier in sorted(tiers):
        share = max(1, round(limit * len(tiers[tier]) / len(candidates)))
        chosen.extend(sorted(tiers[tier], key=rank)[:share])
    return sorted(chosen, key=lambda i: (depth[i], *rank(i)))[:limit]


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def prettify(ident: str) -> str:
    """Human-ish label from an id or a bare namespace."""
    path = ident.split(":", 1)[1] if ":" in ident else ident
    return path.replace("_", " ").title()


def known_items() -> set[str]:
    """Every id the manifests consider a real item."""
    items: set[str] = set()
    data = REPO_ROOT / "tools" / "data"
    for path in sorted(data.glob("*items*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for info in payload.get("namespaces", {}).values():
            items.update(info.get("items", []))
    return items


def build_chapter(chosen: list[str], deps: dict[str, set[str]],
                  depth: dict[str, int], module: int, chapter: int,
                  namespace: str, group: str) -> dict:
    order = {item: i + 1 for i, item in enumerate(chosen)}
    picked = set(chosen)

    def nearest(item: str, seen: frozenset = frozenset()) -> set[str]:
        """Closest chosen ancestors, hopping over items we did not keep."""
        out: set[str] = set()
        for parent in deps.get(item, ()):
            if parent in seen or parent == item:
                continue
            if parent in picked:
                out.add(parent)
            else:
                out |= nearest(parent, seen | {item})
        return out

    tiers: dict[int, list[str]] = defaultdict(list)
    for item in chosen:
        tiers[depth[item]].append(item)

    items = known_items()
    quests = []
    for item in chosen:
        n = order[item]
        column = sorted(tiers).index(depth[item])
        row = tiers[depth[item]].index(item)
        offset = (len(tiers[depth[item]]) - 1) / 2

        parents = sorted(nearest(item))
        quest = {
            "id": qid.qid(module, chapter, "quest", n, 0),
            # A derived node is not always an item. Productive Bees' whole
            # progression is bees, which are entities -- you cannot ask a
            # player to hand one in. Fall back to a checkmark so the structure
            # survives and the author decides what the real objective is.
            "tasks": [{
                "id": qid.qid(module, chapter, "task", n, 1),
                "type": "item",
                "item": {"count": 1, "id": item},
            } if item in items else {
                "id": qid.qid(module, chapter, "task", n, 1),
                "type": "checkmark",
            }],
            "rewards": [{
                "id": qid.qid(module, chapter, "reward", n, 11),
                "type": "xp",
                "xp": 10 + 10 * depth[item],
            }],
            "title": prettify(item),
            "subtitle": "TODO: one line on why this matters",
            "description": [
                "TODO: write this.",
                "",
                f"Derived: depth {depth[item]} in {namespace}'s recipe graph."
                + ("" if item in items else
                   f"  NOTE: {item} is not an item (entity or non-item node), "
                   f"so this is a checkmark -- pick a real objective."),
            ],
            "x": d(column * 1.5),
            "y": d((row - offset) * 1.5),
        }
        if parents:
            quest["dependencies"] = [qid.qid(module, chapter, "quest", order[p], 0)
                                     for p in parents]
        quests.append(quest)

    return {
        "default_hide_dependency_lines": False,
        "default_quest_shape": "",
        "filename": namespace,
        "group": group,
        "icon": {"id": chosen[0]} if chosen else {"id": "minecraft:book"},
        "id": qid.qid(module, chapter, "chapter", 0, 1),
        "images": [],
        "order_index": 0,
        "quest_links": [],
        "quests": quests,
        "subtitle": "TODO: chapter subtitle",
        "title": prettify(namespace),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mod", required=True, help="mod id, e.g. productivebees")
    parser.add_argument("--jar", type=Path, help="path to the jar (else searched for)")
    parser.add_argument("--module", type=int, help="module number for minted IDs")
    parser.add_argument("--chapter", type=int, default=1)
    parser.add_argument("--max-quests", type=int, default=14)
    parser.add_argument("--group", default="", help="chapter group hex id")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--tier-words", nargs="*", default=None, metavar="WORD",
                        help="extra words that mark a progression tier in this "
                             "mod, e.g. --tier-words inferium prudentium "
                             "tertium imperium supremium")
    parser.add_argument("--no-collapse", action="store_true",
                        help="do not merge variant families. Right for tiered "
                             "tech mods, where near-identical names are real "
                             "progression steps rather than colours of one thing")
    parser.add_argument("--analyse", action="store_true",
                        help="report the graph instead of writing a chapter")
    args = parser.parse_args(argv)

    jar = args.jar or find_jar(args.mod, [
        REPO_ROOT.parent / "Refrence mods", REPO_ROOT.parent / "Reference mods",
        REPO_ROOT / "reference-mods",
    ])
    if not jar or not jar.exists():
        print(f"error: no jar found for {args.mod!r}", file=sys.stderr)
        return 1

    recipes, tags = read_jar(jar, args.mod)
    if not recipes:
        print(f"error: no {args.mod} recipes in {jar.name}", file=sys.stderr)
        return 1
    if args.tier_words:
        _EXTRA_TIER_WORDS.update(w.lower() for w in args.tier_words)
    collapse = not args.no_collapse
    deps, depth, dropped = build_graph(recipes, tags, args.mod, collapse)

    if args.analyse:
        print(f"jar          : {jar.name}")
        print(f"recipes      : {len(recipes)} producing {args.mod} items")
        print(f"needs others : {len(dropped)} dropped (require a third mod)")
        print(f"buildable    : {len(deps)}")
        fam = build_families(list(deps), collapse=collapse)
        families = set(fam.values())
        # Show what the other mode would do, and say which fits. Collapsing
        # suits mods whose names vary by material; it actively harms tiered
        # tech mods, where near-identical names are the progression.
        other = set(build_families(list(deps), collapse=not collapse).values())
        tiered = sum(1 for i in deps
                     if any(is_tier_token(tok)
                            for tok in i.split(":", 1)[1].split("_")))
        label = "after collapsing variants" if collapse else "(collapsing off)"
        print(f"families     : {len(families)} {label}")
        print(f"               {len(other)} with --no-collapse"
              if collapse else
              f"               {len(other)} if variants were collapsed")
        # The pathology to warn about is a family named for a tier -- e.g.
        # `advanced_*`, which lumps advanced circuits in with advanced energy
        # cubes and keeps only one of them.
        tier_families = sorted(
            f for f in families
            if "*" in f and all(is_tier_token(tok) or tok == "*"
                                for tok in f.split("_") if tok)
        )
        if collapse and tier_families:
            print(f"\n  WARNING: {len(tier_families)} families are named for a "
                  f"progression tier:")
            print(f"    {', '.join(tier_families[:6])}")
            print("  That merges unrelated machines of the same tier and keeps "
                  "only one.")
            print("  This is a tiered tech mod -- rerun with --no-collapse.")

        tiers: dict[int, list[str]] = defaultdict(list)
        for item in deps:
            tiers[depth[item]].append(item)

        print("\ndepth   items  representative sample")
        for tier in sorted(tiers):
            sample = sorted({fam[i] for i in tiers[tier]})[:6]
            print(f"  {tier:<5} {len(tiers[tier]):>5}  {', '.join(sample)}")
        used = centrality(deps)
        print("\nwhat a chapter would use:")
        for item in pick(deps, depth, args.max_quests, collapse):
            parents = sorted(p.split(':')[1] for p in deps[item] if p in deps)
            print(f"  d{depth[item]}  {item.split(':')[1]:<26} "
                  f"used by {used.get(item, 0):<3} <- "
                  f"{', '.join(parents[:3]) or '(vanilla only)'}")
        return 0

    if args.module is None:
        print("error: --module is required unless --analyse", file=sys.stderr)
        return 1

    chosen = pick(deps, depth, args.max_quests, collapse)
    chapter = build_chapter(chosen, deps, depth, args.module, args.chapter,
                            args.mod, args.group)
    output = args.output or REPO_ROOT / f"{args.mod}-draft.snbt"
    if output.exists():
        print(f"error: {output} exists; refusing to overwrite", file=sys.stderr)
        return 1
    snbt.dump(chapter, str(output))
    print(f"wrote {output}  ({len(chosen)} quests, "
          f"{len(set(depth[i] for i in chosen))} tiers)")
    print("This is a draft: titles are item names and descriptions are stubs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
