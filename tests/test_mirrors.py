"""The estate the server holds.

An engineer clones the one repository they work in. The server discovers the
organisation's repositories through the forge and mirrors them itself -- a
ledger that can only see what somebody already cloned answers questions about
exactly the repositories nobody needed answered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ledger.adapters.vcs.base import ForgeUnavailable, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.pipeline import mirrors as m


def origin(tmp_path, name="orders", branch="main"):
    """A bare repository standing in for one the forge would serve."""
    repo = tmp_path / "origins" / f"{name}.git"
    repo.parent.mkdir(exist_ok=True)
    work = tmp_path / "work" / name
    work.mkdir(parents=True)
    run = lambda *a, **k: subprocess.run(["git", "-C", str(k.get("cwd", work)), *a],
                                         check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "init", "-q", "-b", branch], check=True, capture_output=True)
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (work / "app.py").write_text("__tablename__ = 'order_line'\n")
    run("add", "."); run("commit", "-qm", "ABC-1 init")
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                   check=True, capture_output=True)
    return repo


class TestDiscover:
    def adapter(self, *names, host="github.com", source="github"):
        a = FakeAdapter(repos=[RepoRef(full_name=n, default_branch="main", host=host,
                                       url=f"https://{host}/{n}") for n in names])
        a.source = source
        return a

    def test_the_organisation_defines_the_estate(self):
        remotes, res = m.discover([self.adapter("Acme/orders", "Acme/billing")])
        assert [r.name for r in remotes] == ["billing", "orders"]
        assert remotes[0].full_name == "Acme/billing"
        assert remotes[0].clone_url == "https://github.com/Acme/billing.git"
        assert res.discovered == 2

    def test_an_unreachable_forge_is_reported_not_an_empty_organisation(self):
        """Treating it as "no repositories" shrinks the estate to whatever
        happened to answer, silently."""
        down = FakeAdapter(repos=[], unavailable=True)
        remotes, res = m.discover([down, self.adapter("Acme/orders")])
        assert [r.name for r in remotes] == ["orders"] and len(res.failed) == 1

    def test_two_organisations_with_the_same_repo_name_collide_loudly(self):
        remotes, res = m.discover([self.adapter("Acme/orders"), self.adapter("Other/orders")])
        assert len(remotes) == 1 and res.collisions
        assert "Acme/orders" in res.collisions[0] and "Other/orders" in res.collisions[0]

    def test_excluded_repos_are_not_part_of_the_estate(self):
        remotes, _ = m.discover([self.adapter("Acme/orders", "Acme/ledger")], exclude=("ledger",))
        assert [r.name for r in remotes] == ["orders"]

    def test_a_forge_that_gives_no_url_still_yields_a_clone_url(self):
        a = FakeAdapter(repos=[RepoRef(full_name="Acme/orders", default_branch="main",
                                       host="git.example.com")])
        remotes, _ = m.discover([a])
        assert remotes[0].clone_url == "https://git.example.com/Acme/orders.git"


class TestSync:
    def remote(self, path, name="orders"):
        return m.Remote(name=name, full_name=f"Acme/{name}", clone_url=str(path),
                        default_branch="main", host="github.com", source="github")

    def test_it_clones_what_is_missing_and_fetches_what_is_there(self, tmp_path):
        src = origin(tmp_path)
        root = tmp_path / "mirrors"
        first = m.sync(root, [self.remote(src)])
        assert first.cloned == ["orders"] and first.fetched == [] and not first.failed
        assert (root / "orders.git" / "HEAD").is_file()
        assert not (root / "orders.git" / "config").read_text().count("password")

        second = m.sync(root, [self.remote(src)])
        assert second.fetched == ["orders"] and second.cloned == []

    def test_a_mirror_has_no_working_tree(self, tmp_path):
        root = tmp_path / "mirrors"
        m.sync(root, [self.remote(origin(tmp_path))])
        assert not (root / "orders.git" / "app.py").exists()

    def test_the_index_can_read_a_mirror_it_just_made(self, tmp_path):
        root = tmp_path / "mirrors"
        m.sync(root, [self.remote(origin(tmp_path))])
        vcs = LocalGitAdapter(root)
        assert [r.full_name for r in vcs.list_repos()] == ["orders"]
        assert vcs.list_repos()[0].default_branch == "main"
        assert [b.name for b in vcs.list_branches("orders")] == ["main"]
        assert vcs.read_file("orders", "main", "app.py").startswith("__tablename__")
        # No working tree is not "nothing uncommitted" -- it is "cannot tell".
        assert vcs.working_changes("orders") is None

    def test_a_repository_that_cannot_be_cloned_is_named_not_swallowed(self, tmp_path):
        res = m.sync(tmp_path / "mirrors",
                     [m.Remote(name="gone", full_name="Acme/gone",
                               clone_url=str(tmp_path / "nope.git"), default_branch="main",
                               host="github.com", source="github")])
        assert [n for n, _ in res.failed] == ["gone"] and res.cloned == []

    def test_mirrors_the_organisation_no_longer_lists_are_reported(self, tmp_path):
        root = tmp_path / "mirrors"
        m.sync(root, [self.remote(origin(tmp_path))])
        res = m.sync(root, [])
        assert res.orphaned == ["orders.git"]

    def test_the_secret_is_never_written_to_disk(self, tmp_path):
        """It is handed to git for the life of one command, through askpass."""
        src = origin(tmp_path)
        root = tmp_path / "mirrors"
        m.sync(root, [self.remote(src)], credentials={"github": ("x-access-token", "s3cret")})
        on_disk = "\n".join(p.read_text(errors="ignore") for p in root.rglob("*")
                            if p.is_file() and p.stat().st_size < 100_000)
        assert "s3cret" not in on_disk


class TestWhereTheEstateComesFrom:
    """Precedence, because getting this wrong means the ledger silently
    answers about a different set of repositories than the one it claims."""

    def cfg(self, tmp_path, body):
        from ledger.config import Config
        p = tmp_path / "l.yml"
        p.write_text("org: o\nproject_keys: [ABC]\n" + body)
        return Config.load(str(p))

    def test_mirrors_and_clones_combine_rather_than_compete(self, tmp_path):
        """Both, in that order. A mirror root is the whole organisation; a
        developer's checkouts are the part of it that also has working trees,
        so uncommitted work stays visible. Neither alone is enough."""
        from ledger import service
        (tmp_path / "mirrors").mkdir()
        (tmp_path / "clones").mkdir()
        cfg = self.cfg(tmp_path, f"mirror_root: {tmp_path}/mirrors\nlocal_root: {tmp_path}/clones\n")
        assert service.build_adapters(cfg, None)[0].roots == [tmp_path / "clones",
                                                              tmp_path / "mirrors"]

    def test_an_explicit_local_root_still_wins(self, tmp_path):
        from ledger import service
        (tmp_path / "mirrors").mkdir()
        (tmp_path / "here").mkdir()
        cfg = self.cfg(tmp_path, f"mirror_root: {tmp_path}/mirrors\n")
        assert service.build_adapters(cfg, str(tmp_path / "here"))[0].root == tmp_path / "here"

    def test_a_missing_mirror_root_says_what_to_run(self, tmp_path):
        from ledger import service
        cfg = self.cfg(tmp_path, f"mirror_root: {tmp_path}/not-there\n")
        with pytest.raises(service.Unavailable, match="aglium-ledger mirrors"):
            service.build_adapters(cfg, None)

    def test_discovery_needs_a_credential_from_somewhere(self, tmp_path, monkeypatch):
        """A mirror root can only report what is already mirrored into it.

        Git's configuration is isolated, because the answer otherwise depends
        on whose machine runs the suite: this assertion passed everywhere
        except the laptop whose keychain holds a github.com credential, which
        is the laptop the fallback was written for.
        """
        from ledger import service
        monkeypatch.delenv("LEDGER_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
        cfg = self.cfg(tmp_path, "forges:\n  - kind: github\n    namespace: Acme\n"
                                 "    token_env: LEDGER_GITHUB_TOKEN\n")
        warned = []
        assert service.forge_adapters(cfg, warn=warned.append) == []
        assert warned and "LEDGER_GITHUB_TOKEN" in warned[0]

    def test_credentials_come_from_the_environment_only(self, tmp_path, monkeypatch):
        from ledger import service
        monkeypatch.setenv("LEDGER_GITHUB_TOKEN", "tok")
        cfg = self.cfg(tmp_path, "forges:\n  - kind: github\n    namespace: Acme\n"
                                 "    token_env: LEDGER_GITHUB_TOKEN\n")
        assert service.forge_credentials(cfg) == {"github": ("x-access-token", "tok")}


class TestDefaultBranch:
    def test_a_branch_name_with_a_slash_is_not_truncated(self, tmp_path):
        """`refs/heads/feat/x` is the branch `feat/x`. Taking the last segment
        reported `x`, which exists nowhere, and then every read failed."""
        src = origin(tmp_path, branch="release/v2")
        root = tmp_path / "mirrors"
        m.sync(root, [m.Remote(name="orders", full_name="Acme/orders", clone_url=str(src),
                               default_branch="release/v2", host="github.com", source="github")])
        assert LocalGitAdapter(root).list_repos()[0].default_branch == "release/v2"

    def test_the_mirror_takes_its_default_from_the_organisation(self, tmp_path):
        """A mirror of somebody's clone inherits whatever they had checked
        out. The forge is the authority on what the default branch is."""
        src = origin(tmp_path, branch="main")
        subprocess.run(["git", "-C", str(src), "symbolic-ref", "HEAD", "refs/heads/somebody-else"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(src), "branch", "somebody-else", "main"], capture_output=True)
        root = tmp_path / "mirrors"
        m.sync(root, [m.Remote(name="orders", full_name="Acme/orders", clone_url=str(src),
                               default_branch="main", host="github.com", source="github")])
        assert LocalGitAdapter(root).list_repos()[0].default_branch == "main"


class TestCredentialsInTheUrl:
    def test_an_email_username_is_encoded(self):
        """Atlassian accounts are email addresses. Unencoded, git reads
        `example.com@bitbucket.org` as the hostname and every clone from the
        workspace fails with "Bad hostname"."""
        got = m._url_with_user("https://bitbucket.org/ws/repo.git", "person@example.com")
        assert got == "https://person%40example.com@bitbucket.org/ws/repo.git"

    def test_a_url_that_already_names_a_user_is_left_alone(self):
        url = "https://someone@bitbucket.org/ws/repo.git"
        assert m._url_with_user(url, "other@example.com") == url

    def test_ssh_and_paths_carry_no_username(self):
        assert m._url_with_user("git@github.com:Acme/orders.git", "u") == "git@github.com:Acme/orders.git"
        assert m._url_with_user("/srv/mirrors/orders.git", "u") == "/srv/mirrors/orders.git"

    def test_no_secret_leaves_the_machines_credential_helper_alone(self, tmp_path, monkeypatch):
        """An API credential is not always a git credential -- a Bitbucket API
        token reads the REST API and is rejected by git. Overriding askpass
        with one that answers nothing turns a configured machine into one that
        cannot authenticate at all."""
        seen = {}
        real = m.subprocess.run

        def spy(args, **kw):
            seen.update(kw.get("env") or {})
            return real(args, **kw)

        monkeypatch.setattr(m.subprocess, "run", spy)
        m.sync(tmp_path / "mirrors",
               [m.Remote(name="orders", full_name="Acme/orders", clone_url=str(origin(tmp_path)),
                         default_branch="main", host="github.com", source="github")],
               credentials={"github": ("someone", None)})
        assert "LEDGER_GIT_SECRET" not in seen
        assert seen.get("GIT_ASKPASS") in (None, "")
        assert seen.get("GIT_TERMINAL_PROMPT") == "0"


class TestForgeCredentials:
    def cfg(self, tmp_path, body):
        from ledger.config import Config
        p = tmp_path / "l.yml"
        p.write_text("org: o\nproject_keys: [ABC]\n" + body)
        return Config.load(str(p))

    def test_helper_mode_yields_a_username_and_no_secret(self, tmp_path, monkeypatch):
        from ledger import service
        monkeypatch.setenv("LEDGER_BITBUCKET_USER", "someone@example.com")
        monkeypatch.setenv("LEDGER_BITBUCKET_APP_PASSWORD", "api-token-not-a-git-credential")
        cfg = self.cfg(tmp_path, "forges:\n  - kind: bitbucket\n    namespace: ws\n"
                                 "    user_env: LEDGER_BITBUCKET_USER\n"
                                 "    password_env: LEDGER_BITBUCKET_APP_PASSWORD\n"
                                 "    git_credentials: helper\n    git_user: someone\n")
        assert service.forge_credentials(cfg) == {"bitbucket": ("someone", None)}

    def test_a_checkout_sits_in_front_of_a_mirror_of_the_same_repo(self, tmp_path):
        """The checkout has a working tree, so uncommitted work stays visible;
        the mirrors supply every repository nobody checked out."""
        from ledger import service
        src = origin(tmp_path, name="orders")
        mirrors = tmp_path / "mirrors"
        m.sync(mirrors, [m.Remote(name="orders", full_name="Acme/orders", clone_url=str(src),
                                  default_branch="main", host="github.com", source="github"),
                         m.Remote(name="billing", full_name="Acme/billing", clone_url=str(src),
                                  default_branch="main", host="github.com", source="github")])
        clones = tmp_path / "work"          # `origin()` left a checkout of orders here
        cfg = self.cfg(tmp_path, f"mirror_root: {mirrors}\nlocal_root: {clones}\n")
        vcs = service.build_adapters(cfg, None)[0]
        names = {r.full_name: r.url for r in vcs.list_repos()}
        assert set(names) == {"orders", "billing"}
        assert names["orders"].startswith(str(clones)), "the checkout wins"
        assert names["billing"].startswith(str(mirrors)), "the mirror fills in"
        # And the checkout's working tree is readable, which a mirror's is not.
        (clones / "orders" / "new.py").write_text("x = 1\n")
        assert [f.path for f in vcs.working_changes("orders")] == ["new.py"]
        assert vcs.working_changes("billing") is None
