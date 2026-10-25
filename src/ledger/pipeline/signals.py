"""What each open change touches, so two changes can be linked by substance.

The ticket join answers "who else is working on this ticket". This answers the
question that is left when nobody typed a ticket key, which on the estate this
was written against is a quarter of live branches: *what is this change about,
and is anyone else's change about the same thing?*

The matching itself is `core.overlap`, which has been built and tested since
the control arm was specified and was reachable only from a CLI command. This
module persists its output per change so a brief can join on it, and does
nothing clever of its own.

Two measurements decided the shape, over 192 live branches of a 179-repository
estate:

**Which kinds are worth storing.** `extract_signals` produced 536,820 signals,
434,813 of them `identifier` -- every name used anywhere in a diff. Storing
that is not an index, it is the diff again. Topics, tables and migrations are
6,620, about thirty-five per change, and they are exactly the kinds
`core.overlap` exempts from its own frequency filters because sharing one
means something.

**What makes a shared signal worth reporting.** Nothing: 2,815 of 15,368
cross-repo pairs shared at least one, 18% of the estate, because two coupled
repositories share their whole event vocabulary. `drop_frequent` says why in
its own docstring -- "what is worth an alert is a RARE shared signal:
`invoice_agreements_signature` in two changes, not `Invoice` in thirty-four" --
and that is the rule here. Requiring the shared signal to be rare takes 2,815
pairs to 64, and what survives is three communication repositories sharing
`schedule.followup`, `pilot.start` and `inbound.message`.
"""

from __future__ import annotations

from ledger.core.overlap import DiffFile, Kind, extract_signals

#: Bump when the answer would change: the kinds stored, the extraction in
#: `core.overlap`, or the diff a change is read against. Re-reading the estate
#: costs about twenty seconds of local git and no model calls, so this is cheap
#: to bump -- unlike the field layer, which is why they are separate versions.
SIGNALS_VERSION = "s2"   # filenames and Helm keys are no longer topics; prose is no longer a table

#: Topics, tables and migrations. See the module docstring for the 536,820 to
#: 6,620 that chose this.
STORED_KINDS = (Kind.EVENT, Kind.TABLE, Kind.MIGRATION)

#: Which refs are somebody's change is configurable now: `branches` in the
#: config, defaults in ledger.core.branches.DEFAULT_IGNORE (release trains,
#: long-lived lines, dependency bots).

#: Diff bytes read per change. The p90 branch is 1.8 MB and the largest 11.6;
#: past this the change is a vendored drop or a generated-file sweep, and its
#: signals say more about its build than about its subject.
DIFF_LIMIT = 2_000_000


def signals_of(files, *, limit: int = DIFF_LIMIT) -> set:
    """The storable signals of one change's diff.

    Separate from `sync_signals` so a brief can compute the subject's own
    signals from the diff it has already read, rather than depending on the
    subject being indexed -- a branch created since the last sync is exactly
    when somebody asks about it.
    """
    budget = limit
    diffs: list[DiffFile] = []
    for f in files:
        body = f.body or ""
        if budget <= 0:
            break
        diffs.append(DiffFile(path=f.path, body=body[:budget]))
        budget -= len(body)
    return {s for s in extract_signals(diffs) if s.kind in STORED_KINDS}
