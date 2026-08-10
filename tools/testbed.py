"""Build and run a throwaway Minecraft server that loads the questbook.

`validate.py` checks the library against a model of the format. This runs the
actual mod and asks it. The two catch different things, and the second one is
the one that counts before you touch a real pack.

What the server catches that static checking cannot:

    [Server thread/ERROR] [minecraft/ItemStack]: Tried to load invalid item:
      'Unknown registry key in ResourceKey[...]: minecraft:crafting_tables'

Minecraft logs that -- not FTB Quests -- and the book still reports a happy
"Loaded 10 quests" afterwards, because FTB Quests substitutes a placeholder and
carries on. So a quest with a typo'd item loads "successfully" and simply shows
a barrier block in game. Grepping for that line is the only reliable way to
know, which is why this exists.

Everything lands in testbed/ (gitignored): a portable JDK, a NeoForge server,
and a world. Nothing is installed system-wide, so this works on atomic distros
where dnf does not persist.

    python3 tools/testbed.py setup           # download JDK + NeoForge, install
    python3 tools/testbed.py run             # install modules, boot, report
    python3 tools/testbed.py run --modules 01-getting-started
    python3 tools/testbed.py run --all-mods  # stage every reference jar too
    python3 tools/testbed.py clean           # delete the world, keep the JDK

Running a server means accepting the Minecraft EULA
(https://aka.ms/MinecraftEULA). `setup` will not write eula.txt unless you pass
--accept-eula, and it says so if you forget.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install  # noqa: E402  (for its mods.toml parser)

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTBED = REPO_ROOT / "testbed"
DOWNLOADS = TESTBED / "downloads"
JDK_DIR = TESTBED / "jdk"
SERVER_DIR = TESTBED / "server"

MC_VERSION = "1.21.1"
NEOFORGE_VERSION = "21.1.248"
JDK_URL = ("https://api.adoptium.net/v3/binary/latest/21/ga/"
           "linux/x64/jdk/hotspot/normal/eclipse")
NEOFORGE_URL = ("https://maven.neoforged.net/releases/net/neoforged/neoforge/"
                "{v}/neoforge-{v}-installer.jar")

#: The mods FTB Quests needs to load at all. Filenames are matched loosely so
#: version bumps in the reference folder do not break this.
CORE_MOD_PATTERNS = ["ftb-quests-neoforge", "ftb-library-neoforge",
                     "ftb-teams-neoforge", "architectury"]

# Log signals. The first is the payoff; the rest are ordinary failure modes.
INVALID_ITEM = re.compile(r"Tried to load invalid item: '([^']*)'")
QUEST_SUMMARY = re.compile(
    r"Loaded (\d+) chapter groups?, (\d+) chapters?, (\d+) quests?, "
    r"(\d+) reward tables?"
)
QUEST_LOAD_FAILURE = re.compile(r"failed to load quest data from (\S+): (.*)")
SEVERE = re.compile(r"\[(?:[^\]]*)/(ERROR|FATAL)\]")

# Noise that says nothing about the questbook. The RecipeManager entries are
# mods shipping recipes for other mods that are not installed -- a property of
# the pack, not of anything we wrote.
BENIGN = re.compile(
    r"No key layers in MapLike|OFFLINE/INSECURE MODE|authenticate usernames|"
    r"hackers to connect|online-mode|Environment: |experimental|"
    r"RecipeManager|Negative index in crash report handler"
)


def declared_mod_ids(jar: Path) -> set[str]:
    """Mod ids a jar declares, from its own metadata rather than its name."""
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
                    ids.update(install.mod_ids_from_toml(
                        archive.read(toml).decode("utf-8", errors="replace")))
                    break
    except (zipfile.BadZipFile, OSError):
        pass
    return ids


def java_bin() -> Path:
    return JDK_DIR / "bin" / "java"


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def download(url: str, destination: Path, label: str) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"  {label}: already downloaded")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {label}: downloading...")
    request = urllib.request.Request(
        url, headers={"User-Agent": "ftb-universal-questbook-testbed"}
    )
    with urllib.request.urlopen(request, timeout=900) as response, \
            open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)
    size_mb = destination.stat().st_size / (1024 * 1024)
    print(f"  {label}: {size_mb:.0f} MB")


def reference_jars(extra: list[Path] | None = None) -> list[Path]:
    """Find the reference mod jars, which live outside the repo.

    An explicit --reference directory (e.g. a launcher instance's mods
    folder) replaces the default search entirely -- mixing it with the old
    folders would stage two versions of the same mod.
    """
    if extra:
        jars: list[Path] = []
        for directory in extra:
            jars.extend(sorted(Path(directory).expanduser().rglob("*.jar")))
        return jars
    candidates = [
        REPO_ROOT.parent / "Refrence mods",   # as spelled in this project
        REPO_ROOT.parent / "Reference mods",
        REPO_ROOT / "reference-mods",
    ]
    for directory in candidates:
        if directory.is_dir():
            return sorted(directory.rglob("*.jar"))
    return []


# --------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> int:
    TESTBED.mkdir(parents=True, exist_ok=True)

    print("JDK 21:")
    jdk_archive = DOWNLOADS / "temurin-jdk21.tar.gz"
    if java_bin().exists():
        print("  already installed")
    else:
        download(JDK_URL, jdk_archive, "temurin-jdk21.tar.gz")
        print("  extracting...")
        with tarfile.open(jdk_archive) as archive:
            archive.extractall(TESTBED)
        extracted = next((p for p in TESTBED.glob("jdk-21*") if p.is_dir()), None)
        if not extracted:
            die("could not find the extracted JDK directory")
        extracted.rename(JDK_DIR)
    version = subprocess.run([str(java_bin()), "-version"],
                             capture_output=True, text=True)
    print(f"  {version.stderr.splitlines()[0]}")

    print(f"\nNeoForge {NEOFORGE_VERSION} (Minecraft {MC_VERSION}):")
    installer = DOWNLOADS / f"neoforge-{NEOFORGE_VERSION}-installer.jar"
    if (SERVER_DIR / "run.sh").exists():
        print("  server already installed")
    else:
        download(NEOFORGE_URL.format(v=NEOFORGE_VERSION), installer, installer.name)
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        print("  installing server (this downloads Minecraft and libraries)...")
        result = subprocess.run(
            [str(java_bin()), "-jar", str(installer), "--installServer", "."],
            cwd=SERVER_DIR, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0 or not (SERVER_DIR / "run.sh").exists():
            print(result.stdout[-2000:], file=sys.stderr)
            die("NeoForge server installation failed")
        print("  installed")

    (SERVER_DIR / "user_jvm_args.txt").write_text(f"-Xmx{args.memory}\n")
    (SERVER_DIR / "server.properties").write_text(
        "online-mode=false\n"
        "level-name=questtest\n"
        "level-type=minecraft\\:flat\n"
        "generator-settings={\"layers\":[{\"block\":\"bedrock\",\"height\":1},"
        "{\"block\":\"stone\",\"height\":2}],\"biome\":\"minecraft:plains\"}\n"
        "spawn-protection=0\nmax-tick-time=-1\n"
        "view-distance=4\nsimulation-distance=4\nsync-chunk-writes=false\n"
        "motd=Questbook testbed\n"
    )

    eula = SERVER_DIR / "eula.txt"
    if args.accept_eula:
        eula.write_text("eula=true\n")
        print("\nEULA accepted (you passed --accept-eula).")
    elif not eula.exists() or "eula=true" not in eula.read_text():
        print(f"\nEULA not accepted. The server will refuse to start.")
        print(f"Read https://aka.ms/MinecraftEULA, then either re-run with")
        print(f"--accept-eula or write it yourself:")
        print(f"    echo 'eula=true' > {eula}")

    print(f"\nsetup complete: {TESTBED}")
    return 0


def required_dep_ids(jar: Path) -> set[str]:
    """Mod ids this jar's metadata declares as REQUIRED dependencies.

    NeoForge toml: `[[dependencies.<modid>]]` blocks with `type = "required"`
    (required is also the default when type is absent). Loader-level ids
    (minecraft, neoforge, forge) are the server's problem, not ours.
    """
    deps: set[str] = set()
    try:
        with zipfile.ZipFile(jar) as archive:
            names = set(archive.namelist())
            toml = next((t for t in ("META-INF/neoforge.mods.toml",
                                     "META-INF/mods.toml") if t in names), None)
            if not toml:
                return deps
            text = archive.read(toml).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError):
        return deps

    in_dep_section = False
    dep_id: str | None = None
    dep_type = "required"

    def flush() -> None:
        nonlocal dep_id, dep_type
        if in_dep_section and dep_id and dep_type == "required":
            deps.add(dep_id)
        dep_id, dep_type = None, "required"

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[["):
            flush()
            in_dep_section = stripped.startswith("[[dependencies")
            continue
        if stripped.startswith("["):
            flush()
            in_dep_section = False
            continue
        if in_dep_section:
            match = re.match(r'modId\s*=\s*"([^"]+)"', stripped)
            if match:
                dep_id = match.group(1)
                continue
            match = re.match(r'type\s*=\s*"([^"]+)"', stripped)
            if match:
                dep_type = match.group(1).lower()
    flush()
    return deps - {"minecraft", "neoforge", "forge"}


def embedded_artifact_ids(jar: Path) -> set[str]:
    """Artifacts a jar ships inside itself (NeoForge jar-in-jar).

    Ender IO bundles endercore this way: the toml still declares it required,
    but the loader finds it embedded. Treat those as already satisfied.
    """
    try:
        with zipfile.ZipFile(jar) as archive:
            if "META-INF/jarjar/metadata.json" not in archive.namelist():
                return set()
            meta = json.loads(archive.read("META-INF/jarjar/metadata.json"))
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError):
        return set()
    return {entry["identifier"]["artifact"].lower()
            for entry in meta.get("jars", [])
            if isinstance(entry.get("identifier"), dict)
            and entry["identifier"].get("artifact")}


def stage_mods(all_mods: bool, extra_patterns: list[str] | None = None,
               reference: list[Path] | None = None) -> tuple[int, list[str]]:
    mods_dir = SERVER_DIR / "mods"
    if mods_dir.exists():
        shutil.rmtree(mods_dir)
    mods_dir.mkdir(parents=True)

    jars = reference_jars(reference)
    if not jars:
        die("no reference mod jars found -- expected a 'Refrence mods' folder "
            "next to the repository")

    missing: list[str] = []
    if all_mods:
        chosen = jars
    else:
        chosen = []
        # Staging every reference jar usually fails on library mods that are
        # not in the folder (balm, curios, caelus...), which says nothing about
        # the questbook. Default to the FTB stack plus whatever was asked for.
        # Filenames are not mod ids. `easy_villagers` ships as
        # `easy-villagers-...jar` and `appflux` as `AppliedFlux-...jar`, so ask
        # each jar what it declares before falling back to the filename.
        def flatten(text: str) -> str:
            return text.lower().replace("-", "").replace("_", "")

        declared: dict[str, Path] = {}
        for jar in jars:
            for mod_id in declared_mod_ids(jar):
                declared.setdefault(mod_id, jar)

        for pattern in CORE_MOD_PATTERNS + list(extra_patterns or []):
            if pattern in declared:
                chosen.append(declared[pattern])
                continue
            flat = flatten(pattern)
            # Prefer a jar whose filename STARTS with the pattern. A plain
            # substring search picks "more_umapyoi_skills" for "umapyoi", which
            # then fails to load because the mod it extends is absent.
            candidates = [j for j in jars if flat in flatten(j.name)]
            match = next(
                (j for j in candidates if flatten(j.name).startswith(flat)),
                candidates[0] if candidates else None,
            )
            if match:
                chosen.append(match)
            else:
                missing.append(pattern)

        # Pull in required dependencies transitively: ars_nouveau is useless
        # without curios and geckolib, and nobody should have to know that to
        # run a testbed. A required dep with no jar in the reference folder is
        # reported like any other missing mod.
        queue = list(chosen)
        staged_ids = {i for jar in chosen
                      for i in declared_mod_ids(jar) | embedded_artifact_ids(jar)}
        while queue:
            jar = queue.pop()
            for dep in sorted(required_dep_ids(jar)):
                if dep in staged_ids:
                    continue
                dep_jar = declared.get(dep)
                if dep_jar is None:
                    missing.append(f"{dep} (needed by {jar.name})")
                    staged_ids.add(dep)     # report once
                    continue
                chosen.append(dep_jar)
                staged_ids |= declared_mod_ids(dep_jar) \
                    | embedded_artifact_ids(dep_jar)
                queue.append(dep_jar)

    for jar in dict.fromkeys(chosen):
        shutil.copy2(jar, mods_dir / jar.name)
    return len(dict.fromkeys(chosen)), missing


def analyse(log_text: str, expected: dict | None) -> tuple[bool, list[str]]:
    """Turn a server log into a verdict."""
    notes: list[str] = []
    ok = True

    invalid = INVALID_ITEM.findall(log_text)
    if invalid:
        ok = False
        notes.append(f"{len(invalid)} invalid item reference(s) -- these render "
                     f"as barrier blocks in game:")
        for message in dict.fromkeys(invalid):
            ident = message.rsplit(": ", 1)[-1] if ": " in message else message
            notes.append(f"    {ident}")

    for path, reason in QUEST_LOAD_FAILURE.findall(log_text):
        ok = False
        notes.append(f"FTB Quests could not load {path}: {reason}")

    summary = QUEST_SUMMARY.search(log_text)
    if not summary:
        ok = False
        notes.append("FTB Quests never reported loading the quest file -- "
                     "the server may have failed before that point")
    else:
        groups, chapters, quests, tables = (int(g) for g in summary.groups())
        notes.append(f"loaded {chapters} chapter(s), {quests} quest(s), "
                     f"{tables} reward table(s), {groups} chapter group(s)")
        if expected:
            for label, actual, want in (("chapters", chapters, expected.get("chapters")),
                                        ("quests", quests, expected.get("quests")),
                                        ("reward tables", tables, expected.get("tables"))):
                if want is not None and actual != want:
                    ok = False
                    notes.append(f"expected {want} {label}, the server loaded {actual}")

    if "crash report" in log_text.lower() or "Caused by:" in log_text:
        ok = False
        notes.append("the server crashed -- see the log")

    severe = [
        line for line in log_text.splitlines()
        if SEVERE.search(line) and not BENIGN.search(line)
        and "Tried to load invalid item" not in line
    ]
    if severe:
        notes.append(f"{len(severe)} other error line(s):")
        notes.extend(f"    {line.strip()[:160]}" for line in severe[:8])

    return ok, notes


def cmd_run(args: argparse.Namespace) -> int:
    if not java_bin().exists() or not (SERVER_DIR / "run.sh").exists():
        die("testbed is not set up -- run `python3 tools/testbed.py setup` first")

    eula = SERVER_DIR / "eula.txt"
    if not eula.exists() or "eula=true" not in eula.read_text():
        die("the Minecraft EULA has not been accepted; see "
            "`python3 tools/testbed.py setup --help`")

    staged, missing = stage_mods(args.all_mods, args.mods, args.reference)
    print(f"staged {staged} mod jar(s)"
          + (" (all reference mods)" if args.all_mods else " (FTB Quests + dependencies)"))
    if missing:
        die(f"missing required mod(s) in the reference folder: {', '.join(missing)}")

    quests_dir = SERVER_DIR / "config" / "ftbquests" / "quests"
    if quests_dir.exists():
        shutil.rmtree(quests_dir)   # always a clean install, never a merge

    # Default to every module whose mods are actually staged, so a bare `run`
    # tests as much of the library as this server can support.
    modules = args.modules or list_installable(args)
    if not modules:
        die(f"no modules available for Minecraft {args.mc_version}")
    print(f"installing modules: {', '.join(modules)}")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "install.py"),
         "--quests-dir", str(quests_dir), "--mc-version", args.mc_version,
         "--modules", *modules],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout + result.stderr, file=sys.stderr)
        die("installing modules into the testbed failed")
    for line in result.stdout.splitlines():
        if line.startswith(("copied", "chapter groups:")):
            print(f"  {line}")

    # wipe the world so each run starts from nothing
    world = SERVER_DIR / "questtest"
    if world.exists():
        shutil.rmtree(world)

    log_path = TESTBED / "server-run.log"
    print(f"\nbooting server (up to {args.timeout}s)...")
    env_path = f"{JDK_DIR / 'bin'}:{__import__('os').environ.get('PATH', '')}"
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            ["./run.sh", "nogui"], cwd=SERVER_DIR, stdin=subprocess.PIPE,
            stdout=log, stderr=subprocess.STDOUT, text=True,
            env={**__import__("os").environ, "PATH": env_path},
        )
        try:
            process.communicate(input="stop\n", timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    log_text = log_path.read_text(errors="replace")
    expected = None
    if args.expect_quests is not None:
        expected = {"quests": args.expect_quests}
    ok, notes = analyse(log_text, expected)

    print()
    for note in notes:
        print(f"  {note}")
    print(f"\nlog: {log_path}")
    print("\nPASS -- the questbook loaded cleanly." if ok else
          "\nFAIL -- see above.")
    return 0 if ok else 1


def list_installable(args: argparse.Namespace) -> list[str]:
    """Modules that need no mods beyond FTB Quests itself."""
    import json
    names = []
    for manifest_path in sorted((REPO_ROOT / "modules").glob("*/module.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        needed = manifest.get("requires_mods") or []
        staged_ok = all(
            any(pat in n for pat in (args.mods or []))
            for n in needed
        ) if args.mods else not needed
        if args.all_mods or not needed or staged_ok:
            if (manifest_path.parent / args.mc_version).exists():
                names.append(manifest_path.parent.name)
    return names


def cmd_clean(args: argparse.Namespace) -> int:
    removed = []
    for path in (SERVER_DIR / "questtest", SERVER_DIR / "config",
                 SERVER_DIR / "mods", TESTBED / "server-run.log"):
        if path.exists():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed.append(path.name)
    print("removed: " + (", ".join(removed) if removed else "nothing"))
    print("kept the JDK and NeoForge install; `run` will rebuild the rest")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="download and install the testbed")
    setup.add_argument("--accept-eula", action="store_true",
                       help="accept the Minecraft EULA (https://aka.ms/MinecraftEULA)")
    setup.add_argument("--memory", default="4G")
    setup.set_defaults(func=cmd_setup)

    run = subparsers.add_parser("run", help="boot the server and check the questbook")
    run.add_argument("--modules", nargs="*", default=None)
    run.add_argument("--mc-version", default=MC_VERSION)
    run.add_argument("--all-mods", action="store_true",
                     help="stage every reference jar, not just FTB Quests")
    run.add_argument("--mods", nargs="*", default=None,
                     help="also stage reference jars whose filename contains "
                          "these strings (e.g. farmersdelight productivebees)")
    run.add_argument("--reference", nargs="*", type=Path, default=None,
                     help="take jars from these folders (e.g. a launcher "
                          "instance's mods dir) instead of 'Refrence mods'")
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("--expect-quests", type=int, default=None,
                     help="fail unless exactly this many quests load")
    run.set_defaults(func=cmd_run)

    clean = subparsers.add_parser("clean", help="delete world/config, keep the JDK")
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
