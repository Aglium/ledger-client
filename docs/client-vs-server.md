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
| A brief | about 7 s warm, 18 to 25 s cold, on ~175 repositories | about 2 s |
| Who produces and consumes a contract | Who names it | Who publishes, consumes or declares it |
| Fields another repository reads | Not found | Found, so removing one is flagged |
| Work merged to an integration branch, not released | Not seen | Seen |
| Decisions still on unmerged branches | Not seen | Seen |
| How in-flight work is linked | A ticket key in the branch name or written in the diff | Also the same author pushing minutes apart, and what the changes touch |
| Context before any code is written | None | Yes |
| Price | Free | Commercial; a four-week pilot is offered |

On one estate of about 175 repositories, the client alone gave the same verdict as the server
on 33 of 35 real branches; where they differed, the server escalated both.

## When the server is worth it

- Two or more engineers in the organisation are using the client.
- Someone found a useful issue and wants the rest of the team to see the same information.
- The team needs context before code is written, not only when a branch is reviewed.
- Keeping every repository mirrored on every laptop has become a chore.
- An engineering manager wants the same checks across teams.

Details and the pilot: [aglium.com/platform/ledger-server.html](https://aglium.com/platform/ledger-server.html).
