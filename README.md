# aglium-ledger

What does this change affect in the repositories you are not looking at, and does someone need
to decide something before it merges?

`aglium-ledger` is the Ledger client. It reads your organisation's repositories on your own
machine and answers that question for a branch: a recommendation, the reasons, and who to
involve. It needs no database and no service. It returns names, contracts, decisions and paths
as evidence, never file contents.

## Install

Python 3.12 or later.

```bash
uv tool install 'aglium-ledger[mcp]'      # or: pipx install 'aglium-ledger[mcp]'
```

The `mcp` extra is only needed for the MCP server (`aglium-ledger mcp`).

## Start

From a checkout of one of your organisation's repositories:

```bash
aglium-ledger init        # writes ~/.config/ledger/ledger.yml from this checkout; contacts nothing
aglium-ledger mirrors     # optional: the rest of the organisation, as bare mirrors
aglium-ledger brief <repo> <branch>
```

`init` reads the origin remote for the forge and the organisation, branch names and commit
subjects for the ticket keys your team uses, and the directory the checkout sits in. What it
cannot derive it leaves out and says so. Without mirrors, point it at clones you already have
with `--local-root ~/src/your-org`.

## The recommendation

| Verdict | Meaning |
|---|---|
| `proceed` | Nothing found elsewhere |
| `review_with` | Other work overlaps: name these repositories, branches and people in your review |
| `decision_needed` | An open decision governs this change; a person with authority decides first |
| `insufficient_data` | Ledger could not see enough. Unknown, never safe |

`brief --json` gives the same answer for a CI gate or an agent.

## Commands

| Command | What it does |
|---|---|
| `init` | Write a config from the current checkout |
| `mirrors` | Discover the organisation's repositories and keep bare mirrors of them |
| `brief <repo> <ref>` | What a branch affects elsewhere |
| `explain <repo> <ref>` | The evidence behind a brief |
| `scan` | What the estate holds: repositories, contracts, work in flight |
| `replay` | Brief your recent merged changes as they were, to see whether Ledger would have helped |
| `comment` | Post a brief on a pull request (used by the GitHub Action) |
| `mcp` | Run the MCP server for coding agents |
| `hook` | The push-time hook for Claude Code |
| `server` | What an organisation server adds, and how to connect to one |

## Use it from a coding agent

- **Claude Code:** `/plugin install ledger --marketplace Aglium/ledger-client`. The plugin
  adds the skill, the MCP server, a push-time hook and `/ledger:setup`, `/ledger:brief`,
  `/ledger:replay`. Its own README is in [`plugin/`](plugin/README.md).
- **Any MCP client:** run `aglium-ledger mcp` as a stdio server. Tools: `brief`, `who_owns`,
  `inflight_for`, `decisions_for`, `feedback`.

## On a pull request

The GitHub Action in this repository runs the client statelessly on each pull request and
comments when other work shares the change's ticket. It is advisory and never fails a build.

```yaml
- uses: Aglium/ledger-client@v0.1.0
  continue-on-error: true
  with:
    github-token: ${{ secrets.LEDGER_TOKEN }}
    config: .github/ledger.yml
```

## What it contacts

| What | When |
|---|---|
| Your forge's API (`api.github.com`, `api.bitbucket.org`) | Listing the organisation's repositories, and pull request comments. Token from `LEDGER_GITHUB_TOKEN` / the Bitbucket variables, or from git's credential helper |
| Your forge over git | Cloning and fetching mirrors, with the credential git already has |
| Your organisation's Ledger server | Only when `LEDGER_URL` is set |

It sends nothing to Aglium, makes no model calls, and sends no source code anywhere.

## With an organisation server

Alone, the client reads the estate live: slower, and with no memory between calls. An
organisation server keeps a stored index for everyone: faster briefs, context before any code
is written, producers and consumers of each contract, decisions on unmerged branches. Set
`LEDGER_URL` and `LEDGER_API_TOKEN` and the client asks it instead. The server is a separate
commercial product; `aglium-ledger server` says what it adds, and
[the Ledger page](https://aglium.com/platform/ledger-server.html) says how to get one.

## Licence

[PolyForm Shield License 1.0.0](LICENSE), source-available, unmodified. Free to use, change and
redistribute, at work too; not to build a product that competes with Ledger. The licence text
is what counts. Copyright (c) 2026 Aglium, [aglium.com](https://aglium.com).
