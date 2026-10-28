# Aglium Ledger client

**Your coding agent did everything right. Your system still broke.**

You gave Claude Code two tasks and it ran them side by side, each in its own session. In the
storefront repository, one session started calling the catalog's price endpoint. In the
catalog repository, the other retired that same endpoint. Both changes were small and clean,
both passed their tests, and both branches were pushed for review. Neither session could see
the other, and nothing linked them: no shared ticket, no shared file, different repositories.
Both were merged, and the storefront started calling an endpoint that no longer existed.

Ledger is a cross-repository change-impact checker for coding agents. Before a change lands,
it checks what the change affects outside the repository it was made in: who reads what it
removes, which other work in flight it cannot land with, which recorded decision it runs into,
and who should look at it.

Source-available · No Aglium account · No model calls by the client · Your code stays on your
machine

**Don't take our word for it:** [replay your last merges](#would-it-have-warned-you) and see
what Ledger would have told you at the time.

## What a finding looks like

Here is that story as Ledger reports it. This is real Ledger output, from the demo estate its
own tests run against (twelve small services of a made-up shop), not from your repositories:

```text
### storefront · `show-live-price`

Merged 2026-10-23, started 2026-10-21, PR #109 · decision_needed (confidence medium)
· other repositories: catalog

Strength: conflict. The evidence shows something another repository uses being removed,
or a decision still open over what it touches.

- cannot both land as they are: catalog `retire-price-endpoint` — it REMOVES route
  `/v1/products/{id}/price`, which this change starts using
- touches `/v1/products/{id}/price` (published by catalog)

Evidence, as of 2026-10-21:
- this change: storefront `show-live-price` (PR #109): <commit link>
- in flight that day: catalog `retire-price-endpoint` by erin@example.com (its diff also
  names `/v1/products/{id}/price`, which this change touches; no ticket links them): <commit link>
- http `/v1/products/{id}/price` is also named in catalog (app/api.py): <file link>
```

In Claude Code the same answer arrives before a push, through the plugin's hook, or whenever
the agent asks. It carries names, contracts, decisions and paths, never your code.

Your tests check whether the change works. Your coding agent checks whether it does the task.
Ledger checks whether it can coexist with everything else changing around it: it does not
review your code, it checks what your code changes outside the repository it lives in.



## Why agents miss this

Coding agents made writing code parallel. They did not make coordinating changes parallel.

Each session sees one repository, one branch and one task, and does it well. The trouble sits
between sessions: a field one change removes and another repository reads, two changes that
cannot both land, a decision made months ago in a document nobody opened. Tests in one
repository cannot see any of it, and neither can a review of one pull request.

None of this is new. Separate teams have always made related changes without knowing it:
payments changes an event, support builds a screen on the old shape of it, a third team
already decided the question both are answering. Agents make it happen faster and more often,
because there are more branches in flight, each locally correct.

## What you get

| Ledger finds | So you |
|---|---|
| Another repository that reads a field or route your change removes | Know which service you are about to break, and where it reads it |
| Other work in flight that touches what yours touches | Find the conflicting change before merge time, not after |
| A recorded decision that governs what you are changing | Don't contradict an architectural decision by accident |
| The people and repositories on the other side | Know who needs to see the change |
| What it could not see | Never mistake missing information for safety |

It says one of four things about a branch, and keeps quiet when there is nothing worth your
attention, so that a warning means something when it appears:

| Verdict | Meaning |
|---|---|
| `proceed` | Nothing found in what Ledger could see, or only items that share a ticket key, a file or a word (listed, never led with) |
| `review_with` | Other work overlaps, and at least one item is strong enough to check first: name these repositories, branches and people in your review |
| `decision_needed` | Don't merge as is: something another repository reads is removed, two changes cannot both land, or an open decision governs this change. Someone has to agree a way forward with the other side first |
| `insufficient_data` | Ledger could not see enough. Unknown, never safe |

Every answer names the repositories it read, so a quiet answer from a small estate is not
mistaken for a clean one. `aglium-ledger brief <repo> <branch>` gives it for any branch;
`--json` for a CI gate or an agent.

## Would it have warned you?

Most tools ask you to trust that they will catch the next problem. Ledger lets you check it
against your own history: it replays changes your team already merged, each as it was on the
day its work started. A replay measures two things, what Ledger would have flagged and how
much of your organisation it could see. If your last 90 days held no cross-repository
trouble, the honest result is a short list, and the report says whether "nothing found" came
from a full view or a partial one.

In Claude Code:

```text
/plugin marketplace add Aglium/ledger-client
/plugin install ledger@aglium
/ledger:setup       # writes a config, checks prerequisites, says which repositories Ledger can see
/ledger:replay      # your merged changes from the last 90 days; --everyone for your team's
```

