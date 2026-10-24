# Trust, security and data handling

The Ledger client runs on your machine, on demand. This page lists everything it reads,
contacts, stores and runs, so you can check it before you install it.

## In one paragraph

The client reads your organisation's repositories with git and with your forge's API, using
credentials you already have. It sends nothing to Aglium, has no telemetry, and makes no calls
to any language model. Its answers carry derived facts (repository and branch names, contract
names, decision titles, file paths, commit ids), never file contents, so an agent given its
tools is not given your code through them.

## What it contacts

| Destination | When | With what |
|---|---|---|
| `api.github.com` or `api.bitbucket.org` | Listing your organisation's repositories (`mirrors`, `coverage --forge`) and posting pull request comments (`comment --post`) | `LEDGER_GITHUB_TOKEN` or the Bitbucket variables, or the credential git's helper already holds |
| Your forge over git | Cloning and fetching mirrors (`mirrors`) | The credential git already uses |
| Your organisation's Ledger server | Only when `LEDGER_URL` is set | `LEDGER_API_TOKEN`. It receives the repository, branch and question, and your local change when the server cannot see it |
| The Python package index | When the plugin fetches the client with `uvx`, the first time | Nothing of yours |

Nothing else. In particular: no Aglium endpoint, no analytics, no model provider.

## What it stores, and where

| What | Where | Contents |
|---|---|---|
| Config | `~/.config/ledger/ledger.yml` (`LEDGER_CONFIG`) | Organisation, forge, ticket keys, paths. No secrets |
| Mirrors | `~/.ledger/mirrors` (configurable), only if you run `mirrors` | Bare git clones of your organisation's repositories |
| Scan cache | `~/.cache/ledger/scan` (`LEDGER_CACHE_DIR`, `off` to disable) | Contract names and decision metadata per repository and commit |
| Feedback | `~/.local/share/ledger/feedback.jsonl` (`LEDGER_DATA_DIR`) | Your verdicts, the names that place them, your notes |
| Replay report | `ledger-replay.md` and `.json`, where you run it | Names, links, verdicts. No code |

Delete any of them at any time; the client rebuilds what it needs.

## Feedback is yours

Ledger never collects feedback by itself. `feedback record` and `feedback import` write to the
local file above. `feedback summary --share` writes a file you can read and then send if you
choose: verdict counts and rates, and each answer with repository and branch names replaced by
a salted hash. Notes are left out unless you pass `--with-notes`. The salt stays on your machine.

## Permissions it needs

- Read access to the repositories you want it to see. It never pushes, and never writes to a
  repository.
- For the GitHub Action, a token that can read the organisation's repositories and write pull
  request comments. The Action is advisory: it never fails a build.
- In Claude Code, the plugin adds one hook, before a Bash command, that runs only for
  `git push`, `gh pr create`, `glab mr create` and new branches. It never blocks the command,
  even when Ledger fails.

## Source and licence

The full source of the client and the plugin is in this repository, under the
[PolyForm Shield License 1.0.0](../LICENSE): source-available, free to use and change, not to
build a competing product. Report a security problem privately through this repository's
security advisories (Security, then "Report a vulnerability"), not in a public issue.
