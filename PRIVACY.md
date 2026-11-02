# Privacy policy: Ledger client and Claude Code plugin

Effective 2 November 2026. Applies to the `aglium-ledger` client and the Ledger plugin for
Claude Code, both published by Aglium from this repository. It does not cover Aglium's
organisation server (`ledger-server`), which is a separate product with its own terms.

## The short version

Aglium collects nothing from you through the client or the plugin. There is no account, no
telemetry, no analytics and no call to any Aglium service. Everything Ledger reads and writes
stays on your machine, apart from the requests to your own services listed below.

## What the client processes, on your machine

To answer "what does this change affect elsewhere?", the client reads:

- the repository you are working in, and its git history;
- your organisation's other repositories, from clones you already have or from bare mirrors
  it creates only when you agree to it during setup or run `aglium-ledger mirrors`;
- your forge credential (`LEDGER_GITHUB_TOKEN`, the Bitbucket variables, or the one git's
  credential helper already holds for that host), used only to talk to that forge.

Its answers to Claude carry derived facts (repository, branch and contract names, decision
titles, file paths, commit ids), never file contents.

## Where data goes

| Destination | What is sent | When |
|---|---|---|
| Your forge's API (`api.github.com`, `api.bitbucket.org`) | Requests to list your organisation's repositories, and pull request comments you ask it to post | Setup, `mirrors`, `coverage --forge`, `comment --post` |
| Your forge over git | Standard git fetches | Cloning and updating mirrors |
| Your organisation's Ledger server | The repository, branch and question, and your local change when the server cannot see it | Only when you set `LEDGER_URL` |
| The Python package index (PyPI) | A standard package download | The first time the plugin fetches the client |

Nothing is sent to Aglium. The client makes no language-model calls. The one exception is
`aglium-ledger bench --judge`, an evaluation command you run by hand, which sends text with
personal data removed to your own Claude Code CLI.

## What is stored, and for how long

Ledger stores a config file, a scan cache, optional bare mirrors, your own feedback verdicts
and any replay report you generate, all in your user directories (see
[docs/trust.md](docs/trust.md) for exact paths). They stay until you delete them; the client
rebuilds what it needs. Aglium holds no copy.

## Children

Ledger is a developer tool and is not directed at anyone under 18.

## Changes

Changes to this policy are made in this file, and its history is the record. A change in what
the client contacts or stores is listed in the release notes.

## Contact

Questions and support: [GitHub Issues](https://github.com/Aglium/ledger-client/issues).
Security reports: privately, through this repository's Security tab ("Report a vulnerability").
Aglium: [aglium.com](https://aglium.com).
