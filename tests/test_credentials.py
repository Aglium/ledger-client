"""Reading a forge credential from the store git already uses.

Why this exists, from the installation it was written for: discovery said
`discover=no-credentials` every six hours on a machine where `git fetch` had
been authenticating to both forges for weeks. Nothing had broken. The API
tokens were supplied inline for one `aglium-ledger mirrors` run and never written
anywhere, so the scheduled discovery added later had never had them -- while
git's own credential store had a working credential the whole time.

Every test here isolates git's configuration, because otherwise the answer
depends on whose laptop is running the suite: the first version of this
asserted "no credential" and failed on the machine that motivated it.
"""

from __future__ import annotations

import pytest

from ledger.core.credentials import api_host, from_git

HELPER = ('[credential]\n\thelper = "!f() { test \\"$1\\" = get && echo username=bot '
          '&& echo password=sekret; }; f"\n')


@pytest.fixture
def no_helper(tmp_path, monkeypatch):
    """A git that has been told nothing about credentials."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A git whose helper always answers."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text(HELPER)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


class TestAskingGit:
    def test_a_configured_helper_answers(self, helper):
        assert from_git("example.test") == ("bot", "sekret")

    def test_no_helper_is_no_credential_not_an_error(self, no_helper):
        assert from_git("example.test") is None

    def test_a_username_from_the_config_is_passed_through(self, helper):
        """`store` returns nothing without one, and a keychain may hold
        several for one host."""
        assert from_git("example.test", username="someone") == ("bot", "sekret")

    def test_no_host_is_not_asked_about(self, helper):
        assert from_git("") is None

    def test_it_never_waits_for_a_person(self, no_helper):
        """This runs inside a sync loop. A helper that wants to prompt must
        fail rather than hold the loop open."""
        assert from_git("example.test", timeout=5.0) is None


class TestWhichHostToAsk:
    def test_the_known_forges(self):
        assert api_host("github") == "github.com"
        assert api_host("bitbucket") == "bitbucket.org"

    def test_a_self_hosted_forge_is_its_own_host(self):
        assert api_host("github", "https://git.example.com/api/v3") == "git.example.com"

    def test_an_unknown_kind_has_no_host_rather_than_a_wrong_one(self):
        assert api_host("gitlab") == ""


class TestWhatTheAdaptersDoWithIt:
    def cfg(self, tmp_path, body):
        from ledger.config import Config
        p = tmp_path / "l.yml"
        p.write_text("org: t\nproject_keys: [ABC]\n" + body)
        return Config.load(str(p))

    def forges(self, tmp_path):
        return self.cfg(tmp_path, "forges:\n  - kind: github\n    namespace: Acme\n"
                                  "    token_env: LEDGER_GITHUB_TOKEN\n")

    def token_of(self, adapter):
        """The adapter keeps its credential inside an httpx client."""
        return adapter._client.headers.get("authorization", "")

    def test_the_environment_wins(self, tmp_path, monkeypatch, helper):
        """The explicit answer, and the only one a server without a keychain
        will have."""
        from ledger import service
        monkeypatch.setenv("LEDGER_GITHUB_TOKEN", "from-env")
        [a] = service.forge_adapters(self.forges(tmp_path))
        assert "from-env" in self.token_of(a)

    def test_git_answers_when_the_environment_does_not(self, tmp_path, monkeypatch, helper):
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        [a] = service.forge_adapters(self.forges(tmp_path))
        assert "sekret" in self.token_of(a)

    def test_a_credential_for_somebody_else_is_refused(self, tmp_path, monkeypatch, helper):
        """The case this rule exists for: a developer machine holds a personal
        bitbucket.org login (`jdoe`) next to the work one, and the
        organisation's install is `jane.doe@example.com`. Acting as the
        wrong person against the organisation's API is worse than having no
        credential."""
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        cfg = self.cfg(tmp_path, "forges:\n  - kind: github\n    namespace: Acme\n"
                                 "    token_env: LEDGER_GITHUB_TOKEN\n"
                                 "    api_user: work@example.com\n")
        warned: list[str] = []
        assert service.forge_adapters(cfg, warn=warned.append) == []
        assert "belongs to bot" in warned[0] and "work@example.com" in warned[0]

    def test_the_right_person_is_accepted(self, tmp_path, monkeypatch, helper):
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        cfg = self.cfg(tmp_path, "forges:\n  - kind: github\n    namespace: Acme\n"
                                 "    token_env: LEDGER_GITHUB_TOKEN\n"
                                 "    api_user: bot\n")
        [a] = service.forge_adapters(cfg)
        assert "sekret" in self.token_of(a)

    def test_with_neither_it_says_both_places_it_looked(self, tmp_path, monkeypatch, no_helper):
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        warned: list[str] = []
        assert service.forge_adapters(self.forges(tmp_path), warn=warned.append) == []
        assert "LEDGER_GITHUB_TOKEN" in warned[0] and "github.com" in warned[0]

    def test_git_credentials_still_come_from_the_environment_only(self, tmp_path, monkeypatch,
                                                                  helper):
        """`forge_credentials` feeds `GIT_ASKPASS` for a clone, and git does
        not need to be handed back what it already knows. A secret of None
        means "let the helper answer", which is the configured behaviour."""
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        assert service.forge_credentials(self.forges(tmp_path)) == {}
