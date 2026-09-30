"""Workspace deletion must confirm once, and state exactly what is destroyed.

deleteWorkspace() used to confirm "it will stop appearing for signups", then hit
the server's 409 guard and ask a second time. The list response already carries
data_file_count, so the prompt states the real cost up front and the second
prompt is only a backstop for a count that went stale.
"""

import re
from pathlib import Path

ADMIN = (Path(__file__).resolve().parents[1] / "src/chatbot/static/admin.html").read_text()


def _fn(name):
    match = re.search(rf"async function {name}\(.*?\n\}}", ADMIN, re.S)
    assert match, f"{name}() not found"
    return match.group(0)


def test_delete_button_passes_the_document_count():
    row = re.search(r"del\.onclick = \(\) => deleteWorkspace\((.*?)\);", ADMIN)
    assert row, "delete button not wired"
    assert "data_file_count" in row.group(1), \
        "delete prompt cannot state the count without it"


def test_prompt_states_what_is_permanently_lost():
    body = _fn("deleteWorkspace")
    assert "cannot be undone" in body
    assert "permanently deletes" in body
    # pluralisation must not print "1 documents"
    assert '(n === 1 ? "" : "s")' in body


def test_purge_is_chosen_from_the_count_not_from_a_failed_request():
    """The destructive flag is decided up front, so the common path is one
    confirm and one request."""
    body = _fn("deleteWorkspace")
    assert 'n > 0 ? "?purge=true" : ""' in body


def test_stale_count_backstop_still_guards():
    """A count rendered before an upload must not silently delete it."""
    body = _fn("deleteWorkspace")
    assert "e.status === 409" in body
    assert "Delete anyway?" in body
