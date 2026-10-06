"""Organisation self-service: what an org admin can do without the platform key.

Q11 gives Organisation Admin the right to manage their own people and workspaces.
These tests are mostly about the ways that could go wrong: an admin addressing
another organisation's seats, a demoted admin keeping power through an
unexpired token, and an owner making the organisation unownable.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore

KEY = "platform-key"
PASSWORD = "password123"


def admin_auth():
    return {"authorization": f"Bearer {KEY}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "admin.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def make_org(c, org_id, name=None):
    res = c.post("/admin/organisations", headers=admin_auth(),
                 json={"id": org_id, "name": name or org_id.title()})
    assert res.status_code == 200, res.text
    return res.json()


def seat(c, email, org_id, category="finance"):
    """Create a seat and return (user, session token)."""
    res = c.post("/admin/users", headers=admin_auth(),
                 json={"email": email, "password": PASSWORD, "name": email,
                       "category": category, "organisation_id": org_id})
    assert res.status_code == 200, res.text
    user = res.json()
    token = c.post("/auth/login",
                   json={"email": email, "password": PASSWORD}).json()["token"]
    return user, token


def owner_of(c, org_id):
    c.post("/admin/organisations", headers=admin_auth(),
           json={"id": org_id, "name": org_id.title()})
    res = c.post("/admin/users", headers=admin_auth(),
                 json={"email": f"owner@{org_id}.test", "password": PASSWORD,
                       "name": "Owner", "category": "finance",
                       "organisation_id": org_id})
    assert res.status_code == 200, res.text
    user = res.json()
    role = c.post("/admin/organisations/role", headers=admin_auth(),
                  json={"organisation_id": org_id, "user_id": user["id"],
                        "role": "owner"})
    assert role.status_code == 200, role.text
    token = c.post("/auth/login",
                   json={"email": user["email"], "password": PASSWORD}).json()["token"]
    return user, token


def admin_of(c, org_id, email="admin@acme.test"):
    """A seat holding the admin role, plus its token."""
    user, _token = seat(c, email, org_id)
    res = c.post("/admin/organisations/role", headers=admin_auth(),
                 json={"organisation_id": org_id, "user_id": user["id"],
                       "role": "admin"})
    assert res.status_code == 200, res.text
    token = c.post("/auth/login",
                   json={"email": email, "password": PASSWORD}).json()["token"]
    return user, token


def auth(token):
    return {"authorization": f"Bearer {token}"}


class TestWhoMaySelfServe:
    def test_an_org_admin_can_list_their_own_people(self, env):
        c, _ = env
        owner, token = owner_of(c, "acme")
        res = c.get("/org/members", headers=auth(token))
        assert res.status_code == 200, res.text
        assert [m["email"] for m in res.json()["members"]] == [owner["email"]]

    def test_a_plain_member_cannot(self, env):
        c, _ = env
        owner_of(c, "acme")
        _member, token = seat(c, "member@acme.test", "acme")
        res = c.get("/org/members", headers=auth(token))
        assert res.status_code == 403

    def test_a_free_tier_user_cannot(self, env):
        """The sample organisation has no owner and is not a customer."""
        c, _ = env
        signup = c.post("/auth/signup", json={
            "email": "free@visitor.com", "password": PASSWORD,
            "name": "V", "category": "finance"}).json()
        res = c.get("/org/members", headers=auth(signup["token"]))
        assert res.status_code == 403

    def test_an_anonymous_caller_cannot(self, env):
        c, _ = env
        assert c.get("/org/members").status_code == 401

    def test_the_platform_key_does_not_grant_self_service_identity(self, env):
        """The platform key is not a customer identity; self-service routes need
        a real seat, so there is no organisation to act on."""
        c, _ = env
        assert c.get("/org/members", headers=admin_auth()).status_code == 401

    def test_the_response_reports_the_callers_own_role(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        assert c.get("/org/members", headers=auth(token)).json()["your_role"] == "owner"


class TestCrossOrganisationIsolation:
    """The property that matters most: ids are not secret, so authorisation
    cannot rest on them."""

    def test_an_admin_cannot_list_another_organisations_people(self, env):
        c, _ = env
        owner_of(c, "acme")
        _a, admin_token = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": _a["id"], "role": "admin"})
        make_org(c, "globex")
        globex_owner, _tok = owner_of(c, "globex")
        res = c.get("/org/members", headers=auth(admin_token))
        assert res.status_code == 200
        assert globex_owner["email"] not in [
            m["email"] for m in res.json()["members"]
        ]

    def test_an_admin_cannot_remove_another_organisations_seat(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        make_org(c, "globex")
        victim, _vt = seat(c, "victim@globex.test", "globex")
        res = c.delete(f"/org/members/{victim['id']}", headers=auth(admin_token))
        assert res.status_code == 404
        assert c.app.state.user_store.get(victim["id"]) is not None

    def test_an_admin_cannot_change_another_organisations_roles(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        make_org(c, "globex")
        victim, _vt = seat(c, "victim@globex.test", "globex")
        res = c.post(f"/org/members/{victim['id']}/role",
                     headers=auth(admin_token), json={"role": "admin"})
        assert res.status_code == 404

    def test_an_admin_cannot_grant_workspace_access_to_another_org(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        make_org(c, "globex")
        victim, _vt = seat(c, "victim@globex.test", "globex")
        res = c.post(f"/org/members/{victim['id']}/workspaces",
                     headers=auth(admin_token), json={"categories": ["hr"]})
        assert res.status_code == 404

    def test_a_created_seat_lands_in_the_callers_organisation(self, env):
        """There is no organisation_id in the request, so there is nothing to
        point somewhere else."""
        c, _ = env
        _owner, token = owner_of(c, "acme")
        make_org(c, "globex")
        res = c.post("/org/members", headers=auth(token), json={
            "email": "new@acme.test", "password": PASSWORD, "name": "New",
            "category": "finance"})
        assert res.status_code == 200, res.text
        assert res.json()["organisation_id"] == "acme"


class TestDemotion:
    def test_a_demoted_admin_loses_access_on_their_existing_token(self, env):
        """A token issued while someone was an admin outlives the demotion."""
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        assert c.get("/org/members", headers=auth(admin_token)).status_code == 200
        c.post("/admin/organisations/role/revoke", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        assert c.get("/org/members", headers=auth(admin_token)).status_code == 403

    def test_a_demoted_admin_cannot_still_create_seats(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        c.post("/admin/organisations/role/revoke", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        res = c.post("/org/members", headers=auth(admin_token), json={
            "email": "sneaky@acme.test", "password": PASSWORD, "name": "S",
            "category": "finance"})
        assert res.status_code == 403


class TestOwnership:
    def test_ownership_cannot_be_granted_it_is_transferred(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        other, _t = seat(c, "other@acme.test", "acme")
        res = c.post(f"/org/members/{other['id']}/role", headers=auth(token),
                     json={"role": "owner"})
        assert res.status_code == 409

    def test_only_the_owner_can_transfer(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        other, _t2 = seat(c, "other@acme.test", "acme")
        res = c.post("/org/transfer-ownership", headers=auth(admin_token),
                     json={"user_id": other["id"]})
        assert res.status_code == 403

    def test_the_owner_can_transfer(self, env):
        c, _ = env
        owner, token = owner_of(c, "acme")
        other, _t = seat(c, "other@acme.test", "acme")
        res = c.post("/org/transfer-ownership", headers=auth(token),
                     json={"user_id": other["id"]})
        assert res.status_code == 200, res.text
        roles = {r["user_id"]: r["role"]
                 for r in c.app.state.user_store.org_roles_for("acme")}
        assert roles[other["id"]] == "owner"
        assert roles[owner["id"]] == "admin"

    def test_transferring_demotes_the_previous_owner(self, env):
        """One operation, so there is never a moment with two owners or none."""
        c, _ = env
        owner, token = owner_of(c, "acme")
        other, _t = seat(c, "other@acme.test", "acme")
        c.post("/org/transfer-ownership", headers=auth(token),
               json={"user_id": other["id"]})
        assert c.app.state.user_store.org_role(owner["id"]) == "admin"

    def test_the_owner_cannot_be_removed(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        owner = c.app.state.user_store.list_users_in_organisation("acme")[0]
        res = c.delete(f"/org/members/{owner['id']}", headers=auth(admin_token))
        assert res.status_code == 409

    def test_an_admin_cannot_remove_their_own_seat(self, env):
        c, _ = env
        owner_of(c, "acme")
        admin_user, _t = seat(c, "admin@acme.test", "acme")
        c.post("/admin/organisations/role", headers=admin_auth(),
               json={"organisation_id": "acme", "user_id": admin_user["id"],
                     "role": "admin"})
        admin_token = c.post("/auth/login", json={
            "email": "admin@acme.test", "password": PASSWORD}).json()["token"]
        res = c.delete(f"/org/members/{admin_user['id']}",
                       headers=auth(admin_token))
        assert res.status_code == 409


class TestWorkspaceAccess:
    def test_an_admin_can_grant_a_workspace(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        member, _t = seat(c, "member@acme.test", "acme")
        res = c.post(f"/org/members/{member['id']}/workspaces",
                     headers=auth(token), json={"categories": ["hr"]})
        assert res.status_code == 200, res.text
        assert "hr" in res.json()["workspaces"]

    def test_an_admin_can_revoke_a_workspace(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        member, _t = seat(c, "member@acme.test", "acme")
        c.post(f"/org/members/{member['id']}/workspaces", headers=auth(token),
               json={"categories": ["hr"]})
        res = c.post(f"/org/members/{member['id']}/workspaces",
                     headers=auth(token), json={"revoke": ["hr"]})
        assert res.status_code == 200, res.text
        assert "hr" not in res.json()["workspaces"]

    def test_the_last_workspace_cannot_be_revoked(self, env):
        """Otherwise the seat is stranded with nowhere to work."""
        c, _ = env
        _owner, token = owner_of(c, "acme")
        member, _t = seat(c, "member@acme.test", "acme")
        res = c.post(f"/org/members/{member['id']}/workspaces",
                     headers=auth(token), json={"revoke": ["finance"]})
        assert res.status_code == 409

    def test_an_unknown_workspace_is_refused(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        member, _t = seat(c, "member@acme.test", "acme")
        res = c.post(f"/org/members/{member['id']}/workspaces",
                     headers=auth(token), json={"categories": ["../escape"]})
        assert res.status_code == 422

    def test_no_customer_side_content_upload_exists(self, env):
        """Q1: there is no customer-side upload path, whatever the caller's role.

        Content is the platform administrator's alone, so this must not exist
        for an owner either."""
        c, _ = env
        _owner, token = owner_of(c, "acme")
        for path, body in (
            ("/admin/upload", {"category": "finance"}),
        ):
            res = c.post(path, headers=auth(token), data=body,
                         files={"files": ("x.pdf", b"%PDF", "application/pdf")})
            assert res.status_code in (401, 403, 404), res.status_code
        res = c.post("/admin/library/upload", headers=auth(token),
                     files={"files": ("x.pdf", b"%PDF", "application/pdf")})
        assert res.status_code in (401, 403), res.status_code


class TestSeatCreation:
    def test_a_seat_is_created_with_the_requested_workspaces(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        res = c.post("/org/members", headers=auth(token), json={
            "email": "new@acme.test", "password": PASSWORD, "name": "New",
            "category": "finance", "workspaces": ["finance", "hr"]})
        assert res.status_code == 200, res.text
        assert sorted(res.json()["workspaces"]) == ["finance", "hr"]

    def test_a_duplicate_email_is_refused(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        payload = {"email": "dupe@acme.test", "password": PASSWORD,
                   "name": "D", "category": "finance"}
        assert c.post("/org/members", headers=auth(token),
                      json=payload).status_code == 200
        assert c.post("/org/members", headers=auth(token),
                      json=payload).status_code == 422

    def test_a_bad_workspace_does_not_leave_a_half_provisioned_seat(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        res = c.post("/org/members", headers=auth(token), json={
            "email": "half@acme.test", "password": PASSWORD, "name": "H",
            "category": "finance", "workspaces": ["hr", "nope"]})
        assert res.status_code == 422
        users = c.app.state.user_store.list_users_in_organisation("acme")
        assert "half@acme.test" not in [u["email"] for u in users]

    def test_self_service_actions_are_audited(self, env):
        c, _ = env
        _owner, token = owner_of(c, "acme")
        c.post("/org/members", headers=auth(token), json={
            "email": "audited@acme.test", "password": PASSWORD, "name": "A",
            "category": "finance"})
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "org.member_create" in kinds