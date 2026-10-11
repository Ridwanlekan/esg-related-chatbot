"""The Organisations tab has to match the API it drives, and the destructive
paths have to stay guarded.

These are source-level checks on admin.html, in the same spirit as
test_admin_ui.py: the failure this prevents is a console that renders a
plausible screen wired to endpoints that do not exist, or a delete that fires
on one stray click. Behaviour that can be tested through HTTP is tested through
HTTP in test_content_assignment.py; what is left here is the wiring itself.
"""

import re
from pathlib import Path

ADMIN = (Path(__file__).resolve().parents[1] / "src/chatbot/static/admin.html").read_text()


def _fn(name):
    match = re.search(rf"async function {name}\(.*?\n\}}", ADMIN, re.S)
    assert match, f"{name}() not found"
    return match.group(0)


def _sync_fn(name):
    match = re.search(rf"function {name}\(.*?\n\}}", ADMIN, re.S)
    assert match, f"{name}() not found"
    return match.group(0)


def test_organisations_tab_is_registered():
    assert 'id="tab-organisations"' in ADMIN
    assert 'id="view-organisations"' in ADMIN
    assert '["tab-organisations", "view-organisations"]' in ADMIN


def test_every_tab_button_actually_has_a_click_listener():
    """A tab can be declared, rendered, and handled by showTab and still do
    nothing on click if no listener was ever attached — which is exactly how
    the Organisations tab shipped dead: showTab handled it, nobody called it."""
    declared = set(re.findall(r'\["(tab-[a-z]+)",', ADMIN))
    wired = set(re.findall(r'\$\("(tab-[a-z]+)"\)\.addEventListener\("click"', ADMIN))
    missing = declared - wired
    assert not missing, f"tabs declared but with no click listener: {sorted(missing)}"


def test_switching_to_the_tab_loads_both_halves():
    """The tab is useless if it opens empty, and the two halves load separately."""
    body = _sync_fn("showTab")
    assert 'name === "tab-organisations"' in body
    assert "loadLibrary()" in body
    assert "loadOrganisations()" in body


def test_every_element_the_tab_touches_exists_in_the_markup():
    """A $("...") with no matching id renders an empty screen, not an error."""
    section = re.search(
        r'<section id="view-organisations".*?</section>', ADMIN, re.S
    ).group(0)
    body = ADMIN.split('/* ---------------- organisations and master library ---------------- */')[1]
    body = body.split("/* ---------------- workspaces ---------------- */")[0]
    # $("...") with a hyphen is an element id; without one it is elm("tag", ...)
    # building a DOM node, which is not looked up in the markup.
    ids = set(re.findall(r'\$?\("([a-z0-9]+-[a-z0-9-]+)"\)', body)) | set(
        re.findall(r'\$?\("([a-z0-9]+-[a-z0-9-]+)"\)', section)
    )
    declared = set(re.findall(r'id="([a-z0-9-]+)"', ADMIN))
    missing = {i for i in ids if i not in declared}
    assert not missing, f"admin tab references ids that do not exist: {sorted(missing)}"


class TestEndpointsMatchTheApi:
    def test_library_upload_posts_to_the_real_endpoint(self):
        body = _fn("uploadLibrary")
        assert '"/admin/library/upload"' in body
        assert 'method: "POST"' in body

    def test_library_list_reads_the_real_endpoint(self):
        assert 'api("/admin/library"' in ADMIN

    def test_assignment_posts_both_organisation_and_files(self):
        """Assigning without the filename would silently assign nothing."""
        body = _fn("assignContent")
        assert '"/admin/library/assign"' in body
        assert "organisation_id: selectedOrg.id" in body
        assert "filenames: files" in body
        assert "categories: [cat]" in body

    def test_unassignment_names_the_workspace_too(self):
        body = _fn("unassignContent")
        assert '"/admin/library/unassign"' in body
        assert "categories: [cat]" in body
        assert "filenames: [filename]" in body

    def test_organisation_content_is_read_per_organisation(self):
        body = _fn("loadOrgContent")
        assert '"/admin/organisations/"' in body
        assert "encodeURIComponent(selectedOrg.id)" in body
        assert '"/content"' in body

    def test_create_and_delete_use_the_organisation_endpoints(self):
        assert '"/admin/organisations"' in _fn("createOrganisation")
        assert '"/admin/organisations/delete"' in _fn("deleteOrganisation")


class TestDestructivePaths:
    def test_organisation_delete_states_what_is_permanently_lost(self):
        body = _fn("deleteOrganisation")
        assert "cannot be undone" in body
        assert "Permanently delete" in body
        assert "Master documents in the library are kept" in body
        # pluralisation must not print "1 users"
        assert '(n === 1 ? "" : "s")' in body

    def test_organisation_delete_confirms_before_the_request(self):
        """The server re-checks with the echoed id; the console must not skip
        straight to the call on one stray click."""
        body = _fn("deleteOrganisation")
        first = body.index("confirm(")
        call = body.index('"/admin/organisations/delete"')
        assert first < call, "the confirmation must come before the request"
        assert body.count("confirm(") >= 2, "one prompt is not a two-step confirm"

    def test_delete_sends_the_id_as_confirmation(self):
        body = _fn("deleteOrganisation")
        assert "organisation_id: org.id" in body
        assert "confirm: org.id" in body

    def test_system_organisations_offer_no_delete_button(self):
        """_sample holds the free tier's shared content; it must not be
        deletable from the console at all, not merely refused server-side."""
        body = _sync_fn("renderOrganisations")
        assert "if (!o.system_owned)" in body
        assert "deleteOrganisation(o)" in body

    def test_unassignment_confirms_that_users_lose_it(self):
        body = _fn("unassignContent")
        assert "Users lose it immediately" in body


