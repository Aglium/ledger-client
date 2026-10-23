"""GitHub adapter."""

from __future__ import annotations

from datetime import datetime

import httpx

from .base import BranchRef, ChangedFile, ForgeUnavailable, PullRef, RepoRef, VcsAdapter

API = "https://api.github.com"


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


class GitHubAdapter(VcsAdapter):
    source = "github"
    supports_checks = True
    supports_diffs = True
    supports_tree = True

    def __init__(self, org: str, token: str, *, client: httpx.Client | None = None, host: str = "github.com"):
        self.org = org
        self.host = host
        self._client = client or httpx.Client(
            base_url=API,
            timeout=20.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _get(self, path: str, **params):
        """Paginated GET. Any non-2xx becomes ForgeUnavailable rather than a short list."""
        out, page = [], 1
        while True:
            try:
                r = self._client.get(path, params={**params, "per_page": 100, "page": page})
            except httpx.HTTPError as e:
                raise ForgeUnavailable(self.source, str(e)) from e
            if r.status_code == 403 and "rate limit" in r.text.lower():
                raise ForgeUnavailable(self.source, "rate limited")
            if r.status_code >= 400:
                raise ForgeUnavailable(self.source, f"HTTP {r.status_code} on {path}")
            batch = r.json()
            if not isinstance(batch, list):
                raise ForgeUnavailable(self.source, f"unexpected payload on {path}")
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def list_repos(self) -> list[RepoRef]:
        return [
            RepoRef(
                full_name=r["full_name"],
                default_branch=r.get("default_branch") or "main",
                host=self.host,
                url=r.get("html_url"),
            )
            for r in self._get(f"/orgs/{self.org}/repos", type="all")
            if not r.get("archived")
        ]

    def list_branches(self, repo: str, *, since: datetime | None = None) -> list[BranchRef]:
        """Every branch, with no date and no author.

        GitHub's branch list carries neither, and a commit lookup per branch is
        a call per branch. So `since` cannot be applied here, and `author` stays
        None -- which the collector must treat as "this source cannot tell",
        not as "nobody". It used to drop every branch without an author, which
        silently removed every ticketless GitHub branch from the estate.
        """
        return [
            BranchRef(repo=repo, name=b["name"], head_sha=b["commit"]["sha"])
            for b in self._get(f"/repos/{repo}/branches")
        ]

    def list_open_pulls(self, repo: str) -> list[PullRef]:
        return [
            PullRef(
                repo=repo,
                number=p["number"],
                title=p.get("title") or "",
                head_ref=p["head"]["ref"],
                base_ref=p["base"]["ref"],
                head_sha=p["head"]["sha"],
                author=(p.get("user") or {}).get("login"),
                url=p.get("html_url"),
                updated_at=_ts(p.get("updated_at")),
                body=p.get("body") or "",
            )
            for p in self._get(f"/repos/{repo}/pulls", state="open")
        ]

    def list_tree(self, repo: str, ref: str) -> list[str]:
        try:
            r = self._client.get(f"/repos/{repo}/git/trees/{ref}", params={"recursive": "1"})
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} listing tree at {ref}")
        body = r.json()
        if body.get("truncated"):
            # Loud, not silent: a truncated tree would quietly produce a card
            # built on half a repo.
            raise ForgeUnavailable(self.source, f"{repo} tree truncated at {ref}; too large for one call")
        return [t["path"] for t in body.get("tree", []) if t.get("type") == "blob"]

    def read_file(self, repo: str, ref: str, path: str) -> str:
        import base64
        try:
            r = self._client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} reading {path}")
        body = r.json()
        if body.get("encoding") != "base64":
            raise ForgeUnavailable(self.source, f"{path}: unexpected encoding {body.get('encoding')}")
        try:
            return base64.b64decode(body["content"]).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as e:
            raise ForgeUnavailable(self.source, f"{path} is not utf-8 text") from e

    def commits_ahead(self, repo: str, base: str, head: str) -> int:
        try:
            r = self._client.get(f"/repos/{repo}/compare/{base}...{head}")
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} comparing {base}...{head}")
        return int(r.json().get("ahead_by", 0))

    def changed_files(self, repo: str, base: str, head: str) -> list[ChangedFile]:
        """`compare` uses the merge base, which is what we want: it excludes
        commits that landed on base after this change forked."""
        try:
            r = self._client.get(f"/repos/{repo}/compare/{base}...{head}")
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} comparing {base}...{head}")
        return [
            ChangedFile(path=f["filename"], body=f.get("patch") or "")
            for f in (r.json().get("files") or [])
        ]

    def set_check_status(
        self, repo: str, head_sha: str, name: str, conclusion: str, title: str, summary: str
    ) -> str:
        """Check-runs are keyed by (name, head_sha), so re-running updates in place."""
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        }
        existing = None
        try:
            r = self._client.get(f"/repos/{repo}/commits/{head_sha}/check-runs", params={"check_name": name})
            if r.status_code < 400:
                runs = r.json().get("check_runs") or []
                existing = runs[0]["id"] if runs else None
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e

        if existing:
            r = self._client.patch(f"/repos/{repo}/check-runs/{existing}", json=payload)
        else:
            r = self._client.post(f"/repos/{repo}/check-runs", json=payload)
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} writing check run")
        return str(r.json().get("id", existing))

    def upsert_comment(self, repo: str, pull_number: int, marker: str, body: str) -> str:
        payload = f"{body}\n\n<!-- {marker} -->"
        for c in self._get(f"/repos/{repo}/issues/{pull_number}/comments"):
            if marker in (c.get("body") or ""):
                r = self._client.patch(f"/repos/{repo}/issues/comments/{c['id']}", json={"body": payload})
                if r.status_code >= 400:
                    raise ForgeUnavailable(self.source, f"HTTP {r.status_code} updating comment")
                return str(c["id"])
        r = self._client.post(f"/repos/{repo}/issues/{pull_number}/comments", json={"body": payload})
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} creating comment")
        return str(r.json()["id"])
