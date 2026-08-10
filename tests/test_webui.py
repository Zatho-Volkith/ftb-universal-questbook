"""End-to-end tests for the chapter-import web UI.

The server is started in-process on an ephemeral port against a throwaway
copy of the repository, and driven with urllib exactly the way the page's
fetch() calls drive it. Nothing here touches the real repo or the network.
"""
import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import snbt    # noqa: E402
import webui   # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def call(port, path, body=None, token=None, host=None):
    """Returns (status, payload) without raising on HTTP errors."""
    if host:
        # urllib always rewrites the Host header from the URL, so a spoofed
        # Host needs the raw client.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Host": host, "Content-Type": "application/json"}
        if token:
            headers["X-Import-Token"] = token
        conn.request("POST" if body is not None else "GET", path,
                     None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data)
    if token:
        request.add_header("X-Import-Token", token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def make_fixture_repo() -> Path:
    work = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(REPO, work, ignore=shutil.ignore_patterns(
        ".git", "testbed", "dist", "__pycache__", "*.bak-*"))
    return work


def make_fixture_instance() -> Path:
    instance = Path(tempfile.mkdtemp()) / "instance"
    chapters = instance / "config" / "ftbquests" / "quests" / "chapters"
    chapters.mkdir(parents=True)
    (instance / "config" / "ftbquests" / "quests" / "lang").mkdir()
    tree = {
        "default_hide_dependency_lines": False,
        "default_quest_shape": "",
        "filename": "imported",
        "group": "",
        "icon": {"id": "sophisticatedstorage:controller"},
        "id": "1234ABCD5678EF01",
        "images": [],
        "order_index": 0,
        "quest_links": [],
        "quests": [
            {
                "id": "0011223344556677",
                "rewards": [{"id": "0011223344556678", "type": "xp", "xp": 10}],
                "subtitle": "Craft a barrel",
                "tasks": [{"id": "0011223344556679",
                           "item": {"count": snbt.Num(1),
                                    "id": "sophisticatedstorage:barrel"},
                           "type": "item"}],
                "title": "A Better Barrel",
                "x": snbt.Num(0.0, "d"),
                "y": snbt.Num(0.0, "d"),
            },
        ],
        "subtitle": "test chapter",
        "title": "Imported",
    }
    lang = {"quest.0011223344556677.quest_desc":
            ["Recovered through the web UI.", "", "Second paragraph."]}
    snbt.dump(tree, str(chapters / "imported.snbt"))
    snbt.dump(lang, str(chapters.parent / "lang" / "en_us.snbt"))
    return instance


def start(repo_root, allow_any_path):
    httpd = webui.make_server(repo_root, port=0, allow_any_path=allow_any_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    token = httpd.RequestHandlerClass.session.token
    return httpd, port, token


def main() -> int:
    print("webui end to end\n")
    fixture_repo = make_fixture_repo()
    instance = make_fixture_instance()
    httpd, port, token = start(fixture_repo, allow_any_path=True)

    # -- guards ------------------------------------------------------------
    status, payload = call(port, "/api/scan", {"instance": str(instance)})
    check("POST without token is refused", status == 403, str(payload))

    status, payload = call(port, "/api/scan", {"instance": str(instance)},
                           token=token, host="evil.example:80")
    check("bad Host header is refused", status == 403, str(payload))

    status, payload = call(port, "/api/apply", {"confirm": True}, token=token)
    check("apply before preview is refused", status == 422, str(payload))

    # -- fresh and empty instances are explained, not errors ----------------
    fresh = Path(tempfile.mkdtemp()) / "fresh-instance"
    (fresh / "mods").mkdir(parents=True)          # no config folder at all
    status, payload = call(port, "/api/scan", {"instance": str(fresh)},
                           token=token)
    check("fresh instance scan succeeds with a note", status == 200
          and payload["chapters"] == [] and "no questbook yet"
          in (payload.get("note") or ""), str(payload))

    blank = Path(tempfile.mkdtemp()) / "blank-instance"
    (blank / "config" / "ftbquests" / "quests" / "chapters").mkdir(parents=True)
    status, payload = call(port, "/api/scan", {"instance": str(blank)},
                           token=token)
    check("blank questbook scan succeeds with a note", status == 200
          and payload["chapters"] == [] and "no chapters"
          in (payload.get("note") or ""), str(payload))
    shutil.rmtree(fresh.parent, ignore_errors=True)
    shutil.rmtree(blank.parent, ignore_errors=True)

    # -- the flow the page drives ------------------------------------------
    status, payload = call(port, "/api/scan", {"instance": str(instance)},
                           token=token)
    check("scan finds the chapter", status == 200
          and payload["chapters"][0]["stem"] == "imported"
          and payload.get("note") is None, str(payload))

    status, payload = call(port, "/api/suggest",
                           {"instance": str(instance), "chapter": "imported"},
                           token=token)
    check("suggest infers the group", status == 200
          and payload["suggestion"]["group"] == "tech", str(payload))
    check("suggest reports lang recovery",
          any("recovered" in n for n in payload["lang_notes"]), str(payload))

    status, payload = call(port, "/api/preview",
                           {"instance": str(instance), "chapter": "imported",
                            "new_slug": "7a-testmod"}, token=token)
    check("preview plans the import", status == 200
          and payload["slug"] == "7a-testmod"
          and payload["quest_count"] == 1, str(payload))
    check("preview mints a registry row",
          payload.get("registry_row", "").startswith("| `7A`"), str(payload))

    status, payload = call(port, "/api/apply",
                           {"confirm": True, "update_registry": True},
                           token=token)
    chapter_path = fixture_repo / "modules/7a-testmod/1.21.1/chapters/imported.snbt"
    manifest_path = fixture_repo / "modules/7a-testmod/module.json"
    check("apply writes into the fixture repo", status == 200
          and chapter_path.exists() and manifest_path.exists(), str(payload))
    check("nothing was written to the real repo",
          not (REPO / "modules" / "7a-testmod").exists())

    written = snbt.load(str(chapter_path))
    quest = written["quests"][0]
    check("quest was renumbered to the slot",
          str(quest["id"]) == "7B7A010300010000", str(quest["id"]))
    check("lang text was recovered on the way in",
          quest.get("description") == ["Recovered through the web UI.", "",
                                       "Second paragraph."],
          str(quest.get("description")))
    registry = (fixture_repo / "ID-REGISTRY.md").read_text(encoding="utf-8")
    check("registry row inserted", "| `7A` | `7a-testmod` |" in registry)

    status, payload = call(port, "/api/validate?module=7a-testmod",
                           token=token)
    check("validate endpoint runs clean", status == 200 and payload["clean"],
          str(payload)[:400])

    httpd.shutdown()

    # -- path policy -------------------------------------------------------
    httpd2, port2, token2 = start(fixture_repo, allow_any_path=False)
    status, payload = call(port2, "/api/scan", {"instance": str(instance)},
                           token=token2)
    check("outside-home path refused by default", status == 422
          and "home" in payload.get("error", ""), str(payload))
    httpd2.shutdown()

    shutil.rmtree(fixture_repo.parent, ignore_errors=True)
    shutil.rmtree(instance.parent, ignore_errors=True)

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total}" if total else ""))
    for line in FAILURES:
        print(f"  {line}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
