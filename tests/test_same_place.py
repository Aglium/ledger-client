"""Open work in the same repository meets this change at a place, not a file.

Real: on one re-measure 39 of 51 same-repository items were noise. A service
registry or a translation catalogue every change edits, its own entry each;
two changes of one error module, each its own class; a port of the same commits
to another branch; a pull request stacked on this one.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from ledger import service
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.places import label, meet, places_of

NOW = datetime.now(timezone.utc)

SERVICES = ("services:\n"
            "    App\\Orders:\n        arguments: ['@db']\n"
            "    App\\Refunds:\n        arguments: ['@db']\n")
ERRORS = ("class NotFound(Exception):\n    code = 404\n\n\n"
          "class Conflict(Exception):\n    code = 409\n\n\n"
          "def handle(e):\n    log(e)\n    return e.code\n")


def git(repo, *a):
    when = (NOW - timedelta(hours=2)).isoformat()
    env = {"GIT_AUTHOR_NAME": "ann", "GIT_AUTHOR_EMAIL": "ann@example.com", "GIT_COMMITTER_NAME": "ann",
           "GIT_COMMITTER_EMAIL": "ann@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "HOME": str(repo.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                          env=env).stdout


def commit(repo, name, files, base="main"):
    git(repo, "checkout", "-qB", name, base)
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    service._DELTAS.clear()
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "config").mkdir()
    (repo / "config" / "services.yml").write_text(SERVICES)
    (repo / "errors.py").write_text(ERRORS)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    return repo


def brief(tmp_path, repo, ref="feat/mine"):
    root = tmp_path / "mirrors"
    if root.exists():
        subprocess.run(["rm", "-rf", str(root)], check=True)
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--mirror", str(repo), str(root / "core.git")], check=True)
    cfg = tmp_path / "ledger.yml"
    cfg.write_text(f"org: acme\nproject_keys: [SKY]\nmirror_root: {root}\n")
    vcs = LocalGitAdapter(root)
    return service.compute_brief(Config.load(cfg), [vcs], "core", ref,
                                 lambda repos: service.LiveIndex(vcs, repos, None))


def listed(b):
    return {i.ref: i for i in b.impacts if i.repo == "core"}


def test_each_its_own_service_entry_is_not_an_item(tmp_path, work):
    commit(work, "feat/mine", {"config/services.yml": SERVICES.replace(
        "    App\\Refunds:", "    App\\Invoices:\n        arguments: ['@db']\n    App\\Refunds:")})
    commit(work, "feat/theirs", {"config/services.yml": SERVICES + "    App\\Mail:\n        arguments: ['@smtp']\n"})
    assert "feat/theirs" not in listed(brief(tmp_path, work))


def test_the_same_service_entry_is_an_item_naming_it(tmp_path, work):
    commit(work, "feat/mine", {"config/services.yml": SERVICES.replace(
        "App\\Orders:\n        arguments: ['@db']", "App\\Orders:\n        arguments: ['@db', '@log']")})
    commit(work, "feat/theirs", {"config/services.yml": SERVICES.replace(
        "App\\Orders:\n        arguments: ['@db']", "App\\Orders:\n        arguments: ['@cache']")})
    got = listed(brief(tmp_path, work))
    assert "feat/theirs" in got
    assert "the same place in `services.yml` (`arguments`)" in got["feat/theirs"].how


def test_different_classes_of_one_module_are_not_an_item(tmp_path, work):
    commit(work, "feat/mine", {"errors.py": ERRORS.replace("code = 404", "code = 410")})
    commit(work, "feat/theirs", {"errors.py": ERRORS.replace("code = 409", "code = 422")})
    assert "feat/theirs" not in listed(brief(tmp_path, work))


def test_the_same_function_is_an_item(tmp_path, work):
    commit(work, "feat/mine", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")})
    commit(work, "feat/theirs", {"errors.py": ERRORS.replace("    return e.code\n", "    return e.code or 500\n")})
    got = listed(brief(tmp_path, work))
    assert "feat/theirs" in got and "`def handle(e)`" in got["feat/theirs"].how


def test_adding_after_the_same_line_is_a_note(tmp_path, work):
    commit(work, "feat/mine", {"config/services.yml": SERVICES + "    App\\Mail:\n        arguments: ['@smtp']\n"})
    commit(work, "feat/theirs", {"config/services.yml": SERVICES + "    App\\Sms:\n        arguments: ['@gw']\n"})
    b = brief(tmp_path, work)
    assert "feat/theirs" not in listed(b)
    assert any(r.startswith("note: adds next to the same lines as `feat/theirs`") for r in b.reasons)


def test_a_port_of_the_same_commits_is_not_an_item(tmp_path, work):
    change = {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")}
    commit(work, "feat/mine", change)
    git(work, "checkout", "-qb", "feat/port-mine", "main")
    (work / "unrelated.txt").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "unrelated first")
    git(work, "cherry-pick", git(work, "rev-parse", "feat/mine").strip())
    git(work, "checkout", "-q", "main")
    b = brief(tmp_path, work)
    assert "feat/port-mine" not in listed(b)
    assert any(r.startswith("note: the same work in another form") and "feat/port-mine" in r for r in b.reasons)


def test_a_change_stacked_on_this_one_is_not_an_item(tmp_path, work):
    commit(work, "feat/mine", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")})
    git(work, "checkout", "-qb", "feat/more", "feat/mine")
    (work / "errors.py").write_text((work / "errors.py").read_text().replace("return e.code", "return e.code or 1"))
    git(work, "add", "-A")
    git(work, "commit", "-qm", "more")
    git(work, "checkout", "-q", "main")
    b = brief(tmp_path, work)
    assert "feat/more" not in listed(b)


def test_a_crowded_file_alone_counts_only_where_places_cannot_be_read(tmp_path, work, monkeypatch):
    for n in range(service.SAME_REPO_HOT + 1):
        commit(work, f"feat/other-{n}", {"config/services.yml": SERVICES.replace(
            "App\\Orders:\n        arguments: ['@db']", f"App\\Orders:\n        arguments: ['@db{n}']")})
    commit(work, "feat/mine", {"config/services.yml": SERVICES.replace(
        "App\\Orders:\n        arguments: ['@db']", "App\\Orders:\n        arguments: ['@mine']")})
    # Unreadable places: the file is all there is, and too many share it.
    monkeypatch.setattr(service, "_compare_places", lambda *a, **k: None)
    assert not [r for r in listed(brief(tmp_path, work)) if r.startswith("feat/other-")]


class TestPlaces:
    def test_a_key_is_read_with_its_parents(self):
        after = "claim:\n  form:\n    title: T\n    name: X\n"
        got = places_of(after, "@@ -4 +4 @@\n-    name: N\n+    name: X")
        assert ("key", "claim:", "form:", "name") in got.at
        assert label(("key", "claim:", "form:", "name")) == "name"

    def test_a_method_under_its_opening_bracket(self):
        after = ("<?php\nclass S\n{\n    public function a($x)\n    {\n        $y = 11;\n"
                 "        return $y;\n    }\n}\n")
        got = places_of(after, "@@ -6 +6 @@\n-        $y = 1;\n+        $y = 11;")
        assert ("in", "class S", "public function a($x)") in got.at

    def test_imports_and_brackets_are_no_place(self):
        got = places_of("import os\nimport re\nx = {\n}\n", "@@ -1,0 +2 @@\n+import re")
        assert got.at == set()

    def test_two_changes_inside_one_function_meet_there(self):
        a = places_of("def f():\n    a = 2\n    b = 1\n", "@@ -2 +2 @@\n-    a = 1\n+    a = 2")
        b = places_of("def f():\n    a = 1\n    b = 3\n", "@@ -3 +3 @@\n-    b = 1\n+    b = 3")
        same, _after = meet(a, b)
        assert same == [("in", "def f():")]


def test_the_same_ticket_in_another_place_is_a_note(tmp_path, work):
    commit(work, "feat/SKY-3-mine", {"errors.py": ERRORS.replace("code = 404", "code = 410")})
    commit(work, "feat/SKY-3-theirs", {"errors.py": ERRORS.replace("code = 409", "code = 422")})
    b = brief(tmp_path, work, ref="feat/SKY-3-mine")
    assert "feat/SKY-3-theirs" not in listed(b)
    assert any(r.startswith("note: open in this repository under the same ticket")
               and "feat/SKY-3-theirs" in r for r in b.reasons)


def test_a_php_array_value_on_the_next_line_is_not_a_function():
    after = "<?php\nreturn [\n    'services' =>\n        ['a', 'b'],\n];\n"
    got = places_of(after, "@@ -4 +4 @@\n-        ['a'],\n+        ['a', 'b'],")
    assert all(p[0] != "in" for p in got.at)


class TestNoPlaceFromACommonLine:
    """Three lines every change has: a new entry's own keys, the closing line
    of a signature, and a block only re-wrapped."""

    REGISTRY = "PAGES = [\n    Page(\n        slug=\"a\",\n        path=\"/a\",\n    ),\n]\n"

    def add_page(self, name):
        after = self.REGISTRY.replace("]\n", f"    Page(\n        slug=\"{name}\",\n        path=\"/{name}\",\n    ),\n]\n")
        return places_of(after, "@@ -5,0 +6,4 @@\n" + "\n".join(
            "+" + line for line in after.splitlines()[5:9]))

    def test_two_new_entries_of_one_list_do_not_meet(self):
        same, after = meet(self.add_page("x"), self.add_page("y"))
        assert same == []
        assert after                                    # still a merge conflict to note

    def test_a_signature_s_closing_line_is_its_function(self):
        base = "class S:\n    def a(\n        self,\n    ) -> None:\n        pass\n"

        def new_method(name):
            after = base + f"\n    def {name}(\n        self,\n    ) -> None:\n        pass\n"
            return places_of(after, "@@ -5,0 +6,5 @@\n" + "\n".join(
                "+" + line for line in after.splitlines()[5:10]))
        a, b = new_method("b"), new_method("c")
        assert ("in", "class S:", "def b(") in a.at
        assert meet(a, b)[0] == []

    def test_whitespace_only_is_no_change(self):
        after = "class C {\n    function send() {\n        try { go(); } catch (E $e) {\n        }\n    }\n}\n"
        got = places_of(after, "@@ -3 +3,2 @@\n-        try { go(); } catch (E $e) {}\n"
                               "+        try { go(); } catch (E $e) {\n+        }")
        assert got.at == set()

    def test_the_same_edit_on_both_sides_is_not_a_meeting(self):
        after = "def f():\n    a = 2\n"
        a = places_of(after, "@@ -2 +2 @@\n-    a = 1\n+    a = 2")
        b = places_of(after, "@@ -2 +2 @@\n-    a = 1\n+    a = 2")
        assert meet(a, b)[0] == []
        c = places_of("def f():\n    a = 3\n", "@@ -2 +2 @@\n-    a = 1\n+    a = 3")
        assert meet(a, c)[0] == [("in", "def f():")]

    def test_a_call_rewritten_inside_a_function_is_still_that_function(self):
        after = "def f():\n    send(\n        to=1,\n    )\n"
        got = places_of(after, "@@ -2,3 +2,3 @@\n-    post(\n-        to=0,\n-    )\n"
                               "+    send(\n+        to=1,\n+    )")
        assert ("in", "def f():") in got.at


def test_a_squashed_copy_of_their_work_is_the_same_work(tmp_path, work):
    """Their branch is still open; its work reached `main` as one squashed
    commit, and this change is built on that `main`."""
    commit(work, "feat/theirs", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")})
    commit(work, "feat/theirs", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=3)\n")},
           base="feat/theirs")
    git(work, "merge", "-q", "--squash", "feat/theirs")
    git(work, "commit", "-qm", "Squashed theirs")
    commit(work, "feat/mine", {"errors.py": (work / "errors.py").read_text().replace(
        "return e.code", "return e.code or 500")})
    b = brief(tmp_path, work)
    assert "feat/theirs" not in listed(b)
    assert any(r.startswith("note: the same work in another form") and "feat/theirs" in r for r in b.reasons)


def test_a_port_with_one_commit_of_its_own_meets_only_where_that_commit_does(tmp_path, work):
    """Three of four commits are already this change's; the fourth is elsewhere."""
    ported = ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")
    commit(work, "feat/mine", {"errors.py": ported})
    commit(work, "feat/theirs", {"errors.py": ported.replace("code = 409", "code = 422")})
    b = brief(tmp_path, work)
    assert "feat/theirs" not in listed(b)


