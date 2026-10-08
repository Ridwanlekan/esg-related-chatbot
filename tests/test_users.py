import pytest

from chatbot.users import (
    DEFAULT_ORGANISATION_ID,
    SAMPLE_ORGANISATION_ID,
    UserStore,
    sign_jwt,
    verify_jwt,
)


@pytest.fixture
def store(tmp_path):
    return UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret")


def test_create_and_authenticate(store):
    user = store.create_user("Ada@Bank.com", "correct-horse-42", "Ada Lovelace", "hr")
    assert user["email"] == "ada@bank.com"
    assert user["category"] == "hr"
    assert "password_hash" not in user
    assert store.verify("ada@bank.com", "correct-horse-42")["id"] == user["id"]
    assert store.verify("ada@bank.com", "wrong") is None


def test_duplicate_email_rejected(store):
    store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="already exists"):
        store.create_user("a@b.com", "password123", "B", "finance")


def test_password_too_short_rejected(store):
    with pytest.raises(ValueError, match="at least 8"):
        store.create_user("a@b.com", "short", "A", "finance")


def test_invalid_email_rejected(store):
    with pytest.raises(ValueError, match="valid email"):
        store.create_user("not-an-email", "password123", "A", "finance")


def test_invalid_category_rejected(store):
    with pytest.raises(ValueError, match="Category must be one of"):
        store.create_user("a@b.com", "password123", "A", "admin")


def test_passwords_hashed_and_salted(store):
    store.create_user("a@b.com", "password123", "A", "finance")
    store.create_user("c@b.com", "password123", "C", "finance")
    rows = store.conn.execute("SELECT password_hash, salt FROM users ORDER BY email").fetchall()
    assert rows[0]["password_hash"] != rows[1]["password_hash"]
    assert rows[0]["salt"] != rows[1]["salt"]
    assert b"password" not in rows[0]["password_hash"]


def test_token_roundtrip(store):
    user = store.create_user("a@b.com", "password123", "Ada", "finance")
    token = store.token_for(user)
    payload = verify_jwt(store.secret, token)
    assert payload is not None
    for key in ("sub", "email", "name", "category", "iat", "exp"):
        assert key in payload
    assert payload["category"] == "finance"


def test_tampered_token_rejected(store):
    user = store.create_user("a@b.com", "password123", "Ada", "finance")
    token = store.token_for(user)
    assert verify_jwt(store.secret, token + "x") is None
    assert verify_jwt(store.secret, "") is None
    assert verify_jwt(store.secret, "a.b") is None


def test_expired_token_rejected(store):
    token = sign_jwt(store.secret, {"sub": "u1", "email": "e", "name": "n", "category": "finance"}, ttl_seconds=-10)
    assert verify_jwt(store.secret, token) is None


def test_minimal_jwt_helpers():
    payload = {"sub": "1", "email": "e@x.com", "name": "N", "category": "hr"}
    token = sign_jwt("s", payload, ttl_seconds=60)
    body = verify_jwt("s", token)
    assert body["category"] == "hr"
    assert verify_jwt("other-secret", token) is None


# ---- organisations ---------------------------------------------------------


def test_system_organisations_seeded(store):
    orgs = {o["id"]: o for o in store.list_organisations()}
    assert set(orgs) == {SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID}
    assert all(o["system_owned"] for o in orgs.values())


