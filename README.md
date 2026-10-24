# Aglium Ledger client

**Your coding agent sees your repository. Ledger sees what your changes might affect elsewhere.**

Claude Code can work on the repository in front of it. The change often reaches further: another
team's branch on the same ticket, a service that reads the table you are changing, a design
decision whose open question this change quietly answers. Ledger reads the rest of your
organisation's repositories on your own machine and answers, for a branch: a recommendation,
the evidence behind it, and the people or decisions to involve.

Source-available · No Aglium account · No model calls by the client · Nothing sent to Aglium

## Would it have warned you? Replay your last merges

The quickest way to find out whether Ledger is useful to you is to run it on changes your team
already merged, as they were on the day each one started.

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

The replay writes `ledger-replay.md`. For each change it keeps three outcomes apart:

- **something to say**: other work or contracts this change affected, each with links to the
  commits and files you can open to check it, and how strong the claim is (a *conflict*: it
  removes something another repository reads, or cannot land with another change in flight; a
  *dependency*: it faces another repository over a named topic or route; or a *correlation*,
  such as a shared ticket);
- **nothing found**, by a replay that could see enough to say so;
- **could not see enough to tell**, which is never the same as safe.

There is no hindsight in it: every other repository is read at the commit its default branch
had on the day the change started, so nothing merged later can make a finding look smarter
than it was. More in [docs/replay.md](docs/replay.md).

You then say, for each finding, whether it was **useful**, **already known**, a **false
positive**, or something you **can't verify**. The answers stay on your machine
(`aglium-ledger feedback import ledger-replay.md`). If you want to share the result,
`aglium-ledger feedback summary --share` writes a file with the verdicts and hashed names, no
code, for you to read and send.

## The recommendation

| Verdict | Meaning |
|---|---|
| `proceed` | Nothing found in what Ledger could see |
| `review_with` | Other work overlaps: name these repositories, branches and people in your review |
| `decision_needed` | An open decision governs this change; a person with authority decides first |
| `insufficient_data` | Ledger could not see enough. Unknown, never safe |

`aglium-ledger brief <repo> <branch>` gives it for any branch; `--json` for a CI gate or an
agent. Every answer names the repositories it read, so a quiet answer from a small estate is
not mistaken for a clean one.

## Commands

| Command | What it does |
|---|---|
| `init` | Write a config from the current checkout |
| `mirrors` | Discover the organisation's repositories and keep bare mirrors of them |
| `coverage` | Which repositories Ledger can see, their history, the gaps, and the prerequisites |
| `replay` | Brief your recent merged changes as they were, to see whether Ledger would have helped |
| `feedback record / import / summary` | Your verdicts on findings, kept locally |
| `brief <repo> <ref>` | What a branch affects elsewhere |
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
- uses: Aglium/ledger-client@v0.1.0
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

## Client and server

The client is complete by itself and free to use. It reads the estate live, on demand, from
what is on your machine. What it does not do, alone:

- see repositories or branches that are not on this machine;
- answer in about 2 seconds (it reads again each time: seconds to tens of seconds);
- read topic wiring declared in infrastructure (ACLs, Terraform, AsyncAPI), only code and config;
- check every field of a contract (it checks the names a change removes, in consumers' code);
- see work merged to an integration branch and not released, or decisions on unmerged branches;
- say anything before code exists, or notice documentation a change leaves wrong;
- share findings and feedback with your team, or learn which verdicts were right.

An organisation server does all of these, for everyone, through the same plugin and commands.
Side by side: [docs/client-vs-server.md](docs/client-vs-server.md). `aglium-ledger server`
says the same from the command line.

## Licence

[PolyForm Shield License 1.0.0](LICENSE): source-available, not open source. Free to use,
change and redistribute, at work too; not to build a product that competes with Ledger. The
licence text is what counts. Copyright (c) 2026 Aglium, [aglium.com](https://aglium.com).