def test_prose_never_leads_a_conflict_in_code(tmp_path, work, monkeypatch):
    guide = "".join(f"Paragraph {n} of the guide.\n\n" for n in range(6))
    (work / "GUIDE.md").write_text(guide)
    git(work, "add", "-A"); git(work, "commit", "-qm", "guide")
    edited = guide.replace("of the guide", "of the new guide")
    commit(work, "feat/mine", {"GUIDE.md": edited,
                               "errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")})
    commit(work, "feat/docs", {"GUIDE.md": guide.replace("of the guide", "of the other guide")})
    commit(work, "feat/code", {"errors.py": ERRORS.replace("    return e.code\n", "    return 0\n")})
    monkeypatch.setattr(service, "SAME_REPO_SHOWN", 1)
    assert list(listed(brief(tmp_path, work))) == ["feat/code"]


def test_a_line_the_file_already_had_is_not_their_work_being_here(tmp_path, work):
    """Their only addition is a line this file has elsewhere: not the same work."""
    commit(work, "feat/theirs", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e)\n    return e.code\n")})
    commit(work, "feat/mine", {"errors.py": ERRORS.replace("    log(e)\n", "    log(e, level=2)\n")})
    b = brief(tmp_path, work)
    assert not any("same work" in r and "feat/theirs" in r for r in b.reasons)
