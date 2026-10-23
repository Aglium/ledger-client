"""Coding-agent hooks: what the estate knows, at the moments work is decided.

Four moments, in the order a session reaches them:

  the task is stated     the prompt names a ticket and, often, a contract.
                         Nothing has been read yet, so this is the earliest
                         anything can be said -- and on a real session the
                         prompt named a related ticket whose branch the agent
                         then spent fourteen minutes not finding.
  the plan is presented  the last moment a change is cheap. The plan is prose
                         and prose names things.
  a branch is created    work acquires an identity and a ticket.
  work is pushed         the original trigger, and the latest useful one.

The first two need no diff, because there is none: they answer who else is
working on this and what governs it, and they never judge -- nothing has been
written to judge.

A skill is advice the agent may skip. A PreToolUse hook runs whether or not the
agent thought to ask: when the agent pushes a branch or opens a PR, the brief
for that branch is put in front of it, and a `decision_needed` verdict stops to
ask the human before the push goes out.

Three rules keep it from becoming the thing people disable:

  * It never approves. Returning "allow" would bypass the user's own
    permission rules; the hook only adds context or escalates to "ask".
  * It fails open. No config, no database, a repo outside the estate, a
    timeout, any exception: no output, and the command runs as if the hook
    did not exist. A broken ledger must not stop anyone pushing.
  * It is quiet on `proceed`. Context the agent does not need is noise.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

#: Beyond this, in-flight state is old enough that "nothing in flight" has to
#: be read as "unknown". The sync runs every few minutes, so this is many
#: missed cycles, not a slow one -- on a real estate a merged sibling stayed
#: invisible for fifteen hours and a brief flipped from review_with to
#: proceed with nothing else changed.
STALE_AFTER_SECONDS = 30 * 60

#: Commands that publish work. Deliberately narrow: `git push` and opening a
#: PR/MR, not every git call.
_PUBLISH = re.compile(
    r"(?:^|[;&|(]\s*|\s)(git\s+(?:-C\s+\S+\s+)?push|gh\s+pr\s+create|glab\s+mr\s+create)\b")

#: Starting a branch. The moment work acquires an identity -- and a ticket, in
#: an estate that names branches after them -- which is the earliest point a
#: brief can say anything, and long before a push.
_BRANCH_START = re.compile(
    r"(?:^|[;&|(]\s*|\s)git\s+(?:-C\s+\S+\s+)?(?:checkout\s+-b|switch\s+-c)\s+(\S+)")


#: Where a pull request is opened against: `gh pr create --base stage`,
#: `glab mr create --target-branch stage`. The one place a change names the
#: branch it is built for, so it outranks any guess about its base.
_PR_BASE = re.compile(r"(?:gh\s+pr\s+create|glab\s+mr\s+create)\b[^;&|]*?"
                      r"\s(?:--base|-B|--target-branch|-b)[=\s]+['\"]?([^\s'\";&|]+)")

#: A command run somewhere else than the session's directory: `cd x && ...`
#: or `git -C x push`. Feedback on a real session: a PR was opened from
#: `cd ../acme-leads && gh pr create`, and the brief was about the
#: repository the session had been in before -- unrelated, start to end.
_CD = re.compile(r"(?:^|[;&|(]\s*)cd\s+(['\"]?)([^'\";&|]+?)\1\s*(?:&&|;)")
_GIT_C = re.compile(r"\bgit\s+-C\s+(['\"]?)([^'\"\s;&|]+)\1")


def pr_base(command: str) -> str | None:
    m = _PR_BASE.search(command or "")
    return m.group(1) if m else None


def effective_cwd(command: str, cwd: str) -> str:
    """The directory a command acts in: the last `cd` before it, or `git -C`."""
    where = cwd
    for m in _CD.finditer(command or ""):
        target = os.path.expanduser(m.group(2).strip())
        where = target if os.path.isabs(target) else os.path.normpath(os.path.join(where, target))
    m = _GIT_C.search(command or "")
    if m:
        target = os.path.expanduser(m.group(2))
        where = target if os.path.isabs(target) else os.path.normpath(os.path.join(where, target))
    return where


def is_publish(command: str) -> bool:
    return bool(_PUBLISH.search(command or ""))


def branch_started(command: str) -> str | None:
    """The branch a command is about to create, if it is creating one."""
    m = _BRANCH_START.search(command or "")
    return m.group(1).strip("'\"") if m else None


def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_and_ref(cwd: str) -> tuple[str, str] | None:
    """(estate repo name, current branch) for a working directory.

    The repo is named after its origin remote, not the directory it sits in:
    a real estate had `acme-orchestrator-service` checked out as
    `orcestrator_service`, so a hook naming the directory asked the index
    about a repository that does not exist there. The directory name is the
    fallback for a clone with no remote.

    The main clone, not the working directory: an agent working in a git
    worktree of `orders` is still changing `orders`.
    """
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ref = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not common or not ref or ref == "HEAD":        # detached HEAD: nothing to name
        return None
    main_clone = Path(common).parent if Path(common).name == ".git" else Path(common)
    url = _git(str(main_clone), "remote", "get-url", "origin") or ""
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if url else ""
    return name or main_clone.name, ref


def freshness(b: dict) -> str | None:
    """How old the in-flight state behind this answer is.

    An answer that does not carry its age cannot be told apart from a fresh
    one, and the expensive half of a brief -- who else is working on this --
    is exactly the half that goes stale.
    """
    at = (b.get("index") or {}).get("synced_at")
    if not at:
        return None
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age < 90:
        return "in-flight state: synced just now"
    if age < STALE_AFTER_SECONDS:
        return f"in-flight state: synced {int(age // 60)} min ago"
    older = (f"{int(age // 3600)}h {int(age % 3600 // 60)}m" if age < 86400
             else f"{int(age // 86400)}d")
    return (f"in-flight state: synced {older} ago — STALE. Anything about other repos, "
            "including \"nothing in flight\", is unknown rather than false.")


#: Longest any one of an item's detail lines may be. Four of these under a
#: heading is still something an agent reads; a paragraph is not.
DETAIL_WIDTH = 230


#: How `core.effect` opens when a diff names no topic, route or table.
NAMES_NOTHING = "names no topic, route or table"


def behaviour_line(i: dict) -> str | None:
    """A model's sentence about what the change does at runtime, labelled.

    The label is the point. Everything else in a brief is derived the same
    way every time; this is a model reading a diff, possibly a cut one, and a
    reader who takes it as fact has swapped one unread branch for another.
    """
    b = i.get("behaviour")
    if not isinstance(b, dict) or not b.get("text"):
        return None
    how = f"confidence {b.get('confidence') or 'low'}" + (", diff was cut" if b.get("partial") else "")
    return f"behaviour (model summary of its diff, not verified; {how}): {b['text']}"


def described(i: dict) -> str:
    """What an in-flight item does, how it faces this change, how far to
    trust that, and what to ask -- as short indented lines, five at most.

    A branch name and an author is a pointer. A review that was handed one
    went on to read the other branch's commit titles and reported the opposite
    of what it does. These lines are what the reader would otherwise have to
    open another repository to find, and often cannot.
    """
    out = ""
    said = behaviour_line(i)
    for label, key in (("does", "effect"), ("behaviour", None), ("relation", "relation"),
                       ("evidence", "evidence"), ("ask", "ask")):
        value = said if key is None else i.get(key)
        # A diff that names nothing says only that, and the model's sentence
        # is the better use of the line.
        if key == "effect" and said and str(value or "").startswith(NAMES_NOTHING):
            continue
        if value:
            text = value if key is None else f"{label}: {value}"
            # The model's sentence is stored already cut to length, and it
            # is the one line that cannot be shortened without changing what
            # it says; its label is not to be lost either.
            width = DETAIL_WIDTH * 2 + 40 if key is None else DETAIL_WIDTH
            if len(text) > width:
                text = text[:width - 1].rstrip() + "…"
            out += f"\n    {text}"
    return out


#: Reasons that are bookkeeping about the change itself rather than a claim
#: about another repository: always worth their line.
_ABOUT_THE_CHANGE = ("file(s) are changed in the working tree", "it has no commits beyond",
                     "this branch does not exist yet", "this change could not be found",
                     "the index is incomplete")

_KIND_WORDS = {"impact": "in-flight change(s)", "decision": "decision(s)",
               "contract": "shared contract(s)", "local": "change(s) on an integration branch here"}


def nothing_touched(b: dict) -> bool:
    """Ranked, and nothing in it is strong enough to lead.

    Never true of a stop: decision_needed and insufficient_data say why.
    """
    r = b.get("ranked")
    return bool(r and r.get("nothing")) and b.get("recommendation") in ("proceed", "review_with")


def also_line(r: dict) -> str | None:
    """Everything that did not lead, counted, with no names a reader would chase."""
    also = r.get("also") or {}
    if not also.get("count"):
        return None
    parts = []
    for kind, n in sorted((also.get("by_kind") or {}).items(), key=lambda kv: -kv[1]):
        word = _KIND_WORDS.get(kind, f"{kind}(s)")
        if kind == "impact" and also.get("repos"):
            repos = ", ".join(f"{k} ×{v}" if v > 1 else k
                              for k, v in sorted(also["repos"].items(), key=lambda kv: -kv[1])[:3])
            word += f" ({repos})"
        parts.append(f"{n} {word}")
    return ("also seen, not linked to your diff: " + ", ".join(parts)
            + " — `mcp__ledger__brief` lists them; do not chase these without a reason from the diff")


def _lead_lines(b: dict, r: dict) -> tuple[list[str], set[str]]:
    """The leading items in full, each with how to check it. And who they name."""
    from ledger.core.rank import contract_key, decision_key, impact_key, local_key
    by_key: dict[str, tuple[str, dict | str]] = {}
    for i in b.get("impacts", []):
        by_key[impact_key(i)] = ("impact", i)
    for d in b.get("decisions", []):
        by_key[decision_key(d)] = ("decision", d)
    for c in b.get("contracts", []):
        by_key[contract_key(c)] = ("contract", c)
    for line in b.get("not_local", []):
        by_key[local_key(line)] = ("local", line)
    out: list[str] = []
    people: set[str] = set()
    for lead in r.get("lead", []):
        kind, src = by_key.get(lead["key"], (None, None))
        check = f"\n    {lead['check']}" if lead.get("check") else ""
        if kind == "impact":
            i = src
            where = (f" (in {i['where']}" + (f", {i['sha']}" if i.get("sha") else "") + ")"
                     if i.get("where") else "")
            out.append(f"in flight: {i['repo']} {i['ref']}{where}"
                       + (f" by {i['author']}" if i.get("author") else "")
                       + (f" — {i['how']}" if i.get("how") else "") + described(i) + check)
            if i.get("author"):
                people.add(i["author"])
        elif kind == "decision":
            d = src
            gate = f" — OPEN GATE: {d['gate_hit'][:140]}" if d.get("gate_hit") else ""
            who = f" (deciders: {', '.join(d['deciders'])})" if d.get("deciders") else ""
            out.append(f"decision {d['repo']}/{d['path']} [{d['status']}]{who}{gate}{check}")
            people.update(d.get("deciders") or [])
        elif kind == "contract":
            c = src
            roles = [f"{p['repo']} {p['uses']} ({p['how']})" for p in c.get("parties", [])
                     if p["uses"] in ("publishes", "consumes")]
            line = (f"shared contract {c['surface']} `{c['name']}`: "
                    + ("; ".join(roles) if roles else "also in " + ", ".join(c["also_touched_by"])))
            for fd in (c.get("fields") or [])[:4]:
                role = f" — {fd['role']}" if fd.get("role") else ""
                line += f"\n    {fd['repo']} {fd['direction']} `{fd['field']}`{role}"[:180]
            out.append(line + check)
        elif kind == "local":
            out.append(f"{src}{check}")
    return out, people


def summarise(b: dict) -> str:
    """The brief as a few lines an agent reads before it publishes.

    Ranked (`core.rank`): the few items with a reason to be there, in full and
    with how to check each; everything else in one counted line. Without a
    `ranked` section -- an older server -- everything, as before.
    """
    r = b.get("ranked")
    if not r:
        return _summarise_everything(b)
    s = b["subject"]
    rec = b["recommendation"]
    if nothing_touched(b):
        lines = [f"Ledger brief for {s['repo']} @ {s['ref']}: nothing in flight or pending that "
                 "this diff touches"]
    else:
        lines = [f"Ledger brief for {s['repo']} @ {s['ref']}: {rec.upper()} "
                 f"(confidence {b['confidence']})"]
    for f in b.get("field_breaks", [])[:5]:
        who = ", ".join(f"{x['repo']} ({x['direction']})" for x in f.get("readers", [])[:4])
        lines.append(f"- REMOVES `{f['field_path']}` from {f['surface']} `{f['name']}` — {who} "
                     f"depend(s) on it; deleted in {f['path']}")
    if rec in ("decision_needed", "insufficient_data"):
        lines += [f"- {x}" for x in b.get("reasons", [])[:4]]
    lead, people = _lead_lines(b, r)
    if lead:
        lines.append("NOT VISIBLE FROM THIS REPOSITORY — weigh these first:")
        lines += [f"- {x}" for x in lead]
        lines.append("Say what each one means for this change, or say why it does not apply.")
    if r.get("repeated"):
        when = min(r["repeated"].values())[:10]
        names = ", ".join(k.split(":", 1)[1] for k in list(r["repeated"])[:4])
        lines.append(f"- already shown for this branch since {when}, unchanged, not repeated: {names}")
    also = also_line(r)
    if also:
        lines.append(f"- {also}")
    lines += [f"- {x}" for x in b.get("reasons", [])
              if x.startswith(_ABOUT_THE_CHANGE) and rec not in ("decision_needed", "insufficient_data")]
    named = [a for a in b.get("actors", []) if a in people]
    if named:
        lines.append(f"- involve: {', '.join(named)}")
    if rec == "insufficient_data":
        lines.append("- insufficient_data means UNKNOWN, not safe.")
    fresh = freshness(b)
    if fresh:
        lines.append(f"- {fresh}")
    return "\n".join(lines)


def _summarise_everything(b: dict) -> str:
    """The brief as a few lines an agent reads before it publishes."""
    s = b["subject"]
    lines = [f"Ledger brief for {s['repo']} @ {s['ref']}: {b['recommendation'].upper()} "
             f"(confidence {b['confidence']})"]
    # The facts that are the whole reason this exists, in their own block with
    # something to do about them. They used to be bullets among bullets, and a
    # measured session proved what that costs: of three facts in one brief,
    # the two that could be found by grepping were rediscovered by hand and
    # the one that could not -- `order_refund_row` sitting unreleased on
    # `stage` -- was never mentioned again.
    elsewhere = list(b.get("not_local") or [])
    # A named branch before a commit that merged. Both are in flight, but one
    # is a thing the reader can open and reason about and the other is twelve
    # hex digits: a real brief on a six-repo ticket spent all six of its lines
    # on `marketing-site 8c70c360ad29` and hid the branches underneath.
    impacts = sorted(b.get("impacts", []), key=lambda i: bool(i.get("where")))
    for i in impacts[:6]:
        # Merged work is named after a branch that was deleted when it merged,
        # so the commit goes alongside: it is what the reader can still open.
        where = (f" (in {i['where']}" + (f", {i['sha']}" if i.get("sha") else "") + ")"
                 if i.get("where") else "")
        elsewhere.append(f"in flight: {i['repo']} {i['ref']}{where}"
                         + (f" by {i['author']}" if i.get("author") else "")
                         + (f" — {i['how']}" if i.get("how") else "")
                         + described(i))
    if elsewhere:
        lines.append("NOT VISIBLE FROM THIS REPOSITORY — reading the code here cannot show these:")
        lines += [f"- {x}" for x in elsewhere[:8]]
        lines.append("Say what each one means for this change, or say why it does not apply.")
    said_elsewhere = set(b.get("not_local") or [])
    lines += [f"- {r}" for r in b.get("reasons", []) if r not in said_elsewhere][:6]
    for f in b.get("field_breaks", [])[:5]:
        who = ", ".join(f"{r['repo']} ({r['direction']})" for r in f.get("readers", [])[:4])
        lines.append(f"- REMOVES `{f['field_path']}` from {f['surface']} `{f['name']}` — {who} "
                     f"depend(s) on it; deleted in {f['path']}")
    shared = [c for c in b.get("contracts", []) if c.get("also_touched_by")]
    for c in shared[:5]:
        roles = [f"{p['repo']} {p['uses']} ({p['how']})" for p in c.get("parties", [])
                 if p["uses"] in ("publishes", "consumes")]
        lines.append(f"- shared contract {c['surface']} `{c['name']}`: "
                     + ("; ".join(roles) if roles else "also in " + ", ".join(c["also_touched_by"])))
        # What they do with it, in words. Without this the agent has a pointer
        # and has to go and read a repository that may not be checked out.
        for fd in (c.get("fields") or [])[:6]:
            role = f" — {fd['role']}" if fd.get("role") else ""
            lines.append(f"    {fd['repo']} {fd['direction']} `{fd['field']}`{role}"[:180])
    # Only what the reasons did not already say: the verdict names the
    # documentation for the contracts it ranked, and repeating those is noise.
    said = "\n".join(lines)
    for c in b.get("contracts", [])[:20]:
        if c["name"] in said:
            continue
        for pg in (c.get("documented_in") or [])[:1]:
            when = f", last updated {pg['updated'][:10]}" if pg.get("updated") else ""
            lines.append(f"- documented: {c['surface']} `{c['name']}` is described in "
                         f"\"{pg['title'][:70]}\"{when} — {pg.get('url') or pg['path']}")
    for d in b.get("decisions", [])[:4]:
        gate = f" — OPEN GATE: {d['gate_hit']}" if d.get("gate_hit") else ""
        who = f" (deciders: {', '.join(d['deciders'])})" if d.get("deciders") else ""
        lines.append(f"- decision {d['repo']}/{d['path']} [{d['status']}]{who}{gate}")
    if b.get("actors"):
        lines.append(f"- involve: {', '.join(b['actors'])}")
    if b["recommendation"] == "insufficient_data":
        lines.append("- insufficient_data means UNKNOWN, not safe.")
    fresh = freshness(b)
    if fresh:
        lines.append(f"- {fresh}")
    return "\n".join(lines)


def response(b: dict) -> dict | None:
    """Hook output for a brief, or None for no output at all.

    Never "allow": see the module docstring.
    """
    rec = b.get("recommendation")
    if rec == "proceed" or nothing_touched(b):
        return None
    text = summarise(b)
    out: dict = {"hookEventName": "PreToolUse", "additionalContext": text}
    if rec == "decision_needed":
        out["permissionDecision"] = "ask"
        out["permissionDecisionReason"] = text
    return {"hookSpecificOutput": out}


def starting_response(b: dict, branch: str) -> dict | None:
    """Output for a branch that is only now being created.

    It can carry no diff, so it never blocks: what it knows is what else is in
    flight for the ticket in the branch name, which is worth reading before
    the first line is written and worth nothing as an interruption.
    """
    if b.get("recommendation") == "proceed" or nothing_touched(b):
        return None
    text = (f"Ledger, before you start `{branch}`:\n" + summarise(b)
            + "\n\nThis is what the rest of the estate is already doing under this ticket.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def context_text(got: dict, *, lead: str) -> str | None:
    """A context answer as a few lines, or None when there is nothing to say."""
    lines = []
    # Grouped by ticket. Eighteen branch names is a wall of text that buries
    # the contracts underneath it; the counts and the repos are the content.
    by_ticket: dict[str, list[dict]] = {}
    for c in got.get("inflight", []):
        by_ticket.setdefault(c.get("ticket") or "", []).append(c)
    for ticket, changes in list(by_ticket.items())[:4]:
        repos = sorted({c["repo"] for c in changes})
        integrated = sum(1 for c in changes if c.get("where"))
        named = ", ".join(c["ref"] for c in changes if not c.get("where"))[:150]
        tail = (f"; {integrated} merged to an integration branch, not released"
                if integrated else "")
        lines.append(f"- {ticket}: {len(changes)} change(s) in {len(repos)} repo(s) — "
                     f"{', '.join(repos)}{tail}")
        if named:
            lines.append(f"    not yet integrated: {named}")
        # What each one does. Before a diff exists there is nothing to relate
        # it to, so this is the effect and how far to trust it, and no more
        # than three: a prompt is not the place for a report.
        told = [c for c in changes if c.get("effect") or behaviour_line(c)][:3]
        for c in told:
            at = f" ({c['where']})" if c.get("where") else ""
            said = behaviour_line(c)
            if c.get("effect") and not (said and c["effect"].startswith(NAMES_NOTHING)):
                lines.append(f"    {c['repo']} {c['ref']}{at} does: {c['effect']}"[:DETAIL_WIDTH + 40])
            if said:
                lines.append(f"    {c['repo']} {c['ref']}{at} {said}"[:DETAIL_WIDTH + 140])
        if told:
            lines.append("    (summarised by Ledger from their diffs; you have not read them — "
                         "a branch name or commit title is not evidence)")
    for c in got.get("contracts", [])[:5]:
        roles = ", ".join(f"{p['repo']} {p['uses']}" for p in c.get("parties", [])[:4])
        line = f"- {c['surface']} `{c['name']}`" + (f": {roles}" if roles else "")
        for pg in c.get("documented_in", [])[:1]:
            behind = f", {pg['behind']} commit(s) behind" if pg.get("behind") else ""
            line += f"; documented in \"{pg['title'][:60]}\"{behind}"
        lines.append(line)
        # The part no amount of reading this repository can find, so it gets
        # its own line rather than a clause at the end of a wiring list. One
        # real brief carried exactly this fact about `order_refund_row`
        # inside a fourteen-line block and the session never mentioned it.
        for m in c.get("moving", [])[:3]:
            lines.append(f"    MOVING NOW: {m['repo']} is changing it on `{m['ref']}` — "
                         f"merged there, not released, and not on your branch")
    for d in got.get("decisions", [])[:4]:
        gate = f" — OPEN GATE: {d['gate_hit'][:110]}" if d.get("gate_hit") else ""
        lines.append(f"- decision {d['repo']}/{d['path']} [{d['status']}]{gate}")
    if not lines:
        return None
    fresh = freshness(got)
    if fresh:
        lines.append(f"- {fresh}")
    return lead + "\n" + "\n".join(lines)


def handle_user_prompt(payload: dict, context_for: Callable[..., dict],
                       root: str | None) -> dict | None:
    """UserPromptSubmit: what the estate already knows about the task.

    The one moment that always exists. A session may never push, never open a
    branch and never commit -- a real one did all three -- but it always
    begins with somebody saying what they want.
    """
    try:
        if not under(root, payload.get("cwd") or os.getcwd()):
            return None
        prompt = payload.get("prompt") or ""
        if len(prompt.strip()) < 12 or machine_generated(prompt):
            return None
        where = repo_and_ref(payload.get("cwd") or os.getcwd())
        text = context_text(context_for(prompt, repo=where[0] if where else None),
                            lead="Ledger, on what the rest of the estate already has for this:")
        if text is None:
            return None
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": text + _PROMPT_TAIL}}
    except Exception:                               # noqa: BLE001 -- fail open, always
        return None


#: A prompt the person did not write. A coding tool replays tool output, task
#: notifications and system reminders through the same hook, and one of those
#: was 13,000 characters of somebody else's log -- from which the ledger
#: cheerfully matched six contracts and five decisions, none of them the task.
_MACHINE = re.compile(r"\A\s*<(task-notification|system-reminder|command-message|command-name|"
                      r"local-command-stdout|user-prompt-submit-hook)\b", re.I)


def machine_generated(prompt: str) -> bool:
    return bool(_MACHINE.match(prompt or ""))


_PROMPT_TAIL = ("\n\nThis is cross-repo context, found from the words of the request alone. "
                "Check it before planning; `mcp__ledger__inflight_for` and "
                "`mcp__ledger__who_owns` answer follow-up questions.")


def handle(payload: dict, brief_for: Callable[[str, str], dict],
           brief_for_new: Callable[[str, str], dict] | None = None,
           context_for: Callable[[str], dict] | None = None) -> dict | None:
    """Pure decision: payload in, hook output (or None) out. Any failure -> None.

    `brief_for_new` answers about a branch that does not exist yet; without it
    a branch start is simply not a trigger.
    """
    try:
        if payload.get("tool_name") == "ExitPlanMode" and context_for is not None:
            plan = (payload.get("tool_input") or {}).get("plan") or ""
            here = repo_and_ref(payload.get("cwd") or os.getcwd())
            text = context_text(context_for(plan, repo=here[0] if here else None),
                                lead="Ledger, on what this plan touches:")
            if text is None:
                return None
            # Context only. The person is already being asked to approve the
            # plan; a second gate on the same decision is not a safeguard.
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "additionalContext": text}}
        if payload.get("tool_name") != "Bash":
            return None
        command = (payload.get("tool_input") or {}).get("command", "")
        where = repo_and_ref(effective_cwd(command, payload.get("cwd") or os.getcwd()))
        if where is None:
            return None
        if is_publish(command):
            base = pr_base(command)
            return response(brief_for(*where, base=base) if base else brief_for(*where))
        branch = branch_started(command)
        if branch and brief_for_new is not None:
            # The branch does not exist yet, so the brief is asked about the
            # name it is about to have: the ticket is in it, and that is the
            # part that can be answered.
            return starting_response(brief_for_new(where[0], branch), branch)
        return None
    except Exception:                               # noqa: BLE001 -- fail open, always
        return None


def http_brief(url: str, token: str | None, timeout: float,
               via: str = "push_hook", worktree: bool = False,
               allow_missing_ref: bool = False) -> Callable[[str, str], dict]:
    """Ask a running `ledger-server serve` -- the fast path, the index is warm there.

    `worktree` is safe here only because the server reads the same clones this
    hook is running in; a remote ledger has no working tree to read and says so.
    """
    def go(repo: str, ref: str, base: str | None = None) -> dict:
        q = urllib.parse.urlencode({"repo": repo, "ref": ref, "via": via,
                                    **({"base": base} if base else {}),
                                    **({"worktree": "1"} if worktree else {}),
                                    **({"new": "1"} if allow_missing_ref else {})})
        req = urllib.request.Request(f"{url.rstrip('/')}/brief?{q}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return go


# ------------------------------------------------------------ session start --

#: The observed double-fires were one and five seconds apart. Kept deliberately
#: short: a second window opened on the same branch a minute later is a real
#: session that deserves its own answer, not a duplicate.
DEDUPE_SECONDS = 30


def answered_recently(surface: str, repo: str, ref: str, *,
                      within_seconds: int = DEDUPE_SECONDS, stamps: Path | None = None) -> bool:
    """Has this exact question been asked from this machine a moment ago?

    A coding agent fires SessionStart more than once -- startup and resume
    arrived seconds apart on a real estate -- and the second copy is the same
    brief, computed again and injected again. Remembered as a stamp file on
    this machine, which is where both copies fire. Asking marks it asked.
    """
    import hashlib
    import tempfile
    import time
    try:
        root = stamps or Path(tempfile.gettempdir()) / "ledger-hooks"
        root.mkdir(parents=True, exist_ok=True)
        stamp = root / hashlib.sha256(f"{surface}\0{repo}\0{ref}".encode()).hexdigest()[:32]
        recent = stamp.exists() and time.time() - stamp.stat().st_mtime < within_seconds
        stamp.touch()
        return recent
    except Exception:                                   # noqa: BLE001 -- never block a hook
        return False


def under(root: str | None, cwd: str) -> bool:
    """Is this session inside the estate? Everything else is none of our business."""
    if not root:
        return False
    try:
        return Path(cwd).expanduser().resolve().is_relative_to(Path(root).expanduser().resolve())
    except (OSError, ValueError):
        return False


#: Named in full because these are the tool names an agent must ask for. On a
#: real session the tools were never loaded, so they were never callable: the
#: brief arrived, said nothing was in flight, and the ledger was never asked
#: another question all session.
_TOOLS = (
    "Ledger tools, loadable by name: `mcp__ledger__who_owns` (before changing a topic, "
    "endpoint or table), `mcp__ledger__inflight_for` (when a ticket is named), "
    "`mcp__ledger__decisions_for` (before contradicting a design), `mcp__ledger__brief`, "
    "`mcp__ledger__feedback`. If they are not already loaded, load them before answering a "
    "cross-repo question rather than guessing from this repo alone."
)


def session_context(b: dict) -> str:
    """What the agent sees from turn one. One line when there is nothing to say.

    A session starting on a branch nobody else is touching gets a single line:
    enough to know the ledger is there, not enough to be noise.
    """
    event = f" (event {b['event_id']})" if b.get("event_id") else ""
    if nothing_touched(b):
        s = b["subject"]
        fresh = freshness(b)
        also = also_line(b["ranked"])
        return (f"Ledger: nothing in flight or pending that this diff touches for {s['repo']} @ "
                f"{s['ref']}{event}" + (f" ({fresh})" if fresh else "") + "."
                + (f" {also[0].upper()}{also[1:]}." if also else "")
                + " Ask `mcp__ledger__who_owns` before changing a topic, endpoint or table.")
    if b.get("recommendation") == "proceed":
        s = b["subject"]
        # Still one line. A session with nothing to say gets nothing to read;
        # the tools are named where there is a reason to reach for them.
        fresh = freshness(b)
        age = f" ({fresh})" if fresh else ""
        return (f"Ledger: no cross-repo work or pending decisions found for {s['repo']} @ "
                f"{s['ref']}{event}, as it stands now{age}. Ask `mcp__ledger__who_owns` "
                "before changing a topic, endpoint or table.")
    return (summarise(b)
            + f"\n\nThis is cross-repo context from the ledger{event}, computed at session start. "
              "Mention it when it bears on the work. If the user reacts to it (useful, noise, "
              "wrong, or something it missed), record that with the ledger `feedback` tool.\n"
            + _TOOLS)


def headline(b: dict) -> str:
    """One line for the person at the keyboard."""
    s = b.get("subject", {})
    rec = (b.get("recommendation") or "unknown").upper()
    where = f"{s.get('repo')} @ {s.get('ref')}"
    if rec == "PROCEED":
        return f"Ledger · {where}: nothing cross-repo found for this branch."
    if nothing_touched(b):
        n = ((b.get("ranked") or {}).get("also") or {}).get("count") or 0
        return (f"Ledger · {where}: nothing cross-repo this diff touches"
                + (f" ({n} weak link(s) not shown)" if n else "") + ".")
    reason = (b.get("reasons") or [""])[0]
    stale = " · STALE INDEX" if "STALE" in (freshness(b) or "") else ""
    return f"Ledger · {where}: {rec}{stale} — {reason[:140]}"


def handle_session_start(payload: dict, brief_for: Callable[[str, str], dict],
                         root: str | None) -> dict | None:
    """Pure decision for SessionStart. Any failure -> None (no output)."""
    try:
        cwd = payload.get("cwd") or os.getcwd()
        if not under(root, cwd):
            return None
        where = repo_and_ref(cwd)
        if where is None:
            return None
        b = brief_for(*where)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": session_context(b)},
                # Shown to the person, not only the agent. On real use the verdict
                # reached the agent's context and the developer never saw it.
                "systemMessage": headline(b)}
    except Exception:                               # noqa: BLE001 -- fail open, always
        return None


# ------------------------------------------------------------- session stop --

_EVENT = re.compile(r"\(event ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)")

#: Characters of reply sent per answer: the end of a session's reply is where
#: its conclusions are, and a review runs to a few thousand.
REPLY_CHARS = 12_000


def replies_to_answers(lines) -> dict[str, str]:
    """For each ledger answer a transcript carries, what the agent wrote after it.

    `lines` are the transcript's JSON lines. An answer is found by the event id
    its own text carries; a reply is the agent's prose after it, to the end.
    Only the agent's words leave: never tool output, never the user's.
    """
    shown: list[tuple[int, str]] = []
    texts: list[tuple[int, str]] = []
    for n, raw in enumerate(lines):
        try:
            e = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if e.get("type") == "attachment":
            for m in _EVENT.finditer(json.dumps(e.get("attachment") or {})):
                shown.append((n, m.group(1)))
        elif e.get("type") == "assistant":
            for b in ((e.get("message") or {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    texts.append((n, b["text"]))
    out: dict[str, str] = {}
    for at, event in shown:
        after = "\n\n".join(t for n, t in texts if n > at)
        if after:
            out[event] = after[-REPLY_CHARS:]
    return out


def handle_session_stop(payload: dict, send: Callable[[str, str], object], root: str | None, *,
                        stamps: Path | None = None) -> int:
    """Stop: send each answer's reply once it has grown, for the items it set aside.

    Stop fires at the end of every turn. A reply is sent again only when the
    agent has written more since, and remembered on this machine like the
    session-start stamps. Returns how many were sent; never raises.
    """
    import hashlib
    import tempfile
    try:
        cwd = payload.get("cwd") or os.getcwd()
        path = payload.get("transcript_path")
        if not path or not under(root, cwd):
            return 0
        with open(Path(path).expanduser(), encoding="utf-8", errors="ignore") as f:
            got = replies_to_answers(f)
        if not got:
            return 0
        folder = stamps or Path(tempfile.gettempdir()) / "ledger-hooks"
        folder.mkdir(parents=True, exist_ok=True)
        sent = 0
        for event, reply in got.items():
            stamp = folder / ("stop-" + hashlib.sha256(event.encode()).hexdigest()[:32])
            size = str(len(reply))
            if stamp.exists() and stamp.read_text() == size:
                continue
            send(event, reply)
            stamp.write_text(size)
            sent += 1
        return sent
    except Exception:                               # noqa: BLE001 -- fail open, always
        return 0
