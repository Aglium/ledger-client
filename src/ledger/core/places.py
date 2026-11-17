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
    r"|\)\s*(?::\s*[\w<>\[\]|,.? ]+)?\s*=>\s*[{(]?\s*$"
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
#: A line that closes a bracket opened above and goes on: `) -> None:`, `}) {`.
#: It belongs to the line that opened the bracket.
_CLOSES = re.compile(r"^\s*[)\]}]")
#: A line that only opens a bracket, with no key of its own: `Entry(`, `[`.
_OPENS = re.compile(r"[(\[{]\s*$")
#: A name another change can depend on: `mariadb-tunnel`, `calculate_delay`,
#: `calculateDelay`. Six characters or more, and not a plain word.
_NAME = re.compile(r"[A-Za-z_$][\w$-]*[\w$]")
#: The name a function line declares: the last identifier before its `(`.
_FUNC_NAME = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")


def _specific(name: str) -> bool:
    return len(name) >= 6 and ("-" in name or "_" in name.strip("_$")
                               or any(ch.isupper() for ch in name[1:]))


def _declared(line: str) -> str | None:
    """The name a line declares: its key, or the function it opens."""
    if _is_func(line):
        names = _FUNC_NAME.findall(line)
        return names[0] if names else None
    m = _KEY.match(line.strip())
    return m.group(1) if m else None


def func_name(place) -> str | None:
    """The name of the function an `in` place ends with."""
    if not place or place[0] != "in":
        return None
    names = _FUNC_NAME.findall(place[-1])
    return names[0] if names else None


@dataclass
class Places:
    """The places one change of one file is at."""

    #: Function bodies changed, and keys or lines changed outside of one.
    at: set = field(default_factory=set)
    #: For blocks only added, the unchanged line they were added after: two
    #: changes adding after the same line conflict when merged, and nothing
    #: more -- each adds its own.
    after: set = field(default_factory=set)
    #: What the change does at each place, as its changed lines: two changes
    #: making the same edit at a place (a port, a shared commit) merge clean.
    edits: dict = field(default_factory=dict)
    #: Names the change stops declaring in this file -- a key, a function --
    #: that the file no longer has anywhere after it.
    gone: set = field(default_factory=set)
    #: Names its added lines refer to, beyond the one each line declares.
    uses: set = field(default_factory=set)
    #: Functions it adds right next to, above or below, with the names of the
    #: functions it adds there: `{("in", "class C", "calculateDelay()"): {"calculateDelayMinutes"}}`.
    next_to: dict = field(default_factory=dict)


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


def _holder_lines(lines: list[str], pos: int, indent: int) -> list[int]:
    """Where the lines above `pos` that hold a line indented by `indent` are, outermost first."""
    out: list[int] = []
    want = indent
    j = min(pos, len(lines)) - 1
    while j >= 0 and want > 0:
        line = lines[j]
        if line.strip() and _indent(line) < want:
            o = _opener(lines, j)
            out.append(o)
            want = _indent(lines[o])
            j = o - 1
            continue
        j -= 1
    out.reverse()
    return out


def _holders(lines: list[str], pos: int, indent: int) -> list[str]:
    """The lines above `pos` that hold a line indented by `indent`, outermost first."""
    return [lines[j].strip() for j in _holder_lines(lines, pos, indent)]


