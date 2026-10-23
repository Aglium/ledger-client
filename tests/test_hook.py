"""The agent hook. Its three rules are the whole design, so each has a test:
never approve, fail open, stay quiet on proceed."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from ledger.cli import app
from ledger.surfaces import hook as h


def brief(rec, **kw):
    b = {"schema": 4, "subject": {"repo": "orders", "ref": "feat/abc-1-x"},
         "recommendation": rec, "confidence": "medium", "reasons": ["because"],
         "actors": [], "contracts": [], "impacts": [], "decisions": []}
    b.update(kw)
    return b


class TestWhatCounts:
    @pytest.mark.parametrize("cmd", [
        "git push", "git push -u origin feat/abc-1", "cd x && git push", "git -C ../orders push",
        "gh pr create --fill", "glab mr create", "npm test; git push origin HEAD",
    ])
    def test_publishing_commands(self, cmd):
        assert h.is_publish(cmd)

    @pytest.mark.parametrize("cmd", [
        "git status", "git pull", "git log --grep push", "echo git-push", "gh pr view 3", "",
    ])
    def test_everything_else(self, cmd):
        assert not h.is_publish(cmd)


class TestNeverApproves:
    @pytest.mark.parametrize("rec", ["proceed", "review_with", "decision_needed", "insufficient_data"])
    def test_no_verdict_produces_allow(self, rec):
        out = h.response(brief(rec)) or {}
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"

    def test_proceed_is_silent(self):
        assert h.response(brief("proceed")) is None

    def test_review_with_adds_context_and_leaves_permission_alone(self):
        out = h.response(brief("review_with", impacts=[
            {"repo": "billing", "ref": "feat/abc-1-y", "where": "stage", "author": "bob"}]))
        spec = out["hookSpecificOutput"]
        assert "permissionDecision" not in spec
        assert "billing feat/abc-1-y (in stage) by bob" in spec["additionalContext"]

    def test_decision_needed_asks_the_human(self):
        out = h.response(brief("decision_needed", decisions=[{
            "repo": "billing", "path": "docs/adr/0001.md", "status": "accepted",
            "deciders": ["Alice"], "gate_hit": "order_line lacks a backfill"}]))
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "ask"
        assert "Alice" in spec["permissionDecisionReason"] and "OPEN GATE" in spec["additionalContext"]

    def test_insufficient_data_is_never_worded_as_safe(self):
        text = h.response(brief("insufficient_data"))["hookSpecificOutput"]["additionalContext"]
        assert "UNKNOWN, not safe" in text


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "orders"
    run = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    repo.mkdir()
    run("init", "-q", "-b", "main")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
    run("checkout", "-q", "-b", "feat/abc-1-x")
    return repo, run


class TestFailsOpen:
    def payload(self, cwd, cmd="git push"):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}

    def test_a_crashing_brief_means_no_output(self, clone):
        repo, _ = clone
        def boom(*_):
            raise RuntimeError("database down")
        assert h.handle(self.payload(repo), boom) is None

    def test_outside_a_git_repo_means_no_output(self, tmp_path):
        assert h.handle(self.payload(tmp_path), lambda *_: brief("decision_needed")) is None

    def test_detached_head_means_no_output(self, clone):
        repo, run = clone
        run("checkout", "-q", "--detach")
        assert h.handle(self.payload(repo), lambda *_: brief("decision_needed")) is None

    def test_other_tools_are_ignored(self, clone):
        repo, _ = clone
        p = {"tool_name": "Edit", "tool_input": {"command": "git push"}, "cwd": str(repo)}
        assert h.handle(p, lambda *_: brief("decision_needed")) is None

    def test_cli_is_silent_and_exits_zero_on_garbage(self):
        r = CliRunner().invoke(app, ["hook"], input="not json")
        assert r.exit_code == 0 and r.output == ""

    def test_cli_is_silent_without_any_config(self, clone, monkeypatch, tmp_path):
        repo, _ = clone
        monkeypatch.delenv("LEDGER_URL", raising=False)
        monkeypatch.setenv("LEDGER_CONFIG", str(tmp_path / "missing.yml"))
        monkeypatch.chdir(tmp_path)
        r = CliRunner().invoke(app, ["hook"], input=json.dumps(self.payload(repo)))
        assert r.exit_code == 0 and r.output == ""


class TestNaming:
    def test_the_brief_is_asked_about_this_repo_and_branch(self, clone):
        repo, _ = clone
        seen = []
        h.handle({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(repo)},
                 lambda r, ref: seen.append((r, ref)) or brief("proceed"))
        assert seen == [("orders", "feat/abc-1-x")]

    def test_a_worktree_is_named_after_its_main_clone(self, clone, tmp_path):
        repo, run = clone
        wt = tmp_path / "orders-wt"
        run("worktree", "add", "-q", "-b", "feat/abc-2-y", str(wt))
        assert h.repo_and_ref(str(wt)) == ("orders", "feat/abc-2-y")


class TestSessionStart:
    """The trigger that does not depend on anyone remembering to ask."""

    def test_outside_the_estate_it_says_nothing(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle_session_start({"cwd": str(repo)}, lambda *_: brief("decision_needed"),
                                     root=str(tmp_path / "somewhere-else"))
        assert out is None

    def test_inside_the_estate_it_briefs_the_branch_it_opens_on(self, clone, tmp_path):
        repo, _ = clone
        seen = []
        out = h.handle_session_start(
            {"cwd": str(repo)},
            lambda r, ref: seen.append((r, ref)) or brief("review_with", event_id="e-1"),
            root=str(tmp_path))
        assert seen == [("orders", "feat/abc-1-x")]
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "SessionStart"
        assert "REVIEW_WITH" in spec["additionalContext"] and "event e-1" in spec["additionalContext"]
        assert "feedback" in spec["additionalContext"]

    def test_all_clear_is_a_single_line(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle_session_start({"cwd": str(repo)}, lambda *_: brief("proceed"),
                                     root=str(tmp_path))
        text = out["hookSpecificOutput"]["additionalContext"]
        assert "\n" not in text and "no cross-repo work" in text

    def test_it_never_approves_or_asks(self, clone, tmp_path):
        """Session start is context only: nothing is being run yet."""
        repo, _ = clone
        out = h.handle_session_start({"cwd": str(repo)}, lambda *_: brief("decision_needed"),
                                     root=str(tmp_path))
        assert "permissionDecision" not in out["hookSpecificOutput"]

    def test_a_crashing_brief_means_no_output(self, clone, tmp_path):
        repo, _ = clone
        def boom(*_):
            raise RuntimeError("db down")
        assert h.handle_session_start({"cwd": str(repo)}, boom, root=str(tmp_path)) is None

    def test_the_cli_is_silent_without_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LEDGER_CONFIG", str(tmp_path / "missing.yml"))
        monkeypatch.delenv("LEDGER_URL", raising=False)
        r = CliRunner().invoke(app, ["session-start"], input='{"cwd": "/tmp"}')
        assert r.exit_code == 0 and r.output == ""


class TestTheDeveloperSeesIt:
    """On real use the verdict reached the agent's context and the developer
    never saw it -- "I don't see a verdict or anything"."""

    def test_a_verdict_is_shown_to_the_person(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle_session_start({"cwd": str(repo)}, lambda *_: brief("review_with"),
                                     root=str(tmp_path))
        assert out["systemMessage"].startswith("Ledger · orders @ feat/abc-1-x: REVIEW_WITH")
        assert "because" in out["systemMessage"]

    def test_all_clear_is_shown_too_so_you_know_it_ran(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle_session_start({"cwd": str(repo)}, lambda *_: brief("proceed"),
                                     root=str(tmp_path))
        assert "nothing cross-repo found" in out["systemMessage"]


class TestBranchStart:
    """The moment work acquires an identity.

    From a real session: the hook ran at session start on `master`, the branch
    was created twenty minutes later, and nothing asked the ledger again. A
    branch name carries the ticket, and the ticket is what the rest of the
    estate is working under.
    """

    def test_it_recognises_a_branch_being_created(self):
        assert h.branch_started("git checkout -b feat/abc-9-x") == "feat/abc-9-x"
        assert h.branch_started("cd repo && git switch -c fix/abc-9-y") == "fix/abc-9-y"
        assert h.branch_started("git -C ~/src/orders checkout -b feat/abc-9-z") == "feat/abc-9-z"

    def test_switching_to_an_existing_branch_is_not_a_start(self):
        """Only creation. Moving between branches all day is not an event."""
        assert h.branch_started("git checkout master") is None
        assert h.branch_started("git switch main") is None
        assert h.branch_started("git branch -d old") is None

    def test_it_asks_about_the_branch_being_created_not_the_current_one(self, clone, tmp_path):
        repo, _ = clone
        asked = []
        out = h.handle({"tool_name": "Bash", "cwd": str(repo),
                        "tool_input": {"command": "git checkout -b feat/abc-9-new"}},
                       lambda *_: brief("proceed"),
                       lambda r, ref: (asked.append((r, ref)), brief("review_with"))[1])
        assert asked == [(repo.name, "feat/abc-9-new")]
        assert "before you start `feat/abc-9-new`" in out["hookSpecificOutput"]["additionalContext"]

    def test_starting_a_branch_never_blocks(self, clone, tmp_path):
        """There is no diff yet to judge, and an interruption at `checkout -b`
        is the kind of thing people disable the hook over."""
        repo, _ = clone
        out = h.handle({"tool_name": "Bash", "cwd": str(repo),
                        "tool_input": {"command": "git checkout -b feat/abc-9-new"}},
                       lambda *_: brief("proceed"), lambda *_: brief("decision_needed"))
        assert "permissionDecision" not in out["hookSpecificOutput"]

    def test_nothing_in_flight_says_nothing(self, clone, tmp_path):
        repo, _ = clone
        assert h.handle({"tool_name": "Bash", "cwd": str(repo),
                         "tool_input": {"command": "git checkout -b feat/abc-9-new"}},
                        lambda *_: brief("proceed"), lambda *_: brief("proceed")) is None

    def test_without_a_new_branch_brief_it_is_not_a_trigger(self, clone, tmp_path):
        """A caller that cannot answer about a branch that does not exist yet
        simply does not fire here."""
        repo, _ = clone
        assert h.handle({"tool_name": "Bash", "cwd": str(repo),
                         "tool_input": {"command": "git checkout -b feat/abc-9-new"}},
                        lambda *_: brief("decision_needed")) is None

    def test_a_push_is_still_judged_as_a_push(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle({"tool_name": "Bash", "cwd": str(repo),
                        "tool_input": {"command": "git push -u origin HEAD"}},
                       lambda *_: brief("decision_needed"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


CONTEXT = {"tickets": ["ABC-7"],
           "inflight": [{"ticket": "ABC-7", "repo": "orders", "ref": "feat/abc-7-x", "where": None},
                        {"ticket": "ABC-7", "repo": "billing", "ref": "sha1", "where": "stage",
                         "author": "bob"}],
           "contracts": [{"surface": "table", "name": "order_line",
                          "parties": [{"repo": "billing", "uses": "publishes", "how": "literal"}],
                          "documented_in": [{"title": "Order line reference", "behind": 4,
                                             "url": "https://wiki/1", "path": "p.md"}]}],
           "decisions": [{"repo": "billing", "path": "docs/adr/0001.md", "status": "accepted",
                          "gate_hit": "still lacks a backfill"}]}
EMPTY = {"tickets": [], "inflight": [], "contracts": [], "decisions": []}


class TestTheTaskIsStated:
    """UserPromptSubmit: the earliest moment, and the only one that always
    happens. A real session never pushed, and its branch never had a commit."""

    def test_it_answers_from_the_words_of_the_request(self, clone, tmp_path):
        repo, _ = clone
        out = h.handle_user_prompt({"cwd": str(repo), "prompt": "Fix ABC-7 in order_line"},
                                   lambda _t, **_k: CONTEXT, root=str(tmp_path))
        text = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "ABC-7: 2 change(s) in 2 repo(s)" in text and "billing, orders" in text
        assert "merged to an integration branch, not released" in text
        assert "feat/abc-7-x" in text                      # the one not integrated, by name
        assert "`order_line`" in text and "4 commit(s) behind" in text
        assert "OPEN GATE" in text

    def test_nothing_known_says_nothing(self, clone, tmp_path):
        repo, _ = clone
        assert h.handle_user_prompt({"cwd": str(repo), "prompt": "Fix the thing"},
                                    lambda _t, **_k: EMPTY, root=str(tmp_path)) is None

    def test_a_one_word_prompt_is_not_a_task(self, clone, tmp_path):
        repo, _ = clone
        asked = []
        h.handle_user_prompt({"cwd": str(repo), "prompt": "yes"},
                             lambda t, **_k: (asked.append(t), CONTEXT)[1], root=str(tmp_path))
        assert asked == []

    def test_outside_the_estate_it_says_nothing(self, clone, tmp_path):
        repo, _ = clone
        assert h.handle_user_prompt({"cwd": str(repo), "prompt": "Fix ABC-7 in order_line"},
                                    lambda _t, **_k: CONTEXT, root="/somewhere/else") is None

    def test_a_failure_is_silence_not_an_error(self, clone, tmp_path):
        repo, _ = clone
        def boom(_t, **_k):
            raise RuntimeError("index down")
        assert h.handle_user_prompt({"cwd": str(repo), "prompt": "Fix ABC-7 in order_line"},
                                    boom, root=str(tmp_path)) is None


class TestThePlanIsPresented:
    """The last moment a change is cheap. In a real session the plan was
    written 14 minutes in and the branch came 5 minutes after it."""

    def test_it_reads_the_plan(self, clone, tmp_path):
        repo, _ = clone
        seen = []
        out = h.handle({"tool_name": "ExitPlanMode", "cwd": str(repo),
                        "tool_input": {"plan": "# Plan\nChange order_line for ABC-7"}},
                       lambda *_: brief("proceed"), None,
                       lambda t, **_k: (seen.append(t), CONTEXT)[1])
        assert "order_line" in seen[0]
        assert "on what this plan touches" in out["hookSpecificOutput"]["additionalContext"]

    def test_it_never_blocks_the_plan(self, clone, tmp_path):
        """The person is already being asked to approve it; a second gate on
        the same decision is not a safeguard."""
        repo, _ = clone
        out = h.handle({"tool_name": "ExitPlanMode", "cwd": str(repo),
                        "tool_input": {"plan": "Change order_line"}},
                       lambda *_: brief("decision_needed"), None, lambda _t, **_k: CONTEXT)
        assert "permissionDecision" not in out["hookSpecificOutput"]

    def test_a_plan_touching_nothing_says_nothing(self, clone, tmp_path):
        repo, _ = clone
        assert h.handle({"tool_name": "ExitPlanMode", "cwd": str(repo),
                         "tool_input": {"plan": "Rename a local variable"}},
                        lambda *_: brief("proceed"), None, lambda _t, **_k: EMPTY) is None

    def test_without_a_context_reader_a_plan_is_not_a_trigger(self, clone, tmp_path):
        repo, _ = clone
        assert h.handle({"tool_name": "ExitPlanMode", "cwd": str(repo),
                         "tool_input": {"plan": "Change order_line"}},
                        lambda *_: brief("decision_needed")) is None


class TestFreshness:
    """An answer that does not carry its age cannot be told apart from a
    fresh one. On a real estate a merged sibling stayed invisible for fifteen
    hours and a brief flipped from review_with to proceed with nothing else
    changed."""

    def at(self, seconds_ago):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()

    def brief_synced(self, seconds_ago, rec="review_with"):
        b = brief(rec)
        b["index"] = dict(b.get("index") or {}, synced_at=self.at(seconds_ago))
        return b

    def test_a_fresh_answer_says_so_quietly(self):
        assert h.freshness(self.brief_synced(30)) == "in-flight state: synced just now"
        assert h.freshness(self.brief_synced(600)) == "in-flight state: synced 10 min ago"

    def test_a_stale_answer_says_unknown_not_false(self):
        text = h.freshness(self.brief_synced(15 * 3600))
        assert "STALE" in text and "15h" in text and "unknown rather than false" in text

    def test_the_person_at_the_keyboard_sees_stale_in_the_headline(self):
        assert "STALE INDEX" in h.headline(self.brief_synced(15 * 3600))
        assert "STALE" not in h.headline(self.brief_synced(120))

    def test_it_reaches_the_lines_an_agent_reads(self):
        assert "synced 10 min ago" in h.summarise(self.brief_synced(600))

    def test_the_all_clear_carries_it_and_stays_one_line(self):
        text = h.session_context(self.brief_synced(600, rec="proceed"))
        assert "synced 10 min ago" in text and "\n" not in text

    def test_a_live_read_has_no_age_to_report(self):
        """Read for this call: there is no stored state to be stale."""
        assert h.freshness(brief("proceed")) is None

    def test_an_unreadable_stamp_is_not_a_guess(self):
        b = brief("proceed")
        b["index"] = {"synced_at": "not-a-date"}
        assert h.freshness(b) is None

    def test_context_from_a_request_carries_it_too(self):
        got = dict(CONTEXT, index={"synced_at": self.at(15 * 3600)})
        text = h.context_text(got, lead="Ledger:")
        assert "STALE" in text


class TestRepoIdentity:
    """A directory name is whatever the person who cloned it typed. A real
    estate had `acme-orchestrator-service` checked out as
    `orcestrator_service`, typo and all."""

    def clone_named(self, tmp_path, directory, origin):
        import subprocess
        repo = tmp_path / directory
        repo.mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                       check=True, capture_output=True)
        run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (repo / "f.txt").write_text("x")
        run("add", "."); run("commit", "-qm", "init")
        if origin:
            run("remote", "add", "origin", origin)
        return repo

    def test_the_remote_names_the_repository_not_the_directory(self, tmp_path):
        repo = self.clone_named(tmp_path, "orcestrator_service",
                                "https://github.com/Acme/acme-orchestrator-service.git")
        assert h.repo_and_ref(str(repo)) == ("acme-orchestrator-service", "main")

    def test_an_ssh_remote_names_it_too(self, tmp_path):
        repo = self.clone_named(tmp_path, "whatever", "git@github.com:Acme/orders.git")
        assert h.repo_and_ref(str(repo))[0] == "orders"

    def test_a_clone_with_no_remote_falls_back_to_its_directory(self, tmp_path):
        repo = self.clone_named(tmp_path, "local-only", None)
        assert h.repo_and_ref(str(repo))[0] == "local-only"


class TestPromptsNobodyWrote:
    """A coding tool replays tool output, task notifications and system
    reminders through the same hook. One was 13,000 characters of somebody
    else's log, from which the ledger cheerfully matched six contracts and
    five decisions, none of them the task."""

    def test_a_task_notification_is_not_a_task(self):
        assert h.machine_generated("<task-notification>\n<task-id>abc</task-id>")
        assert h.machine_generated("  <system-reminder>do the thing</system-reminder>")
        assert h.machine_generated("<command-message>review</command-message>")

    def test_what_a_person_types_is(self):
        for p in ("Continue XY-234: the signature should not hold the invoice",
                  "yes, open PR's to stage",
                  "<-- this is just how I write arrows"):
            assert not h.machine_generated(p)

    def test_the_hook_stays_silent_on_one(self, clone, tmp_path):
        repo, _ = clone
        asked = []
        out = h.handle_user_prompt(
            {"cwd": str(repo), "prompt": "<task-notification>\n" + "order_line " * 400},
            lambda t, **_k: (asked.append(t), CONTEXT)[1], root=str(tmp_path))
        assert out is None and asked == [], "it should not even be asked"


class TestTheBriefSeparatesWhatCannotBeFoundLocally:
    """Measured failure: a brief for `acme-core XY-562` carried three facts.
    Two were findable by grepping the checkout and the session rediscovered
    both by hand; the third -- `order_refund_row` merged to `stage` and not
    released -- was the only one that needed the ledger, and it was never
    mentioned again. It had been one bullet among bullets.
    """

    def brief(self):
        return {
            "subject": {"repo": "core", "ref": "XY-562"},
            "recommendation": "review_with", "confidence": "high",
            "reasons": ["1 change(s) in 1 other repo(s) share this ticket: voice-bot",
                        "table `order_refund_row` is also touched on `stage` in this "
                        "repository: merged there, not released, and not on this branch"],
            "not_local": ["table `order_refund_row` is also touched on `stage` in this "
                          "repository: merged there, not released, and not on this branch"],
            "impacts": [{"repo": "voice-bot", "ref": "feat/xy-562-result-code"}],
            "contracts": [], "decisions": [], "field_breaks": [],
        }

    def test_it_gets_its_own_block_with_something_to_do(self):
        out = h.summarise(self.brief())
        head, rest = out.split("\n", 1)
        assert rest.startswith("NOT VISIBLE FROM THIS REPOSITORY")
        assert "order_refund_row" in rest.split("Say what each one")[0]
        assert "in flight: voice-bot feat/xy-562-result-code" in rest.split("Say what each one")[0]

    def test_it_is_not_also_repeated_as_a_reason(self):
        out = h.summarise(self.brief())
        assert out.count("order_refund_row") == 1

    def test_a_locally_findable_reason_still_appears(self):
        out = h.summarise(self.brief())
        assert "1 change(s) in 1 other repo(s) share this ticket: voice-bot" in out

    def test_nothing_non_local_means_no_block(self):
        b = self.brief()
        b["not_local"], b["impacts"] = [], []
        b["reasons"] = ["no cross-repo work shares this ticket"]
        assert "NOT VISIBLE" not in h.summarise(b)


class TestAnInFlightItemStandsOnItsOwn:
    """A branch name and an author is a pointer. Real feedback, after the
    branch's author checked a review built on one: "The brief gave only a
    branch name and author... The reviewing model read the commit list...
    and cited it as proof" of the opposite of what the branch does."""

    ITEM = {"repo": "leads", "ref": "xy-525-announce-on-crm-link", "author": "rg",
            "sha": "e526d0e11aa2",
            "how": "shares XY-525, which this diff names and the branch name does not",
            "effect": "starts publishing topic `intake.drafts.v1`; removes field(s) `document_id`",
            "relation": "it produces → you consume topic `intake.drafts.v1`; field(s) crossing: "
                        "`document_id`",
            "evidence": "unmerged @e526d0e11aa2; summarised by Ledger from its diff against "
                        "`main` (names and directions, never code). You have not read it: a "
                        "branch name or commit title is not evidence — trace the mechanism "
                        "before citing it",
            "ask": "Does `xy-525-announce-on-crm-link` change the presence, format or meaning "
                   "of `document_id` that this change reads from `intake.drafts.v1`? Ask rg."}

    def text(self, **kw):
        return h.summarise(brief("review_with", impacts=[dict(self.ITEM, **kw)]))

    def test_it_says_what_the_other_change_does_and_how_it_faces_this_one(self):
        t = self.text()
        assert "    does: starts publishing topic `intake.drafts.v1`" in t
        assert "    relation: it produces → you consume topic `intake.drafts.v1`" in t
        assert "    evidence: unmerged @e526d0e11aa2" in t
        assert "    ask: Does `xy-525-announce-on-crm-link` change" in t

    def test_an_item_is_its_heading_and_at_most_four_short_lines(self):
        t = self.text(effect="x " * 400)
        block = t.split("- in flight: ")[1].split("\nSay what each one means")[0].splitlines()
        assert len(block) == 5
        assert all(len(line) <= h.DETAIL_WIDTH + 4 for line in block[1:])

    def test_an_item_without_them_reads_as_it_always_did(self):
        t = h.summarise(brief("review_with", impacts=[
            {"repo": "billing", "ref": "feat/abc-1-y", "where": "stage", "author": "bob"}]))
        assert "- in flight: billing feat/abc-1-y (in stage) by bob\n" in t
        assert "does:" not in t and "evidence:" not in t

    def test_session_start_carries_them_too(self):
        t = h.session_context(brief("review_with", impacts=[self.ITEM]))
        assert "relation: it produces → you consume" in t

    def test_prompt_time_context_says_what_in_flight_work_does(self):
        got = {"inflight": [{"ticket": "XY-525", "repo": "leads",
                             "ref": "xy-525-announce-on-crm-link",
                             "effect": "starts publishing topic `intake.drafts.v1`"}]}
        t = h.context_text(got, lead="Ledger:")
        assert "leads xy-525-announce-on-crm-link does: starts publishing topic" in t
        assert "a branch name or commit title is not evidence" in t

    def test_prompt_time_context_without_effects_is_unchanged(self):
        got = {"inflight": [{"ticket": "XY-525", "repo": "leads", "ref": "xy-525-x"}]}
        assert "does:" not in h.context_text(got, lead="Ledger:")

    BEHAVIOUR = {"text": "Closing states now clear the LEAD flag; an invoice reopened into review "
                         "re-enters LEAD as a new cycle.",
                 "touches": ["LEAD", "reenterOnReopen"], "confidence": "medium",
                 "partial": True, "model": "fake", "prompt_version": "b1"}

    def test_a_model_summary_is_shown_and_labelled_as_one(self):
        t = self.text(behaviour=self.BEHAVIOUR)
        assert ("    behaviour (model summary of its diff, not verified; confidence medium, "
                "diff was cut): Closing states now clear the LEAD flag") in t
        assert "    does: starts publishing topic" in t

    def test_it_replaces_a_does_line_that_only_says_the_diff_names_nothing(self):
        """The case the summary exists for: a branch whose whole point is
        behaviour, naming no topic, route or table."""
        t = self.text(behaviour=self.BEHAVIOUR,
                      effect="names no topic, route or table in its diff (3 file(s) changed) — "
                             "its effect is in behaviour, which this cannot summarise")
        assert "does:" not in t and "behaviour (model summary" in t

    def test_without_a_summary_the_does_line_still_says_the_diff_names_nothing(self):
        t = self.text(effect="names no topic, route or table in its diff (3 file(s) changed)")
        assert "    does: names no topic, route or table" in t

    def test_an_empty_summary_is_not_shown(self):
        assert "behaviour" not in self.text(behaviour={"text": "", "confidence": "low"})

    def test_an_item_stays_within_five_lines_and_keeps_the_whole_sentence(self):
        t = self.text(behaviour=dict(self.BEHAVIOUR, text="x" * 320))
        block = t.split("- in flight: ")[1].split("\nSay what each one means")[0].splitlines()
        assert len(block) == 6 and ("x" * 320) in t

    def test_prompt_time_context_carries_the_summary_with_its_label(self):
        got = {"inflight": [{"ticket": "XY-515", "repo": "core", "ref": "xy-515-lead-exit",
                             "behaviour": self.BEHAVIOUR}]}
        t = h.context_text(got, lead="Ledger:")
        assert "core xy-515-lead-exit behaviour (model summary of its diff, not verified" in t


