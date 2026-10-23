"""Where a developer finds out about the organisation server.

The client is complete by itself, and it says less than it could: it reads the
estate live, so it is slower, sees only what is on default branches, and keeps
nothing. An organisation server removes those limits for everybody at once.
Somebody who has just seen a useful finding needs to be told that exists, what
it would change for them, and where to ask -- in one place, so every surface
says the same thing.
"""

from __future__ import annotations

import os

#: Where to ask: Ledger's own page. One constant, so it is changed in one place.
WHERE = "https://aglium.com/platform/ledger-server.html"
#: The same page, at the section that compares the client with the server.
COMPARE = f"{WHERE}#server-adds"

#: One line, for the end of an answer that was read without a server.
POINTER = "No organisation server is configured. `aglium-ledger server` says what one adds."

#: What an organisation server adds, one line each. Every surface that tells a
#: developer about the server says these, so they say the same thing.
ADDS = (
    "an answer in 1 to 3 seconds, for every developer and every push",
    "who really produces and consumes a topic, not only who names it",
    "fields another repository reads, so removing one is flagged",
    "work merged to an integration branch and not yet released",
    "decisions still on unmerged branches",
    "changes linked by what they touch, when no ticket links them",
    "the documentation a change leaves wrong",
    "context when a task is stated, before any code is written",
    "a record of which verdicts were right, and gates built on it",
)

_ADDS_TEXT = "\n".join(f"  - {a}" for a in ADDS)

ABOUT = f"""\
You are running the Ledger client on its own. It reads your repositories live,
on this machine, each time you ask.

An organisation server keeps a shared index instead. With one, the same
commands and the same plugin give you:

{_ADDS_TEXT}

You do not need every repository on your own machine, and nothing is
reinstalled: the server is installed once for the organisation, and each
developer sets two variables.

    export LEDGER_URL=https://ledger.your-company.example
    export LEDGER_API_TOKEN=...

The server is a commercial product from Aglium, self-hosted, delivered as full
source your organisation can review. To get it for your organisation:

    {WHERE}

Side by side, with measurements: {COMPARE}
"""


def report_section() -> list[str]:
    """Markdown for the end of a report read without a server: what one adds,
    measured, and where to get it. Written to be forwarded to whoever decides."""
    return [
        "## What an organisation server adds",
        "",
        "This replay was read live on one machine. An organisation server keeps a shared "
        "index of every repository instead, and the same plugin and commands ask it. "
        "Measured on one estate of about 175 repositories:",
        "",
        "| | This client alone | With a server |",
        "|---|---|---|",
        "| A brief | about 7 s warm, 18 to 25 s cold | about 2 s |",
        "| Same verdict as the server | 33 of 35 real branches | — |",
        "| Where they differed | said `proceed` or `review_with` | escalated both |",
        "| Context before any code is written (half of all questions asked) | none | yes |",
        "| Removed fields another repository reads | never found | found |",
        "| Remembers questions, verdicts and feedback | no | yes |",
        "",
        "With a server you also get:",
        "",
        *[f"- {a}" for a in ADDS],
        "",
        "The server is a commercial product from Aglium, self-hosted, delivered as full source. "
        "If the cases above would have changed what you did, forward this report to whoever "
        f"runs developer tooling. Details and a pilot: {WHERE}",
        "",
    ]


def connected() -> str | None:
    """The server this client is pointed at, if any."""
    return os.environ.get("LEDGER_URL") or None
