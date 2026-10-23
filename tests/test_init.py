"""A first config from the checkout a developer is already in."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from ledger import init as initial
from ledger.cli import app
from ledger.config import Config


class TestTheRemoteSaysWhichForge:
    @pytest.mark.parametrize("url", [
        "git@github.com:acme/orders.git",
        "https://github.com/acme/orders.git",
        "https://someone@github.com/acme/orders",
        "ssh://git@github.com/acme/orders.git",
    ])
    def test_every_spelling_of_one_remote(self, url):
        r = initial.parse_remote(url)
        assert (r.host, r.namespace, r.repo, r.kind) == ("github.com", "acme", "orders", "github")

    def test_bitbucket(self):
        assert initial.parse_remote("git@bitbucket.org:acme-ws/orders.git").kind == "bitbucket"

    def test_a_forge_with_no_adapter_is_named_not_guessed(self):
        r = initial.parse_remote("https://gitlab.com/acme/platform/orders.git")
        assert r.kind is None and r.namespace == "acme/platform" and r.repo == "orders"

    def test_something_that_is_not_a_remote(self):
        assert initial.parse_remote("") is None and initial.parse_remote("/srv/git/orders") is None


class TestTicketKeysComeFromWhatTheTeamWrites:
    def test_a_key_that_recurs_is_the_convention(self):
        texts = ["feat/abc-12-export", "ABC-14 fix totals", "fix/abc-9-rounding", "XYZ-1 one-off"]
        assert initial.guess_keys(texts) == ["ABC"]

    def test_things_shaped_like_a_key_are_not_one(self):
        texts = ["utf-8 everywhere", "bump python-3 image", "UTF-8 again", "utf-8 once more",
                 "sha-256 digests", "SHA-256", "sha-256 checks"]
        assert initial.guess_keys(texts) == []

    def test_a_placeholder_number_does_not_count(self):
        assert initial.guess_keys(["ABC-0 merge", "ABC-0 merge", "ABC-0 tidy"]) == []

    def test_one_text_votes_once(self):
        assert initial.guess_keys(["ABC-1 ABC-2 ABC-3 ABC-4"]) == []


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "PATH": __import__("os").environ["PATH"], "HOME": str(path)})


def checkout(root, name, remote, subjects):
    p = root / name
    p.mkdir(parents=True)
    git(p, "init", "-q", "-b", "main")
    git(p, "remote", "add", "origin", remote)
    for s in subjects:
        git(p, "commit", "-q", "--allow-empty", "-m", s)
    return p


@pytest.fixture
def estate(tmp_path):
    src = tmp_path / "src"
    orders = checkout(src, "orders", "git@github.com:acme/orders.git",
                      ["ABC-1 start", "ABC-2 totals", "ABC-3 rounding"])
    checkout(src, "billing", "git@github.com:acme/billing.git", ["ABC-4 invoices"])
    return orders


class TestThePlan:
    def test_it_is_derived_from_the_checkout_and_its_neighbours(self, estate):
        got = initial.plan(estate)
        assert got.config["org"] == "acme" and got.config["project_keys"] == ["ABC"]
        assert got.config["forges"] == [
            {"kind": "github", "namespace": "acme", "token_env": "LEDGER_GITHUB_TOKEN"}]
        assert got.config["local_root"] == str(estate.parent)
        assert got.config["mirror_root"].endswith("/acme")

    def test_a_subdirectory_of_the_checkout_will_do(self, estate):
        (estate / "app").mkdir()
        assert initial.plan(estate / "app").config["org"] == "acme"

    def test_outside_a_checkout_it_says_where_to_run_it(self, tmp_path):
        with pytest.raises(ValueError, match="not inside a git checkout"):
            initial.plan(tmp_path)

    def test_a_forge_without_an_adapter_configures_none_and_says_so(self, tmp_path):
        p = checkout(tmp_path / "src", "orders", "https://gitlab.com/acme/orders.git", ["x"])
        got = initial.plan(p)
        assert "forges" not in got.config and "mirror_root" not in got.config
        assert got.config["local_root"] == str(p.parent)
        assert any("gitlab.com has no adapter" in n for n in got.notes)

    def test_what_it_writes_is_a_config_that_loads(self, estate, tmp_path):
        out = tmp_path / "ledger.yml"
        out.write_text(initial.plan(estate).render())
        cfg = Config.load(str(out))
        assert cfg.org == "acme" and cfg.project_keys == ("ABC",) and cfg.forges[0].kind == "github"


class TestTheCommand:
    def test_it_writes_the_user_config_and_the_next_command_finds_it(self, estate, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("LEDGER_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(app, ["init", str(estate)])
        assert res.exit_code == 0 and "aglium-ledger mirrors" in res.output
        assert Config.load(None).org == "acme"

    def test_it_does_not_overwrite_a_config_unless_told(self, estate, tmp_path):
        out = tmp_path / "ledger.yml"
        out.write_text("org: mine\n")
        res = CliRunner().invoke(app, ["init", str(estate), "--out", str(out)])
        assert res.exit_code == 2 and out.read_text() == "org: mine\n"
        assert CliRunner().invoke(app, ["init", str(estate), "--out", str(out), "--force"]).exit_code == 0
        assert Config.load(str(out)).org == "acme"

    def test_dry_run_writes_nothing(self, estate, tmp_path):
        out = tmp_path / "ledger.yml"
        res = CliRunner().invoke(app, ["init", str(estate), "--out", str(out), "--dry-run"])
        assert res.exit_code == 0 and "org: acme" in res.output and not out.exists()
