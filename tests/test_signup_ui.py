"""The signup form must not ask the caller to pick a workspace.

Free self-service signup lands in the shared sample organisation, so the
category/workspace picker was removed. These tests pin the form to name/email/
password so the unbounded picker cannot silently return.
"""

import re
from pathlib import Path

UI = (Path(__file__).resolve().parents[1] / "src/chatbot/static/ui.html").read_text()


def test_signup_form_has_no_workspace_picker():
    form = re.search(
        r'<div id="signup-form" class="hidden">(.*?)</div>\s*<div id="auth-err"',
        UI,
        re.S,
    )
    assert form
    fields = form.group(1)
    assert '<select id="signup-cat">' not in fields
    assert 'id="cat-pills"' not in fields


def test_no_category_control_remains():
    assert 'id="signup-cat"' not in UI
    assert 'id="cat-pills"' not in UI


def test_signup_payload_does_not_send_a_category():
    body = re.search(r"async function submitAuth\(endpoint\) \{(.*?)\n\}", UI, re.S)
    assert body
    assert "selectedCat" not in body.group(1)


def test_invite_badge_is_rendered_instead_of_a_picker():
    assert 'id="signup-invite-note"' in UI
    body = re.search(r"async function loadWorkspaces\(\) \{(.*?)\n\}", UI, re.S)
    assert body
    assert "signup-invite-note" in body.group(1)
