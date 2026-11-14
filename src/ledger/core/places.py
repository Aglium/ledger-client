"""Where in a file a change is, so two changes of one file can be compared.

Two open changes of the same file are not two changes of the same thing.
A service registry, a translation catalogue, a module of error classes: every
change of the repository edits them, each its own entry. What says two changes
meet is the place: the same function, the same key, the same line.

A place is worked out from the file after the change and the change's hunks
alone, never from line numbers: the two changes are each read against their
own base, and a line number from one means nothing in the other. A line's
place is the chain of lines above it that hold it, by indentation -- a
method in a class, a key under its parents -- so it reads the same on any
base where the surrounding code is the same.

Language-neutral on purpose; it is a heuristic of indentation and a few
keywords, not a parser. When a place cannot be read, callers fall back to
the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: A line that opens a function or method: changes inside one are at one place.
_FUNC = re.compile(
    r"\b(def|function|func|fn|fun|sub)\b"
    r"|=>\s*[{(]?\s*$"
    r"|^\s*(?:(?:public|private|protected|internal|static|final|abstract|async|override|"
    r"virtual|export|default)\s+)+[\w<>\[\],.? ]*\(",
)
#: A JS/TS/Java-style method or block that looks like a call: `name(args) {`.
_METHODISH = re.compile(r"^\s*(?:async\s+)?[A-Za-z_$][\w$]*\s*\([^;]*\)\s*(?::\s*[^{=]+)?\{\s*$")
_CONTROL = re.compile(r"^\s*(?:\}\s*)?(if|else|elif|for|foreach|while|switch|case|catch|try|"
                      r"with|return|match|do|finally)\b")
#: Lines that are only brackets: the line that opens the block is above them.
_BRACKETISH = re.compile(r"^[\s{}()\[\];,]*$|^\s*\)")
#: Lines whose own text says nothing: two changes adding a `}` do not meet.
_TRIVIAL = re.compile(r"^[\s{}()\[\];,:\-*#/<>'\"`|=+.]*$|^\s*(?:return|break|continue|pass|end|"
                      r"else|try|finally)\b\W*$")
#: Imports: two changes importing the same name have not met.
_IMPORT = re.compile(r"^\s*(?:import|from|use|require|require_once|include|#include|using)\b")
#: The key of a `key: value`, `key = value`, `"key": value` or `'key' => value` line.
_KEY = re.compile(r"""^\s*(?:-\s+)?["']?([^"'\s:=]+(?:\s[^"'\s:=]+)*?)["']?\s*(?::(?!:)|=>|=(?!=))""")


@dataclass
class Places:
    """The places one change of one file is at."""

    #: Function bodies changed, and keys or lines changed outside of one.
    at: set = field(default_factory=set)
    #: For blocks only added, the unchanged line they were added after: two
    #: changes adding after the same line conflict when merged, and nothing
    #: more -- each adds its own.
    after: set = field(default_factory=set)


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _opener(lines: list[str], i: int) -> int:
    """For a bracket-only line, the line above at its indentation that opens the block."""
    k = _indent(lines[i])
    j = i
    while j > 0 and _BRACKETISH.match(lines[j]):
        j -= 1
        while j > 0 and (not lines[j].strip() or _indent(lines[j]) > k):
            j -= 1
    return j


def _holders(lines: list[str], pos: int, indent: int) -> list[str]:
    """The lines above `pos` that hold a line indented by `indent`, outermost first."""
    out: list[str] = []
    want = indent
    j = min(pos, len(lines)) - 1
    while j >= 0 and want > 0:
        line = lines[j]
        if line.strip() and _indent(line) < want:
            o = _opener(lines, j)
            out.append(lines[o].strip())
            want = _indent(lines[o])
            j = o - 1
            continue
        j -= 1
    out.reverse()
    return out


def _is_func(line: str) -> bool:
    if _CONTROL.match(line):
        return False
    return bool(_FUNC.search(line) or _METHODISH.match(line))


def _place(holders: list[str], line: str):
    """One changed line's place: the function holding it, else its key or text."""
    for n in range(len(holders), 0, -1):
        if _is_func(holders[n - 1]):
            return ("in",) + tuple(holders[:n])
    text = line.strip()
    if _is_func(line):
        return ("in",) + tuple(holders) + (text,)
    if not text or _TRIVIAL.match(text) or _IMPORT.match(text):
        return None
    m = _KEY.match(text)
    return ("key",) + tuple(holders) + ((m.group(1) if m else text),)


def places_of(after: str, body: str) -> Places:
    """The places a change is at, from the file after it and its `--unified=0` hunks."""
    lines = after.splitlines()
    got = Places()
    hunks: list[tuple[int, int, list[str]]] = []
    for raw in body.splitlines():
        m = _HUNK.match(raw)
        if m:
            hunks.append((int(m.group(3)), int(m.group(4)) if m.group(4) is not None else 1, []))
        elif hunks and raw.startswith("-"):
            hunks[-1][2].append(raw[1:])
    for start, count, gone in hunks:
        if count:
            first = start - 1
            for i in range(first, min(first + count, len(lines))):
                line = lines[i]
                p = _place(_holders(lines, i, _indent(line)), line)
                if p:
                    got.at.add(p)
            if not gone and first > 0:
                before = first - 1
                while before > 0 and not lines[before].strip():
                    before -= 1
                anchor = lines[before] if before < len(lines) else ""
                if anchor.strip():
                    got.after.add(tuple(_holders(lines, before, _indent(anchor)))
                                  + (anchor.strip(),))
        # A removed line stood where the hunk is; with nothing added, git
        # numbers the line before the removal.
        pos = (start - 1) if count else start
        for line in gone:
            p = _place(_holders(lines, pos, _indent(line)), line)
            if p:
                got.at.add(p)
    return got


def label(place) -> str:
    """A short name for a place, for a brief."""
    parts = [p for p in place[1:] if p]
    if not parts:
        return ""
    last = parts[-1]
    last = re.sub(r"\s*[{(:]\s*$", "", last).strip()
    return last if len(last) <= 60 else last[:57] + "..."


def meet(a: Places, b: Places) -> tuple[list, list]:
    """Where two changes of a file meet: shared places, then shared insertion points."""
    shared = sorted(a.at & b.at, key=lambda p: (len(p), p))
    # Inside the same function is one place for both; a key under a function
    # one of them changes is in it too.
    funcs_a = {p for p in a.at if p[0] == "in"}
    funcs_b = {p for p in b.at if p[0] == "in"}
    for fa in funcs_a:
        for fb in funcs_b:
            if fa != fb and (fa[: len(fb)] == fb or fb[: len(fa)] == fa):
                shared.append(fa if len(fa) < len(fb) else fb)
    seen, out = set(), []
    for p in shared:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, sorted(a.after & b.after)
