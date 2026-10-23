"""Feedback on Ledger's answers, kept on this machine.

The client sends nothing anywhere, so whether a finding was any good is
something the person who read it says, explicitly, and it stays here until
they choose to share it. What is kept is a verdict and the names that place
it -- repository, branch, commit, the recommendation it answered -- and an
optional note in the person's own words. Never code, never a diff.

`summary(share=True)` is what someone can hand back to Aglium or a colleague:
counts, with every repository and branch name replaced by a salted hash and
notes left out unless they ask for them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

#: What a reader can say about a finding. The first four are the ones a replay
#: asks for; `missed` is for a change Ledger stayed silent on that mattered.
VERDICTS = ("useful", "known", "false_positive", "insufficient_evidence", "missed")

#: Earlier words for the same answers, still accepted from agents and scripts.
ALIASES = {
    "already known": "known", "already_known": "known", "noise": "known",
    "false positive": "false_positive", "wrong": "false_positive", "fp": "false_positive",
    "can't verify": "insufficient_evidence", "cant_verify": "insufficient_evidence",
    "cannot verify": "insufficient_evidence", "insufficient evidence": "insufficient_evidence",
    "unverifiable": "insufficient_evidence",
}

#: What an organisation server's `feedback` endpoint accepts, for each verdict.
#: The original word travels in the note, so nothing is lost on the way.
SERVER_WORD = {"useful": "useful", "known": "noise", "false_positive": "wrong",
               "insufficient_evidence": "wrong", "missed": "missed"}


class BadVerdict(ValueError):
    pass


def normalise(verdict: str) -> str:
    v = (verdict or "").strip().lower()
    v = ALIASES.get(v, v).replace("-", "_").replace(" ", "_")
    v = ALIASES.get(v, v)
    if v not in VERDICTS:
        raise BadVerdict("verdict must be one of: " + ", ".join(VERDICTS))
    return v


def data_dir() -> Path:
    """`$LEDGER_DATA_DIR`, or the user's data directory."""
    where = os.environ.get("LEDGER_DATA_DIR")
    if where:
        return Path(where).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "ledger"


def store() -> Path:
    return data_dir() / "feedback.jsonl"


def record(verdict: str, *, repo: str | None = None, ref: str | None = None,
           sha: str | None = None, recommendation: str | None = None, note: str = "",
           source: str = "cli", case: str | None = None, event_id: str | None = None) -> dict:
    """Append one answer to the local store, and return what was kept."""
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": normalise(verdict), "repo": repo, "ref": ref, "sha": sha,
        "recommendation": recommendation, "case": case, "event_id": event_id,
        "source": source, "note": (note or "").strip()[:2000],
    }
    row = {k: v for k, v in row.items() if v not in (None, "")}
    f = store()
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def rows() -> list[dict]:
    try:
        text = store().read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --------------------------------------------------------- from a replay --

_LABEL = {"useful": "useful", "already known": "known", "false positive": "false_positive",
          "can't verify": "insufficient_evidence", "missed": "missed"}
_VERDICT_LINE = re.compile(r"Your verdict \(`(?P<id>[0-9a-f]{8})`\):(?P<boxes>.*)$")
_MISSED_LINE = re.compile(r"^- \[[xX]\] missed \(`(?P<id>[0-9a-f]{8})`\)")
_TICKED = re.compile(r"\[[xX]\]\s+(useful|already known|false positive|can't verify)")


def from_report(md: Path) -> list[tuple[str, str]]:
    """(case id, verdict) for every box ticked in a replay report."""
    out: list[tuple[str, str]] = []
    for line in md.read_text().splitlines():
        m = _VERDICT_LINE.search(line)
        if m:
            for label in _TICKED.findall(m.group("boxes")):
                out.append((m.group("id"), _LABEL[label]))
            continue
        m = _MISSED_LINE.search(line)
        if m:
            out.append((m.group("id"), "missed"))
    return out


def cases_beside(md: Path) -> dict[str, dict]:
    """The replay's cases by id, from the JSON written next to its report."""
    try:
        doc = json.loads(md.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return {}
    return {c["id"]: c for c in doc.get("cases", [])}


def import_report(md: Path) -> tuple[list[dict], list[str]]:
    """Keep the answers ticked in a replay report. Returns what was kept and
    the ids that were already kept, so importing twice does not count twice."""
    cases = cases_beside(md)
    have = {(r.get("case"), r.get("verdict")) for r in rows()}
    kept, skipped = [], []
    for cid, verdict in from_report(md):
        if (cid, verdict) in have:
            skipped.append(cid)
            continue
        c = cases.get(cid, {})
        kept.append(record(verdict, repo=c.get("repo"), ref=c.get("ref"), sha=c.get("sha"),
                           recommendation=c.get("recommendation"), source="replay", case=cid))
        have.add((cid, verdict))
    return kept, skipped


def case(md: Path, cid: str) -> dict | None:
    return cases_beside(md).get(cid)


# --------------------------------------------------------------- summary --

def _salt() -> str:
    f = data_dir() / "share-salt"
    try:
        return f.read_text().strip()
    except OSError:
        s = secrets.token_hex(16)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(s)
        except OSError:
            pass
        return s


def _h(salt: str, name: str | None) -> str | None:
    return hashlib.sha256(f"{salt}\0{name}".encode()).hexdigest()[:10] if name else None


def summary(*, share: bool = False, notes: bool = False) -> dict:
    """Counts by verdict and by the recommendation answered.

    With `share`, also every answer with its names hashed, so whoever receives
    it can tell answers apart and see two answers were about the same
    repository, but not which repository. Notes only with `notes`.
    """
    got = rows()
    by_verdict = {v: 0 for v in VERDICTS}
    by_rec: dict[str, dict[str, int]] = {}
    for r in got:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        rec = r.get("recommendation") or "unknown"
        by_rec.setdefault(rec, {}).setdefault(r["verdict"], 0)
        by_rec[rec][r["verdict"]] += 1
    judged = sum(by_verdict[v] for v in ("useful", "known", "false_positive", "insufficient_evidence"))
    out = {
        "answers": len(got),
        "by_verdict": by_verdict,
        "by_recommendation": by_rec,
        "rates": {
            "useful": round(by_verdict["useful"] / judged, 2) if judged else None,
            "false_positive": round(by_verdict["false_positive"] / judged, 2) if judged else None,
            "insufficient_evidence": (round(by_verdict["insufficient_evidence"] / judged, 2)
                                      if judged else None),
        },
    }
    if share:
        salt = _salt()
        out["answers_hashed"] = [{
            "at": r.get("at", "")[:10], "verdict": r["verdict"],
            "recommendation": r.get("recommendation"), "source": r.get("source"),
            "repo": _h(salt, r.get("repo")), "ref": _h(salt, r.get("ref")),
            **({"note": r["note"]} if notes and r.get("note") else {}),
        } for r in got]
        out["contains"] = ("verdicts, dates, Ledger's recommendation, and salted hashes of "
                           "repository and branch names"
                           + (", and your notes as you wrote them" if notes else "")
                           + ". No code, no paths, no names.")
    return out
