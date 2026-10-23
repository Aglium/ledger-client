---
description: Replay your recent merged changes - would a brief have told you something you did not know?
argument-hint: "[--days N] [--everyone]"
---

# /ledger:replay

Find out whether Ledger would have said anything useful about work that already merged.

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" replay $ARGUMENTS`. It briefs each of
   the user's merged changes as it was on the day it started and writes `ledger-replay.md`.
   It can take a minute on a large estate.
2. Read `ledger-replay.md`. Tell the user the two measured numbers: how many changes were
   replayed, and how many had cross-repository context.
3. For each change under "Changes with something to say", give one or two lines: what the
   change was, and what the brief would have told its author. Use the report's words.
4. Ask the report's question about each one, together rather than one at a time: **would
   knowing this have changed what you did?** The answers are: prevented a defect, avoided
   rework, sped up review, useful but no change, noise.
5. Tick the matching box in `ledger-replay.md` for each answer the user gives. Also ask
   whether any change under "Changes it would have stayed silent on" broke something in
   another repository, and tick `missed` for those.
6. Fill in the three blank lines of the funnel from the answers.

Report what the report says. Do not soften a `noise` answer or argue for a finding: the
value of a replay is an honest count, and a replay that finds nothing has still answered the
question.

If no changes were found, say why in the report's own words: merge commits are what a replay
reads, and squash-merged changes leave none.
