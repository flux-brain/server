"""Which translation flags a transcript gets ([capture] translate_to). Shared by the relay (the echo under a voice note,
the flags under a voicemail notice) and the Gmail module (the `translations:` line of a voicemail capture), so the
three can never disagree about the languages on offer."""
import re

from ..config import CFG

TRANSLATE_LANGS = {"en": ("🇬🇧", "English"), "fr": ("🇫🇷", "French"), "it": ("🇮🇹", "Italian"), "es": ("🇪🇸", "Spanish"),
                   "de": ("🇩🇪", "German"), "pt": ("🇵🇹", "Portuguese")}
SURE_LANGUAGE = 80                 # % from which the detected language is trusted: no flag for a note's own language
HEARD_LANGUAGE = re.compile(r"language (\w+) \((\d+)%\)")   # in transcribe()'s method line


def flags_for(method):
    """{flag emoji: language code} to offer under one transcript: the configured languages, minus the one the
    recording is already in when the model was sure of it (an unsure detection keeps every flag)."""
    found = HEARD_LANGUAGE.search(method or "")
    own = found.group(1) if found and int(found.group(2)) >= SURE_LANGUAGE else None
    return {TRANSLATE_LANGS[c][0]: c for c in CFG.translate_to if c in TRANSLATE_LANGS and c != own}
