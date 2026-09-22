import pytest

from chatbot.users import UserStore, sign_jwt, verify_jwt


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