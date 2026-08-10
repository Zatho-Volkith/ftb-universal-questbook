#!/usr/bin/env python3
"""Browser front-end for importing chapters into the library.

The same flow as import_wizard.py, served as one local web page: pick the
instance, pick the chapter, accept or adjust the suggested slot, preview
exactly what will be written, apply, validate. Pure standard library.

Security model, stated plainly: this is a single-user tool for the machine it
runs on. The server binds 127.0.0.1 only, every mutating request must carry a
token minted at startup (embedded in the page it serves), the Host header must
be the loopback address, instance paths must resolve inside your home
directory (--allow-any-path to override), and writes only ever land inside
this repository's modules/ directory via import_chapter.apply_import.

    python3 tools/webui.py            # starts, opens your browser
    python3 tools/webui.py --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import secrets
import sys
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import import_chapter as ic     # noqa: E402
from wizard import normalise_path, resolve_instance  # noqa: E402

MC_VERSION = "1.21.1"

_SLUG_OK = re.compile(r"^[0-9a-z-]+$")


class ImportSession:
    """One import at a time; a new preview replaces the old plan."""

    def __init__(self, repo_root: Path, allow_any_path: bool):
        self.repo_root = repo_root
        self.allow_any_path = allow_any_path
        self.token = secrets.token_hex(16)
        self.plan: ic.ImportPlan | None = None

    def quests_dir(self, raw: str, must_exist: bool = True) -> Path:
        instance, problem = resolve_instance(normalise_path(raw))
        if not instance:
            raise ic.Problem(problem)
        if not self.allow_any_path:
            home = Path.home().resolve()
            if home not in instance.resolve().parents \
                    and instance.resolve() != home:
                raise ic.Problem(
                    f"{instance} is outside your home directory. Start the "
                    f"server with --allow-any-path if that is intentional.")
        quests = instance / ic.QUESTS_SUBPATH
        if must_exist and not quests.is_dir():
            raise ic.Problem(f"no questbook at {quests}")
        return quests


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class Handler(BaseHTTPRequestHandler):
    session: ImportSession        # set by make_server

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(_json_safe(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guarded(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._reply(403, {"error": "bad Host header"})
            return False
        if self.headers.get("X-Import-Token") != self.session.token:
            self._reply(403, {"error": "missing or wrong token"})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.replace("__TOKEN__", self.session.token).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/validate"):
            if not self._guarded():
                return
            slug = (self.path.split("module=", 1) + [""])[1].split("&")[0]
            if not _SLUG_OK.match(slug):
                self._reply(400, {"error": "bad module slug"})
                return
            self._reply(200, _run_validate(self.session.repo_root, slug))
            return
        self._reply(404, {"error": "no such path"})

    def do_POST(self):
        if not self._guarded():
            return
        body = self._body()
        try:
            if self.path == "/api/scan":
                # A missing or empty questbook is a normal state for a fresh
                # instance, not an error -- explain it instead of failing.
                quests = self.session.quests_dir(str(body.get("instance", "")),
                                                 must_exist=False)
                candidates = ic.scan(quests, MC_VERSION,
                                     self.session.repo_root) \
                    if quests.is_dir() else []
                note = None
                if not quests.is_dir():
                    note = (f"This instance has no questbook yet (nothing at "
                            f"{quests}). Launch the pack once with FTB Quests "
                            f"installed -- or install this library into it -- "
                            f"then author a chapter in game and come back.")
                elif not candidates:
                    note = ("The questbook here has no chapters to import "
                            "yet. Author one in game first: open the book, "
                            "enter edit mode, add a chapter.")
                self._reply(200, {"quests_dir": quests,
                                  "chapters": [asdict(c) for c in candidates],
                                  "note": note})
            elif self.path == "/api/suggest":
                quests = self.session.quests_dir(str(body.get("instance", "")))
                import snbt
                tree = snbt.load(str(quests / "chapters"
                                     / f"{body.get('chapter')}.snbt"))
                modules = ic.known_modules(MC_VERSION, self.session.repo_root)
                suggestion = ic.suggest_slot(tree, modules,
                                             self.session.repo_root)
                _tree, lang_notes = ic.recover_lang(quests, tree)
                statuses = ic.free_module_numbers(modules,
                                                  self.session.repo_root)
                self._reply(200, {
                    "suggestion": asdict(suggestion),
                    "lang_notes": lang_notes,
                    "free_numbers": {f"0x{n:02X}": s for n, s in
                                     sorted(statuses.items())},
                })
            elif self.path == "/api/preview":
                quests = self.session.quests_dir(str(body.get("instance", "")))
                plan = ic.plan_import(
                    quests, str(body.get("chapter", "")),
                    into=body.get("into") or None,
                    new_slug=body.get("new_slug") or None,
                    group=body.get("group") or None,
                    extends=body.get("extends") or None,
                    name=body.get("name") or None,
                    mc_version=MC_VERSION,
                    repo_root=self.session.repo_root,
                )
                self.session.plan = plan
                self._reply(200, {
                    "slug": plan.slug,
                    "module_number": plan.module_number,
                    "chapter_number": plan.chapter_number,
                    "group": plan.group_key,
                    "requires": plan.requires,
                    "quest_count": plan.quest_count,
                    "notes": plan.notes,
                    "registry_row": plan.registry_row,
                    "targets": {slug: str(target.relative_to(
                        self.session.repo_root))
                        for slug, (target, _t) in plan.trees.items()},
                })
            elif self.path == "/api/apply":
                if not body.get("confirm"):
                    raise ic.Problem("apply requires confirm: true")
                if self.session.plan is None:
                    raise ic.Problem("nothing previewed yet")
                written = ic.apply_import(
                    self.session.plan, self.session.repo_root,
                    update_registry=bool(body.get("update_registry")))
                plan, self.session.plan = self.session.plan, None
                self._reply(200, {
                    "written": [str(p.relative_to(self.session.repo_root))
                                for p in written],
                    "slug": plan.slug,
                })
            else:
                self._reply(404, {"error": "no such path"})
        except ic.Problem as exc:
            self._reply(422, {"error": str(exc)})
        except Exception as exc:                       # noqa: BLE001
            self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def _run_validate(repo_root: Path, slug: str) -> dict:
    import subprocess
    result = subprocess.run(
        [sys.executable, str(repo_root / "tools" / "validate.py"),
         "--module", slug],
        capture_output=True, text=True,
    )
    return {"clean": result.returncode == 0,
            "output": (result.stdout + result.stderr).strip()}


def make_server(repo_root: Path = ic.REPO_ROOT, port: int = 0,
                allow_any_path: bool = False) -> ThreadingHTTPServer:
    session = ImportSession(repo_root, allow_any_path)
    handler = type("BoundHandler", (Handler,), {"session": session})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Questbook Chapter Import</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 780px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.3rem; }
  fieldset { border: 1px solid #ccc; border-radius: 8px; margin: 1rem 0;
             padding: 1rem; }
  fieldset:disabled { opacity: .45; }
  legend { font-weight: 600; padding: 0 .4rem; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: .45rem;
                     font: inherit; }
  button { font: inherit; padding: .4rem 1rem; margin-top: .5rem;
           cursor: pointer; }
  .row { padding: .2rem .4rem; border-radius: 4px; cursor: pointer; }
  .row:hover { background: #eef; }
  .row.picked { background: #dde8ff; }
  .note { color: #555; font-size: .9rem; white-space: pre-wrap; }
  .warn { color: #a15c00; }
  .err  { color: #b00020; white-space: pre-wrap; }
  .ok   { color: #1a7f37; }
  code  { background: #f4f4f4; padding: 0 .3rem; border-radius: 3px; }
</style>
<h1>FTB Universal Questbook — Chapter Import</h1>

<fieldset id="s1">
  <legend>1 · Instance</legend>
  <p>Path to the instance you authored in (the folder containing <code>config</code>):</p>
  <input type="text" id="instance" placeholder="/home/you/…/Instances/My Pack">
  <button onclick="scan()">Scan</button>
  <div id="s1out" class="note"></div>
</fieldset>

<fieldset id="s2" disabled>
  <legend>2 · Chapter</legend>
  <div id="chapters"></div>
</fieldset>

<fieldset id="s3" disabled>
  <legend>3 · Destination</legend>
  <div id="suggestion" class="note"></div>
  <p><label><input type="radio" name="mode" value="into" id="modeInto">
     Update the module that owns it: <code id="ownerSlug">—</code></label></p>
  <p><label><input type="radio" name="mode" value="new" id="modeNew" checked>
     New module slug: </label><input type="text" id="slug"></p>
  <button onclick="preview()">Preview</button>
</fieldset>

<fieldset id="s4" disabled>
  <legend>4 · Preview</legend>
  <div id="previewOut" class="note"></div>
  <p><label><input type="checkbox" id="reg" checked>
     Insert the ID-REGISTRY.md row (new modules only)</label></p>
  <button onclick="apply()">Write it</button>
</fieldset>

<fieldset id="s5" disabled>
  <legend>5 · Result</legend>
  <div id="applyOut" class="note"></div>
</fieldset>

<script>
const TOKEN = "__TOKEN__";
let picked = null;

async function api(path, body) {
  const opts = body === undefined
    ? { headers: { "X-Import-Token": TOKEN } }
    : { method: "POST", headers: { "X-Import-Token": TOKEN,
        "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const $ = id => document.getElementById(id);
const enable = (...ids) => ids.forEach(i => $(i).disabled = false);
const fail = (el, e) => { el.innerHTML = `<div class="err">${e.message}</div>`; };

async function scan() {
  const out = $("s1out");
  try {
    const data = await api("/api/scan", { instance: $("instance").value });
    if (data.note) {
      out.innerHTML = `<div class="warn">${data.note}</div>`;
      $("s2").disabled = true;
      return;
    }
    out.innerHTML = `<span class="ok">✓ ${data.quests_dir}</span>`;
    const list = $("chapters");
    list.innerHTML = "";
    for (const c of data.chapters) {
      const div = document.createElement("div");
      div.className = "row";
      div.textContent = c.parse_error
        ? `${c.stem} — could not parse`
        : `${c.stem} — ${c.quest_count} quests (${c.origin})`;
      if (!c.parse_error) div.onclick = () => pick(c, div);
      list.appendChild(div);
    }
    enable("s2");
  } catch (e) { fail(out, e); }
}

async function pick(c, div) {
  picked = c;
  document.querySelectorAll(".row").forEach(r => r.classList.remove("picked"));
  div.classList.add("picked");
  try {
    const data = await api("/api/suggest",
        { instance: $("instance").value, chapter: c.stem });
    const s = data.suggestion;
    let text = `mods: ${s.inferred_mods.join(", ") || "vanilla only"}\\n` +
               `suggested group: ${s.group}`;
    for (const n of data.lang_notes)
      text += `\\n${n.startsWith("warning") ? "⚠ " : ""}${n}`;
    $("suggestion").textContent = text;
    $("ownerSlug").textContent = c.owner_slug || "—";
    $("modeInto").disabled = !c.owner_slug;
    $("modeInto").checked = !!c.owner_slug;
    $("modeNew").checked = !c.owner_slug;
    $("slug").value = s.slug || "";
    enable("s3");
  } catch (e) { fail($("suggestion"), e); }
}

async function preview() {
  const out = $("previewOut");
  try {
    const into = $("modeInto").checked ? picked.owner_slug : null;
    const data = await api("/api/preview", {
      instance: $("instance").value, chapter: picked.stem,
      into: into, new_slug: into ? null : $("slug").value,
    });
    let text = `${data.slug}  (module 0x${data.module_number.toString(16)
        .toUpperCase()} / chapter ${data.chapter_number})  group: ${data.group}\\n` +
      `mods: ${data.requires.join(", ") || "vanilla only"}\\n` +
      `${data.quest_count} quests into:\\n`;
    for (const [slug, target] of Object.entries(data.targets))
      text += `  ${target}\\n`;
    for (const n of data.notes)
      text += `${n.startsWith("warning") ? "⚠ " : "· "}${n}\\n`;
    if (data.registry_row) text += `registry row: ${data.registry_row}`;
    out.textContent = text;
    enable("s4");
  } catch (e) { fail(out, e); }
}

async function apply() {
  const out = $("applyOut");
  try {
    const data = await api("/api/apply",
        { confirm: true, update_registry: $("reg").checked });
    out.innerHTML = data.written.map(w => `<div class="ok">✓ wrote ${w}</div>`)
        .join("");
    enable("s5");
    const v = await api(`/api/validate?module=${data.slug}`);
    out.innerHTML += v.clean
      ? `<div class="ok">✓ ${data.slug} validates clean</div>`
      : `<div class="warn">validate found problems:\\n${v.output}</div>`;
    out.innerHTML += `<div class="note">Prove it on a real server:\\n` +
      `<code>python3 tools/testbed.py run --modules 00-core ${data.slug}</code></div>`;
  } catch (e) { fail(out, e); }
}
</script>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0,
                        help="port to bind (default: any free port)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--allow-any-path", action="store_true",
                        help="permit instance paths outside your home directory")
    args = parser.parse_args(argv)

    httpd = make_server(port=args.port, allow_any_path=args.allow_any_path)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"chapter import UI: {url}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()
    print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