def _squashed(text: str) -> str:
    return "".join(text.split())


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
    text = "\n".join(lines)
    for start, count, gone in hunks:
        first = start - 1
        added = range(first, min(first + count, len(lines))) if count else range(0)
        for line in gone:
            name = _declared(line)
            if name and _specific(name) and not re.search(rf"(?<![\w$-]){re.escape(name)}(?![\w$-])", text):
                got.gone.add(name)
        for i in added:
            own = _declared(lines[i])
            got.uses |= {n for n in _NAME.findall(lines[i]) if n != own and _specific(n)}
        # Only whitespace moved -- `catch {}` put on two lines: nothing happened there.
        if gone and count and _squashed("".join(gone)) == _squashed("".join(lines[i] for i in added)):
            continue
        for i in added:
            line = lines[i]
            if not line.strip():
                continue
            o = i
            if _CLOSES.match(line) and not _TRIVIAL.match(line.strip()):
                o = _opener(lines, i)           # `) -> None:` is part of its `def`
            held = _holder_lines(lines, o, _indent(lines[o]))
            here = lines[o]
            # Inside a block this same change adds -- a new entry's own lines,
            # or `Entry(` opening one -- the entry has no place another change
            # can share: every `Entry(slug=..., path=...)` has a `slug` and a
            # `path`, and two changes each adding their own have not met. Only
            # the function around it, if any, is a place.
            k = next((n for n, j in enumerate(held) if j in added), None)
            opens = (o == i and i + 1 in added and _OPENS.search(here) and not _is_func(here)
                     and not _KEY.match(here.strip()))
            if k is not None or opens:
                outer = [lines[j].strip() for j in (held if k is None else held[:k])]
                fn = [n for n, h in enumerate(outer) if _is_func(h)]
                if not fn:
                    continue
                p = ("in",) + tuple(outer[:fn[-1] + 1])
            else:
                p = _place([lines[j].strip() for j in held], here)
            if p:
                got.at.add(p)
                got.edits.setdefault(p, set()).add("+" + _squashed(line))
        if count:
            if not gone and first > 0:
                before = first - 1
                while before > 0 and not lines[before].strip():
                    before -= 1
                anchor = lines[before] if before < len(lines) else ""
                if anchor.strip():
                    got.after.add(tuple(_holders(lines, before, _indent(anchor)))
                                  + (anchor.strip(),))
                _neighbours(got, lines, added, before)
        # A removed line stood where the hunk is; with nothing added, git
        # numbers the line before the removal.
        pos = (start - 1) if count else start
        for line in gone:
            p = _place(_holders(lines, pos, _indent(line)), line)
            if p:
                got.at.add(p)
                got.edits.setdefault(p, set()).add("-" + _squashed(line))
    return got


def _neighbours(got: Places, lines: list[str], added: range, before: int) -> None:
    """The functions an added block sits right next to, and what it adds there."""
    near = []
    first = next((i for i in added if lines[i].strip()), None)
    if first is None:
        return
    level = _indent(lines[first])
    if 0 <= before < len(lines) and lines[before].strip():
        # Just after a function's end: the `}` that closes it, or in an
        # indented language the last line of its body.
        o = _opener(lines, before) if _BRACKETISH.match(lines[before]) else before
        for j in [o, *reversed(_holder_lines(lines, o, _indent(lines[o])))]:
            if _indent(lines[j]) == level and _is_func(lines[j]):
                near.append(j)
                break
            if _indent(lines[j]) < level:
                break
    nxt = added.stop
    while nxt < len(lines) and not lines[nxt].strip():
        nxt += 1
    if nxt < len(lines) and _is_func(lines[nxt]):         # just above a function
        near.append(nxt)
    if not near:
        return
    new = set()
    for i in added:
        if _is_func(lines[i]):
            name = _declared(lines[i])
            if name:
                new.add(name)
    for o in near:
        place = ("in",) + tuple(_holders(lines, o, _indent(lines[o]))) + (lines[o].strip(),)
        got.next_to.setdefault(place, set()).update(new)


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
    def differ(p) -> bool:
        # The same edit on both sides merges clean: a port, or a commit both carry.
        return not a.edits.get(p) or a.edits.get(p) != b.edits.get(p)
    shared = sorted((p for p in a.at & b.at if differ(p)), key=lambda p: (len(p), p))
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


def removed_used(a: Places, b: Places) -> tuple[list, list]:
    """Names one change removes and the other's added lines refer to:
    (`a` removes, `b` uses), then (`b` removes, `a` uses)."""
    return sorted(a.gone & b.uses), sorted(b.gone & a.uses)


def beside(a: Places, b: Places) -> list[tuple]:
    """Where `b` adds a function right next to one `a` changes.

    `(place, new names, named after it)`: named after it when a new name
    holds the old one -- `calculateDelayMinutes` beside `calculateDelay`.
    """
    out = []
    # By name under the same holders: the other may have rewritten the
    # signature line itself.
    changed = {(p[:-1], func_name(p)) for p in a.at if p[0] == "in" and func_name(p)}
    for place, new in b.next_to.items():
        if (place[:-1], func_name(place)) in changed:
            old = func_name(place) or ""
            kin = sorted(n for n in new if old and len(old) >= 4 and old.lower() in n.lower())
            out.append((place, kin or sorted(new), bool(kin)))
    return out