From a terminal (Python 3.12+ and [uv](https://docs.astral.sh/uv/)):

```bash
uv tool install 'aglium-ledger[mcp]'
cd ~/src/your-org/one-of-your-repos
aglium-ledger init          # a config from this checkout; contacts nothing
aglium-ledger mirrors       # optional: the rest of the organisation, as bare mirrors
aglium-ledger coverage      # which repositories Ledger can see, and the gaps
aglium-ledger replay --everyone
```

There is no hindsight in it. Every other repository is read at the commit its default branch
had on the day the change started, and the other work in flight is rebuilt from merge
history: the pull requests that had started and not yet merged that day, each read as it
stood then. Nothing merged later can make a finding look smarter than it was. The
report, `ledger-replay.md`, keeps three outcomes apart:

- **something to say**, with how strong the claim is (a *conflict*, a *dependency*, or only a
  *correlation* such as a shared ticket) and links to the commits and files to check it;
- **nothing found**, by a replay that could see enough to say so;
- **could not see enough to tell**, which is never the same as safe.

A replay shows what Ledger would have flagged, not whether the warning would have prevented
anything. That part is yours: mark each finding **useful**, **already known**, **false
positive** or **can't verify**, and `aglium-ledger feedback summary` gives you the rates. The
answers stay on your machine. More in [docs/replay.md](docs/replay.md).

## How it works

- **It reads what your machine can read.** Your checkouts, with their local branches, and with
  `aglium-ledger mirrors` bare mirrors of every repository your git credentials can see,
  including other teams' pushed branches. So parallel sessions on your machine are visible to
  each other, and work on someone else's machine is visible once it is pushed. It says which
  repositories it could not read.
- **It links changes by what they touch.** Topics, routes, tables and the names two diffs
  share; a ticket key counts only when the diffs back it up. It reads the call sites around
  a contract to tell a producer from a consumer, and the fields a consumer reads.
- **It reads your decisions where you keep them.** ADRs, decision records and TODO files in
  your repositories, with their open questions, linked to a change by the contracts and files
  they name.
- **It is cautious by design.** A weak link is listed, not led with. A gap in what it could see
  is reported as `insufficient_data`, never as `proceed`.
- **It runs locally.** No account, no model calls, nothing sent to Aglium. Details:
  [docs/trust.md](docs/trust.md).

## Commands

| Command | What it does |
|---|---|
| `init` | Write a config from the current checkout |
| `mirrors` | Discover the organisation's repositories and keep bare mirrors of them |
| `coverage` | Which repositories Ledger can see, their history, the gaps, and the prerequisites |
| `replay` | Brief your recent merged changes as they were, to see whether Ledger would have helped |
| `feedback record / import / summary` | Your verdicts on findings, kept locally |
| `brief <repo> <ref>` | What a branch affects elsewhere (`--base` for a PR's target, `--worktree` for uncommitted edits) |
| `ask inflight\|owners\|decisions` | The MCP tools' questions, as JSON, for a skill that calls the CLI |
| `explain <repo> <ref>` | Other in-flight work on a branch's ticket |
| `scan` | What the estate holds: repositories, contracts, work in flight |
| `comment` | Post a sibling comment on a pull request (used by the GitHub Action) |
| `mcp` | Run the MCP server for coding agents |
| `hook` | The push-time hook for Claude Code |
| `server` | What an organisation server adds, and how to connect to one |

## Use it from a coding agent

- **Claude Code:** the plugin in [`plugin/`](plugin/README.md) adds the skill, the MCP server,
  a push-time hook and `/ledger:setup`, `/ledger:coverage`, `/ledger:brief`, `/ledger:replay`.
- **Any MCP client:** run `aglium-ledger mcp` as a stdio server. Tools: `brief`, `who_owns`,
  `inflight_for`, `decisions_for`, `feedback`.

Installation on a clean machine, permissions and troubleshooting:
[docs/install.md](docs/install.md).

## On a pull request

The GitHub Action in this repository runs the client statelessly on each pull request and
comments with the full brief: a field or contract the change removes that another repository
reads, another change in flight that cannot land with it, who is on the other side of each
topic or route, and other work linked by ticket, by what it touches or by author. A branch
with no ticket key is briefed like any other. It is advisory and never fails a build; set
`mode: siblings` for the old ticket-only comment.

```yaml
- uses: Aglium/ledger-client@v0.4.0
  continue-on-error: true
  with:
    github-token: ${{ secrets.LEDGER_TOKEN }}
    config: .github/ledger.yml
```

## Trust and data

The client runs on your machine. It reads your repositories with the credential git already
has, sends nothing to Aglium, makes no model calls, and puts no file contents in its answers:
they carry names, contracts, decisions and paths. Exactly what it reads, contacts and stores
is in [docs/trust.md](docs/trust.md).

## For you, or for your whole team

**The client** is the whole check, for one person, free: it answers about the repositories
on your machine, reads them fresh each time (seconds to tens of seconds), and lets you test
the claim on your own history with a replay. No account, no server.

**An organisation server** makes the same answers continuous and shared, for everyone,
through the same plugin and commands:

- every repository and branch in the organisation, indexed and kept current, so answers come
  in seconds instead of a fresh read;
- work merged to an integration branch and not yet released, and decisions still being written
  on unmerged branches;
- topic wiring declared in infrastructure (ACLs, Terraform, AsyncAPI), the fields of each
  contract extracted from both sides, and the documentation pages that describe what a change
  touches;
- context for a task from its description, before any code is written;
- findings and feedback shared across the team, so everyone learns which warnings were right.

Side by side: [docs/client-vs-server.md](docs/client-vs-server.md). `aglium-ledger server`
says the same from the command line.

## Licence

[PolyForm Shield License 1.0.0](LICENSE): source-available, not open source. Free to use,
change and redistribute, at work too; not to build a product that competes with Ledger. The
licence text is what counts. Copyright (c) 2026 Aglium, [aglium.com](https://aglium.com).
