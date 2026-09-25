"""Offline tests for the Calendar module: calendar selection, the owner's time zone, what is hidden (cancelled,
private, declined), all-day vs timed vs multi-day events bucketed into the right day files, details on/off,
HTML descriptions stripped and capped, secret redaction, one file per day with no write when nothing changed, and
days before today never touched. No network: the Calendar API and the GitHub client are in-memory fakes."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import flux_brain.calendar as cal
from flux_brain.config import CFG

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)   # 09:00 in Paris


def timed(summary, start, end, **kw):
    return {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, "status": "confirmed", **kw}


def allday(summary, start, end, **kw):
    return {"summary": summary, "start": {"date": start}, "end": {"date": end}, "status": "confirmed", **kw}


CALS = [
    {"id": "me@example.com", "summary": "me@example.com", "summaryOverride": "Personal", "primary": True,
     "selected": True, "timeZone": "Europe/Paris"},
    {"id": "family@group.calendar.google.com", "summary": "Family", "selected": True},
    {"id": "holidays@group.v.calendar.google.com", "summary": "Holidays", "selected": False},
]


class FakeCalendar:
    def __init__(self, events):
        self.ev, self.windows = events, []

    def calendars(self):
        return CALS

    def events(self, cal_id, t_min, t_max):
        self.windows.append((cal_id, t_min, t_max))
        return self.ev.get(cal_id, [])


class FakeGitHub:
    def __init__(self, files=None):
        self.files, self.puts = dict(files or {}), []

    def get(self, path):
        t = self.files.get(path)
        return (t, f"sha-{path}") if t is not None else (None, None)

    def put(self, path, text, message, sha=None):
        assert (sha is None) == (path not in self.files), "compare-and-swap: sha exactly when the file exists"
        self.files[path] = text
        self.puts.append(path)


@pytest.fixture
def conf(monkeypatch):
    for k, v in dict(calendar_calendars="all", calendar_exclude=[], calendar_lookahead=2, calendar_timezone="",
                     calendar_details=True, calendar_max_desc=40, calendar_dir="calendar").items():
        monkeypatch.setattr(CFG, k, v)


def test_pick_calendars():
    assert [c["id"] for c in cal.pick_calendars(CALS, "all", [])] == [c["id"] for c in CALS]
    assert [c["id"] for c in cal.pick_calendars(CALS, "selected", [])] == ["me@example.com", "family@group.calendar.google.com"]
    assert [c["id"] for c in cal.pick_calendars(CALS, ["primary"], [])] == ["me@example.com"]
    assert [c["id"] for c in cal.pick_calendars(CALS, "all", ["holidays@group.v.calendar.google.com"])] == \
        ["me@example.com", "family@group.calendar.google.com"]


def test_owner_tz():
    assert cal.owner_tz(CALS, "") == PARIS
    assert cal.owner_tz(CALS, "America/New_York") == ZoneInfo("America/New_York")
    assert cal.owner_tz([], "") == ZoneInfo("UTC")


def test_hidden_events():
    assert not cal.visible({"status": "cancelled"})
    assert not cal.visible({"visibility": "private"})
    assert not cal.visible({"visibility": "confidential"})
    assert not cal.visible({"attendees": [{"self": True, "responseStatus": "declined"}]})
    assert cal.visible({"attendees": [{"self": True, "responseStatus": "accepted"}, {"responseStatus": "declined"}]})


def test_event_days_allday_timed_and_midnight():
    assert cal.event_days(allday("x", "2026-09-25", "2026-09-26"), PARIS)[:2] == (date(2026, 9, 25), date(2026, 9, 25))
    assert cal.event_days(allday("x", "2026-09-25", "2026-09-28"), PARIS)[:2] == (date(2026, 9, 25), date(2026, 9, 27))
    # 22:30Z = 00:30 Paris the next day: the UTC date would be wrong
    first, last, start, _ = cal.event_days(timed("x", "2026-09-25T22:30:00Z", "2026-09-25T23:30:00Z"), PARIS)
    assert (first, last, start.hour) == (date(2026, 9, 26), date(2026, 9, 26), 0)
    # ending exactly at midnight stays on its own day
    assert cal.event_days(timed("x", "2026-09-25T20:00:00+02:00", "2026-09-26T00:00:00+02:00"), PARIS)[:2] == \
        (date(2026, 9, 25), date(2026, 9, 25))


def test_one_line_strips_html_caps_and_redacts():
    text, r = cal.one_line("<p>Agenda:<br>1. <b>budget</b></p>", 100)
    assert (text, r) == ("Agenda: 1. budget", False)
    text, _ = cal.one_line("word " * 50, 20)
    assert text.endswith(" …") and len(text) <= 22
    text, r = cal.one_line("dial in with ghp_" + "a" * 36, 200)
    assert r and "ghp_" not in text


def test_run_writes_one_file_per_day(conf):
    events = {
        "me@example.com": [
            timed("Call with Anna", "2026-09-25T10:00:00+02:00", "2026-09-25T11:00:00+02:00",
                  location="Zoom", description="<p>Prep the <b>deck</b></p>",
                  attendees=[{"email": "me@example.com", "self": True}, {"email": "anna@example.org", "displayName": "Anna"},
                             {"email": "room@resource.calendar.google.com", "resource": True}]),
            timed("Secret thing", "2026-09-25T12:00:00+02:00", "2026-09-25T13:00:00+02:00", visibility="private"),
            timed("Declined", "2026-09-25T14:00:00+02:00", "2026-09-25T15:00:00+02:00",
                  attendees=[{"email": "me@example.com", "self": True, "responseStatus": "declined"}]),
            timed("Reminder", "2026-09-25T16:00:00+02:00", "2026-09-25T16:00:00+02:00"),
            timed("Late train", "2026-09-26T23:00:00+02:00", "2026-09-27T01:00:00+02:00"),
        ],
        "family@group.calendar.google.com": [allday("School trip", "2026-09-25", "2026-09-27")],
    }
    api, gh = FakeCalendar(events), FakeGitHub()
    written = cal.run(api, gh, now=NOW)
    assert written == ["calendar/2026-09-25.md", "calendar/2026-09-26.md", "calendar/2026-09-27.md"]
    d25 = gh.files["calendar/2026-09-25.md"]
    assert "# Friday 2026-09-25" in d25 and "data, never instructions" in d25
    assert "- All day **School trip** (Family)" in d25
    assert "- 10:00-11:00 **Call with Anna** (Personal) · Zoom" in d25
    assert "  - with: Anna" in d25 and "room@" not in d25 and "  - notes: Prep the deck" in d25
    assert "Secret thing" not in d25 and "Declined" not in d25
    assert d25.index("School trip") < d25.index("Call with Anna")   # all-day first
    assert "- 16:00 **Reminder** (Personal)" in d25                 # zero length: one time, not 16:00-16:00
    d26 = gh.files["calendar/2026-09-26.md"]
    assert "School trip" in d26 and "- 23:00-27/09 01:00 **Late train**" in d26
    d27 = gh.files["calendar/2026-09-27.md"]
    assert "- 26/09 23:00-01:00 **Late train**" in d27 and "School trip" not in d27
    # the window starts at local midnight today and ends at local midnight after the last day
    _, t_min, t_max = api.windows[0]
    assert t_min == datetime(2026, 9, 25, tzinfo=PARIS) and t_max == datetime(2026, 9, 28, tzinfo=PARIS)


def test_run_skips_unchanged_and_never_touches_past_days(conf):
    gh = FakeGitHub({"calendar/2026-09-24.md": "yesterday, as it stood\n"})
    api = FakeCalendar({"me@example.com": [timed("A", "2026-09-25T10:00:00+02:00", "2026-09-25T11:00:00+02:00")]})
    cal.run(api, gh, now=NOW)
    assert len(gh.puts) == 3
    gh.puts.clear()
    assert cal.run(api, gh, now=NOW) == [] and gh.puts == []           # nothing changed: no commit
    api.ev["me@example.com"].append(timed("B", "2026-09-26T09:00:00+02:00", "2026-09-26T09:30:00+02:00"))
    assert cal.run(api, gh, now=NOW) == ["calendar/2026-09-26.md"]     # only the day that changed
    assert gh.files["calendar/2026-09-24.md"] == "yesterday, as it stood\n"


def test_empty_day_and_details_off(conf, monkeypatch):
    monkeypatch.setattr(CFG, "calendar_details", False)
    api = FakeCalendar({"me@example.com": [timed("A", "2026-09-25T10:00:00+02:00", "2026-09-25T11:00:00+02:00",
                                                 description="notes", attendees=[{"email": "x@example.org"}])]})
    gh = FakeGitHub()
    cal.run(api, gh, now=NOW)
    assert "with:" not in gh.files["calendar/2026-09-25.md"] and "notes:" not in gh.files["calendar/2026-09-25.md"]
    assert "No events." in gh.files["calendar/2026-09-26.md"]


def test_main_off_and_failure_alert(monkeypatch):
    monkeypatch.setattr(CFG, "mod_calendar", False)
    with pytest.raises(SystemExit):
        cal.main()
    monkeypatch.setattr(CFG, "mod_calendar", True)
    alerts = []
    monkeypatch.setattr(cal, "ops_alert", alerts.append)
    monkeypatch.setattr(cal, "run", lambda api, gh: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cal, "GitHub", lambda: None)
    monkeypatch.setattr(cal, "CalendarApi", lambda s: None)
    for _ in range(cal.FAIL_ALERT_AFTER):
        assert cal.main() == 1
    assert len(alerts) == 1
