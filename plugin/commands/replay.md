---
description: Replay your recently merged changes - would Ledger have told you something you did not know?
argument-hint: "[--days N] [--everyone]"
---

# /ledger:replay

Find out whether Ledger would have said anything useful about work that already merged.

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" replay $ARGUMENTS`. It briefs each of
   the user's merged pull requests as it was on the day it started, against every other
   repository as it stood that day, and writes `ledger-replay.md` and `ledger-replay.json`.
   It can take a minute or two on a large estate. If it finds none of the user's changes, it
   says so and suggests `--everyone`; offer that.
2. Read `ledger-replay.md`. Give the user the measured lines of "The result": changes
   replayed, with something to say, nothing found, and could not see enough. Read out every
   **Gap:** line under "What Ledger could see". A change Ledger could not see enough to judge
   is never a clean result; do not count it as one.
3. For each change under "Changes with something to say", give two or three lines: what the
   change was, what Ledger would have told its author, and one piece of its evidence with the
   link, so the user can open it. Use the report's words. Do not open the other repositories
   to check it yourself: the user is the judge here, and the evidence links are for them.
4. Ask the user for a verdict on each finding, all together rather than one at a time:
   - **useful**: they did not know it, and it mattered
   - **already known**: right, but they knew
   - **false positive**: wrong
   - **can't verify**: the evidence was not enough to tell
   Also ask whether any change under "Changes where nothing was found" broke something in
   another repository: that is a **missed**.
5. Record each answer with
   `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" feedback record <verdict> --case <id> --report ledger-replay.md`,
   where the verdict is `useful`, `known`, `false_positive`, `insufficient_evidence` or
   `missed`, and the id is the one in backticks next to that change. Add `--note` with the
   user's reason in their words, if they gave one. It is kept on this machine; nothing is sent.
6. Finish with `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" feedback summary`, and say once
   that `feedback summary --share` writes a file with the verdicts and hashed names, no code,
   that they can send to Aglium if they want to.

Report what the report says. Do not soften a false positive or argue for a finding: the value
of a replay is an honest count, and a replay that finds nothing, with enough coverage, has
still answered the question.
