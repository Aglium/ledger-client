"""The `ledger` command: the client. Answers with no database -- from a live
read of the estate, or from the organisation server when LEDGER_URL is set."""

from __future__ import annotations

import os

import typer

from ledger.adapters.vcs.base import VcsAdapter
from ledger.config import Config
from ledger.core.check import NAME as CHECK_NAME
from ledger.core.check import decide
from ledger.core.join import find_siblings
from ledger.core.render import render_siblings
from ledger.core.tickets import extract_tickets
from ledger.pipeline.collect import collect_for
from ledger.pipeline.scan import ScanCache

from ledger import service
from ledger.service import LedgerError
from ledger.pipeline.overlap_scan import compare_arms, overlaps_for, scan_signals

app = typer.Typer(add_completion=False, help="Cross-repo impact brief for a change.", pretty_exceptions_enable=False)


def _load_config(path: str | None) -> Config:
    """Load config, turning a missing file into one actionable line."""
    try:
        return Config.load(path)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from None


def _fail(e: LedgerError) -> None:
    typer.secho(str(e), fg="red", err=True)
    raise typer.Exit(2)


def _resolve_subject(cfg: Config, vcs, repo: str, ref: str) -> tuple[dict[str, str], str, int]:
    try:
        s = service.resolve_subject(cfg, vcs, repo, ref)
    except LedgerError as e:
        _fail(e)
    return s.repos, s.base, s.own_commits


def _repos_for(cfg: Config, vcs) -> dict[str, str]:
    return service.repos_for(cfg, vcs)


def _adapters(cfg: Config, local_root: str | None) -> list[VcsAdapter]:
    try:
        return service.build_adapters(
            cfg, local_root, warn=lambda m: typer.secho(m, fg="yellow", err=True))
    except LedgerError as e:
        raise typer.BadParameter(str(e)) from None


@app.command()
def init(
    path: str = typer.Argument(".", help="A checkout of one of your organisation's repositories"),
    out: str = typer.Option(None, "--out", help="Where to write the config (default: your user config)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the config, write nothing"),
):
    """Write a first config from the checkout you are in.

    The forge and organisation come from the origin remote, the ticket keys
    from branch names and commit subjects, the estate from the checkouts next
    door. Nothing is contacted and nothing is cloned; it says what to run next.
    """
    from pathlib import Path

    from ledger import init as initial

    try:
        got = initial.plan(path)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from None
    text = got.render()
    if dry_run:
        typer.echo(text, nl=False)
    else:
        target = Path(out).expanduser() if out else Config.user_path()
        if target.exists() and not force:
            typer.secho(f"{target} already exists; pass --force to replace it", fg="red", err=True)
            raise typer.Exit(2)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        Config.load(str(target))            # what was written must be loadable, or say so now
        typer.echo(f"wrote {target}")
        typer.echo(f"  org={got.config['org']}  ticket keys={', '.join(got.config['project_keys']) or 'none found'}")
    for n in got.notes:
        typer.secho(f"  note: {n}", fg="yellow", err=True)
    if not dry_run:
        typer.echo("\nnext:")
        if "mirror_root" in got.config:
            typer.echo("  aglium-ledger mirrors                      # the organisation's repositories, as bare mirrors")
        typer.echo("  aglium-ledger brief <repo> <branch>        # what a change affects")


