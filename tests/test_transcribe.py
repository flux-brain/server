"""Offline tests for the transcription settings ([capture] whisper_model, whisper_beam): the model named in flux.toml
is the one loaded, the beam is passed on, and the method line names the model. faster-whisper is a stub."""
import sys
import types

from flux_brain.config import CFG
from flux_brain.lib import extract


class Seg:
    start, text = 0.0, " hello there "


class Info:
    language, language_probability, duration = "en", 0.99, 7.0


def stub(monkeypatch, calls):
    class WhisperModel:
        def __init__(self, name, **kw):
            calls.append(("load", name))

        def transcribe(self, path, **kw):
            calls.append(("beam", kw["beam_size"]))
            return [Seg()], Info()
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(extract, "_WHISPER", None)   # the model is cached per process: start each case without one


def test_defaults_are_small_and_beam_one(monkeypatch):
    calls = []
    stub(monkeypatch, calls)
    assert (CFG.whisper_model, CFG.whisper_beam) == ("small", 1)
    text, method = extract.transcribe("x.ogg")
    assert calls == [("load", "small"), ("beam", 1)]
    assert text == "[00:00] hello there" and method == "Whisper small transcript, language en (99%), 7 s"


def test_model_and_beam_from_the_configuration(monkeypatch):
    calls = []
    stub(monkeypatch, calls)
    monkeypatch.setattr(CFG, "whisper_model", "medium")
    monkeypatch.setattr(CFG, "whisper_beam", 5)
    _, method = extract.transcribe("x.ogg")
    assert calls == [("load", "medium"), ("beam", 5)] and method.startswith("Whisper medium transcript, ")
