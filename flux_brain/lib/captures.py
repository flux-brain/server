"""The inbox captures the server-side programs write, registered in one place (2026-09-24, architecture review).

Every program on the server that files a capture names it `inbox/<stamp>-<kind>-<rest>.md` (the Discord relay's own
notes are the exception: `inbox/<stamp>-<discord snowflake>.md`). The relay needs that list three times: to start the
routine on the tick such a note appears (nobody is still typing it), to word the start message, and to know which
captures name a project page whose memory files the run should open first. The three used to be three regexes and
had drifted: the settle rule knew Tasks and WhatsApp captures, the start message and the page hints did not, so a
Tasks edit was announced as an Obsidian note and got no hint. One table now feeds all three.
"""
import re

__all__ = ["KINDS", "RELAY_NOTE", "HOST_NOTE", "host_kind", "describe", "carries_page"]

# kind tag in the file name -> (wording for the start message, `{rest}` is what follows the tag;
#                               the capture's frontmatter names a project page (`project:` or `page:`))
KINDS = {
    "keep-note": ("Keep note", False),
    "keep": ("Keep checklist edit, project {rest}", True),
    "gmail": ("Gmail", False),
    "memory-reconcile": ("memory reconcile, project {rest}", True),
    "tasks": ("Tasks checklist edit, project {rest}", True),
    "whatsapp": ("WhatsApp forward, {rest}", False),   # <account>-<last message id>, written by a private companion filer
}
_TAGS = "|".join(sorted(KINDS, key=len, reverse=True))   # longest first, so keep-note is not read as keep + "note-..."
# Notes the relay writes itself: they already started a run through `filed`, so the watcher skips them.
RELAY_NOTE = re.compile(r"^inbox/\d{4}-\d{2}-\d{2}T\d{4}Z-\d{17,20}\.md$")
# Notes other programs write COMPLETE in one commit (stamps: Keep and Tasks %H%M%S, Gmail and reconcile %H%M).
HOST_NOTE = re.compile(rf"^inbox/\d{{4}}-\d{{2}}-\d{{2}}T\d{{4}}(?:\d{{2}})?Z-({_TAGS})-([^/]+)\.md$")


def host_kind(path):
    """(kind, rest) of a server-written capture, or None for a typed note or a relay note."""
    m = HOST_NOTE.match(path)
    return (m.group(1), m.group(2)) if m else None


def describe(kind, rest):
    """The wording of the start message for this capture."""
    return KINDS[kind][0].format(rest=rest)


def carries_page(kind):
    """Whether page_hints should open the capture to find its project page."""
    return KINDS[kind][1]