@app.command()
def coverage(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root", help="Read clones in this directory"),
    days: int = typer.Option(90, help="How much history to count"),
    forge: bool = typer.Option(False, "--forge",
                               help="Also list the organisation on its forge, to name repositories "
                                    "that are not on this machine (the one network call)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Which repositories Ledger can see from here, what it needs, and where the gaps are.

    Run it after setup and before a replay. It names every repository it
    read, how much merged history each holds, and what is missing, so a quiet
    answer can be told apart from a blind one.
    """
    from ledger import coverage as cv

    checks = cv.prerequisites(config)
    broken = [c for c in checks if not c.ok and c.required and c.name != "config"]
    try:
        cfg = Config.load(config)
    except FileNotFoundError:
        cfg = None
    cov = None
    if cfg is not None:
        try:
            adapters = service.build_adapters(cfg, local_root)
            from ledger.adapters.vcs.localgit import LocalGitAdapter
            vcs = next((a for a in adapters if isinstance(a, LocalGitAdapter)), None)
        except LedgerError as e:
            vcs = None
            checks.append(cv.Check("estate", False, str(e)))
        if vcs is not None:
            fa = service.forge_adapters(cfg) if forge else None
            cov = cv.survey(cfg, vcs, days=days, forge=fa)
        elif not any(c.name == "estate" for c in checks):
            checks.append(cv.Check("estate", False, "no local repositories configured: set "
                                   "`mirror_root` or `local_root`, or pass --local-root. Briefs "
                                   "over the forge API alone cannot replay history."))
    if as_json:
        import json
        typer.echo(json.dumps({
            "prerequisites": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                               "required": c.required} for c in checks],
            "coverage": cov.to_dict() if cov else None}, indent=2))
        raise typer.Exit(1 if broken or cov is None else 0)
    typer.secho("PREREQUISITES", bold=True)
    for c in checks:
        mark = "ok " if c.ok else ("!! " if c.required else "-- ")
        typer.secho(f"  {mark}{c.name:<10} {c.detail}",
                    fg=None if c.ok else ("red" if c.required else "yellow"))
    if cov is None:
        raise typer.Exit(1)
    typer.secho(f"\nREPOSITORIES LEDGER CAN SEE  ({len(cov.readable)} readable"
                + (f", from {', '.join(cov.roots)}" if cov.roots else "") + ")", bold=True)
    for r in cov.repos:
        if r.error:
            typer.secho(f"  {r.repo:<32} unreadable: {r.error}", fg="red")
            continue
        fetched = (f"  fetched {r.fetched_days_ago}d ago" if r.fetched_days_ago is not None else "")
        typer.echo(f"  {r.repo:<32} {r.default_branch:<10} last commit "
                   f"{r.last_commit.isoformat() if r.last_commit else '—':<10}  "
                   f"{r.pull_requests:>3} PR(s) in {days}d{fetched}")
    if cov.excluded:
        typer.echo(f"  excluded by config: {', '.join(cov.excluded)}")
    gaps = cov.gaps()
    if gaps:
        typer.secho("\nGAPS", bold=True, fg="yellow")
        for g in gaps:
            typer.secho(f"  - {g}", fg="yellow")
    if cov.not_here is None:
        typer.echo("\nRepositories that are not on this machine are invisible to Ledger. "
                   "`--forge` lists the organisation to name them.")
    typer.secho(f"\n{cov.verdict()}", bold=True,
                fg="green" if cov.verdict().startswith("ready") else "yellow")
    if cov.verdict().startswith("ready"):
        typer.echo("next: aglium-ledger replay --everyone")
    raise typer.Exit(1 if broken else 0)


feedback_app = typer.Typer(help="Say whether Ledger's answers were any good. Kept on this machine, "
                                "and sent to the organisation server when LEDGER_URL is set.",
                           no_args_is_help=True)
app.add_typer(feedback_app, name="feedback")


@feedback_app.command("record")
def feedback_record(
    verdict: str = typer.Argument(..., help="useful | known | false_positive | insufficient_evidence | missed"),
    case: str = typer.Option(None, "--case", help="A case id from a replay report"),
    report: str = typer.Option("ledger-replay.md", "--report", help="The replay report the case is in"),
    repo: str = typer.Option(None, "--repo"),
    ref: str = typer.Option(None, "--ref"),
    note: str = typer.Option("", "--note", help="Why, in your words"),
    event: str = typer.Option(None, "--event",
                              help="The `event_id` of the answer this is about, when it came "
                                   "from an organisation server"),
):
    """Record one answer about a finding.

    Kept on this machine. With LEDGER_URL set it is also sent to the
    organisation server, where the team's verdicts are counted.
    """
    from pathlib import Path

    from ledger import feedback as fb

    found = fb.case(Path(report), case) if case else None
    if case and found is None:
        typer.secho(f"no case {case} beside {report}; recording it without one", fg="yellow", err=True)
    found = found or {}
    try:
        row = fb.record(verdict, repo=repo or found.get("repo"), ref=ref or found.get("ref"),
                        sha=found.get("sha"), recommendation=found.get("recommendation"),
                        note=note, source="replay" if case else "cli", case=case,
                        event_id=event)
    except fb.BadVerdict as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"kept: {row['verdict']}" + (f" for {row['repo']} {row.get('ref', '')}" if row.get("repo") else "")
               + f"  ({fb.store()})")
    url = os.environ.get("LEDGER_URL")
    if url:
        from ledger import remote
        word = fb.SERVER_WORD[row["verdict"]]
        sent_note = note if word == row["verdict"] else f"[{row['verdict']}] {note}".strip()
        try:
            remote.post(url, os.environ.get("LEDGER_API_TOKEN"), "/feedback",
                        {"verdict": word, "note": sent_note, "event_id": event,
                         "repo": row.get("repo"), "ref": row.get("ref"), "tool": "cli"})
        except LedgerError as e:
            typer.secho(f"not sent to the organisation server: {e}", fg="yellow", err=True)
        else:
            typer.echo(f"sent to {url}")


ask_app = typer.Typer(help="The questions the MCP tools answer, from the command line, as JSON. "
                           "Asks the organisation server when LEDGER_URL is set.",
                      no_args_is_help=True)
app.add_typer(ask_app, name="ask")


def _backend(config: str | None, local_root: str | None, *, via: str):
    """Where the MCP tools and `ask` get their answers: one function, so they
    cannot disagree.

    With LEDGER_URL, the organisation server. A config is optional there: it
    adds this developer's branch rules and lets a brief send the change from
    this machine, and one that is missing or broken only loses those -- the
    server still answers. Without LEDGER_URL, the estate is read here, and the
    config is required: each call says how to set Ledger up, or what is wrong
    with the file.
    """
    from ledger.surfaces.mcp import LiveBackend, RemoteBackend, SetupBackend

    url = os.environ.get("LEDGER_URL")
    if url and not local_root:
        try:
            cfg = Config.load(config)
        except Exception:                               # noqa: BLE001 -- no usable config: ask plainly
            cfg = None
        return RemoteBackend(url, os.environ.get("LEDGER_API_TOKEN"), cfg=cfg, via=via)
    return SetupBackend(lambda: Config.load(config),
                        lambda cfg: LiveBackend(cfg, lambda: _adapters(cfg, local_root)))


def _answer(fn) -> None:
    import json
    try:
        got = fn()
    except LedgerError as e:
        _fail(e)
    typer.echo(json.dumps(got, indent=2, default=str))


@ask_app.command("inflight")
def ask_inflight(
    ticket: str,
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
):
    """Open work carrying a ticket key, in every repository."""
    _answer(lambda: _backend(config, local_root, via="cli").inflight_for(ticket))


@ask_app.command("owners")
def ask_owners(
    surface: str = typer.Argument(..., help="topic | http | table"),
    name: str = typer.Argument(...),
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
):
    """Repositories that touch a contract, and which way each faces it."""
    _answer(lambda: _backend(config, local_root, via="cli").who_owns(surface, name))


@ask_app.command("decisions")
def ask_decisions(
    ticket: str = typer.Option(None, "--ticket"),
    contract: str = typer.Option(None, "--contract"),
    repo: str = typer.Option(None, "--repo"),
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
):
    """ADRs and TODO entries matching a ticket, naming a contract, or in a repository."""
    if not (ticket or contract or repo):
        typer.secho("give --ticket, --contract or --repo", fg="red", err=True)
        raise typer.Exit(2)
    _answer(lambda: _backend(config, local_root, via="cli").decisions_for(ticket, contract, repo))


@feedback_app.command("import")
def feedback_import(report: str = typer.Argument("ledger-replay.md")):
    """Keep the boxes ticked in a replay report."""
    from pathlib import Path

    from ledger import feedback as fb

    md = Path(report)
    if not md.is_file():
        typer.secho(f"{md} not found", fg="red", err=True)
        raise typer.Exit(2)
    kept, skipped = fb.import_report(md)
    typer.echo(f"kept {len(kept)} answer(s)" + (f"; {len(skipped)} already kept" if skipped else "")
               + f"  ({fb.store()})")
    if not kept and not skipped:
        typer.echo("no ticked boxes found: tick one per finding, like [x] useful, and run this again")


@feedback_app.command("summary")
def feedback_summary(
    share: bool = typer.Option(False, "--share",
                               help="Write a file you can send: verdicts and hashed names, no code"),
    notes: bool = typer.Option(False, "--with-notes", help="Include your notes in the shared file"),
    out: str = typer.Option("ledger-feedback.json", "--out"),
):
    """Totals of your answers. With --share, a file to send if you choose to."""
    import json
    from pathlib import Path

    from ledger import feedback as fb

    s = fb.summary(share=share, notes=notes)
    if not s["answers"]:
        typer.echo("no answers kept yet. After a replay: aglium-ledger feedback import ledger-replay.md")
        raise typer.Exit(0)
    typer.echo(f"{s['answers']} answer(s)")
    for v, n in s["by_verdict"].items():
        if n:
            typer.echo(f"  {v:<22} {n}")
    r = s["rates"]
    if r["useful"] is not None:
        typer.echo(f"useful {r['useful']:.0%} · false positive {r['false_positive']:.0%} · "
                   f"can't verify {r['insufficient_evidence']:.0%}  (of judged findings)")
    if share:
        Path(out).write_text(json.dumps(s, indent=2))
        typer.echo(f"\nwrote {out}. It contains {s['contains']}")
        typer.echo("Nothing was sent. Read it, then send it to whoever asked, if you want to.")


@app.command("server")
def server_cmd():
    """What an organisation server adds to this client, and where to get one."""
    from ledger import upgrade

    url = upgrade.connected()
    if url:
        typer.echo(f"This client is connected to an organisation server: {url}")
        typer.echo("Briefs, lookups and hooks are answered from its index.")
        raise typer.Exit(0)
    typer.echo(upgrade.ABOUT, nl=False)


@app.command()
def scan(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root", help="Read local clones instead of forge APIs"),
    liveness_days: int = typer.Option(None, help="Override the scope dial"),
):
    """Report what the liveness window actually contains. Run this before any backfill."""
    cfg = _load_config(config)
    days = liveness_days or cfg.liveness_days
    got = collect_for(cfg, _adapters(cfg, local_root), liveness_days=days)

    by_ticket: dict[str, set[str]] = {}
    for c in got.changes:
        for t in c.tickets:
            by_ticket.setdefault(t, set()).add(c.repo)
    spread = {t: r for t, r in by_ticket.items() if len(r) > 1}

    typer.echo(f"liveness_days={days}  repos={got.repos_seen}  ticketed changes={len(got.changes)}")
    typer.echo(f"tickets={len(by_ticket)}  spanning >1 repo={len(spread)}")
    if got.branches_ignored:
        typer.echo(f"branches left out by `branches` rules={got.branches_ignored}")
    if got.partial:
        typer.secho(f"PARTIAL — missing: {', '.join(got.missing_sources)}", fg="yellow")
    for t, repos in sorted(spread.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        typer.echo(f"  {len(repos)} repos  {t}  ->  {' '.join(sorted(repos))}")


@app.command()
def explain(
    repo: str,
    ref: str,
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
):
    """What else is in flight for this branch's ticket? The question people ask a human."""
    cfg = _load_config(config)
    _adapters_ = _adapters(cfg, local_root)
    _vcs = next((a for a in _adapters_ if a.supports_tree), None)
    if _vcs is not None:
        # Same guard as `brief`: an unknown repo or ref is an input error, not
        # a change with nothing else in flight.
        _resolve_subject(cfg, _vcs, repo, ref)
    got = collect_for(cfg, _adapters_)

    subject = next((c for c in got.changes if c.repo == repo and c.ref == ref), None)
    if subject is None:
        from ledger.core.join import Change
        subject = Change(repo=repo, ref=ref, tickets=tuple(extract_tickets(ref, cfg.project_keys)))
    if not subject.tickets:
        typer.secho(f"no ticket key in {ref!r} — nothing to join on", fg="yellow")
        raise typer.Exit(0)

    result = find_siblings(subject, got.changes, partial=got.partial, missing_sources=got.missing_sources)
    body = render_siblings(result, marker=cfg.comment_marker)
    typer.echo(body or f"{', '.join(subject.tickets)}: no other in-flight work found.")


def _gate(cfg: Config, recommendation: str, asked: bool) -> int:
    """The exit status of a gated brief. Says so on stderr when it stops one."""
    from ledger.core.gate import blocks

    if not asked or not blocks(recommendation, cfg.enforce):
        return 0
    typer.secho(f"gate: {recommendation} is enforced by this deployment's config (`enforce`)",
                fg="red", err=True)
    return 1


def print_brief(b, as_json: bool = False) -> None:
    """Render a brief: JSON for a gate or a skill, text for a person."""
    if as_json:
        import json
        typer.echo(json.dumps(b.to_dict(), indent=2))
        return

    colour = {"proceed": "green", "review_with": "cyan",
              "decision_needed": "yellow", "insufficient_data": "yellow"}[b.recommendation]
    typer.secho(f"{b.recommendation.upper()}  (confidence: {b.confidence})", fg=colour, bold=True)
    typer.echo(f"{b.repo} · {b.ref}" + (f"  [{', '.join(b.tickets)}]" if b.tickets else "")
               + f"   (vs {b.base}: {b.own_commits} own commit{'s' if b.own_commits != 1 else ''})")
    for r in b.reasons:
        typer.echo(f"  · {r}")

    if b.contract_breaks:
        typer.secho(f"\nSTOPS NAMING CONTRACTS OTHERS USE  ({len(b.contract_breaks)})",
                    fg="red", bold=True)
        for c in b.contract_breaks[:6]:
            typer.echo(f"  {c.surface} `{c.name}`  used by: {', '.join(c.also_touched_by)}")
            typer.echo(f"      in {c.path}:  {c.evidence[:90]}")

    if b.field_breaks:
        typer.secho(f"\nREMOVES FIELDS OTHERS DEPEND ON  ({len(b.field_breaks)})", fg="red", bold=True)
        for f in b.field_breaks[:8]:
            typer.echo(f"  {f.surface} `{f.name}` · {f.field_path}")
            for r in f.readers[:4]:
                typer.echo(f"      {r['repo']:<24} {r['direction']:<17} {r['role'][:60]}")
            typer.echo(f"      in {f.path}:  {f.evidence[:90]}")

    if b.contracts:
        shared = [c for c in b.contracts if c.also_touched_by]
        typer.echo(f"\nCONTRACTS TOUCHED  ({len(b.contracts)} total, {len(shared)} shared)")
        for c in shared[:8]:
            from ledger.core.brief import _roles
            typer.echo(f"  {c.surface:<7} {c.name:<34} {_roles(c)}")
            for pty in [x for x in c.parties if x["uses"] in ("publishes", "consumes")][:4]:
                typer.echo(f"      {pty['repo']:<24} {pty['uses']:<10} {pty['how']}")
            for fd in c.fields[:8]:
                role = f" — {fd['role']}" if fd.get("role") else ""
                typer.echo(f"        {fd['repo']} {fd['direction']} `{fd['field']}`{role}"[:150])
            if len(c.fields) > 8:
                typer.echo(f"        … {len(c.fields) - 8} more field(s)")
            for pg in c.documented_in[:2]:
                when = f"  (updated {pg['updated'][:10]}" if pg.get("updated") else "  ("
                when += f", {pg['behind']} commit(s) behind)" if pg.get("behind") else ")"
                typer.echo(f"      documented in {pg['title'][:52]}{when}")
                typer.echo(f"        {pg.get('url') or pg['path']}")
        if not shared:
            typer.echo("  (none are touched by another repo)")
        documented = [c for c in b.contracts if c.documented_in and c not in shared]
        for c in documented[:3]:
            pg = c.documented_in[0]
            behind = f"  ({pg['behind']} commit(s) behind)" if pg.get("behind") else ""
            typer.echo(f"  {c.surface:<7} {c.name:<34} documented in {pg['title'][:40]}{behind}")

    if b.impacts:
        typer.echo("\nAFFECTS")
        for i in b.impacts[:10]:
            where = (f" (in {i.where}" + (f", {i.sha}" if i.sha else "") + ")") if i.where else ""
            typer.echo(f"  {i.repo:<24} {i.ref[:52]:<54}{where}  {i.author or '—'}")
            for label, text in (("does", i.effect), ("relation", i.relation),
                                ("evidence", i.evidence), ("ask", i.ask)):
                if text:
                    typer.echo(f"      {label}: {text}")
            if i.behaviour:
                from ledger.surfaces.hook import behaviour_line
                typer.echo(f"      {behaviour_line({'behaviour': i.behaviour})}")
        if len(b.impacts) > 10:
            typer.echo(f"  … {len(b.impacts) - 10} more")

    if b.decisions:
        typer.echo("\nGOVERNING DECISIONS")
        for d in b.decisions:
            typer.echo(f"  [{d.strength:<6}] {d.title[:76]}")
            typer.echo(f"           {d.repo}/{d.path} · {d.status} · {d.why}")
            if d.deciders:
                typer.echo(f"           deciders: {', '.join(d.deciders)}")
            if d.gate_hit:
                typer.secho(f"           OPEN GATE: {d.gate_hit[:120]}", fg="yellow")

    if b.actors:
        typer.echo(f"\nWHO TO INVOLVE: {', '.join(b.actors)}")
    if b.unreadable_repos:
        typer.secho(f"\n{len(b.unreadable_repos)} repo(s) unreadable: "
                    f"{', '.join(b.unreadable_repos)}", fg="yellow", err=True)
    note = {"stored": "stored index", "live": "live read of every repo",
            "mixed": f"stored index + live read of {', '.join(b.live_repos)} (moved since sync)"}
    from ledger.surfaces.hook import freshness
    fresh = freshness(b.to_dict())
    typer.secho(f"\nsource: {note[b.index_source]}" + (f"; {fresh}" if fresh else ""),
                dim=True, err=True, fg="yellow" if fresh and "STALE" in fresh else None)
    if b.index_source == "live" and not os.environ.get("LEDGER_URL"):
        from ledger import upgrade
        typer.secho(upgrade.POINTER, dim=True, err=True)


@app.command()
def brief(
    repo: str,
    ref: str,
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
    as_json: bool = typer.Option(False, "--json", help="Machine verdict for a gate or skill"),
    live: bool = typer.Option(False, "--live", help="Read the estate here even when LEDGER_URL is set"),
    worktree: bool = typer.Option(False, "--worktree",
                                  help="Include uncommitted changes in the local clone"),
    gate: bool = typer.Option(False, "--gate",
                              help="Exit 1 when the verdict is one the config enforces"),
    base: str = typer.Option(None, "--base",
                             help="The branch this change merges into, e.g. a pull request's "
                                  "target. Without it the base is guessed from the default and "
                                  "integration branches, which is wrong for a branch already "
                                  "merged into one of them."),
):
    """Impact brief: what this change affects, who it affects, and what to do.

    Ends in a recommendation rather than a pile of evidence, because the
    consumers are a coding-agent skill and a CI gate. The evidence is carried
    alongside so a consumer can disagree and show its work.

    With --gate the exit status is the gate: 1 when the verdict is in the
    config's `enforce` list, 0 otherwise. Nothing is enforced by default.

    Asks the organisation server when LEDGER_URL is set; otherwise reads the
    estate live, which is slower and remembers nothing.
    """
    cfg = _load_config(config)
    url = os.environ.get("LEDGER_URL")
    if url and not live and not local_root:
        from ledger.surfaces.hook import summarise
        try:
            got = _remote_brief(cfg, url, "cli", 60.0, worktree, False)(repo, ref, base)
        except LedgerError as e:
            _fail(e)
        if as_json:
            import json
            typer.echo(json.dumps(got, indent=2))
        else:
            typer.echo(summarise(got))
        raise typer.Exit(_gate(cfg, got.get("recommendation") or "", gate))
    try:
        b = service.run_brief(cfg, _adapters(cfg, local_root), repo, ref, worktree=worktree,
                              base=base)
    except LedgerError as e:
        _fail(e)
    print_brief(b, as_json)
    raise typer.Exit(_gate(cfg, b.recommendation, gate))


@app.command()
def replay(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
    days: int = typer.Option(90, help="How far back to take merged changes"),
    everyone: bool = typer.Option(False, "--everyone", help="Replay every author's changes, not only yours"),
    author: str = typer.Option(None, "--author", help="Replay this author's changes (an email)"),
    limit: int = typer.Option(30, help="Replay at most this many, newest first"),
    out: str = typer.Option("ledger-replay.md", "--out", help="Where to write the report"),
):
    """What would a brief have said about changes that already merged?

    Briefs your own recent merged changes as they were on the day each
    started, and writes a short report asking, case by case, whether knowing
    it would have changed what you did.
    """
    from datetime import date, timedelta
    from pathlib import Path

    from ledger import replay as rp
    from ledger.adapters.vcs.localgit import LocalGitAdapter

    cfg = _load_config(config)
    vcs = next((a for a in _adapters(cfg, local_root) if isinstance(a, LocalGitAdapter)), None)
    if vcs is None:
        typer.secho("replay reads merge history, which needs local git: set `mirror_root` or "
                    "`local_root`, or pass --local-root", fg="red", err=True)
        raise typer.Exit(2)
    who = None if everyone else (author or rp.me(Path.cwd()))
    if who is None and not everyone:
        typer.secho("no git user.email here to tell which changes are yours: pass --author "
                    "or --everyone", fg="red", err=True)
        raise typer.Exit(2)

    def progress(i, n, m):
        typer.echo(f"  [{i}/{n}] {m.repo} {m.ref[:60]}", err=True)

    got = rp.run(cfg, vcs, since=date.today() - timedelta(days=days), author=who, limit=limit,
                 progress=progress)
    md, side = rp.write(got, out)
    typer.echo(f"replayed {len(got.briefed)} change(s): {len(got.with_context)} with something to say "
               f"about another repository, {len(got.clear)} with nothing found, "
               f"{len(got.uncovered)} where Ledger could not see enough to tell")
    for g in got.gaps():
        typer.secho(f"gap: {g}", fg="yellow", err=True)
    typer.echo(f"report: {md}  (answers go in with `aglium-ledger feedback import {md}`)")
    if os.environ.get("LEDGER_URL") is None:
        from ledger import upgrade
        typer.echo(f"This was a live read. {upgrade.POINTER}")


@app.command()
def overlap(
    repo: str,
    ref: str,
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
    liveness_days: int = typer.Option(None, help="Override the scope dial"),
    min_score: int = typer.Option(1, help="Drop hits below this weighted score"),
):
    """Control arm: what else touches the same files, tables, migrations or symbols?

    Deterministic, no model calls. This is the baseline any later
    inference-based layer has to beat.
    """
    cfg = _load_config(config)
    adapters = _adapters(cfg, local_root)
    got = collect_for(cfg, adapters, liveness_days=liveness_days or cfg.liveness_days)
    scan = scan_signals(adapters, got)

    hits = overlaps_for((repo, ref), scan, min_score=min_score)
    if (repo, ref) not in scan.signals:
        typer.secho(f"{repo}@{ref} produced no readable diff — no claim either way", fg="yellow")
        raise typer.Exit(0)
    if not hits:
        typer.echo(f"{repo}@{ref}: no cross-repo overlap above score {min_score}")
        raise typer.Exit(0)

    typer.echo(f"{repo}@{ref} — {len(hits)} overlapping change(s):")
    for h in hits:
        typer.echo(f"  [{h.score:>3}] {h.repo} {h.ref}")
        for kind, values in sorted(h.by_kind().items(), key=lambda kv: kv[0].value):
            shown = ", ".join(values[:4]) + (f" (+{len(values) - 4})" if len(values) > 4 else "")
            typer.echo(f"         {kind.value:<9} {shown}")


@app.command()
def benchmark(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
    liveness_days: int = typer.Option(None, help="Override the scope dial"),
    min_score: int = typer.Option(1),
    show: int = typer.Option(5, help="Disagreements to print"),
):
    """Ticket join vs overlap join, over the whole corpus.

    The number that matters is the disagreement: where one arm fires and the
    other does not is exactly what a more expensive layer would have to justify.
    """
    cfg = _load_config(config)
    adapters = _adapters(cfg, local_root)
    got = collect_for(cfg, adapters, liveness_days=liveness_days or cfg.liveness_days)
    scan = scan_signals(adapters, got)
    comparisons = compare_arms(got, scan, min_score=min_score)

    t_only = [c for c in comparisons if c.ticket_only]
    o_only = [c for c in comparisons if c.overlap_only]
    both = [c for c in comparisons if c.both]

    typer.echo(f"changes: {len(comparisons)}   diffs read: {scan.readable}   unreadable: {len(scan.unreadable)}")
    typer.echo(f"ticket-join pairs : {sum(len(c.ticket_hits) for c in comparisons)}")
    typer.echo(f"overlap    pairs  : {sum(len(c.overlap_hits) for c in comparisons)}")
    typer.echo(f"agree on >=1 pair : {len(both)} changes")
    typer.echo(f"ticket only       : {len(t_only)} changes  <- the overlap arm misses these")
    typer.echo(f"overlap only      : {len(o_only)} changes  <- the ticket arm misses these")

    for label, group in (("TICKET ONLY", t_only), ("OVERLAP ONLY", o_only)):
        if not group:
            continue
        typer.echo(f"\n{label} (first {show}):")
        for c in group[:show]:
            missed = c.ticket_only if label.startswith("TICKET") else c.overlap_only
            typer.echo(f"  {c.subject[0]} {c.subject[1]}")
            for repo, ref in sorted(missed)[:3]:
                typer.echo(f"      -> {repo} {ref}")

    if scan.unreadable:
        typer.secho(f"\n{len(scan.unreadable)} change(s) had no readable diff; "
                    "they are excluded, not counted as clean", fg="yellow")


def post_comment(cfg: Config, repo: str, pr_number: int, result, *, dry_run: bool,
                 local_root: str | None, head_sha: str | None, brief=None) -> None:
    """The check run and the sticky comment for one PR.

    With a `brief`, the comment is the brief's: what this change affects in
    other repositories, linked by anything, not only a shared ticket. Without
    one it names the other changes on the same ticket. The check fails only on
    a verdict the config enforces.
    """
    from ledger.core.gate import blocks
    from ledger.core.render import render_brief_comment

    enforced = None
    if brief is not None and blocks(brief.recommendation, cfg.enforce):
        enforced = (brief.recommendation, list(brief.reasons))
    verdict = decide(result, enforced=enforced)
    body = (render_brief_comment(brief.to_dict(), marker=cfg.comment_marker) if brief is not None
            else render_siblings(result, marker=cfg.comment_marker))

    if dry_run:
        typer.echo(f"check: {verdict.conclusion.value} — {verdict.title}")
        typer.echo(verdict.summary)
        if body:
            typer.echo("")
            typer.echo(body)
        else:
            typer.echo("(no comment — silence beats a 'nothing found' post)")
        raise typer.Exit(0)

    adapters = _adapters(cfg, local_root)

    # The check always goes out, including when there is no comment: a PR the
    # index examined and found clean deserves to say so, and one it never saw
    # must say neutral rather than nothing.
    if head_sha:
        wrote = False
        for adapter in adapters:
            if not adapter.supports_checks:
                continue
            try:
                adapter.set_check_status(repo, head_sha, CHECK_NAME,
                                         verdict.conclusion.value, verdict.title, verdict.summary)
            except Exception as e:  # noqa: BLE001 - advisory; reported, never fatal
                typer.secho(f"{adapter.source}: check failed: {e}", fg="yellow", err=True)
                continue
            typer.echo(f"check {verdict.conclusion.value} via {adapter.source}")
            wrote = True
            break
        if not wrote:
            typer.secho("no adapter could write a check run", fg="yellow", err=True)
    else:
        typer.secho("--head-sha not given; skipping the check run", fg="yellow", err=True)

    if body is None:
        typer.echo("no siblings — staying silent (a 'nothing found' comment is how a bot gets muted)")
        raise typer.Exit(0)

    for adapter in adapters:
        if not adapter.supports_comments:
            continue
        try:
            cid = adapter.upsert_comment(repo, pr_number, cfg.comment_marker, body)
        except Exception as e:  # noqa: BLE001 - reported, never swallowed
            typer.secho(f"{adapter.source}: {e}", fg="red", err=True)
            continue
        typer.echo(f"posted via {adapter.source} (comment {cid})")
        raise typer.Exit(0)
    typer.secho("no adapter could post the comment", fg="red", err=True)
    raise typer.Exit(1)


@app.command()
def comment(
    repo: str,
    pr_number: int,
    ref: str = typer.Option(None, help="Head ref"),
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    dry_run: bool = typer.Option(True, "--dry-run/--post", help="Print the body, or actually post it"),
    live: bool = typer.Option(True, "--live", hidden=True, help="Kept for existing workflows; always live here"),
    local_root: str = typer.Option(None, "--local-root", help="Read local clones instead of APIs"),
    head_sha: str = typer.Option(None, "--head-sha", help="Commit to attach the check run to"),
    full: bool = typer.Option(True, "--brief/--siblings-only",
                              help="The full brief (default), or only work sharing the ticket"),
):
    """Render (and optionally post) the sticky Ledger comment for one PR.

    Stateless, for CI. One PR's answer needs no database -- just the forges.
    That keeps the Action a single step with no infrastructure behind it, which
    is the difference between "adoptable this week" and "adoptable once someone
    stands up a service".

    By default the comment is the full brief: removed fields and contracts
    other repositories read, changes in flight that cannot land with this one,
    who is on the other side of each topic or route, and other work linked by
    ticket, by what it touches or by author. A branch with no ticket key is
    briefed like any other. If the brief cannot be computed, it falls back to
    the ticket join and says so.
    """
    from ledger.core.join import Change

    cfg = _load_config(config)
    if not ref:
        typer.secho("--ref is required until PR-number lookup lands", fg="yellow")
        raise typer.Exit(1)
    adapters = _adapters(cfg, local_root)
    got = collect_for(cfg, adapters)
    subject = next((c for c in got.changes if c.repo == repo and c.ref == ref), None)
    if subject is None:
        subject = Change(repo=repo, ref=ref, tickets=tuple(extract_tickets(ref, cfg.project_keys)),
                         kind="pr", pr_number=pr_number)
    if not full and not subject.tickets:
        typer.echo(f"no ticket key in {ref!r} — nothing to join on (run without --siblings-only "
                   "to brief it anyway)")
        raise typer.Exit(0)
    result = find_siblings(subject, got.changes, partial=got.partial,
                           missing_sources=got.missing_sources)
    b = None
    if full:
        try:
            vcs = service.tree_reader(adapters)
            b = service.compute_brief(cfg, adapters, repo, ref,
                                      lambda repos: service.LiveIndex(vcs, repos, ScanCache.default()),
                                      collected=got)
        except Exception as e:  # noqa: BLE001 -- advisory: fall back to the ticket join, say why
            typer.secho(f"could not brief {repo} {ref} ({type(e).__name__}: {e}); "
                        "falling back to work sharing its ticket", fg="yellow", err=True)
    post_comment(cfg, repo, pr_number, result, dry_run=dry_run, local_root=local_root,
                 head_sha=head_sha, brief=b)


@app.command("mcp")
def mcp_server(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    local_root: str = typer.Option(None, "--local-root"),
    transport: str = typer.Option("stdio", help="stdio | streamable-http"),
):
    """MCP server for coding agents: brief, inflight_for, who_owns, decisions_for.

    Asks the organisation server when LEDGER_URL is set. Without one it reads
    the estate live: slower, and without what only a stored index knows.
    Nothing is written to stdout except the protocol.
    """
    from ledger.surfaces.mcp import build_server

    # Without a config the server still starts: the plugin launches it on install,
    # before setup, and each tool then says how to set Ledger up.
    backend = _backend(config, local_root, via="mcp")
    try:
        srv = build_server(backend)
    except LedgerError as e:
        _fail(e)
    srv.run(transport)


@app.command()
def hook(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    timeout: float = typer.Option(20.0, help="Seconds to wait for a brief before giving up"),
):
    """PreToolUse hook for coding agents: the brief, when work starts and when
    it is pushed.

    Reads the hook payload on stdin. Asks a running `ledger-server serve` when
    LEDGER_URL is set, otherwise computes the brief in-process. Always exits 0:
    on any failure it prints nothing and the command proceeds untouched.
    """
    import json
    import sys

    from ledger.surfaces import hook as h

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        raise typer.Exit(0)
    command = ((payload.get("tool_input") or {}).get("command")) or ""
    if payload.get("tool_name") != "ExitPlanMode" and not (h.is_publish(command)
                                                           or h.branch_started(command)):
        raise typer.Exit(0)                 # the common case: leave before loading anything

    try:
        cfg = Config.load(config)
    except Exception:                                   # noqa: BLE001 -- fail open
        raise typer.Exit(0)
    out = h.handle(payload, _hook_brief(cfg, "push_hook", timeout),
                   _hook_brief(cfg, "branch_start", timeout, worktree=False,
                               allow_missing_ref=True),
                   _hook_context(cfg, "plan"))
    if out is not None:
        typer.echo(json.dumps(out))
    raise typer.Exit(0)


def _hook_context(cfg: Config, surface: str):
    """Context from text, for the hooks that run before any diff exists.

    Only the organisation server can answer this: it is a lookup over every
    contract and every open change in the estate. With no LEDGER_URL the hook
    raises, and its caller fails open -- nothing is printed.
    """
    def go(text: str, *, repo: str | None = None) -> dict:
        from ledger import remote
        url = os.environ.get("LEDGER_URL")
        if not url:
            raise LedgerError("context needs the organisation server: set LEDGER_URL")
        return remote.post(url, os.environ.get("LEDGER_API_TOKEN"), "/context",
                           {"text": text, "repo": repo, "via": surface},
                           headers=remote.headers_for(cfg))
    return go


def _remote_brief(cfg: Config, url: str, surface: str, timeout: float, worktree: bool,
                  allow_missing_ref: bool):
    """Ask the organisation server for a brief; see `remote.brief_asker`."""
    from ledger import remote
    return remote.brief_asker(cfg, url, os.environ.get("LEDGER_API_TOKEN"), via=surface,
                              timeout=timeout, worktree=worktree,
                              allow_missing_ref=allow_missing_ref,
                              headers=remote.headers_for(cfg))


def _hook_brief(cfg: Config, surface: str, timeout: float, worktree: bool = True,
                allow_missing_ref: bool = False):
    """Brief for a hook: from the organisation server when LEDGER_URL is set,
    read live in-process otherwise."""
    url = os.environ.get("LEDGER_URL")
    if url:
        return _remote_brief(cfg, url, surface, timeout, worktree, allow_missing_ref)

    def brief_for(repo: str, ref: str, base: str | None = None) -> dict:
        return service.run_brief(cfg, service.build_adapters(cfg, None), repo, ref,
                                 worktree=worktree, allow_missing_ref=allow_missing_ref,
                                 base=base).to_dict()
    return brief_for


@app.command("user-prompt")
def user_prompt(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
):
    """UserPromptSubmit hook: what the estate already has for the task asked for.

    The earliest moment anything can be said, and the only one that always
    happens: a session may never branch, commit or push, but it always begins
    with somebody saying what they want. Prints nothing when there is nothing.
    """
    import json
    import sys

    from ledger.surfaces import hook as h

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cfg = Config.load(config)
    except Exception:                                   # noqa: BLE001 -- fail open
        raise typer.Exit(0)
    root = os.environ.get("LEDGER_ROOT") or cfg.local_root
    out = h.handle_user_prompt(payload, _hook_context(cfg, "user_prompt"), root)
    if out:
        typer.echo(json.dumps(out))
    raise typer.Exit(0)


@app.command()
def mirrors(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    root: str = typer.Option(None, "--root", help="Where to keep mirrors (default: config mirror_root)"),
    prune: bool = typer.Option(False, "--prune", help="Delete mirrors the organisation no longer lists"),
):
    """Discover the organisation's repositories and mirror them here.

    The server finds the estate itself, through the forges, and clones what it
    does not have. Nobody has to clone anything for it, and the estate is the
    organisation's repositories rather than whichever ones somebody checked
    out on a laptop.
    """
    from ledger.pipeline import mirrors as m

    cfg = _load_config(config)
    where = root or cfg.mirror_root
    if not where:
        raise typer.BadParameter("set `mirror_root` in the config, or pass --root")
    adapters = service.forge_adapters(cfg, warn=lambda msg: typer.secho(msg, fg="yellow", err=True))
    if not adapters:
        raise typer.BadParameter(
            "no forge credentials: discovery needs them even when the reads are local")
    remotes, found = m.discover(adapters, exclude=tuple(cfg.exclude_repos))
    for name, why in found.failed:
        typer.secho(f"  discovery failed for {name}: {why[:150]}", fg="yellow", err=True)
    for c in found.collisions:
        typer.secho(f"  name collision: {c}", fg="yellow", err=True)
    if not remotes:
        typer.secho("no repositories discovered", fg="yellow", err=True)
        raise typer.Exit(1)

    typer.echo(f"discovered {len(remotes)} repo(s); mirroring into {where}")
    res = m.sync(where, remotes, credentials=service.forge_credentials(cfg))
    typer.echo(f"cloned {len(res.cloned)}, fetched {len(res.fetched)}, failed {len(res.failed)}")
    for name in res.cloned:
        typer.echo(f"  + {name}")
    for name, why in res.failed:
        typer.secho(f"  ! {name}: {why[:150]}", fg="red", err=True)
    if res.orphaned:
        typer.secho(f"  {len(res.orphaned)} mirror(s) the organisation no longer lists: "
                    f"{', '.join(res.orphaned[:8])}", fg="yellow", err=True)
        if prune:
            import shutil
            from pathlib import Path as _P
            for d in res.orphaned:
                shutil.rmtree(_P(where).expanduser() / d, ignore_errors=True)
            typer.echo(f"  pruned {len(res.orphaned)}")
    raise typer.Exit(1 if res.failed else 0)


@app.command("session-start")
def session_start(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    timeout: float = typer.Option(15.0, help="Seconds to wait for a brief before giving up"),
):
    """SessionStart hook: cross-repo context for the branch a session opens on.

    Acts only inside the estate (config `local_root`); elsewhere it prints
    nothing. Always exits 0 -- a broken ledger must never delay a session.
    """
    import json
    import sys

    from ledger.surfaces import hook as h

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cfg = Config.load(config)
    except Exception:                                   # noqa: BLE001 -- fail open
        raise typer.Exit(0)
    # One answer per session start. Startup and resume both fire, seconds
    # apart, and the second copy is the same brief injected twice.
    cwd = payload.get("cwd") or os.getcwd()
    where = h.repo_and_ref(cwd)
    if where and h.under(cfg.local_root, cwd) and h.answered_recently("session_start", *where):
        raise typer.Exit(0)
    out = h.handle_session_start(payload, _hook_brief(cfg, "session_start", timeout),
                                 cfg.local_root)
    if out is not None:
        typer.echo(json.dumps(out))
    raise typer.Exit(0)


@app.command()
def bench(
    cases: str = typer.Argument(..., help="Labelled cases, one JSON object per line"),
    context: str = typer.Option(None, help="JSON {session: change description}, for --judge"),
    heads: str = typer.Option(None, help="JSON {session: the brief's first line}, for --judge"),
    judge: str = typer.Option(None, help="Comma-separated sessions to replay through a reviewer "
                                         "and a judge (Claude Code CLI, `claude -p`)"),
    samples: int = typer.Option(2, help="Reviewer answers per session and variant"),
):
    """Score the ranking rules against labelled past briefs.

    The case file is the organisation's, kept wherever it lives -- never in
    this repository. Prints a table: before (everything shown) against the
    ranking as it ships, and with --judge the reviewer scores, 0-8.
    """
    import json as _json
    import statistics
    from pathlib import Path

    from ledger import bench as b

    rows = b.read_cases(cases)
    typer.echo(b.table(rows))
    if not judge:
        raise typer.Exit(0)
    ctx = _json.loads(Path(context).expanduser().read_text()) if context else {}
    hd = _json.loads(Path(heads).expanduser().read_text()) if heads else {}
    wanted = [s.strip() for s in judge.split(",") if s.strip()]
    full = sorted({r["sid"] for r in rows})
    sids = [next((f for f in full if f.startswith(w)), w) for w in wanted]
    got = b.judge(rows, ctx, hd, sids, samples=samples)
    typer.echo("\n| Session | before | ranked |\n|---|---|---|")
    for sid in sids:
        cell = lambda v: "/".join(str(x["score"]) for x in got if x["sid"] == sid and x["variant"] == v)  # noqa: E731
        typer.echo(f"| {sid[:8]} | {cell('before')} | {cell('ranked')} |")
    for v in ("before", "ranked"):
        xs = [x["score"] for x in got if x["variant"] == v and x["score"] is not None]
        if xs:
            typer.echo(f"{v}: mean {statistics.mean(xs):.2f} of 8 over {len(xs)} answers")


@app.command("eval")
def eval_cmd(
    cases: str = typer.Option("eval/cases.jsonl", envvar="LEDGER_EVAL_CASES",
                              help="Anonymized judged briefs (JSON lines)"),
    baseline: str = typer.Option("eval/baseline.json", help="Counts and verdicts to hold this code to"),
    promote: int = typer.Option(None, help="Model score that lets a weak item lead; default: the baseline's"),
    update: bool = typer.Option(False, "--update", help="Record this run as the new baseline"),
):
    """Did this change make findings better or worse? Exits 1 when worse.

    Re-ranks judged briefs with this checkout's code and holds the counts to
    the baseline. Run from the repository root; the baseline changes in the
    same commit as the code it measures.
    """
    import json as _json
    from pathlib import Path

    from ledger import evaluate

    rows = evaluate.read_cases(Path(cases).expanduser())
    base_path = Path(baseline)
    was = _json.loads(base_path.read_text()) if base_path.exists() else None
    if promote is None:
        promote = (was or {}).get("promote", 3)
    now = evaluate.run(rows, promote=promote)
    if update or was is None:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(_json.dumps({"promote": promote, **now.to_dict()}, indent=1, sort_keys=True) + "\n")
        typer.echo(f"baseline written: {base_path}")
        for k, v in now.metrics.items():
            typer.echo(f"  {k:15} {v}")
        raise typer.Exit(0)
    ok, lines = evaluate.compare(now, was, {r["id"]: r["label"] for r in rows})
    typer.echo(f"{len(rows)} judged brief(s), promote at {promote}:")
    for line in lines:
        typer.echo(line)
    raise typer.Exit(0 if ok else 1)


@app.command("eval-export")
def eval_export(
    source: str = typer.Argument(..., help="JSON lines: label, brief, linked, quiet, touches"),
    out: str = typer.Argument(..., help="Where to write anonymized cases"),
    deny: str = typer.Option("", help="Comma-separated words that must never survive (org, ticket keys)"),
):
    """Anonymize judged briefs into eval cases. Needs LEDGER_EVAL_SALT, a secret
    kept out of every repository. Refuses to write while an original name remains."""
    import json as _json
    import os

    from ledger import anonymize

    salt = os.environ.get("LEDGER_EVAL_SALT", "")
    if not salt:
        typer.secho("set LEDGER_EVAL_SALT to a secret: it keeps pseudonyms from being guessed",
                    fg="red", err=True)
        raise typer.Exit(2)
    p = anonymize.Pseudonyms(salt, deny={d.strip() for d in deny.split(",") if d.strip()})
    with open(source, encoding="utf-8") as f:
        rows = [_json.loads(line) for line in f if line.strip()]
    text = "".join(_json.dumps(anonymize.case(r, p, touches=r.get("touches")), sort_keys=True) + "\n"
                   for r in rows)
    left = anonymize.leaks(text, p)
    if left:
        typer.secho(f"{len(left)} original name(s) would remain, nothing written: {', '.join(left[:10])}",
                    fg="red", err=True)
        raise typer.Exit(1)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    typer.echo(f"{len(rows)} case(s) written to {out}")


@app.command("session-stop")
def session_stop(
    config: str = typer.Option(None, envvar="LEDGER_CONFIG", help="Path to ledger.yml"),
    timeout: float = typer.Option(10.0, help="Seconds to wait for the server"),
):
    """Stop hook: send the agent's reply after each ledger answer, so the items
    it set aside are recorded as noise -- marked automatic, apart from people's
    verdicts. Needs a server (LEDGER_URL). Prints nothing; always exits 0.
    """
    import json
    import sys

    from ledger import remote
    from ledger.surfaces import hook as h

    url = os.environ.get("LEDGER_URL")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cfg = Config.load(config)
    except Exception:                                   # noqa: BLE001 -- fail open
        raise typer.Exit(0)
    if not url:
        raise typer.Exit(0)
    token = os.environ.get("LEDGER_API_TOKEN")
    root = os.environ.get("LEDGER_ROOT") or cfg.local_root
    h.handle_session_stop(payload, lambda event, reply: remote.post(
        url, token, "/feedback/auto", {"event_id": event, "reply": reply}, timeout=timeout), root)
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
