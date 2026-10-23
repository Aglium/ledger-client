---
name: ledger
description: Cross-repo impact check. Use before reviewing, planning or pushing a change on a branch, before changing a shared topic, HTTP endpoint or table, or when a ticket key (e.g. ABC-123) comes up — to learn which other repos and in-flight branches it affects and whether a decision is needed first.
---

# Ledger: what does this change affect beyond this repo?

You can see one repository. The work you are doing often touches others: another team's
branch on the same ticket, a service that reads the field you are removing, an ADR whose
open question this change quietly answers. The `ledger` MCP server sees the whole estate,
including work that is in flight on other branches and integration branches like `stage`.

## When invoked as `/ledger`

Check the branch this session is on: take `repo` and `ref` as described below, call
`brief(repo, ref)`, and report the recommendation, the reasons, and who to involve, in a few
lines. If an argument is given (a ticket key, a contract, or `repo ref`), answer that instead.

## Feedback — record it when it is given

The ledger's usefulness is being measured. When the user reacts to anything the ledger said —
at session start, in a brief, anywhere — record it with `feedback`:

| The user says | verdict |
|---|---|
| "useful", "good catch", "didn't know that" | `useful` |
| "irrelevant", "noise", "not related" | `noise` |
| "that's not true", "that's merged already" | `wrong` |
| "it should have told me about X" | `missed` (put X in `note`) |

Pass the `event_id` from the answer — every answer carries one. If you don't have it, pass the `repo` and the `tool` that gave the answer (`brief`, `who_owns`, ...), so the verdict lands on the right answer. Do not ask for feedback unprompted —
one sentence from the user, recorded, is the whole mechanism.

## Reading `who_owns`

Each repo comes with `uses` — `publishes`, `consumes`, `declares` (only names the contract, e.g.
in config or a shared library) or `mentions` — plus evidence paths and the branches it is on.
`declares` is not a consumer. An empty `repos` list means **not found**, never "unused": read the
`notes` before concluding anything is safe to change.

## When to call it

| Situation | Tool |
|---|---|
| Reviewing, planning or about to push a branch | `brief(repo, ref)` |
| A ticket key is mentioned | `inflight_for(ticket)` |
| About to change or remove a topic, endpoint, table, or a field in one | `who_owns(surface, name)` |
| About to contradict or rework a design | `decisions_for(ticket=… / contract=… / repo=…)` |

`repo` is the repository's name **where it lives**, from its origin remote — not the directory
it sits in, which is whatever the person who cloned it typed:

```bash
basename -s .git "$(git remote get-url origin)"   # falls back to the directory with no remote
git rev-parse --abbrev-ref HEAD                   # ref
```

## Reading the recommendation

- **proceed** — nothing found elsewhere. Carry on.
- **review_with** — other work overlaps. Name the repos, branches and people from `impacts`
  and `actors` in your review or PR description, and check the shared `contracts` for
  compatibility before changing them.
  Each item in `impacts` says what the other change does (`effect`), which way it faces
  yours and over which contract (`relation`), how far to trust that (`evidence`) and what
  to ask its author (`ask`). You have not read that change: do not cite its branch name or
  a commit title as proof of what it does. Reason from `effect` and `relation`, and where
  they are not enough, put the `ask` to the person named rather than guessing.
  An item may also carry `behaviour`: one or two sentences a model wrote from that change's
  diff, with a `confidence` and whether the diff was cut (`partial`). It is a lead, not a
  fact. Use it to decide what to check and whom to ask; do not report it as a finding, and
  say it is a model summary if you repeat it.
- **decision_needed** — an open decision governs this change. Do not resolve it yourself.
  Tell the user which decision (`decisions[].path`), what is open (`gate_hit`), and who
  decides (`deciders`). Continue only if the user says so.
- **insufficient_data** — the ledger could not see enough. That means *unknown*, never
  *safe*. Say so, and say what was missing (`index.missing_sources`, `unreadable_repos`).

Always report `confidence` and never upgrade it. If `index.source` is `mixed`, some repos
were read live because they moved since the last sync — the answer is still current.

**Check the age.** Who else is working on something comes from the last sync, and every answer
carries when that was (`index.synced_at`). If it says STALE, "nothing in flight" is *unknown*,
not false — say so rather than reporting a clean result.

Contracts carry `documented_in`: the pages that document them, each with its own last-updated
date and how many commits have changed the contract since. A change to a documented contract
leaves that page wrong unless somebody updates it; name the page.

## Without the MCP server

The same brief is available from the command line:

```bash
aglium-ledger brief <repo> <ref> --json
```

## What it will not tell you

It returns names, contracts, decisions and paths as evidence — never file contents. To see
the code in another repo, open that repo. It links by ticket keys in branch names and PR
titles, and by contract names found in code; work with neither is invisible to it.
