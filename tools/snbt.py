"""SNBT reader/writer matching the dialect FTB Quests reads and writes.

FTB Quests stores every quest object as SNBT (Stringified NBT). Its dialect
differs from strict JSON in ways that matter if you want files the mod will
round-trip without reformatting the whole thing on first edit:

  * members are newline-separated; commas are legal but optional
  * keys are bare unless they contain a colon or other punctuation
  * numbers carry a type suffix -- 0.5d, 3.0f, 128L, 1b
  * empty containers render as "{ }" and "[ ]", with the inner space
  * indentation is tabs
  * keys are written in alphabetical order

That last point is the one people get wrong. FTB re-serialises alphabetically
whenever a book is edited in-game, so hand-written files in any other order
produce a huge spurious diff the first time someone opens the quest editor.
We sort on write for exactly that reason.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["Num", "loads", "dumps", "load", "dump", "SNBTError"]


class SNBTError(ValueError):
    """Raised when input is not valid SNBT."""


class Num:
    """A number that remembers its NBT type suffix.

    Round-tripping matters: `size: 1.0d` and `size: 1` are different types to
    the mod, and silently rewriting one as the other can make a quest fail to
    load. We keep the literal exactly as authored.
    """

    __slots__ = ("value", "suffix")

    _PATTERN = re.compile(
        r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[bBsSlLfFdD]?$"
    )

    def __init__(self, value: int | float, suffix: str = ""):
        self.value = value
        self.suffix = suffix

    @classmethod
    def matches(cls, token: str) -> bool:
        return bool(cls._PATTERN.match(token))

    @classmethod
    def parse(cls, token: str) -> "Num":
        suffix = ""
        body = token
        if body and body[-1] in "bBsSlLfFdD":
            # A trailing 'd'/'f' is only a suffix if something remains that
            # still parses as a number -- guards against a bare word like "d".
            head = body[:-1]
            if head and re.match(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$", head):
                suffix = body[-1]
                body = head
        if suffix in ("f", "F", "d", "D") or "." in body or "e" in body or "E" in body:
            return cls(float(body), suffix)
        return cls(int(body), suffix)

    def __str__(self) -> str:
        if self.suffix in ("f", "F", "d", "D"):
            # FTB always writes a decimal point on floats and doubles.
            text = repr(float(self.value))
            if "e" in text or "E" in text:
                text = f"{float(self.value):.10f}".rstrip("0")
                if text.endswith("."):
                    text += "0"
            return text + self.suffix
        return f"{self.value}{self.suffix}"

    def __repr__(self) -> str:
        return f"Num({self.value!r}, {self.suffix!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Num):
            return self.value == other.value and self.suffix == other.suffix
        return self.value == other

    def __hash__(self) -> int:
        return hash((self.value, self.suffix))


# Convenience constructors for the suffixes quest files actually use.
def d(value: float) -> Num:
    return Num(float(value), "d")


def f(value: float) -> Num:
    return Num(float(value), "f")


def L(value: int) -> Num:
    return Num(int(value), "L")


_BARE_KEY = re.compile(r"^[A-Za-z0-9_+.\-]+$")
_BARE_TOKEN = re.compile(r"[^\s,;:\[\]{}\"']+")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    # -- diagnostics ------------------------------------------------------
    def _where(self) -> str:
        line = self.text.count("\n", 0, self.pos) + 1
        col = self.pos - (self.text.rfind("\n", 0, self.pos))
        return f"line {line}, column {col}"

    def fail(self, message: str) -> "SNBTError":
        return SNBTError(f"{message} at {self._where()}")

    # -- scanning ---------------------------------------------------------
    def skip_ws(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in " \t\r\n":
                self.pos += 1
            elif ch == "#":
                nl = text.find("\n", self.pos)
                self.pos = n if nl == -1 else nl + 1
            elif ch == "/" and self.pos + 1 < n and text[self.pos + 1] == "/":
                nl = text.find("\n", self.pos)
                self.pos = n if nl == -1 else nl + 1
            else:
                return

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.fail(f"expected {ch!r} but found {self.peek()!r}")
        self.pos += 1

    # -- values -----------------------------------------------------------
    def parse_value(self) -> Any:
        self.skip_ws()
        ch = self.peek()
        if ch == "":
            raise self.fail("unexpected end of input")
        if ch == "{":
            return self.parse_compound()
        if ch == "[":
            return self.parse_list()
        if ch in "\"'":
            return self.parse_string()
        return self.parse_bare()

    def parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == "\\":
                if self.pos + 1 >= n:
                    raise self.fail("dangling escape in string")
                nxt = text[self.pos + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.pos += 2
                continue
            if ch == quote:
                self.pos += 1
                return "".join(out)
            out.append(ch)
            self.pos += 1
        raise self.fail("unterminated string")

    def parse_bare(self) -> Any:
        match = _BARE_TOKEN.match(self.text, self.pos)
        if not match:
            raise self.fail(f"unexpected character {self.peek()!r}")
        token = match.group(0)
        self.pos = match.end()
        if token == "true":
            return True
        if token == "false":
            return False
        if Num.matches(token):
            return Num.parse(token)
        return token

    def parse_key(self) -> str:
        self.skip_ws()
        if self.peek() in "\"'":
            return self.parse_string()
        # A bare key runs up to the colon. Colons are legal inside quoted keys
        # only, so anything unquoted stops there.
        start = self.pos
        text, n = self.text, len(self.text)
        while self.pos < n and text[self.pos] not in " \t\r\n:":
            self.pos += 1
        if self.pos == start:
            raise self.fail("expected a key")
        return text[start:self.pos]

    def parse_compound(self) -> dict[str, Any]:
        self.expect("{")
        result: dict[str, Any] = {}
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.pos += 1
                return result
            if self.peek() == "":
                raise self.fail("unterminated compound")
            key = self.parse_key()
            self.skip_ws()
            self.expect(":")
            result[key] = self.parse_value()
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1

    def parse_list(self) -> list[Any]:
        self.expect("[")
        self.skip_ws()
        # Typed arrays: [I; 1, 2, 3] / [B; ...] / [L; ...]
        saved = self.pos
        if re.match(r"^[BILbil]\s*;", self.text[self.pos:self.pos + 3] or " "):
            self.pos = self.text.index(";", self.pos) + 1
        else:
            self.pos = saved
        result: list[Any] = []
        while True:
            self.skip_ws()
            if self.peek() == "]":
                self.pos += 1
                return result
            if self.peek() == "":
                raise self.fail("unterminated list")
            result.append(self.parse_value())
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1


def loads(text: str) -> Any:
    """Parse SNBT text into Python values."""
    parser = _Parser(text)
    value = parser.parse_value()
    parser.skip_ws()
    if parser.pos != len(parser.text):
        raise parser.fail("trailing content after top-level value")
    return value


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else f'"{_escape(key)}"'


def dumps(value: Any, *, indent: int = 0, sort_keys: bool = True) -> str:
    """Serialise to SNBT in the style FTB Quests itself writes.

    Keys are sorted alphabetically by default because that is what the mod
    does; matching it keeps diffs clean after someone edits in-game.
    """
    pad = "\t" * indent
    inner_pad = "\t" * (indent + 1)

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Num):
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(Num(value, "d"))
    if isinstance(value, str):
        return f'"{_escape(value)}"'
    if value is None:
        return '""'

    if isinstance(value, dict):
        if not value:
            return "{ }"
        keys = sorted(value.keys()) if sort_keys else list(value.keys())
        lines = [
            f"{inner_pad}{_render_key(k)}: "
            f"{dumps(value[k], indent=indent + 1, sort_keys=sort_keys)}"
            for k in keys
        ]
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"

    if isinstance(value, (list, tuple)):
        if not value:
            return "[ ]"
        # FTB writes primitive-only arrays inline -- dependencies: ["ABC..."].
        # Matching that avoids a spurious diff on the first in-game edit.
        if all(not isinstance(v, (dict, list, tuple)) for v in value):
            inline = ", ".join(
                dumps(v, indent=indent, sort_keys=sort_keys) for v in value
            )
            if len(inline) <= 96:
                return f"[{inline}]"
        lines = [
            f"{inner_pad}{dumps(v, indent=indent + 1, sort_keys=sort_keys)}"
            for v in value
        ]
        return "[\n" + "\n".join(lines) + f"\n{pad}]"

    raise TypeError(f"cannot serialise {type(value).__name__} to SNBT")


def load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())


def dump(value: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dumps(value) + "\n")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: snbt.py <file.snbt>   (parses and re-emits, for diffing)")
        raise SystemExit(2)
    print(dumps(load(sys.argv[1])))
