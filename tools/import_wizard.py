#!/usr/bin/env python3
"""Interactive chapter import for the FTB Universal Questbook.

The guided version of import_chapter.py: paste the path to the instance you
authored in, pick the chapter, accept or override the suggested slot, see
exactly what will be written, confirm. Every decision the flags version makes
you look up -- free module numbers, the hex/decimal conversion, the chapter
group, text stranded in lang/ -- is looked up for you and shown before
anything is written.

    python3 tools/import_wizard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import import_chapter as ic     # noqa: E402
from wizard import (            # noqa: E402
    CROSS, TICK, ask, bold, confirm, dim, green, heading, red,
    normalise_path, resolve_instance, yellow,
)

MC_VERSION = "1.21.1"
STEPS = 6


def banner() -> None:
    line = "=" * 58
    print()
    print(bold(line))
    print(bold("  FTB Universal Questbook — Chapter Import"))
    print(bold(line))


def step_instance() -> Path:
    heading(1, STEPS, "Which instance did you author in?")
    print("Paste the path to the instance folder — the one containing")
    print(dim("  'config' (and usually 'mods')."))
    print()
    while True:
        raw = ask("Instance folder")
        if not raw:
            print(yellow("  nothing entered — paste a path, or Ctrl+C to quit"))
            continue
        instance, problem = resolve_instance(normalise_path(raw))
        if not instance:
            print(red(f"  {CROSS} {problem}"))
            continue
        quests_dir = instance / ic.QUESTS_SUBPATH
        if not quests_dir.is_dir():
            print(yellow(f"  {CROSS} no questbook in this instance yet "
                         f"(nothing at {quests_dir})"))
            print(dim("    Launch the pack once with FTB Quests installed --"))
            print(dim("    or install this library into it -- then author a"))
            print(dim("    chapter in game and come back."))
            continue
        print(green(f"  {TICK} {quests_dir}"))
        return quests_dir


def step_chapter(quests_dir: Path) -> ic.Candidate:
    heading(2, STEPS, "Which chapter?")
    candidates = ic.scan(quests_dir, MC_VERSION)
    if not candidates:
        print(yellow("  The questbook here has no chapters to import yet."))
        print(dim("    Author one in game first: open the book, enter edit"))
        print(dim("    mode, add a chapter. Then run this again."))
        raise SystemExit(1)
    while True:
        print()
        for i, c in enumerate(candidates, start=1):
            if c.parse_error:
                print(dim(f"  {i:>3}  {c.stem:<28} (could not parse)"))
            else:
                print(f"  {i:>3}  {c.stem:<28} {c.quest_count:>3} quests   "
                      f"{dim(c.origin)}")
        answer = ask("\n  Number of the chapter to import")
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            chosen = candidates[int(answer) - 1]
            if chosen.parse_error:
                print(yellow("  that one does not parse; fix it in game first"))
                continue
            return chosen
        print(yellow(f"  expected a number 1-{len(candidates)}"))


def step_slot(quests_dir: Path, candidate: ic.Candidate) -> dict:
    """Returns the keyword arguments for plan_import."""
    heading(3, STEPS, "Where does it go in the library?")
    import snbt
    tree = snbt.load(str(quests_dir / "chapters" / f"{candidate.stem}.snbt"))
    modules = ic.known_modules(MC_VERSION)
    suggestion = ic.suggest_slot(tree, modules)

    if suggestion.inferred_mods:
        print(f"  mods in this chapter : {', '.join(suggestion.inferred_mods)}")

    if candidate.owner_slug:
        print(green(f"  {TICK} the library already owns this chapter: "
                    f"{candidate.owner_slug}"))
        if confirm(f"  Update {candidate.owner_slug} from this chapter?",
                   default=True):
            return {"into": candidate.owner_slug}

    if suggestion.extends_candidates:
        print("\n  This chapter's mods extend an existing module. As an add-on")
        print("  its quests merge into the parent's chapter on install:")
        for slug, nxt in suggestion.extends_candidates:
            print(f"    {slug}  (next free chapter slot: {nxt})")

    statuses = ic.free_module_numbers(modules)
    print(f"\n  suggested new module : "
          f"{bold(suggestion.slug or '(no mod detected)')}"
          f"  (0x{suggestion.module_number:02X} / {suggestion.module_number})")

    while True:
        slug = ask("  Module directory name", default=suggestion.slug or "")
        try:
            number, chapter = ic.parse_slug(slug)
        except ic.Problem as exc:
            print(red(f"  {CROSS} {exc}"))
            continue
        status = statuses.get(number)
        parent = None
        if chapter > 1:
            parent = next((s for s, m in modules.items()
                           if m["module_number"] == number
                           and not m.get("extends")), None)
            if not parent:
                print(red(f"  {CROSS} {slug!r} is an add-on slot, but no module "
                          f"owns number 0x{number:02X}"))
                continue
            print(green(f"  {TICK} add-on chapter {chapter} of {parent}"))
        elif status and slug not in modules:
            print(red(f"  {CROSS} 0x{number:02X} is not free ({status})"))
            continue
        return {"new_slug": slug, "extends": parent}


def step_preview(quests_dir: Path, candidate: ic.Candidate,
                 slot: dict) -> ic.ImportPlan:
    heading(4, STEPS, "What will happen")
    try:
        plan = ic.plan_import(quests_dir, candidate.stem,
                              mc_version=MC_VERSION, **slot)
    except ic.Problem as exc:
        print(red(f"  {CROSS} {exc}"))
        raise SystemExit(1)

    for slug, (target, tree) in plan.trees.items():
        print(f"  {TICK} {str(target.relative_to(ic.REPO_ROOT)):<58} "
              f"{len(ic.quests_of(tree)):>3} quests")
    print(f"  {TICK} modules/{plan.slug}/module.json")
    print(f"\n  slot  : 0x{plan.module_number:02X} / chapter "
          f"0x{plan.chapter_number:02X}   group: {plan.group_key}")
    print(f"  mods  : {', '.join(plan.requires) or 'vanilla only'}")
    for note in plan.notes:
        colour = yellow if note.startswith("warning") else dim
        print(colour(f"  {note}"))
    if plan.registry_row:
        print(f"\n  ID-REGISTRY.md row: {plan.registry_row}")
    return plan


def step_write(plan: ic.ImportPlan) -> None:
    heading(5, STEPS, "Write")
    if not confirm(bold("Write these files?"), default=True):
        print("\ncancelled — nothing was written.")
        raise SystemExit(0)
    update_registry = bool(plan.registry_row) and confirm(
        "  Insert the ID-REGISTRY.md row too?", default=True)
    written = ic.apply_import(plan, update_registry=update_registry)
    for path in written:
        print(green(f"  {TICK} wrote {path.relative_to(ic.REPO_ROOT)}"))


def step_validate(plan: ic.ImportPlan) -> int:
    heading(6, STEPS, "Validate")
    import validate
    code = validate.main(["--module", plan.slug])
    print()
    if code == 0:
        print(green(f"  {TICK} {plan.slug} validates clean"))
    else:
        print(yellow("  validate found problems above — fix them, then re-run"))
        print(yellow("  python3 tools/validate.py --module " + plan.slug))
    print(dim("\n  Prove it on a real server with:"))
    print(dim(f"    python3 tools/testbed.py run --modules 00-core {plan.slug}"))
    return code


def main() -> int:
    banner()
    quests_dir = step_instance()
    candidate = step_chapter(quests_dir)
    slot = step_slot(quests_dir, candidate)
    plan = step_preview(quests_dir, candidate, slot)
    step_write(plan)
    return step_validate(plan)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ncancelled.")
        raise SystemExit(130)
