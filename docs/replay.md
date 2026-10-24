# Replay: would Ledger have warned you?

`aglium-ledger replay` (or `/ledger:replay` in Claude Code) takes pull requests that already
merged in your repositories and briefs each one as it was on the day its work started. The
question it answers: *would Ledger have told the author something they did not know, about
another repository?*

## What it reads

- **Which changes.** Pull requests merged into each repository's default branch in the window
  (`--days`, default 90): GitHub and Bitbucket merge commits, and squash merges whose title ends
  in the pull request number, like `Fix totals (#123)`. Your own by default (by git
  `user.email`); `--everyone` for the whole team; `--author` for someone else.
- **The change itself.** Its own diff, against the commit it was merged onto.
- **Everything else, as it was.** Every other repository at the last commit its default branch
  had on the day the change started. A contract another team started using later, or a
  decision written later, is not used. Repositories that did not exist yet are left out.
- **Work in flight that day.** Reconstructed from merge history: the other pull requests whose
  work had started and not yet merged on that day.

## What the report says

`ledger-replay.md` opens with measured counts, then:

1. **What Ledger could see**: the repositories it read and every gap it found (only one
   repository visible, unreadable history, repositories with commits but no pull requests,
   repositories in the organisation that are not on this machine).
2. **Changes with something to say**, each with its recommendation, its reasons, a strength
   label, and evidence links to the commits and files at the commit they were read from.
3. **Changes Ledger could not see enough to judge.** Not a clean result.
4. **Changes where nothing was found**, each checked against a stated number of other
   repositories. If one of them broke something elsewhere, mark it `missed`.

`ledger-replay.json`, beside it, lists the same cases by id, with no code, for `feedback`.

### Strength

| Label | What the evidence shows |
|---|---|
| conflict | The change removes a field or contract another repository reads, another change in flight that day removes what it uses (or uses what it removes), or an open decision governs what it touches |
| dependency | The change and another repository, or another change in flight, face each other over a named topic or route, in a known direction |
| correlation | A shared ticket, or the same name in both places. Worth a look, not yet evidence of a conflict |

Without an organisation server, directions come from call sites in the code and the fields
another repository reads are looked up by name, literally: a field read through a generated
client or another module is not seen. The label is there so nobody mistakes one kind of
finding for another.

### How in-flight work is linked

Each change in flight that day is read as it stood that day, and linked to the change being
replayed by, strongest first: a ticket key in its branch name, title or diff; a topic or route
both diffs name; a topic, table or migration both touch that few other changes do. The same
author starting work in another repository within two hours is listed as a question and never
changes a verdict on its own.

## Grading the findings

Tick one box per finding, then `aglium-ledger feedback import ledger-replay.md`:

| Verdict | Means |
|---|---|
| useful | You did not know it, and it mattered for the change or its review |
| already known | Correct, and the author or reviewer already knew |
| false positive | Wrong: the link or the claim does not hold |
| can't verify | The evidence given was not enough to tell either way |
| missed | Nothing was said, and the change did affect another repository |

For a fair grade, have someone who knows the change grade it without being told what Ledger
expected. `aglium-ledger feedback summary` prints the useful, false-positive and
can't-verify rates. Answers stay on this machine.

## Limits

- Rebased pull requests leave no trace in history and are not replayed.
- A squash merge keeps no branch, so its start day is taken as the day it merged.
- Two changes that share nothing a diff names (a rating written in one service and read from
  a table by another under a different name) are not linked without a ticket key.
- It is an upper bound: it counts how often there was something to say. Only the people who
  did the work can say whether it mattered.
