"""Offline tests for the follow-up module (2026-09-28) and the shared Gmail client: which sent threads count as
waiting, the dismiss label, automated exchanges, the rendered file, and the guarantee that no module can send mail.
No network: the Gmail API is a fake."""
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from flux_brain import followups as fu
from flux_brain.config import CFG
from flux_brain.lib import gmailapi as ga

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
OWN = "owner@example.com"


def ms(days_ago):
    return str(int((NOW - timedelta(days=days_ago)).timestamp() * 1000))


def m(frm, to, days_ago, subject="TRP inquiry", labels=(), **hdr):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    headers += [{"name": k.replace("_", "-"), "value": v} for k, v in hdr.items()]
    return {"internalDate": ms(days_ago), "labelIds": list(labels), "payload": {"headers": headers}}


class FakeApi:
    def __init__(self, threads):
        self.threads = threads

    def owner_addresses(self):
        return {OWN, "alias@example.com"}

    def search(self, q, limit=500):
        assert q.startswith("in:sent")
        return [{"id": f"m{tid}", "threadId": tid} for tid in self.threads]

    def thread(self, tid):
        return {"messages": self.threads[tid]}


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(CFG, "followups_max_items", 30)
    monkeypatch.setattr(CFG, "followups_skip_forwards", True)


def test_waiting_keeps_only_unanswered_sent_threads(cfg):
    threads = {
        "wait": [m(f"Owner <{OWN}>", "Papilio <enquiries@firm.example>", 7)],
        "fresh": [m(OWN, "a@firm.example", 1)],
        "answered": [m(OWN, "b@firm.example", 9), m("B <b@firm.example>", OWN, 8)],
        "self": [m(OWN, "alias@example.com", 9)],
        "bulk": [m(OWN, "news@list.example", 9, List_Unsubscribe="<mailto:x>")],
        "noreply": [m(OWN, "no-reply@shop.example", 9)],
        "dismissed": [m(OWN, "c@firm.example", 9, labels=("LBL_DISMISS",))],
        "ignored": [m(OWN, "d@ignored.example", 9)],
        "draft_last": [m(OWN, "e@firm.example", 9), m(OWN, "e@firm.example", 1, labels=("DRAFT",))],
        "forward": [m(OWN, "f@firm.example", 9, subject="Fwd: receipt")],
        "dup_old": [m(OWN, "g@firm.example", 12, subject="Ticket 42")],
        "dup_new": [m(OWN, "g@firm.example", 10, subject="Re: Ticket 42")],
    }
    got = fu.waiting(FakeApi(threads), now=NOW, days=4, lookback=30, ignore=["@ignored.example"], dismiss_id="LBL_DISMISS")
    # oldest first; a later draft does not count; a forward is FYI; the same subject to the same person once (newest)
    assert [w["thread"] for w in got] == ["dup_new", "draft_last", "wait"]
    w = got[2]
    assert w["age_days"] == 7 and w["to"] == [("Papilio", "enquiries@firm.example")]
    assert w["link"].endswith("#all/wait")


def test_render_names_recipients_and_redacts(cfg):
    items = [{"thread": "t", "subject": "Key sk-ant-api03-" + "A" * 40, "to": [("Anna", "a@x"), ("", "b@x"), ("C", "c@x")],
              "sent": NOW, "age_days": 5, "link": "https://mail/#all/t"}]
    text = fu.render(items, 4)
    assert "**Anna, b@x +1**" in text and "(5 days)" in text and "sk-ant" not in text and "[REDACTED]" in text
    assert "Nothing waiting." in fu.render([], 4)


def test_run_writes_only_on_change(cfg, monkeypatch):
    monkeypatch.setattr(fu, "DRY", False)

    class Api(FakeApi):
        def ensure_label(self, name):
            return "LBL"

    class GH:
        def __init__(self, old):
            self.old, self.puts = old, []

        def get(self, path):
            return self.old, "sha1"

        def put(self, path, text, message, sha=None):
            self.puts.append((path, text, sha))

    monkeypatch.setattr(fu, "waiting", lambda api, dismiss_id=None: [])
    gh = GH(None)
    fu.run(Api({}), gh)
    assert gh.puts and gh.puts[0][0] == "followups/waiting.md"
    gh2 = GH(gh.puts[0][1])
    fu.run(Api({}), gh2)
    assert gh2.puts == []


def test_is_bulk_flags_automated_messages():
    assert ga.is_bulk(m("noreply@x.example", OWN, 1))
    assert ga.is_bulk(m("a@x.example", OWN, 1, Precedence="bulk"))
    assert ga.is_bulk(m("a@x.example", OWN, 1, Auto_Submitted="auto-replied"))
    assert not ga.is_bulk(m("anna@x.example", OWN, 1, Auto_Submitted="no"))


def test_no_module_can_send_mail():
    """The host's Gmail token may carry gmail.send. No Flux module may ever call the send endpoints."""
    root = pathlib.Path(__file__).resolve().parents[1] / "flux_brain"
    send = re.compile(r"""["'/](?:messages|drafts)/send\b|users/me/messages/send""")
    hits = [str(p) for p in root.rglob("*.py") if send.search(p.read_text())]
    assert hits == []