class TestTheCommandsOwnDirectory:
    """Real feedback: `cd ../acme-leads && gh pr create` was briefed about the
    repository the session had been in before, and every line was unrelated."""

    def test_a_leading_cd_moves_the_question(self, clone, tmp_path):
        repo, run = clone
        other = tmp_path / "leads"
        other.mkdir()
        sub = lambda *a: subprocess.run(["git", *a], cwd=other, check=True, capture_output=True)
        sub("init", "-q", "-b", "main")
        sub("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
        sub("checkout", "-q", "-b", "sk-9-y")
        seen = []
        h.handle({"tool_name": "Bash", "cwd": str(repo),
                  "tool_input": {"command": f"cd {other} && gh pr create --fill"}},
                 lambda r, ref, **kw: seen.append((r, ref)) or brief("proceed"))
        assert seen == [("leads", "sk-9-y")]

    def test_git_dash_C_too(self, tmp_path):
        assert h.effective_cwd("git -C ../leads push", str(tmp_path / "core")) == str(tmp_path / "leads")


class TestThePullRequestNamesItsBase:
    @pytest.mark.parametrize("cmd,base", [
        ("gh pr create --base stage --fill", "stage"),
        ("gh pr create -B stage", "stage"),
        ("gh pr create --title x --base=develop", "develop"),
        ("glab mr create --target-branch stage", "stage"),
        ("gh pr create --fill", None),
        ("git push -u origin feat/x", None),
    ])
    def test_it_is_read_from_the_command(self, cmd, base):
        assert h.pr_base(cmd) == base

    def test_it_reaches_the_brief(self, clone):
        repo, _ = clone
        seen = []
        h.handle({"tool_name": "Bash", "cwd": str(repo),
                  "tool_input": {"command": "gh pr create --base stage --fill"}},
                 lambda r, ref, base=None: seen.append(base) or brief("proceed"))
        assert seen == ["stage"]


def ranked(**kw):
    r = {"lead": [], "also": {"count": 0, "by_kind": {}, "repos": {}, "keys": []},
         "repeated": {}, "nothing": True}
    r.update(kw)
    return r


class TestRanked:
    """A brief leads with the few items that have a reason to be there and
    says the rest in one line. Measured on a week of real briefs: 9% of what
    was listed was useful."""

    LEAD = {"repo": "billing", "ref": "feat/abc-1-y", "author": "bob",
            "how": "same author, pushed around the same time; no ticket links them"}
    WEAK = {"repo": "reports", "ref": "stage", "author": "ci-bot[bot]"}

    def test_the_lead_is_in_full_and_the_rest_counted(self):
        b = brief("review_with", impacts=[self.LEAD, self.WEAK], actors=["bob", "carol"],
                  ranked=ranked(nothing=False,
                                lead=[{"key": "impact:billing:feat/abc-1-y", "kind": "impact",
                                       "check": "how to check: read billing branch `feat/abc-1-y`"}],
                                also={"count": 3, "by_kind": {"impact": 1, "decision": 2},
                                      "repos": {"reports": 1}, "keys": []}))
        text = h.summarise(b)
        assert "in flight: billing feat/abc-1-y by bob — same author" in text
        assert "how to check: read billing branch" in text
        assert "reports stage" not in text
        assert "also seen, not linked to your diff: 2 decision(s), 1 in-flight change(s) (reports)" in text
        assert "involve: bob" in text and "carol" not in text, "only people the lead names"

    def test_nothing_strong_says_so_and_never_promotes_a_weak_item(self):
        """The one regression a replay found: a single weak item, shown alone,
        read as the finding, and the agent sent people to chase it."""
        b = brief("review_with", impacts=[self.WEAK],
                  ranked=ranked(also={"count": 1, "by_kind": {"impact": 1}, "repos": {"reports": 1},
                                      "keys": []}))
        text = h.summarise(b)
        assert "nothing in flight or pending that this diff touches" in text
        assert "reports stage" not in text
        assert h.response(b) is None, "a push with nothing to say says nothing"
        assert "nothing in flight or pending that this diff touches" in h.session_context(b)

    def test_a_stop_still_says_why(self):
        b = brief("decision_needed", reasons=["removes `x` from topic `y`"], ranked=ranked())
        assert "removes `x`" in h.summarise(b)
        assert h.response(b)["hookSpecificOutput"]["permissionDecision"] == "ask"

    def test_repeats_are_one_line(self):
        b = brief("review_with", ranked=ranked(nothing=False,
                                               repeated={"impact:billing:feat/abc-1-y": "2026-10-14T08:00"}))
        assert "already shown for this branch since 2026-10-14, unchanged, not repeated: billing:feat/abc-1-y" \
            in h.summarise(b)

    def test_without_ranking_everything_is_shown_as_before(self):
        b = brief("review_with", impacts=[self.WEAK])
        assert "reports stage" in h.summarise(b)


class TestSessionStop:
    """At the end of a turn, the agent's reply after each ledger answer is sent,
    so the items it set aside can be recorded as automatic noise."""

    def transcript(self, tmp_path, *texts):
        rows = [{"type": "attachment", "attachment": {"type": "hook_additional_context",
                 "content": ["Ledger brief ... (event 5afea85a-5471-4484-b539-9680077a6713) ..."]}},
                {"type": "user", "message": {"content": "review this"}}]
        rows += [{"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}} for t in texts]
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows))
        return p

    def test_only_the_agents_words_after_the_answer_leave(self, tmp_path):
        p = self.transcript(tmp_path, "The leads branch does not apply.")
        got = h.replies_to_answers(p.read_text().splitlines())
        assert got == {"5afea85a-5471-4484-b539-9680077a6713": "The leads branch does not apply."}

    def test_sent_once_until_the_reply_grows(self, tmp_path):
        p = self.transcript(tmp_path, "first")
        sent = []
        pay = {"cwd": str(tmp_path), "transcript_path": str(p)}
        stamps = tmp_path / "stamps"
        for _ in range(2):
            h.handle_session_stop(pay, lambda e, r: sent.append(r), str(tmp_path), stamps=stamps)
        assert sent == ["first"]
        p = self.transcript(tmp_path, "first", "and more")
        h.handle_session_stop(pay, lambda e, r: sent.append(r), str(tmp_path), stamps=stamps)
        assert sent[-1] == "first\n\nand more"

    def test_outside_the_estate_nothing(self, tmp_path):
        p = self.transcript(tmp_path, "x")
        assert h.handle_session_stop({"cwd": "/elsewhere", "transcript_path": str(p)},
                                     lambda e, r: 1 / 0, str(tmp_path)) == 0
