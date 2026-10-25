"""Bitbucket Cloud adapter."""

from __future__ import annotations

from datetime import datetime

import httpx

from .base import BranchRef, ChangedFile, ForgeUnavailable, PullRef, RepoRef, VcsAdapter

API = "https://api.bitbucket.org/2.0"


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


class BitbucketAdapter(VcsAdapter):
    source = "bitbucket"
    supports_diffs = True

    def __init__(self, workspace: str, user: str, app_password: str, *, client: httpx.Client | None = None):
        self.workspace = workspace
        self._client = client or httpx.Client(base_url=API, timeout=20.0, auth=(user, app_password))

    def _get(self, path: str, **params):
        """Bitbucket paginates by `next` URL rather than page numbers."""
        out, url, first = [], path, True
        while url:
            try:
                r = self._client.get(url, params={**params, "pagelen": 100} if first else None)
            except httpx.HTTPError as e:
                raise ForgeUnavailable(self.source, str(e)) from e
            if r.status_code == 429:
                raise ForgeUnavailable(self.source, "rate limited")
            if r.status_code >= 400:
                raise ForgeUnavailable(self.source, f"HTTP {r.status_code} on {path}")
            body = r.json()
            out.extend(body.get("values", []))
            url, first = body.get("next"), False
        return out

    def list_repos(self) -> list[RepoRef]:
        return [
            RepoRef(
                full_name=r["full_name"],
                default_branch=(r.get("mainbranch") or {}).get("name") or "master",
                host="bitbucket.org",
                url=(r.get("links", {}).get("html") or {}).get("href"),
            )
            for r in self._get(f"/repositories/{self.workspace}")
        ]

    def list_branches(self, repo: str, *, since: datetime | None = None) -> list[BranchRef]:
        out = []
        for b in self._get(f"/repositories/{repo}/refs/branches"):
            ts = _ts((b.get("target") or {}).get("date"))
            if since is not None and ts is not None and ts < since:
                continue
            out.append(BranchRef(repo=repo, name=b["name"], head_sha=(b.get("target") or {})["hash"], updated_at=ts))
        return out

    def list_open_pulls(self, repo: str) -> list[PullRef]:
        return [
            PullRef(
                repo=repo,
                number=p["id"],
                title=p.get("title") or "",
                head_ref=(p.get("source") or {}).get("branch", {}).get("name", ""),
                base_ref=(p.get("destination") or {}).get("branch", {}).get("name", ""),
                head_sha=(p.get("source") or {}).get("commit", {}).get("hash", ""),
                author=(p.get("author") or {}).get("nickname"),
                url=(p.get("links", {}).get("html") or {}).get("href"),
                updated_at=_ts(p.get("updated_on")),
                body=(p.get("description")
                      or ((p.get("summary") or {}).get("raw") or "")),
                fork=bool(((p.get("source") or {}).get("repository") or {}).get("full_name")
                          and ((p.get("source") or {}).get("repository") or {}).get("full_name")
                          != ((p.get("destination") or {}).get("repository") or {})
                          .get("full_name", repo)),
            )
            for p in self._get(f"/repositories/{repo}/pullrequests", state="OPEN")
        ]

    def upsert_comment(self, repo: str, pull_number: int, marker: str, body: str) -> str:
        payload = f"{body}\n\n<!-- {marker} -->"
        for c in self._get(f"/repositories/{repo}/pullrequests/{pull_number}/comments"):
            if marker in ((c.get("content") or {}).get("raw") or ""):
                r = self._client.put(
                    f"/repositories/{repo}/pullrequests/{pull_number}/comments/{c['id']}",
                    json={"content": {"raw": payload}},
                )
                if r.status_code >= 400:
                    raise ForgeUnavailable(self.source, f"HTTP {r.status_code} updating comment")
                return str(c["id"])
        r = self._client.post(
            f"/repositories/{repo}/pullrequests/{pull_number}/comments",
            json={"content": {"raw": payload}},
        )
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} creating comment")
        return str(r.json()["id"])

    def changed_files(self, repo: str, base: str, head: str) -> list[ChangedFile]:
        """Bitbucket returns a unified diff as text, not JSON. `..` is its
        merge-base form (`...` means something else here than on GitHub)."""
        try:
            r = self._client.get(f"/repositories/{repo}/diff/{head}..{base}")
        except httpx.HTTPError as e:
            raise ForgeUnavailable(self.source, str(e)) from e
        if r.status_code >= 400:
            raise ForgeUnavailable(self.source, f"HTTP {r.status_code} diffing {base}..{head}")
        out: list[ChangedFile] = []
        path, buf = None, []
        for line in r.text.splitlines():
            if line.startswith("diff --git "):
                if path:
                    out.append(ChangedFile(path=path, body="\n".join(buf)))
                path, buf = line.split(" b/", 1)[-1], []
            elif path and line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                buf.append(line)
        if path:
            out.append(ChangedFile(path=path, body="\n".join(buf)))
        return out

    def commits_ahead(self, repo: str, base: str, head: str) -> int:
        """Commits reachable from `head` but not from `base`."""
        return len(self._get(f"/repositories/{repo}/commits/{head}", exclude=base))
