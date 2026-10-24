# Ledger for Claude Code

Before you push: what this change affects in the repositories you are not looking at, and
whether someone needs to decide something first.

A coding session sees one repository. The change often reaches further: another team's branch
on the same ticket, a service that reads the field you are removing, a design decision whose
open question this change quietly answers. Ledger reads the rest of your organisation's
repositories and gives the session a short brief: a recommendation, the reasons, and who to
involve. It returns names, contracts, decisions and paths as evidence, never file contents.

## What it adds

| Component | What it does |
|---|---|
| `ledger` skill | Tells Claude when to ask Ledger: before reviewing, planning or pushing a branch, when a ticket key comes up, before changing a shared topic, endpoint or table |
| `ledger` MCP server | Local tools: `brief`, `who_owns`, `inflight_for`, `decisions_for`, `feedback` (kept on your machine) |
| Push hook | On a `git push`, `gh pr create`, `glab mr create` or new branch, adds a brief to the session. Silent when there is nothing to say; never blocks the command, even if Ledger fails |
| `/ledger:setup` | Checks prerequisites, writes a config for this repository's organisation, optionally mirrors its repositories, and says which ones Ledger can see |
| `/ledger:coverage` | Which repositories Ledger can see from here, and the gaps |
| `/ledger:brief` | What the current branch affects in other repositories |
| `/ledger:replay` | Replays your recent merged changes as they were, with no hindsight: would Ledger have told you something you did not know? Then asks for your verdict on each finding |

The recommendation is one of `proceed`, `review_with` (name these repositories and people in
your review), `decision_needed` (a person must decide first) or `insufficient_data` (unknown,
never safe).

## Requirements

- Python 3.12 or later, and either the `aglium-ledger` client or [uv](https://docs.astral.sh/uv/).
- git, with a credential for your organisation's forge (GitHub or Bitbucket).

The plugin runs the client in this order: `LEDGER_BIN` if set, then `aglium-ledger` on your
PATH, then `uvx` to fetch the client release this plugin pins, with its MCP extra. To install
it yourself:

```bash
uv tool install 'aglium-ledger[mcp] @ git+https://github.com/Aglium/ledger-client@v0.1.0'
```

## Install

In Claude Code 2.1.275 or later:

```text
/plugin install ledger --marketplace Aglium/ledger-client
```

Or add the marketplace first with `/plugin marketplace add Aglium/ledger-client`, then run
`/plugin install ledger@aglium`.

## Set up

1. Install the plugin, then open Claude Code in one of your organisation's repositories.
2. Run `/ledger:setup`. It shows the config it would write (organisation, forge, ticket keys)
   and asks before writing it.
3. Optionally let it mirror the organisation's repositories, so briefs can see repositories
   you have not cloned. This can take a while and some disk.
4. Run `/ledger:replay` to see what Ledger would have said about your recently merged changes,
   and `/ledger:brief` on your current branch.

## What it runs, reads and sends

Ledger runs on your machine. Everything below is listed so you can check it.

| What | When | Where it goes |
|---|---|---|
| Runs the `aglium-ledger` client (a Python package) | Every hook, tool and command | Local. Fetched by `uvx` from this repository's tagged release (and its dependencies from the Python package index) if not installed |
| Reads your checkout and git history | Briefs | Local |
| Clones your organisation's repositories as bare mirrors | Only if you agree during setup, or run `aglium-ledger mirrors` | From your forge to `~/.ledger/mirrors`, using the credential git already has |
| Lists your organisation's repositories | Setup and mirroring | Your forge's API: `api.github.com` or `api.bitbucket.org`. The token comes from `LEDGER_GITHUB_TOKEN` / the Bitbucket variables, or from git's own credential helper (`git credential fill`) |
| Writes a config and a scan cache | Setup, briefs | Your user config and cache directories (`LEDGER_CONFIG`, `LEDGER_CACHE_DIR` override them) |
| Keeps your verdicts on findings | When you give one, in a replay or to the `feedback` tool | A file on your machine (`~/.local/share/ledger/feedback.jsonl`). Sent nowhere |
| Asks your organisation's Ledger server | Only when `LEDGER_URL` is set | That server, with `LEDGER_API_TOKEN`. It receives the repository, branch and question, and returns derived facts |

The client sends nothing to Aglium, makes no model calls, and sends no source code anywhere.

## Licensing

The Ledger client and this plugin are licensed under the
[PolyForm Shield License 1.0.0](LICENSE), a source-available licence, unmodified.

- You may use, copy, change and redistribute them at no charge, for any purpose, including
  at work inside a company.
- You may not use them to provide a product that competes with Ledger or with anything
  Aglium provides using it.
- Copies must carry the licence and the copyright notice.
- They come as is, without warranty.

The licence text in [LICENSE](LICENSE) is what counts. The organisation server
(`ledger-server`) is a separate commercial product and is not part of this plugin. The client
talks to one only when you point it at one with `LEDGER_URL`. Alone, the client sees only the
repositories on your machine, reads them again for each answer, and keeps nothing shared; what
it does not do, and what a server adds, is in
[docs/client-vs-server.md](../docs/client-vs-server.md) and `aglium-ledger server`.

Copyright (c) 2026 Aglium. Questions: [aglium.com](https://aglium.com).
