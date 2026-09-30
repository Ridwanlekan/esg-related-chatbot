"""The signup workspace picker must stay a bounded control.

It used to render one clickable pill per workspace inside a flex row, so every
workspace an admin created widened the auth card. This pins the control to a
single <select> so the picker cannot silently grow again.
"""

import re
from pathlib import Path

UI = (Path(__file__).resolve().parents[1] / "src/chatbot/static/ui.html").read_text()


def test_signup_workspace_picker_is_a_select():
    """The category control in the Create-account form must be a dropdown."""
    form = re.search(r'<div id="signup-form".*?</div>\s*</div>', UI, re.S)
    assert form, "signup form not found"
    assert '<select id="signup-cat">' in form.group(0)
    assert 'class="cats"' not in form.group(0)


def test_no_per_workspace_pills_remain():
    """No control may render a growable list of one-node-per-workspace."""
    assert 'class="cats"' not in UI
    assert ".cat.active" not in UI
    # nothing builds a .cat element any more
    assert not re.search(r'class="cat[ "]', UI)


def test_workspace_options_are_built_as_option_elements():
    """Options, so the control height is independent of workspace count."""
    body = re.search(r'async function loadWorkspaces\(\).*?\n\}', UI, re.S).group(0)
    assert "<option value=" in body
    assert "sel.innerHTML" in body
    # the select is marked disabled rather than left editable when invite-only
    assert "sel.disabled = !!inviteCategory" in body


def test_select_style_is_defined_for_the_dark_auth_card():
    """A bare <select> would render as a white native box on the dark card."""
    assert re.search(r"\.auth-card input, \.auth-card select\s*\{", UI)
