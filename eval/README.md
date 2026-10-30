# Findings eval

`cases.jsonl` holds 102 briefs that people and agent sessions judged on a real
estate (`useful`, `noise`, `wrong`, `missed`), with the relevance model's 0-3
answers stored, so a run asks no model and takes seconds. Every name in them
-- repository, branch, topic, table, path, author, ticket key and number,
date -- is a keyed pseudonym of the same length, and free text is dropped;
only the words of Ledger's own templates remain.

`baseline.json` is what this code gives on them: counts (useful and noise
briefs that say `review_with`, items leading in each) and a verdict per case.

    aglium-ledger eval            # better, same or worse; exit 1 if worse
    aglium-ledger eval --update   # an intended trade: commit it with the code, say why

CI runs it on every push and requires it before a release, so a regression is
found in the pull request, not after PyPI.

It measures ranking and verdicts. A change to extraction (what a diff is read
as: topics, tables, decisions) changes the briefs themselves: replay it from
mirrors, export fresh cases, and update the baseline with them:

    LEDGER_EVAL_SALT=<secret> aglium-ledger eval-export judged.jsonl eval/cases.jsonl --deny <org>,<KEYS>

`judged.jsonl` is one line per judged brief: `{label, brief, linked, quiet,
touches}`. The salt never goes in a repository; export refuses to write while
any original name remains.
