# Findings eval

`baseline.json` is what this code gives on a set of judged briefs: counts
(useful and noise briefs that say `review_with`, items leading in each) and a
verdict per case under an opaque id. It holds no names and no text.

The cases themselves are briefs people and agent sessions judged on a real
estate. Every name in them is a keyed pseudonym and free text is dropped, but
they remain that organisation's, so they live in a private repository
(`EVAL_CASES_REPO`, read in CI with `EVAL_CASES_TOKEN`).

    aglium-ledger eval --cases ../eval-cases/cases.jsonl            # exit 1 if worse
    aglium-ledger eval --cases ../eval-cases/cases.jsonl --update   # an intended trade

CI runs it on every push and requires it before a release. A change that is
meant to trade one count for another commits the new baseline with the code,
and says why in the message.

It measures ranking and verdicts. A change to extraction (what a diff is read
as: topics, tables, decisions) changes the briefs themselves: replay it from
mirrors, export fresh cases (`aglium-ledger eval-export`, with the secret
`LEDGER_EVAL_SALT`), and update the baseline with them.
