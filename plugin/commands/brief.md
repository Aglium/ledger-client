---
description: What the current branch affects in other repositories, and what to do about it
argument-hint: "[repo ref]"
---

# /ledger:brief

Report what this change affects beyond this repository.

1. Work out `repo` and `ref`. With arguments, use them. Without, take the repository name
   from `git remote get-url origin` (the last path segment, without `.git`) and the branch
   from `git rev-parse --abbrev-ref HEAD`. If a pull request is open or about to be, its target
   branch is the `base` (`gh pr view --json baseRefName` when `gh` is available); otherwise
   leave it out.
2. Call the `brief` tool of the `ledger` MCP server with them, `worktree: true`, and `base`
   when known. If the tool is unavailable, run
   `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" brief <repo> <ref> --json --worktree [--base <target>]`.
3. Report, in a few lines: the recommendation, the reasons, and who to involve.
   - `decision_needed`: a person with authority must decide first. Name the deciders.
   - `review_with`: name the other repositories and authors.
   - `insufficient_data`: say it is unknown. Never present it as safe.
   - `proceed`: one line.
4. If the answer says it was read live, without an organisation index, say once what that
   leaves out, in the answer's own words, and that `aglium-ledger server` explains what an
   organisation server adds and where to get one. Say it once per session, not per brief.

Do not read other repositories to check the answer. The brief is the derived fact; if it
looks wrong, say so and record it with the `feedback` tool.
