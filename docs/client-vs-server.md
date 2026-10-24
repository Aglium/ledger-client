# The client and the organisation server

The client in this repository is complete by itself and free to use. The organisation server
is a separate commercial product, self-hosted, delivered as full source your organisation can
review. They answer the same questions through the same commands, plugin and MCP tools; set
`LEDGER_URL` and `LEDGER_API_TOKEN` and the client asks the server instead of reading the
estate itself.

| | Client | Organisation server |
|---|---|---|
| Who it is for | One developer | An engineering organisation |
| What it does | Checks a change against the repositories on this machine | Keeps a shared index of every repository, for everyone |
| How | Local, on demand, read live each time | Persistent index, kept in sync |
| History | Reconstructed from the repositories you can read | Stored, including questions, verdicts and feedback |
| Where it runs | Claude Code, the command line, CI | The same, for every developer and pipeline at once |
| A brief | about 7 to 10 s warm, 20 to 30 s cold, on ~175 repositories | about 2 s |
| Who produces and consumes a contract | Read from call sites in the code | Also as the infrastructure declares it: ACLs, AsyncAPI, deployment config, bound constants |
| Fields another repository reads | The names this change removes, looked up in consumers' code | Every field of every contract, indexed, including reads through generated clients |
| Changes that cannot both land | Found among branches on this machine | Found among every branch in the organisation |
| Work merged to an integration branch, not released | Not seen | Seen |
| Decisions still on unmerged branches | Not seen | Seen |
| How in-flight work is linked | A ticket key; a topic or route both diffs name; a rare topic, table or migration both touch; the same author at the same time (listed, never a verdict) | The same rules over every branch, plus a summary of what each change does |
| Context before any code is written | None | Yes |
| Price | Free | Commercial; a four-week pilot is offered |

Measured with an earlier client on one estate of about 175 repositories: it gave the same
verdict as the server on 33 of 35 real branches, and where they differed, the server escalated
both.

## What the client alone does not do

The client is honest about these: every answer names the repositories it read, and none of
the gaps below turns into a `proceed` that sounds sure of itself. They are what an
organisation server is for.

- **It sees only what is on this machine.** Repositories you have not mirrored or cloned, and
  branches nobody has fetched, are invisible to it. A server mirrors the organisation itself.
- **It reads everything again for each answer.** A brief takes seconds to tens of seconds; a
  server answers in about 2, for every developer and every push.
- **It reads direction from code only.** Who publishes and who consumes a topic comes from call
  sites and from constants bound in config. Wiring declared in infrastructure (Kafka ACLs,
  Terraform, AsyncAPI, a deployment layout) is not read, so two names for one channel are two
  topics to it.
- **It checks fields narrowly.** It looks up only the names a change removes, and only in
  consumers' files that name the topic or route. A field read through a generated client or
  another module is not seen. A server indexes every field of every contract.
- **It cannot tell who owns a table.** A table has no direction in code; it only sets aside
  tables that each repository creates for itself.
- **It does not see work merged to an integration branch and not yet released,** or
  decisions still being written on unmerged branches.
- **It links in-flight work from a short list.** The newest 40 branches on this machine, read
  by name and direction, with no summary of what each one does.
- **It has nothing to say before code exists.** A stated task, with no diff yet, gets no
  context; a server answers from the plan.
- **It does not notice documentation a change leaves wrong.**
- **It keeps nothing shared.** Findings, feedback and which verdicts were right stay on one
  laptop, so nobody else benefits and no gate can be calibrated on the team's experience.
- **In the GitHub Action, without mirrors,** it reads through the forge's API: topics reached
  through a constant are not followed, and a cold run on a large organisation is slow.

## What the server adds

- An answer in 1 to 3 seconds, for every developer and every push.
- Every branch in every repository, indexed as it is pushed.
- Topic wiring as the infrastructure declares it, joined with what the code does.
- Every field of every contract, including reads through generated clients and other modules.
- Work merged to an integration branch and not yet released; decisions on unmerged branches.
- What each change in flight does, summarised from its diff when it is pushed.
- The documentation a change leaves wrong.
- Context when a task is stated, before any code is written.
- A shared record of findings, feedback and which verdicts were right, and gates built on it.

## When the server is worth it

- Two or more engineers in the organisation are using the client.
- Someone found a useful issue and wants the rest of the team to see the same information.
- The team needs context before code is written, not only when a branch is reviewed.
- Keeping every repository mirrored on every laptop has become a chore.
- An engineering manager wants the same checks across teams.

Details and the pilot: [aglium.com/platform/ledger-server.html](https://aglium.com/platform/ledger-server.html).
