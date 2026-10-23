---
description: Set Ledger up for this repository's organisation, from this checkout
---

# /ledger:setup

Set Ledger up on this machine, starting from the repository this session is in.

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" init --dry-run` and show the user the
   config it would write: the organisation, the forge, the ticket keys it found, and where the
   estate is. Read out any `note:` lines; they say what could not be derived.
2. Ask whether the ticket keys and the organisation look right. If the user corrects
   something, say that they can edit the file after it is written.
3. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" init` to write it. If a config already
   exists, say so and stop unless the user asks to replace it (`--force`).
4. If the config has a `mirror_root`, explain that `aglium-ledger mirrors` clones every repository in
   the organisation as a bare mirror so briefs can see repositories that are not checked out
   here, that it can take a while and some disk, and that it uses the credential git already
   has. Run it only if the user says yes.
5. Finish by running a brief for the current branch, as `/ledger:brief` does, so the first
   answer arrives now.

If the runner reports that ledger is not installed, tell the user what it printed and stop.
