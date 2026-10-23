"""Run the control arm across a whole collection, and compare it to the ticket join.

The comparison is the point. Two detectors that agree tell you the cheap one is
enough; two that disagree tell you exactly what the expensive one is buying, in
cases you can read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ledger.adapters.vcs.base import ForgeUnavailable, VcsAdapter
from ledger.core.join import Change, find_siblings
from ledger.core.redact import redact
from ledger.core.overlap import (
    DiffFile, OverlapHit, Signal, drop_common, drop_frequent, extract_signals,
    find_overlaps,
)
from ledger.pipeline.collect import Collection

#: Files whose presence in two diffs means nothing. Lockfiles and CI config
#: change constantly and would otherwise link every branch to every other.
DEFAULT_IGNORE = (
    r"\.lock$", r"lock\.json$", r"^\.github/", r"^\.gitlab", r"CHANGELOG", r"\.md$",
    r"^dist/", r"^build/", r"node_modules/", r"\.snap$",
)


@dataclass
class ScanResult:
    signals: dict[tuple[str, str], set[Signal]] = field(default_factory=dict)
    unreadable: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def readable(self) -> int:
        return len(self.signals)


def scan_signals(
    adapters: list[VcsAdapter],
    collection: Collection,
    *,
    default_branches: dict[str, str] | None = None,
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
    max_repos_per_signal: int | None = None,
) -> ScanResult:
    """Extract deterministic signals for every change we can diff."""
    by_source = {a.source: a for a in adapters if a.supports_diffs}
    default_branches = default_branches or {}
    res = ScanResult()

    for c in collection.changes:
        ref_obj = collection.repo_refs.get(c.repo)
        base = default_branches.get(c.repo) or (ref_obj.default_branch if ref_obj else "main")
        source = collection.repo_sources.get(c.repo)
        adapter = by_source.get(source) or next(iter(by_source.values()), None)
        if adapter is None:
            res.unreadable.append((c.repo, c.ref, "no adapter can produce diffs"))
            continue
        try:
            files = adapter.changed_files(c.repo, base, c.ref)
        except ForgeUnavailable as e:
            # Recorded, never silently skipped: a change we could not read is
            # not a change with no overlap.
            res.unreadable.append((c.repo, c.ref, str(e)))
            continue
        # Redact here too, not only on the model path. Two reasons, and the
        # second was found by measurement: personal data should never be in the
        # index either, AND fixture values were themselves the noise -- street
        # and city names from test addresses were scoring as "shared code".
        res.signals[(c.repo, c.ref)] = extract_signals(
            [DiffFile(path=f.path, body=redact(f.body).text) for f in files], ignore=ignore
        )
    # Corpus-level noise removal, once the whole population is known.
    # Proportional, not a fixed count: ">3 repos" never triggers on a corpus
    # where only three repos have diffs, which is how boilerplate survived the
    # first pass. Half the repos that produced signals is the threshold.
    n_repos = len({repo for repo, _ in res.signals})
    cap = max_repos_per_signal if max_repos_per_signal is not None else max(1, n_repos // 2)
    res.signals = drop_common(res.signals, max_repos=cap)
    # Then by change frequency: shared domain vocabulary is a constant between
    # coupled repos, not an event. Only rare shared signals are actionable.
    res.signals = drop_frequent(res.signals)
    return res


@dataclass
class ArmComparison:
    subject: tuple[str, str]
    ticket_hits: set[tuple[str, str]] = field(default_factory=set)
    overlap_hits: set[tuple[str, str]] = field(default_factory=set)

    @property
    def both(self) -> set[tuple[str, str]]:
        return self.ticket_hits & self.overlap_hits

    @property
    def ticket_only(self) -> set[tuple[str, str]]:
        return self.ticket_hits - self.overlap_hits

    @property
    def overlap_only(self) -> set[tuple[str, str]]:
        return self.overlap_hits - self.ticket_hits


def compare_arms(
    collection: Collection, scan: ScanResult, *, min_score: int = 3
) -> list[ArmComparison]:
    """Per change: what each arm found, and where they disagree."""
    out: list[ArmComparison] = []
    by_key: dict[tuple[str, str], Change] = {(c.repo, c.ref): c for c in collection.changes}

    for key, change in by_key.items():
        cmp_ = ArmComparison(subject=key)
        cmp_.ticket_hits = {
            (s.change.repo, s.change.ref)
            for s in find_siblings(change, collection.changes).siblings
        }
        subject_signals = scan.signals.get(key)
        if subject_signals:
            cmp_.overlap_hits = {
                (h.repo, h.ref)
                for h in find_overlaps(key, subject_signals, scan.signals, min_score=min_score)
            }
        out.append(cmp_)
    return out


def overlaps_for(
    subject: tuple[str, str], scan: ScanResult, *, min_score: int = 1
) -> list[OverlapHit]:
    signals = scan.signals.get(subject)
    if not signals:
        return []
    return find_overlaps(subject, signals, scan.signals, min_score=min_score)
