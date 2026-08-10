#!/usr/bin/env python3
"""Download popular mods from Modrinth for chapter authoring.

The questbook can only cover mods whose jars we can read: recipes drive
derive_chapter.py, item registries drive validation. This fills a reference
folder with the most-downloaded content mods for a Minecraft version so the
authoring pipeline has raw material, without hand-collecting jars.

"Content mod" filtering is best-effort: Modrinth's own "library" category is
skipped, as are client-side-only mods (a server never sees them, so a chapter
cannot task their items). Everything else is a judgment call left to the
human deciding what deserves a chapter.

usage:
    python3 tools/fetch_mods.py --dest reference-mods --count 60
    python3 tools/fetch_mods.py --dest reference-mods --slugs botania twilightforest
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.modrinth.com/v2"
# Modrinth asks every client to identify itself.
AGENT = "ftb-universal-questbook/authoring (github.com/Zatho-Volkith)"

# The download charts are dominated by performance patches and HUD utilities.
# A questbook chapter needs progression to quest through, so only mods
# carrying at least one content category are kept.
SKIP_CATEGORIES = {"library", "optimization"}
CONTENT_CATEGORIES = {
    "technology", "magic", "adventure", "food", "storage", "equipment",
    "worldgen", "mobs", "transportation", "decoration",
}


def get(path: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def search_popular(mc_version: str, loader: str, count: int) -> list[dict]:
    """Most-downloaded mods for the version/loader, minus obvious non-content."""
    hits: list[dict] = []
    offset = 0
    while len(hits) < count and offset < count * 4:
        facets = json.dumps([
            [f"versions:{mc_version}"],
            [f"categories:{loader}"],
            ["project_type:mod"],
        ])
        page = get("/search", {
            "limit": 50, "offset": offset, "index": "downloads",
            "facets": facets,
        })["hits"]
        if not page:
            break
        offset += len(page)
        for hit in page:
            categories = set(hit.get("categories", []))
            if SKIP_CATEGORIES & categories:
                continue
            if not CONTENT_CATEGORIES & categories:
                continue    # perf patches, HUDs, minimaps -- nothing to quest
            if hit.get("client_side") == "required" \
                    and hit.get("server_side") == "unsupported":
                continue    # client-only: a server-side questbook cannot use it
            hits.append(hit)
            if len(hits) >= count:
                break
    return hits


def newest_version(project: str, mc_version: str, loader: str) -> dict | None:
    versions = get(f"/project/{project}/version", {
        "game_versions": json.dumps([mc_version]),
        "loaders": json.dumps([loader]),
    })
    return versions[0] if versions else None


def primary_file(version: dict) -> dict | None:
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")), files[0] if files else None)


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        dest.write_bytes(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=Path, required=True,
                        help="folder to download jars into")
    parser.add_argument("--count", type=int, default=50,
                        help="how many popular mods to fetch (default 50)")
    parser.add_argument("--slugs", nargs="*", default=None,
                        help="fetch these specific Modrinth slugs instead")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--loader", default="neoforge",
                        help="neoforge or fabric")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be fetched, download nothing")
    args = parser.parse_args(argv)

    if args.slugs:
        targets = [{"slug": s, "title": s} for s in args.slugs]
    else:
        print(f"searching Modrinth: top {args.count} {args.loader} mods "
              f"for {args.mc_version} by downloads...")
        targets = search_popular(args.mc_version, args.loader, args.count)

    args.dest.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in args.dest.glob("*.jar")}
    fetched = skipped = missing = 0
    for hit in targets:
        slug = hit["slug"]
        version = newest_version(slug, args.mc_version, args.loader)
        time.sleep(0.25)     # stay well under Modrinth's rate limit
        if not version:
            print(f"  no {args.loader} {args.mc_version} build: {slug}")
            missing += 1
            continue
        file = primary_file(version)
        if not file:
            missing += 1
            continue
        name = file["filename"]
        if name in have:
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would fetch {name}")
            fetched += 1
            continue
        print(f"  fetching {name}")
        download(file["url"], args.dest / name)
        fetched += 1

    print(f"\n{fetched} fetched, {skipped} already present, "
          f"{missing} without a matching build.")
    print("Next: rebuild the item registry so validation knows the new items")
    print("(pass EVERY jar folder, or previously-known namespaces vanish):")
    print(f"  python3 tools/extract_item_ids.py \"{args.dest}\" "
          f"\"<instance>/mods\" -o tools/data/mod-items-{args.mc_version}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
