"""Outbound: new files under briefings/ and notify/ are posted once, split on line boundaries, resumable by part,
questions with an @mention, run summaries to the log channel."""
import urllib.parse

from ..config import CFG
from ..lib.common import log
from . import state
from .state import prune_posted

OUTBOUND_DIRS = ("briefings/daily/", "briefings/weekly/", "notify/")
# Questions for the owner @mention him (the owner, 2026-09-17: "add the @mention for questions"). The routine's Photobooth520
# question of 09:53 sat unseen among summaries and run links, because bot posts never notify. Only a notify/ file
# that IS a question pings; summaries, digests and run links stay silent so the ping keeps its meaning.


def is_question(path, body):
    """A notify/ file whose name says question, or whose text carries an Obsidian question callout."""
    return path.startswith("notify/") and ("question" in path.rsplit("/", 1)[-1] or "[!question]" in body)


def question_text(body):
    """Discord-readable question: drop the callout marker and the '> ' quoting Obsidian needs, keep the words."""
    lines = [ln[2:] if ln.startswith("> ") else (ln[1:] if ln.startswith(">") else ln) for ln in body.split("\n")]
    return "\n".join(lines).replace("[!question]", "").strip()


def is_run_summary(path):
    """The per-run `notify/<stamp>-filed.md` summary the routine writes (vault CLAUDE.md, run protocol): background
    information for the log channel. Everything else in notify/ was written for the owner to read or copy."""
    return path.startswith("notify/") and path.endswith("-filed.md")
MAX_POST_CHUNKS = 5  # outbound files longer than ~9500 chars are cut after this many Discord posts


def chunk_lines(text, size):
    """Split text into pieces <= size, breaking between lines (hard-splitting only overlong lines)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class OutboundMixin:
    def outbound(self, channel, tree=None):
        # tree passed in by main() since 2026-09-15 (shared with watch_obsidian_notes, one GitHub call per tick)
        tree = tree if tree is not None else self.tree()
        posted = set(self.state.get("posted", []))
        # Drop resume counters for files that are gone or already fully posted (deleted mid-post, say)
        pending_paths = {e["path"] for e in tree if e["path"] not in posted}
        for path in [p for p in self.state.get("posting", {}) if p not in pending_paths]:
            self.state["posting"].pop(path)
        new = sorted((e for e in tree if e["type"] == "blob"
                      and e["path"].startswith(OUTBOUND_DIRS) and e["path"].endswith(".md")
                      and e["path"] not in posted), key=lambda e: e["path"])
        for e in new:
            body = self.blob_text(e["sha"]).strip()
            link = f"https://github.com/{CFG.vault_repo}/blob/{CFG.vault_branch}/{urllib.parse.quote(e['path'])}"
            head = "📰" if e["path"].startswith("briefings/") else "💬"
            # Discord caps a message at 2000 chars. Split on line boundaries into several posts
            # (2026-09-15: full drafts must arrive whole, the owner copies them from Discord); only past
            # MAX_POST_CHUNKS is the tail cut, with a link to the page.
            question = is_question(e["path"], body)
            if question:  # no file-name header: the mention and the question itself are what the owner sees
                parts = chunk_lines(f"<@{CFG.owner_discord_id}> ❓ {question_text(body)}", 1900)
            else:
                parts = chunk_lines(f"{head} **{e['path']}**\n{body}", 1900)
            # Routing (2026-09-22): run summaries are background -> log channel; questions, answers, drafts and the
            # digests (the owner's choice: "digest to vault channel") stay in the conversation channel.
            target = self.log_target(channel) if is_run_summary(e["path"]) else channel
            if len(parts) > MAX_POST_CHUNKS:
                parts = parts[:MAX_POST_CHUNKS]
                parts[-1] = parts[-1][:1800] + f"\n… (continued: <{link}>)"
            # Resume where a failed tick stopped (2026-09-16): a post error now fails the tick instead of being
            # retried inside the session, so remember how many parts of this file are already posted. Each part
            # also carries a stable key, so a part whose reply was lost is not duplicated when it is sent again.
            # The counter is tied to the file VERSION (blob sha; code review of 74c4123): if the file changed
            # after a partial post, start again from part 0 so the new version arrives whole and in order (the old
            # version's partial parts stay in the channel above it; they are not deleted), and the sha in the key keeps
            # Discord's nonce check from handing back a part of the old version.
            progress = self.state.setdefault("posting", {})
            rec = progress.get(e["path"])
            if not isinstance(rec, dict) or rec.get("sha") != e["sha"]:
                if rec is not None:
                    log(f"{e['path']} changed after a partial post: posting it again from the start")
                rec = progress[e["path"]] = {"sha": e["sha"], "done": 0}
            for i, part in enumerate(parts):
                if i < rec["done"]:
                    continue
                self.post(target, part, key=f"{e['path']}@{e['sha'][:10]}#{i}",
                          mention_user=CFG.owner_discord_id if question and i == 0 else None)
                rec["done"] = i + 1
                state.save_state(self.state)
            progress.pop(e["path"], None)
            posted.add(e["path"])
            self.state["posted"] = prune_posted(posted, {t["path"] for t in tree})
            state.save_state(self.state)
            # name the channel (2026-09-22): the only proof of the two-channel routing outside Discord itself
            log(f"posted {e['path']} -> {'log' if target != channel else 'conversation'} channel")
