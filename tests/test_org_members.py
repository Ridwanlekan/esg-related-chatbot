"""Organisation seats, roles and workspace access (Section 3.3).

An organisation that exists but has no owner, no additional admin and no way to
grant a workspace is not operable. These tests pin the rules that make it one:
exactly one owner (Q9), no Workspace Admin (Q10), and a seat that can never be
left with nowhere to work.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore

KEY = "secret-admin-key"


def auth():
    return {"authorization": f"Bearer {KEY}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def make_org(c, org_id="acme"):
    res = c.post("/admin/organisations", headers=auth(),
                 json={"id": org_id, "name": org_id.title()})
    assert res.status_code == 200, res.text
    return res.json()


def make_user(c, email, org_id="acme", category="finance"):
    users = c.app.state.user_store
    u = users.create_user(email, "password123", email, category,
                          organisation_id=org_id)
    return u["id"]


def set_role(c, org_id, user_id, role):
    return c.post("/admin/organisations/role", headers=auth(),
                  json={"organisation_id": org_id, "user_id": user_id, "role": role})


def members(c, org_id="acme"):
    return c.get(f"/admin/organisations/{org_id}/members", headers=auth())


class TestMembersEndpoint:
    def test_lists_members_with_role_and_workspaces(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        set_role(c, "acme", uid, "owner")
        body = members(c).json()
        assert body["organisation_id"] == "acme"
        m = body["members"][0]
        assert m["email"] == "a@acme.com"
        assert m["org_role"] == "owner"
        assert m["workspaces"] == ["finance"]
        assert m["primary_workspace"] == "finance"

    def test_new_organisation_has_no_members(self, env):
        c, _ = env
        make_org(c)
        assert members(c).json()["members"] == []

    def test_members_of_other_organisations_are_not_listed(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        make_user(c, "g@globex.com", org_id="globex")
        assert [m["email"] for m in members(c).json()["members"]] == []

    def test_advertises_the_legal_roles_and_workspaces(self, env):
        c, _ = env
        make_org(c)
        body = members(c).json()
        assert body["org_roles"] == ["admin", "owner"]
        assert "finance" in body["available_workspaces"]

    def test_unknown_organisation_is_404(self, env):
        c, _ = env
        assert members(c, "nope").status_code == 404

    def test_requires_admin_key(self, env):
        c, _ = env
        make_org(c)
        assert c.get("/admin/organisations/acme/members").status_code == 401


class TestOwnership:
    def test_owner_can_be_granted(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        assert set_role(c, "acme", uid, "owner").status_code == 200

    def test_second_owner_is_refused(self, env):
        """Q9: one owner per organisation, enforced rather than merely discouraged."""
        c, _ = env
        make_org(c)
        make_user(c, "a@acme.com")
        second = make_user(c, "b@acme.com")
        set_role(c, "acme",
                 [m for m in members(c).json()["members"]
                  if m["email"] == "a@acme.com"][0]["id"], "owner")
        res = set_role(c, "acme", second, "owner")
        assert res.status_code == 422
        assert "already has an owner" in res.json()["detail"]

    def test_unknown_role_is_refused(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        res = set_role(c, "acme", uid, "workspace-admin")
        assert res.status_code == 422
        assert "Role must be one of" in res.json()["detail"]

    def test_user_outside_the_organisation_is_refused(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        other = make_user(c, "g@globex.com", org_id="globex")
        res = set_role(c, "acme", other, "admin")
        assert res.status_code == 422

    def test_transfer_moves_owner_and_demotes_the_old_one(self, env):
        c, _ = env
        make_org(c)
        first = make_user(c, "a@acme.com")
        second = make_user(c, "b@acme.com")
        set_role(c, "acme", first, "owner")
        res = c.post("/admin/organisations/transfer-ownership", headers=auth(),
                     json={"organisation_id": "acme", "from_user_id": first,
                           "to_user_id": second})
        assert res.status_code == 200, res.text
        roles = {r["user_id"]: r["role"] for r in res.json()["roles"]}
        assert roles[second] == "owner"
        assert roles[first] == "admin"

    def test_transfer_from_a_non_owner_is_refused(self, env):
        c, _ = env
        make_org(c)
        first = make_user(c, "a@acme.com")
        second = make_user(c, "b@acme.com")
        res = c.post("/admin/organisations/transfer-ownership", headers=auth(),
                     json={"organisation_id": "acme", "from_user_id": first,
                           "to_user_id": second})
        assert res.status_code == 422

    def test_transfer_to_an_outsider_is_refused(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        first = make_user(c, "a@acme.com")
        outsider = make_user(c, "g@globex.com", org_id="globex")
        set_role(c, "acme", first, "owner")
        res = c.post("/admin/organisations/transfer-ownership", headers=auth(),
                     json={"organisation_id": "acme", "from_user_id": first,
                           "to_user_id": outsider})
        assert res.status_code == 422


class TestRoleRevocation:
    def test_admin_role_can_be_revoked(self, env):
        c, _ = env
        make_org(c)
        owner = make_user(c, "a@acme.com")
        other = make_user(c, "b@acme.com")
        set_role(c, "acme", owner, "owner")
        set_role(c, "acme", other, "admin")
        res = c.post("/admin/organisations/role/revoke", headers=auth(),
                     json={"organisation_id": "acme", "user_id": other, "role": "admin"})
        assert res.status_code == 200, res.text
        rows = {m["email"]: m["org_role"] for m in members(c).json()["members"]}
        assert rows["b@acme.com"] is None
        assert rows["a@acme.com"] == "owner"

    def test_owner_cannot_be_revoked_while_the_only_one(self, env):
        """Q9 read strictly: an organisation with no owner is unownable later."""
        c, _ = env
        make_org(c)
        owner = make_user(c, "a@acme.com")
        set_role(c, "acme", owner, "owner")
        res = c.post("/admin/organisations/role/revoke", headers=auth(),
                     json={"organisation_id": "acme", "user_id": owner, "role": "owner"})
        assert res.status_code == 409
        assert "Transfer ownership" in res.json()["detail"]

    def test_previous_owner_can_be_demoted_after_a_transfer(self, env):
        c, _ = env
        make_org(c)
        first = make_user(c, "a@acme.com")
        second = make_user(c, "b@acme.com")
        set_role(c, "acme", first, "owner")
        c.post("/admin/organisations/transfer-ownership", headers=auth(),
               json={"organisation_id": "acme", "from_user_id": first,
                     "to_user_id": second})
        res = c.post("/admin/organisations/role/revoke", headers=auth(),
                     json={"organisation_id": "acme", "user_id": first, "role": "admin"})
        assert res.status_code == 200

    def test_revoke_of_an_outsider_is_404(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        outsider = make_user(c, "g@globex.com", org_id="globex")
        res = c.post("/admin/organisations/role/revoke", headers=auth(),
                     json={"organisation_id": "acme", "user_id": outsider, "role": "admin"})
        assert res.status_code == 404


class TestWorkspaceAccess:
    def test_a_seat_can_be_given_a_second_workspace(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        res = c.post("/admin/organisations/workspaces/add", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid, "category": "hr"})
        assert res.status_code == 200, res.text
        assert "hr" in members(c).json()["members"][0]["workspaces"]

    def test_adding_twice_is_idempotent(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        for _ in range(2):
            c.post("/admin/organisations/workspaces/add", headers=auth(),
                   json={"organisation_id": "acme", "user_id": uid, "category": "hr"})
        assert members(c).json()["members"][0]["workspaces"].count("hr") == 1

    def test_unknown_workspace_is_refused(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        res = c.post("/admin/organisations/workspaces/add", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid,
                           "category": "not-a-workspace"})
        assert res.status_code == 422

    def test_the_only_workspace_cannot_be_removed(self, env):
        """A seat with zero workspaces has no reachable content."""
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        res = c.post("/admin/organisations/workspaces/remove", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid,
                           "category": "finance"})
        assert res.status_code == 422
        assert "only workspace" in res.json()["detail"]
        assert members(c).json()["members"][0]["workspaces"] == ["finance"]

    def test_a_workspace_can_be_removed_when_another_remains(self, env):
        c, _ = env
        make_org(c)
        uid = make_user(c, "a@acme.com")
        c.post("/admin/organisations/workspaces/add", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "category": "hr"})
        res = c.post("/admin/organisations/workspaces/remove", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid, "category": "finance"})
        assert res.status_code == 200, res.text
        assert members(c).json()["members"][0]["workspaces"] == ["hr"]

    def test_outsider_workspace_change_is_404(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        outsider = make_user(c, "g@globex.com", org_id="globex")
        res = c.post("/admin/organisations/workspaces/add", headers=auth(),
                     json={"organisation_id": "acme", "user_id": outsider,
                           "category": "hr"})
        assert res.status_code == 404


class TestFreeToPaidConversion:
    """Q1: a free visitor upgrades and keeps their identity and history."""

    def _signup(self, c, email="visitor@x.com", category="finance"):
        res = c.post("/auth/signup", json={
            "email": email, "password": "password123", "name": "V",
            "category": category,
        })
        assert res.status_code == 200, res.text
        return res.json()["token"], res.json()["user"]["id"]

    def test_a_free_user_is_moved_with_named_workspaces(self, env):
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c)
        res = c.post("/admin/organisations/move", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid,
                           "workspaces": ["finance", "hr"]})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["from_organisation"] == "_sample"
        assert body["to_organisation"] == "acme"
        assert set(body["user"]["workspaces"]) == {"finance", "hr"}

    def test_the_user_keeps_their_account_and_can_sign_in(self, env):
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c)
        c.post("/admin/organisations/move", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "workspaces": ["hr"]})
        login = c.post("/auth/login", json={"email": "visitor@x.com",
                                            "password": "password123"})
        assert login.status_code == 200
        assert login.json()["user"]["organisation_id"] == "acme"

    def test_the_primary_workspace_becomes_one_they_were_given(self, env):
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c)
        c.post("/admin/organisations/move", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "workspaces": ["hr"]})
        me = c.post("/auth/login", json={"email": "visitor@x.com",
                                         "password": "password123"}).json()
        assert me["user"]["category"] == "hr"

    def test_membership_does_not_inherit_every_workspace(self, env):
        """Access is explicit, so a converted user never arrives with more than
        the administrator granted."""
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c, category="finance")
        c.post("/admin/organisations/move", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "workspaces": ["finance"]})
        row = [m for m in members(c, "acme").json()["members"]
               if m["email"] == "visitor@x.com"][0]
        assert row["workspaces"] == ["finance"]

    def test_a_role_does_not_follow_the_user_out_of_the_sample_org(self, env):
        c, _ = env
        make_org(c, "acme")
        users = c.app.state.user_store
        _token, uid = self._signup(c)
        users.set_org_role("_sample", uid, "admin")
        c.post("/admin/organisations/move", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "workspaces": ["finance"]})
        assert users.org_role(uid) is None

    def test_moving_into_the_sample_org_is_refused(self, env):
        """Section 3.5: the shared organisation cannot receive paying accounts.

        A paying user must not be dumped into _sample, where every free visitor
        shares the same served copy.
        """
        c, _ = env
        make_org(c, "acme")
        uid = make_user(c, "p@acme.com")
        res = c.post("/admin/organisations/move", headers=auth(),
                     json={"organisation_id": "_sample", "user_id": uid,
                           "workspaces": ["finance"]})
        assert res.status_code == 422
        assert "system-owned" in res.json()["detail"]

    def test_moving_into_the_same_organisation_is_refused(self, env):
        c, _ = env
        make_org(c, "acme")
        uid = make_user(c, "a@acme.com")
        res = c.post("/admin/organisations/move", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid,
                           "workspaces": ["finance"]})
        assert res.status_code == 409

    def test_unknown_workspace_is_refused(self, env):
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c)
        res = c.post("/admin/organisations/move", headers=auth(),
                     json={"organisation_id": "acme", "user_id": uid,
                           "workspaces": ["not-a-workspace"]})
        assert res.status_code == 422

    def test_conversion_is_audited(self, env):
        c, _ = env
        make_org(c, "acme")
        _token, uid = self._signup(c)
        c.post("/admin/organisations/move", headers=auth(),
               json={"organisation_id": "acme", "user_id": uid, "workspaces": ["finance"]})
        events = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "user.move" in events


class TestConsoleWiring:
    def test_move_endpoint_requires_admin_key(self, env):
        c, _ = env
        make_org(c, "acme")
        res = c.post("/admin/organisations/move",
                     json={"organisation_id": "acme", "user_id": "x",
                           "workspaces": ["finance"]})
        assert res.status_code == 401

    def test_org_role_endpoints_require_admin_key(self, env):
        c, _ = env
        for path in ("/admin/organisations/role",
                     "/admin/organisations/role/revoke",
                     "/admin/organisations/transfer-ownership",
                     "/admin/organisations/workspaces/add",
                     "/admin/organisations/workspaces/remove"):
            res = c.post(path, json={
                "organisation_id": "acme", "user_id": "x", "role": "admin",
                "category": "finance", "from_user_id": "x", "to_user_id": "y",
                "workspaces": ["finance"],
            })
            assert res.status_code == 401, path