import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


class UsageBot:
    """Stub bot that records token events into the usage_sink the API passes."""

    def __init__(self):
        self.store = SimpleNamespace(count=lambda: 5)
        self.last_results = [SimpleNamespace(source="10. IFRS S1.pdf")]

    def retrieve(self, question, k=3, source=None, history=None, usage_sink=None):
        self.last_results = [SimpleNamespace(source="10. IFRS S1.pdf")]
        return self.last_results

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        if usage_sink is not None:
            usage_sink.append({
                "kind": "answer", "model": "gpt-4.1-mini",
                "prompt_tokens": 2000, "completion_tokens": 400, "duration_s": 1.2,
            })
        return "usage answer"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        if usage_sink is not None:
            usage_sink.append({
                "kind": "rewrite", "model": "gpt-4.1-mini",
                "prompt_tokens": 500, "completion_tokens": 60, "duration_s": 0.4,
            })
        yield "chunk "
        if usage_sink is not None:
            usage_sink.append({
                "kind": "stream", "model": "gpt-4.1-mini",
                "prompt_tokens": 2200, "completion_tokens": 500, "duration_s": 3.1,
            })
        yield "done"


def _insert(s, at, kind="stream", pt=100, ct=50, category="finance", user_id="u1"):
    s.conn.execute(
        "INSERT INTO usage (at, kind, model, prompt_tokens, completion_tokens, "
        "duration_s, category, user_id) VALUES (?, ?, 'm', ?, ?, 1.0, ?, ?)",
        (at, kind, pt, ct, category, user_id),
    )
    s.conn.commit()


# ---- AdminStore unit tests ------------------------------------------------

def test_record_and_aggregate_cost_and_breakdowns():
    s = AdminStore(":memory:")
    s.record_usage(kind="stream", model="gpt-4.1-mini", prompt_tokens=5000,
                   completion_tokens=900, duration_s=2.5,
                   category="finance", user_id="u1", session_id="s1")
    stats = s.usage_stats(range_days=1)
    t = stats["totals"]
    assert t["calls"] == 1
    assert t["prompt_tokens"] == 5000
    assert t["completion_tokens"] == 900
    assert t["cost"] == pytest.approx(5000 / 1e6 * 0.40 + 900 / 1e6 * 1.60)
    assert t["input_cost"] == pytest.approx(5000 / 1e6 * 0.40)
    assert stats["by_kind"][0]["key"] == "stream"
    assert stats["by_category"][0]["key"] == "finance"
    assert stats["by_user"][0]["key"] == "u1"
    assert stats["by_user"][0]["calls"] == 1


def test_rates_affect_cost_and_defaults():
    s = AdminStore(":memory:")
    s.record_usage(kind="stream", prompt_tokens=1_000_000, completion_tokens=0)
    assert s.usage_stats()["totals"]["cost"] == pytest.approx(0.40)
    s.set_rates(1.0, 2.0)
    assert s.usage_stats()["totals"]["cost"] == pytest.approx(1.00)
    assert AdminStore(":memory:").get_rates() == {
        "price_input_per_m": 0.40, "price_output_per_m": 1.60,
    }


def test_rates_persist_across_reopen(tmp_path):
    path = str(tmp_path / "admin.sqlite3")
    s = AdminStore(path)
    s.set_rates(0.5, 1.5)
    s.close()
    s2 = AdminStore(path)
    assert s2.get_rates() == {"price_input_per_m": 0.5, "price_output_per_m": 1.5}


def test_range_filter_and_series_buckets():
    s = AdminStore(":memory:")
    now = datetime.now(timezone.utc)
    _insert(s, now.isoformat(), kind="stream", pt=1000, ct=200, category="finance", user_id="u1")
    _insert(s, (now - timedelta(days=3)).isoformat(), kind="rewrite", pt=300, ct=40,
            category="hr", user_id="u2")
    _insert(s, (now - timedelta(days=40)).isoformat(), kind="answer", pt=600, ct=100,
            category="finance", user_id="u1")

    assert s.usage_stats()["totals"]["calls"] == 3
    assert s.usage_stats(range_days=1)["totals"]["calls"] == 1
    weekly = s.usage_stats(range_days=7)
    assert weekly["totals"]["calls"] == 2
    assert {b["bucket"] for b in weekly["series"]} == {
        now.strftime("%Y-%m-%d"),
        (now - timedelta(days=3)).strftime("%Y-%m-%d"),
    }
    hourly = s.usage_stats(range_days=2)
    assert all(b["bucket"].endswith(":00") for b in hourly["series"])


