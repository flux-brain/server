"""One GitHub REST client for the vault repo (flux_brain.lib, 2026-09-18, code review items 6 + 7).

Retry policy is EXPLICIT per client: `retry_writes=True` retries PUT/POST on 5xx too (the Discord relay: its
put_file keeps an existing file, so a retried write is idempotent); the default retries GETs only (the Keep sync's
Git Data commits and the reconcile applier's compare-and-swap puts must never be replayed blind). This host sees
intermittent packet loss to GitHub's edge (github-edge-blackhole), hence retries at all.

tree(): the recursive tree of the branch, fetched only when the branch tip moved (item 7). The check is a conditional
GET on git/ref/heads/<branch> with If-None-Match: a 304 costs nothing against the rate limit, and the tree (~500
entries and growing with raw/) is served from memory or from `cache_file` (the relay is a per-run process). Before
this the relay fetched the whole tree every 15 s and the Keep daemon every 30 s.
"""
import base64
import json
import os
import subprocess
import urllib.parse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .state import load_json, save_json

__all__ = ["GITHUB", "REPO", "BRANCH", "COMMITTER", "gh_token", "session", "GitHub"]

GITHUB = "https://api.github.com"
from ..config import CFG
REPO = CFG.vault_repo
BRANCH = "main"
# Authored as the owner, like every host-side write: routines may only push to main when all commits are his.
COMMITTER = {"name": CFG.owner_name, "email": CFG.git_email}


def gh_token(user=None):
    """The GitHub token: `GITHUB_TOKEN` from flux.env (a fine-grained token limited to the vault repository, see
    INSTALL.md), else the gh CLI's store for `user`. Never written to disk or logs by any Flux script."""
    if CFG.github_token:
        return CFG.github_token
    cmd = ["gh", "auth", "token"] + (["-u", user] if user else [])
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def session(retry_writes=False):
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None if retry_writes else ("GET", "HEAD"), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class GitHub:
    def __init__(self, repo=REPO, branch=BRANCH, retry_writes=False, cache_file=None, user=None):
        self.repo, self.branch, self.user = repo, branch, user
        self.s = session(retry_writes)
        self.auth()
        self.cache_file = cache_file
        cached = load_json(cache_file, {}) if cache_file else {}
        self._tree, self._tree_sha, self._ref_etag = cached.get("tree"), cached.get("sha"), cached.get("etag")

    def auth(self):
        self.h = {"Authorization": f"Bearer {gh_token(self.user)}", "Accept": "application/vnd.github+json"}

    # ---------- low level ----------
    def _url(self, path):
        return f"{GITHUB}/repos/{self.repo}/{path}"

    def _contents_url(self, path):
        return self._url(f"contents/{urllib.parse.quote(path)}")

    def get_raw(self, url, **kw):
        """GET with one re-auth on 401 (a rotated gh token mid-run, seen by the Keep daemon)."""
        r = self.s.get(url, headers=self.h, timeout=60, **kw)
        if r.status_code == 401:
            self.auth()
            r = self.s.get(url, headers=self.h, timeout=60, **kw)
        return r

    # ---------- contents ----------
    def get(self, path):
        """(text, blob sha) of a file on the branch, (None, None) when absent."""
        r = self.get_raw(self._contents_url(path), params={"ref": self.branch})
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        j = r.json()
        return base64.b64decode(j["content"]).decode("utf-8", "replace"), j["sha"]

    def list_dir(self, path):
        """The file entries of a directory (name, path, sha, size...), [] when the directory does not exist."""
        r = self.get_raw(self._contents_url(path), params={"ref": self.branch})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [e for e in r.json() if e.get("type") == "file"]

    def put_file(self, path, data: bytes, message):
        """Create a file; an EXISTING file is kept as is (422 "sha wasn't supplied"), which is what makes a re-run
        after a crash idempotent (the relays name files after message ids)."""
        body = {"message": message, "branch": self.branch, "content": base64.b64encode(data).decode(),
                "committer": COMMITTER}
        r = self.s.put(self._contents_url(path), headers=self.h, json=body, timeout=60)
        if r.status_code == 422 and "sha" in r.text:
            return False
        r.raise_for_status()
        return True

    def put(self, path, text, message, sha=None):
        """Create or, with `sha`, replace a file compare-and-swap style (409 when the file moved meanwhile)."""
        body = {"message": message, "branch": self.branch, "content": base64.b64encode(text.encode()).decode(),
                "committer": COMMITTER}
        if sha:
            body["sha"] = sha
        r = self.s.put(self._contents_url(path), headers=self.h, json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    # ---------- git data ----------
    def ref_sha(self):
        """(tip sha, changed?) of the branch, via a conditional GET: 304 = same as last time (free)."""
        h = dict(self.h)
        if self._ref_etag:
            h["If-None-Match"] = self._ref_etag
        r = self.s.get(self._url(f"git/ref/heads/{self.branch}"), headers=h, timeout=30)
        if r.status_code == 304 and self._tree_sha:
            return self._tree_sha, False
        if r.status_code == 401:
            self.auth()
            r = self.s.get(self._url(f"git/ref/heads/{self.branch}"), headers=self.h, timeout=30)
        r.raise_for_status()
        self._ref_etag = r.headers.get("ETag")
        sha = r.json()["object"]["sha"]
        return sha, sha != self._tree_sha

    def tree(self):
        """The recursive tree entries of the branch tip; re-fetched only when the tip moved (item 7)."""
        sha, changed = self.ref_sha()
        if not changed and self._tree is not None:
            return self._tree
        r = self.get_raw(self._url(f"git/trees/{sha}?recursive=1"))
        r.raise_for_status()
        j = r.json()
        self._tree, self._tree_sha = j["tree"], sha
        if j.get("truncated"):
            # 100k entries / 7 MB: not this repo, but a partial tree must never drive outbound or the watcher silently
            raise RuntimeError("GitHub returned a truncated tree; the repo has outgrown the recursive tree API")
        if self.cache_file:
            try:
                save_json(self.cache_file, {"sha": sha, "etag": self._ref_etag, "tree": self._tree}, indent=None)
            except OSError:
                pass  # a cache write failure only costs the next tick a full fetch
        return self._tree

    def blob_text(self, sha):
        r = self.get_raw(self._url(f"git/blobs/{sha}"))
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")