def test_new_user_lands_in_sample_organisation(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    assert user["organisation_id"] == SAMPLE_ORGANISATION_ID


def test_explicit_organisation_used_when_given(store):
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    assert user["organisation_id"] == "acme"


def test_unknown_organisation_rejected(store):
    with pytest.raises(ValueError, match="Unknown organisation"):
        store.create_user("a@b.com", "password123", "A", "finance", "nope")


def test_reserved_organisation_ids_rejected(store):
    for org_id in (SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID):
        with pytest.raises(ValueError, match="reserved"):
            store.create_organisation(org_id, "Sneaky")


def test_duplicate_organisation_rejected(store):
    store.create_organisation("acme", "Acme Ltd")
    with pytest.raises(ValueError, match="already exists"):
        store.create_organisation("acme", "Other")


def test_merge_guard_blocks_system_owned(store):
    for org_id in (SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID):
        with pytest.raises(ValueError, match="system-owned"):
            store.merge_guard(org_id)


def test_merge_guard_allows_customer_org(store):
    store.create_organisation("acme", "Acme Ltd")
    assert store.merge_guard("acme")["system_owned"] is False


def test_merge_guard_rejects_unknown_org(store):
    with pytest.raises(ValueError, match="Unknown organisation"):
        store.merge_guard("nope")


def test_move_into_sample_org_rejected(store):
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    with pytest.raises(ValueError, match="system-owned"):
        store.set_user_organisation(user["id"], SAMPLE_ORGANISATION_ID)
    assert store.get(user["id"])["organisation_id"] == "acme"


def test_move_between_customer_orgs_allowed(store):
    store.create_organisation("acme", "Acme Ltd")
    store.create_organisation("globex", "Globex")
    user = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    assert store.set_user_organisation(user["id"], "globex") is True
    assert store.get(user["id"])["organisation_id"] == "globex"


def test_count_users_per_organisation(store):
    store.create_organisation("acme", "Acme Ltd")
    store.create_user("a@b.com", "password123", "A", "finance")
    store.create_user("c@b.com", "password123", "C", "hr")
    store.create_user("e@b.com", "password123", "E", "hr", "acme")
    assert store.count_for_organisation(SAMPLE_ORGANISATION_ID) == 2
    assert store.count_for_organisation("acme") == 1


def test_list_users_in_organisation(store):
    store.create_organisation("acme", "Acme Ltd")
    store.create_user("a@b.com", "password123", "A", "finance")
    store.create_user("e@b.com", "password123", "E", "hr", "acme")
    mine = store.list_users_in_organisation("acme")
    assert [u["email"] for u in mine] == ["e@b.com"]
    assert store.list_users_in_organisation(SAMPLE_ORGANISATION_ID)[0]["email"] == "a@b.com"


def test_token_carries_organisation_id(store):
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user("a@b.com", "password123", "Ada", "finance", "acme")
    payload = verify_jwt(store.secret, store.token_for(user))
    assert payload["organisation_id"] == "acme"


def test_legacy_users_table_backfilled(tmp_path):
    """A users table written before organisations existed opens and backfills."""
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, category TEXT NOT NULL, password_hash BLOB NOT NULL, "
        "salt BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u1','old@b.com','Old','finance',X'00',X'01',"
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = UserStore(db_path=str(db), secret="test-secret")
    assert store.get("u1")["organisation_id"] == DEFAULT_ORGANISATION_ID
    assert store.count_for_organisation(DEFAULT_ORGANISATION_ID) == 1


# ---- workspace memberships (Q7) -------------------------------------------


def test_signup_grants_its_own_workspace(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    assert store.workspace_categories(user["id"]) == ["finance"]


def test_user_may_hold_several_workspaces(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    assert store.workspace_categories(user["id"]) == ["finance", "hr"]
    assert store.has_workspace(user["id"], "hr") is True


def test_memberships_list_primary_first(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    store.set_primary_workspace(user["id"], "hr")
    rows = store.memberships(user["id"])
    assert [r["category"] for r in rows] == ["hr", "finance"]
    assert [r["is_primary"] for r in rows] == [True, False]


def test_add_workspace_is_idempotent(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    store.add_workspace(user["id"], "hr")
    assert store.workspace_categories(user["id"]) == ["finance", "hr"]


def test_add_workspace_rejects_unknown_category(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="Category must be one of"):
        store.add_workspace(user["id"], "nope")


def test_add_workspace_rejects_unknown_role(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="Role must be one of"):
        store.add_workspace(user["id"], "hr", role="superuser")


def test_add_workspace_rejects_unknown_user(store):
    with pytest.raises(ValueError, match="Unknown user"):
        store.add_workspace("nope", "hr")


def test_membership_roles_are_all_member(store):
    """Q10 removed Workspace Admin, so both memberships carry `member`."""
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    roles = {m["category"]: m["role"] for m in store.memberships(user["id"])}
    assert roles == {"finance": "member", "hr": "member"}


def test_remove_workspace(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    assert store.remove_workspace(user["id"], "finance") is True
    assert store.workspace_categories(user["id"]) == ["hr"]


def test_cannot_remove_only_workspace(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="only workspace"):
        store.remove_workspace(user["id"], "finance")
    assert store.workspace_categories(user["id"]) == ["finance"]


def test_set_primary_requires_membership(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="no membership"):
        store.set_primary_workspace(user["id"], "hr")
    assert store.get(user["id"])["category"] == "finance"


def test_update_user_category_grants_membership(store):
    """Admin reassigning a user's workspace must not leave them without access."""
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.update_user(user["id"], category="hr")
    assert store.get(user["id"])["category"] == "hr"
    assert store.has_workspace(user["id"], "hr") is True


def test_delete_user_removes_memberships(store):
    user = store.create_user("a@b.com", "password123", "A", "finance")
    store.add_workspace(user["id"], "hr")
    assert store.delete_user(user["id"]) is True
    assert store.workspace_categories(user["id"]) == []


def test_memberships_are_per_user(store):
    a = store.create_user("a@b.com", "password123", "A", "finance")
    b = store.create_user("b@b.com", "password123", "B", "hr")
    store.add_workspace(a["id"], "hr")
    assert store.workspace_categories(a["id"]) == ["finance", "hr"]
    assert store.workspace_categories(b["id"]) == ["hr"]


def test_token_carries_all_workspaces(store):
    user = store.create_user("a@b.com", "password123", "Ada", "finance")
    store.add_workspace(user["id"], "hr")
    payload = verify_jwt(store.secret, store.token_for(user))
    assert payload["workspaces"] == ["finance", "hr"]
    assert payload["category"] == "finance"


def test_legacy_users_backfilled_into_membership(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, category TEXT NOT NULL, password_hash BLOB NOT NULL, "
        "salt BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u1','old@b.com','Old','hr',X'00',X'01',"
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = UserStore(db_path=str(db), secret="s")
    assert store.workspace_categories("u1") == ["hr"]


def test_membership_backfill_is_idempotent(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, category TEXT NOT NULL, password_hash BLOB NOT NULL, "
        "salt BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u1','old@b.com','Old','hr',X'00',X'01',"
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    UserStore(db_path=str(db), secret="s").add_workspace("u1", "finance")
    reopened = UserStore(db_path=str(db), secret="s")
    # The extra grant survives; the backfill must not duplicate the original.
    assert sorted(reopened.workspace_categories("u1")) == ["finance", "hr"]


# ---- organisation roles (Q9 singular owner, Q10 no workspace admin) --------


def test_workspace_admin_role_removed(store):
    """Q10 removed the role, so it can no longer be granted."""
    user = store.create_user("a@b.com", "password123", "A", "finance")
    with pytest.raises(ValueError, match="Role must be one of"):
        store.add_workspace(user["id"], "hr", role="workspace_admin")


def test_org_role_defaults_to_none(store):
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    assert store.org_role(user["id"]) is None


def test_single_owner_enforced(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    b = store.create_user("b@b.com", "password123", "B", "hr", "acme")
    store.set_org_role("acme", a["id"], "owner")
    with pytest.raises(ValueError, match="already has an owner"):
        store.set_org_role("acme", b["id"], "owner")
    owners = [r for r in store.org_roles_for("acme") if r["role"] == "owner"]
    assert [r["user_id"] for r in owners] == [a["id"]]


def test_owner_may_be_granted_again_to_same_user(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    store.set_org_role("acme", a["id"], "owner")
    assert store.set_org_role("acme", a["id"], "owner") == "owner"


def test_ownership_transfer(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    b = store.create_user("b@b.com", "password123", "B", "hr", "acme")
    store.set_org_role("acme", a["id"], "owner")
    store.transfer_ownership("acme", a["id"], b["id"])
    roles = {r["user_id"]: r["role"] for r in store.org_roles_for("acme")}
    assert roles[b["id"]] == "owner"
    assert roles[a["id"]] == "admin"


def test_transfer_requires_current_owner(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    b = store.create_user("b@b.com", "password123", "B", "hr", "acme")
    store.set_org_role("acme", a["id"], "owner")
    with pytest.raises(ValueError, match="not the owner"):
        store.transfer_ownership("acme", b["id"], a["id"])


def test_org_role_rejected_for_other_organisation(store):
    store.create_organisation("acme", "Acme Ltd")
    store.create_organisation("globex", "Globex")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    with pytest.raises(ValueError, match="does not belong"):
        store.set_org_role("globex", a["id"], "admin")


def test_org_role_rejects_unknown_role(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    with pytest.raises(ValueError, match="Role must be one of"):
        store.set_org_role("acme", a["id"], "superuser")


def test_owner_and_admin_can_manage_workspaces(store):
    """Q11: organisation admins (and owners) manage workspaces; members cannot."""
    store.create_organisation("acme", "Acme Ltd")
    owner = store.create_user("o@b.com", "password123", "O", "finance", "acme")
    admin = store.create_user("a@b.com", "password123", "A", "hr", "acme")
    member = store.create_user("m@b.com", "password123", "M", "finance", "acme")
    store.set_org_role("acme", owner["id"], "owner")
    store.set_org_role("acme", admin["id"], "admin")
    assert store.can_manage_workspaces(owner["id"]) is True
    assert store.can_manage_workspaces(admin["id"]) is True
    assert store.can_manage_workspaces(member["id"]) is False


def test_owner_may_hold_several_workspaces(store):
    """Q8 counts seats per workspace, so one person can occupy several."""
    store.create_organisation("acme", "Acme Ltd")
    o = store.create_user("o@b.com", "password123", "O", "finance", "acme")
    store.set_org_role("acme", o["id"], "owner")
    store.add_workspace(o["id"], "hr")
    store.add_workspace(o["id"], "finance")
    assert store.count_for_organisation("acme") == 1
    assert sorted(store.workspace_categories(o["id"])) == ["finance", "hr"]


def test_delete_user_removes_org_role(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    store.set_org_role("acme", a["id"], "owner")
    store.delete_user(a["id"])
    assert store.org_role(a["id"]) is None
    assert store.org_roles_for("acme") == []


def test_delete_organisation_clears_members(store):
    store.create_organisation("acme", "Acme Ltd")
    a = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    store.set_org_role("acme", a["id"], "owner")
    store.create_user("b@b.com", "password123", "B", "hr", "acme")
    assert store.delete_organisation("acme") is True
    assert store.get_organisation("acme") is None
    assert store.count_users() == 0
    assert store.org_roles_for("acme") == []


def test_cannot_delete_system_organisation(store):
    for org_id in (SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID):
        with pytest.raises(ValueError, match="system-owned"):
            store.delete_organisation(org_id)
        assert store.get_organisation(org_id) is not None


def test_delete_unknown_organisation_returns_false(store):
    assert store.delete_organisation("nope") is False


def test_owner_granted_after_user_joins_org(store):
    """Ownership follows membership: Q9's single owner is per organisation."""
    store.create_organisation("acme", "Acme Ltd")
    store.create_organisation("globex", "Globex")
    u = store.create_user("a@b.com", "password123", "A", "finance", "acme")
    store.set_org_role("acme", u["id"], "owner")

    # The same person cannot own a second organisation while in the first.
    with pytest.raises(ValueError, match="does not belong"):
        store.set_org_role("globex", u["id"], "owner")

    store.set_user_organisation(u["id"], "globex")
    store.set_org_role("globex", u["id"], "owner")
    assert store.org_role(u["id"]) == "owner"
    assert [r["role"] for r in store.org_roles_for("globex")] == ["owner"]


def test_legacy_table_backfill_is_idempotent(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, category TEXT NOT NULL, password_hash BLOB NOT NULL, "
        "salt BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u1','old@b.com','Old','finance',X'00',X'01',"
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    UserStore(db_path=str(db), secret="s").close()
    reopened = UserStore(db_path=str(db), secret="s")
    assert reopened.get("u1")["organisation_id"] == DEFAULT_ORGANISATION_ID
    assert len(reopened.list_organisations()) == 2

# ---- Organisation roles: who owns what ------------------------------------
#
# Q9 makes owner singular per organisation, and every owner-only surface reads
# org_roles. An account that arrives without a row there is a member nothing
# can administer, which is how the platform's own accounts were being created.


def test_ensure_initial_owner_grants_the_first_account(store):
    store.create_organisation("acme", "Acme Ltd")
    first = store.create_user("a@bank.com", "password123", "A", "finance",
                              organisation_id="acme", verified=True)
    second = store.create_user("b@bank.com", "password123", "B", "finance",
                               organisation_id="acme", verified=True)

    assert store.ensure_initial_owner("acme", first["id"]) is True
    assert store.org_role(first["id"]) == "owner"
    # The second account is a plain member until the owner promotes it.
    assert store.ensure_initial_owner("acme", second["id"]) is False
    assert store.org_role(second["id"]) is None
    # And the rule never grants the same account twice.
    assert store.ensure_initial_owner("acme", first["id"]) is False


def test_an_account_with_a_role_is_left_alone(store):
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user("a@bank.com", "password123", "A", "finance",
                             organisation_id="acme", verified=True)
    store.set_org_role("acme", user["id"], "admin")
    assert store.ensure_initial_owner("acme", user["id"]) is False
    assert store.org_role(user["id"]) == "admin"


def test_an_unknown_account_grants_nothing(store):
    store.create_organisation("acme", "Acme Ltd")
    assert store.ensure_initial_owner("acme", None) is False
    assert store.ensure_initial_owner("acme", "nobody") is False


def test_the_free_tier_is_never_given_an_owner(store):
    free = store.create_user("free@sample.com", "password123", "F", "finance",
                             organisation_id=SAMPLE_ORGANISATION_ID, verified=True)
    assert store.ensure_initial_owner(SAMPLE_ORGANISATION_ID, free["id"]) is False
    assert store.org_role(free["id"]) is None


def test_backfill_makes_the_earliest_account_the_owner(store):
    store.create_organisation("acme", "Acme Ltd")
    earliest = store.create_user("a@bank.com", "password123", "A", "finance",
                                 organisation_id="acme", verified=True)
    store.create_user("b@bank.com", "password123", "B", "finance",
                      organisation_id="acme", verified=True)

    assert store.backfill_org_roles() == 1
    assert store.org_role(earliest["id"]) == "owner"
    assert store.org_role(
        store.get_by_email("b@bank.com")["id"]
    ) is None
    # Idempotent: a second pass finds nothing left to grant.
    assert store.backfill_org_roles() == 0


def test_backfill_never_moves_an_owner_already_chosen(store):
    store.create_organisation("acme", "Acme Ltd")
    store.create_user("a@bank.com", "password123", "A", "finance",
                      organisation_id="acme", verified=True)
    chosen = store.create_user("b@bank.com", "password123", "B", "finance",
                               organisation_id="acme", verified=True)
    store.set_org_role("acme", chosen["id"], "owner")

    assert store.backfill_org_roles() == 0
    assert store.org_role(chosen["id"]) == "owner"


def test_backfill_leaves_the_system_organisations_alone(store):
    free = store.create_user("free@sample.com", "password123", "F", "finance",
                             organisation_id=SAMPLE_ORGANISATION_ID, verified=True)
    legacy = store.create_user("old@default.com", "password123", "O", "finance",
                               organisation_id=DEFAULT_ORGANISATION_ID, verified=True)

    assert store.backfill_org_roles() == 0
    assert store.org_role(free["id"]) is None
    assert store.org_role(legacy["id"]) is None


def test_backfill_skips_an_organisation_with_no_accounts(store):
    store.create_organisation("empty", "Empty Ltd")
    assert store.backfill_org_roles() == 0