class TestHonestRendering:
    def test_a_missing_master_file_is_reported(self):
        """A registered master whose file is gone would fail to assign with no
        explanation in the list."""
        body = _sync_fn("renderLibrary")
        assert "!d.present" in body
        assert "missing from the library" in body

    def test_a_missing_served_copy_is_reported(self):
        body = _sync_fn("renderOrgContent")
        assert "!r.present" in body
        assert "file missing from storage" in body

    def test_the_assign_button_states_its_workspace(self):
        body = _sync_fn("renderOrgContent")
        assert '"Assign to "' in body

    def test_library_shows_which_organisations_hold_each_document(self):
        body = _sync_fn("renderLibrary")
        assert "assigned_to" in body
        assert "not assigned" in body

    def test_already_assigned_checkboxes_are_disabled(self):
        """Re-assigning what is already there would look like a no-op that
        actually reindexed."""
        body = _sync_fn("renderOrgContent")
        assert "have.has(d.filename)" in body
        assert "cb.disabled = cb.checked" in body


class TestSeatManagementUi:
    def test_members_panel_loads_with_the_organisation(self):
        """Opening an organisation must not show its content with a blank
        people list beside it."""
        body = _sync_fn("selectOrganisation")
        assert "loadOrgContent()" in body
        assert "loadOrgMembers()" in body

    def test_members_are_read_from_the_members_endpoint(self):
        body = _fn("loadOrgMembers")
        assert '"/admin/organisations/"' in body
        assert '"/members"' in body

    def test_workspace_toggles_call_both_directions(self):
        body = _sync_fn("orgMemberRow")
        assert '"add"' in body
        assert '"remove"' in body

    def test_a_refused_workspace_change_is_reverted_in_the_ui(self):
        """The server will not strand a seat with no workspace; if the console
        left the box ticked it would claim access the user does not have."""
        body = _sync_fn("orgMemberRow")
        assert "cb.checked = !cb.checked" in body
        assert "Change refused" in body

    def test_role_changes_use_the_role_endpoint(self):
        body = _sync_fn("changeRole")
        assert '"/admin/organisations/role"' in body

    def test_revoking_an_owner_is_not_offered(self):
        """Q9: the single owner is transferred, never simply removed."""
        body = _sync_fn("orgMemberRow")
        assert 'm.org_role === "owner"' in body
        assert "transferOwnership(m.id" in body
        assert 'if (m.org_role) {' in body

    def test_transfer_names_both_parties(self):
        body = _sync_fn("transferOwnership")
        assert "becomes an admin" in body
        assert "from_user_id: fromUserId" in body
        assert "to_user_id: toUserId" in body

    def test_transfer_needs_a_confirmation(self):
        body = _sync_fn("transferOwnership")
        assert body.index("confirm(") < body.index('"/admin/organisations/transfer-ownership"')

    def test_conversion_states_what_is_kept_and_lost(self):
        """Q1: the person keeps their account and history, and must be told what
        access they lose."""
        body = _fn("convertFreeUser")
        assert "keep their account and conversation history" in body
        assert "lose access to any workspace not listed" in body

    def test_conversion_uses_the_move_endpoint(self):
        body = _fn("convertFreeUser")
        assert '"/admin/organisations/move"' in body
        assert "workspaces: cats" in body


class TestCreateUserOrganisation:
    def test_create_user_can_target_an_organisation(self):
        """Paid accounts are created into a customer organisation (Q22)."""
        body = _fn("createUser")
        assert "payload.organisation_id = org" in body

    def test_blank_organisation_stays_the_free_path(self):
        body = _fn("createUser")
        assert 'if (org) payload.organisation_id = org' in body

    def test_console_offers_the_free_path_as_the_default(self):
        body = _fn("loadOrgOptions")
        assert "Free (sample) — self-service" in body
        assert 'sel.value = ""' in body


class TestInviteOrganisation:
    """Invites are how a customer's own people join it, so the form has to be
    able to name one — and has to be able to say "no organisation" too, since
    that is what every invite meant before."""

    def test_invite_form_offers_an_organisation_choice(self):
        assert 'id="invite-org"' in ADMIN

    def test_create_invite_sends_the_choice(self):
        body = _fn("createInvite")
        assert "payload.organisation_id = org" in body
        assert 'if (org) payload.organisation_id = org' in body

    def test_the_select_is_filled_from_the_organisation_list(self):
        body = _fn("loadOrgOptions")
        assert '$("invite-org")' in body
        assert "Free (sample) — self-service" in body

    def test_the_invite_table_names_the_organisation(self):
        assert "<th>Organisation</th>" in ADMIN
        assert 'inv.organisation_id || "Free (sample)"' in ADMIN


def test_upload_makes_no_one_able_to_see_the_document():
    """The toast must not imply an upload is already live."""
    body = _fn("uploadLibrary")
    assert "Assign it to an organisation to make it visible" in body