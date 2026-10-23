---
description: Set Ledger up for this repository's organisation, check what it can see, and get to a first replay
---

# /ledger:setup

Set Ledger up on this machine, starting from the repository this session is in, and end with
the user knowing exactly what Ledger can see.

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" coverage`. Its PREREQUISITES block
   says whether Python, git and the MCP extra are in place. If the runner says the client is
   not installed, or a required check shows `!!`, tell the user what it printed, and the fix
   it names, and stop. The first run can take a minute while `uvx` fetches the client.
2. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" init --dry-run` and show the user the
   config it would write: the organisation, the forge, the ticket keys it found, and where the
   estate is. Read out any `note:` lines; they say what could not be derived.
3. Ask whether the ticket keys and the organisation look right. If the user corrects
   something, say that they can edit the file after it is written.
4. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" init` to write it. If a config already
   exists, say so and continue with it unless the user asks to replace it (`--force`).
5. If the config has a `mirror_root`, explain that `aglium-ledger mirrors` clones every
   repository in the organisation as a bare mirror so Ledger can see repositories that are not
   checked out here, that it can take a while and some disk, and that it uses the credential
   git already has. Run it only if the user says yes.
6. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" coverage --forge` and tell the user, in
   a few lines: how many repositories Ledger can read, the gaps it lists in its own words
   (repositories in the organisation that are not on this machine, stale mirrors, repositories
   without pull requests), and its last line. `--forge` lists the organisation on its forge;
   if that fails for lack of a credential, run it without `--forge` and say what that leaves out.
7. If the last line says `ready to replay`, offer `/ledger:replay`: it checks the user's own
   recently merged changes and is the quickest way to see whether Ledger is useful here.
   Otherwise say what to fix first, from the gaps.

Never describe a gap as harmless. A repository Ledger cannot see is one it cannot warn about.
