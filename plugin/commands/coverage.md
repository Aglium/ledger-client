---
description: Which repositories Ledger can see from here, and what it cannot
argument-hint: "[--forge] [--days N]"
---

# /ledger:coverage

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/ledger-run.sh" coverage $ARGUMENTS`.
2. Tell the user how many repositories Ledger read, and list the GAPS block in its own words.
3. End with its last line: ready to replay, or what is missing.

Use this whenever an answer from Ledger looks too quiet. `proceed` from an estate of one
repository means "nothing found in what I could see", and that is not the same as safe.
