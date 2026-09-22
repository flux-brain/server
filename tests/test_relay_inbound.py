"""Offline tests for vault-discord-relay.py inbound filing (2026-09-17, code review fix 2): a message whose attachment
keeps failing is retried strictly MESSAGE_GIVE_UP-1 times, then filed with the attachment listed as not fetched, so
the queue never blocks for good. No network: Discord, the download session, Drive and GitHub are fakes.

  python tests/test_vault_relay_inbound.py [relay.py]
Canonical copy: tests/. Exit 1 on any FAIL.
"""
import sys

from _load import relay as m  # noqa: E402  sets FLUX_HOME to a temp dir first
fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


class Resp:
    def __init__(self, code, js=None, content=b""):
        self.status_code, self._js, self.content, self.headers, self.text = code, js if js is not None else {}, content, {}, ""
    ok = property(lambda s: s.status_code < 400)

    def json(self):
        return self._js

    def raise_for_status(self):
        if not self.ok:
            raise m.requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, fail_urls):
        self.fail, self.gets = set(fail_urls), []

    def get(self, url, **kw):
        self.gets.append(url)
        return Resp(404) if url in self.fail else Resp(200, content=b"%PDF-1.4 fake")


A = {"id": "100", "author": {"bot": False}, "type": 0, "content": "invoice attached",
     "timestamp": "2026-09-17T10:00:00.000000+00:00",
     "attachments": [{"filename": "inv.pdf", "url": "https://cdn/inv.pdf?ex=1", "size": 1000, "content_type": "application/pdf"}]}
B = {"id": "101", "author": {"bot": False}, "type": 0, "content": "plain note",
     "timestamp": "2026-09-17T10:01:00.000000+00:00", "attachments": []}
m.save_state = lambda st: None
m.extract_text = lambda data, mime, name: ("some text", "PDF text layer")


def relay(state, msgs, fail_urls):
    r = m.Relay.__new__(m.Relay)
    r.state, r.dh, r.s = state, {}, FakeSession(fail_urls)

    def discord(method, path, **kw):
        if method == "GET" and path.endswith("/messages"):
            after = int(kw.get("params", {}).get("after", 0))
            return Resp(200, [x for x in msgs if int(x["id"]) > after])
        return Resp(200)  # reactions
    r.discord = discord
    r.puts, r.posts = [], []
    r.put_file = lambda path, data, msg: r.puts.append((path, data.decode()))
    r.drive_upload = lambda name, data, mime: "https://drive/x"
    r.post = lambda channel, content, **kw: r.posts.append((content, kw))
    return r


# ---------- a poisoned attachment: two strict failures, then a lenient filing and the queue moves on ----------
st = {"last_message_id": "99"}
r = relay(st, [A, B], {A["attachments"][0]["url"]})
for attempt in (1, 2):
    try:
        r.inbound("C")
        raised = False
    except m.requests.HTTPError:
        raised = True
    check(f"attempt {attempt}: strict, raises", raised)
    check(f"attempt {attempt}: queue not advanced, nothing filed", st["last_message_id"] == "99" and not r.puts)
    check(f"attempt {attempt}: failure counted", st["message_failures"] == {"100": attempt})
n = r.inbound("C")
check("attempt 3: both messages filed in order", n == 2 and [p[0] for p in r.puts] == ["inbox/2026-09-17T1000Z-100.md", "inbox/2026-09-17T1001Z-101.md"])
check("attempt 3: the note says the attachment was not fetched", "NOT fetched or stored after 3 attempts (HTTPError)" in r.puts[0][1]
      and "invoice attached" in r.puts[0][1])
check("attempt 3: queue advanced past both, counter cleared", st["last_message_id"] == "101" and st["message_failures"] == {})
check("attempt 3: the owner gets a reply on the message", len(r.posts) == 1 and r.posts[0][1].get("reply_to") == "100"
      and "inv.pdf (HTTPError)" in r.posts[0][0] and r.posts[0][1].get("key") == "degraded-100")

# ---------- a transient failure: the second attempt files the attachment normally, no degraded line ----------
st = {"last_message_id": "99"}
r = relay(st, [A], {A["attachments"][0]["url"]})
try:
    r.inbound("C")
except m.requests.HTTPError:
    pass
r.s.fail.clear()  # the CDN answers now
n = r.inbound("C")
note = next(p for p in r.puts if p[0].startswith("inbox/"))  # the text file is put before the note
check("transient: second attempt files with the Drive link", n == 1 and "(https://drive/x)" in note[1] and "NOT fetched" not in note[1])
check("transient: text file written, counter cleared, no degraded reply", any(p[0].startswith("raw/attachments/") for p in r.puts)
      and st["message_failures"] == {} and not r.posts)

# ---------- a message without attachments never touches the counter ----------
st = {"last_message_id": "100"}
r = relay(st, [B], set())
check("plain message filed straight away", r.inbound("C") == 1 and st["message_failures"] == {} and st["last_message_id"] == "101")

print("FAILS:", fails)
sys.exit(1 if fails else 0)