def test_category_filter_and_user_exclusion():
    s = AdminStore(":memory:")
    now = datetime.now(timezone.utc)
    _insert(s, now.isoformat(), kind="stream", pt=100, ct=50, category="finance", user_id="u1")
    _insert(s, now.isoformat(), kind="rewrite", pt=200, ct=30, category="hr", user_id="u2")
    fin = s.usage_stats(range_days=1, category="finance")
    assert fin["totals"]["calls"] == 1
    assert fin["by_category"][0]["key"] == "finance"
    s.record_usage(kind="answer", prompt_tokens=10, completion_tokens=5)
    assert {u["key"] for u in s.usage_stats()["by_user"]} == {"u1", "u2"}
    assert s.usage_stats()["totals"]["calls"] == 3


def test_export_rows_shape():
    s = AdminStore(":memory:")
    s.record_usage(kind="stream", model="m", prompt_tokens=10, completion_tokens=20,
                   duration_s=1.0, category="finance", user_id="u1", session_id="s1")
    rows = s.usage_export_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "stream"
    assert rows[0]["category"] == "finance"
    assert rows[0]["session_id"] == "s1"


# ---- API integration tests ------------------------------------------------

@pytest.fixture
def usage_env(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["INDEX_DIR"] = str(tmp_path / ".index")
    app = create_app(
        bot=UsageBot(),
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "workspaces.sqlite3")),
        api_key="secret-admin-key",
        rate_limit=0,
    )
    return TestClient(app)


def _admin_auth():
    return {"authorization": "Bearer secret-admin-key"}


def _signup(c, email="u@corp.com", category="finance"):
    res = c.post("/auth/signup", json={
        "email": email, "password": "password123", "name": "User", "category": category,
    })
    assert res.status_code == 200, res.text
    return res.json()["token"]


def test_admin_usage_endpoints_require_api_key(usage_env):
    c = usage_env
    assert c.get("/admin/usage").status_code == 401
    assert c.get("/admin/settings/cost").status_code == 401
    assert c.put("/admin/settings/cost", json={}).status_code == 401


def test_bad_range_is_422(usage_env):
    c = usage_env
    assert c.get("/admin/usage?range=1y", headers=_admin_auth()).status_code == 422


def test_chat_records_usage_visible_in_admin(usage_env):
    c = usage_env
    token = _signup(c)
    h = {"authorization": f"Bearer {token}"}
    r = c.post("/chat", json={"question": "targets?"}, headers=h)
    assert r.status_code == 200, r.text
    stats = c.get("/admin/usage?range=all", headers=_admin_auth()).json()
    assert stats["totals"]["calls"] == 1
    assert stats["totals"]["prompt_tokens"] == 2000
    assert stats["totals"]["completion_tokens"] == 400
    assert stats["by_kind"][0]["key"] == "answer"
    assert stats["by_category"][0]["key"] == "finance"
    assert stats["by_user"][0]["calls"] == 1


def test_stream_records_rewrite_and_stream(usage_env):
    c = usage_env
    token = _signup(c)
    h = {"authorization": f"Bearer {token}"}
    with c.stream("POST", "/chat/stream", json={"question": "targets?"}, headers=h) as res:
        assert res.status_code == 200
        "".join(res.iter_text())
    stats = c.get("/admin/usage?range=all", headers=_admin_auth()).json()
    assert stats["totals"]["calls"] == 2
    assert stats["totals"]["prompt_tokens"] == 2700
    assert stats["totals"]["completion_tokens"] == 560
    assert {d["key"] for d in stats["by_kind"]} == {"rewrite", "stream"}


def test_update_cost_rates_and_recompute(usage_env):
    c = usage_env
    token = _signup(c)
    c.post("/chat", json={"question": "targets?"}, headers={"authorization": f"Bearer {token}"})
    put = c.put("/admin/settings/cost", json={"price_input_per_m": 1.0, "price_output_per_m": 2.0},
                headers=_admin_auth())
    assert put.status_code == 200
    assert put.json() == {"price_input_per_m": 1.0, "price_output_per_m": 2.0}
    stats = c.get("/admin/usage?range=all", headers=_admin_auth()).json()
    assert stats["totals"]["cost"] == pytest.approx(1.0 / 1e6 * 2000 + 2.0 / 1e6 * 400)
    bad = c.put("/admin/settings/cost", json={"price_input_per_m": -1, "price_output_per_m": 1},
                headers=_admin_auth())
    assert bad.status_code == 422


def test_usage_csv_export(usage_env):
    c = usage_env
    token = _signup(c)
    c.post("/chat", json={"question": "targets?"}, headers={"authorization": f"Bearer {token}"})
    res = c.get("/admin/usage/export.csv?range=all", headers=_admin_auth())
    assert res.status_code == 200
    lines = res.text.splitlines()
    assert "prompt_tokens" in lines[0]
    assert "2000" in res.text