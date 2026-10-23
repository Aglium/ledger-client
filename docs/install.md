# Installing Ledger

Ledger is a Python command-line client, `aglium-ledger`, and a Claude Code plugin that runs it.
It runs on macOS and Linux. On Windows, use WSL.

## What you need

- Python 3.12 or later. [uv](https://docs.astral.sh/uv/) is the easiest way to get it and the
  client together; the plugin uses `uvx` to fetch the client if it is not installed.
- git, with a credential for your organisation's forge (GitHub or Bitbucket): the one you
  already clone with is enough.
- Read access to the repositories you want Ledger to see.

`aglium-ledger coverage` checks all of this and says what is missing.

## Claude Code

```text
/plugin marketplace add Aglium/ledger-client
/plugin install ledger@aglium
```

Start a new session in one of your organisation's repositories and run `/ledger:setup`. It
checks the prerequisites, shows the config it would write and asks before writing it, offers
to mirror the organisation's repositories, and ends by saying which repositories Ledger can
see. Then `/ledger:replay`.

The first command can take a minute: `uvx` is fetching the client release this plugin pins.

## Command line

```bash
uv tool install 'aglium-ledger[mcp] @ git+https://github.com/Aglium/ledger-client@v0.1.0'
# or: pipx install 'aglium-ledger[mcp] @ git+https://github.com/Aglium/ledger-client@v0.1.0'
```

Then, from a checkout of one of your organisation's repositories:

```bash
aglium-ledger init        # writes ~/.config/ledger/ledger.yml; contacts nothing
aglium-ledger mirrors     # optional: every repository in the organisation, as bare mirrors
aglium-ledger coverage    # what Ledger can see; --forge also names what is missing here
aglium-ledger replay
```

Without mirrors, point Ledger at clones you already have: `local_root: ~/src/your-org` in the
config, or `--local-root` on any command.

## Credentials

| Forge | Listing repositories and commenting | Cloning |
|---|---|---|
| GitHub | `LEDGER_GITHUB_TOKEN`, or git's credential helper for `api.github.com` | git's own credential |
| Bitbucket | the user and app password variables named in the config, or git's credential helper | git's own credential |

Nothing is written into the config. A token needs read access to the repositories; the GitHub
Action's token also needs to write pull request comments.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `aglium-ledger is not installed, and uvx is not here to fetch it` | Install uv, start a new session, or set `LEDGER_BIN` to an `aglium-ledger` executable |
| The MCP tools do not appear in Claude Code | The `mcp` extra is missing: `aglium-ledger coverage` shows it. Reinstall with `[mcp]` |
| `no ledger config found` | Run `aglium-ledger init` inside one of your repositories, or set `LEDGER_CONFIG` |
| `coverage` says `only 1 readable repository` | Ledger needs the other repositories: run `aglium-ledger mirrors`, or set `local_root` |
| `skipping github: LEDGER_GITHUB_TOKEN is unset` | Set the token, or sign in with `gh auth login` so git's helper can answer |
| Replay finds no changes | Your team rebases, or squash-merges without the pull request number in the title. `coverage` says which repositories have no pull requests in the window. Try `--everyone` and a longer `--days` |
| Replay is slow the first time | It reads each repository as it was on several past days. Later runs reuse the scan cache |
| A brief says `proceed` too easily | Run `aglium-ledger coverage`: a quiet answer from a small estate covers little |

Still stuck: open an issue with the output of `aglium-ledger coverage --json`. It contains
repository names and counts, no code; remove anything you would rather not share.
