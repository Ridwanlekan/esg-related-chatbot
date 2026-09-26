from prometheus_client import REGISTRY

from fastapi.testclient import TestClient

from chatbot import telemetry
from chatbot.api import create_app
from chatbot.session_store import SessionStore


class FakeBot:
    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        return f"answer to: {question}"

    def retrieve(self, question, k=3, source=None):
        return []

    def read_and_embed_data(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            documents_seen=3,
            documents_reindexed=1,
            chunks_upserted=42,
            stale_chunks_removed=0,
        )

    @property
    def store(self):
        from types import SimpleNamespace

        return SimpleNamespace(count=lambda: 350)


def _client():
    app = create_app(
        bot=FakeBot(),
        session_store=SessionStore(db_path=":memory:"),
        rate_limit=0,
    )
    return TestClient(app)


def test_metrics_endpoint_exposes_telemetry():
    c = _client()
    c.get("/health")
    res = c.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text = res.text
    assert "esg_http_requests_total" in text
    assert 'route="/health"' in text
    assert "esg_metric_requests" not in text


def test_metrics_not_rate_limited(client_with_noop=None):
    c = _client()
    assert c.get("/metrics").status_code == 200


def test_request_id_header_echoed():
    c = _client()
    res = c.get("/health")
    assert res.headers["X-Request-ID"]
    res2 = c.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert res2.headers["X-Request-ID"] == "trace-abc"


def test_request_log_record(caplog):
    import logging

    caplog.set_level(logging.INFO)
    c = _client()
    res = c.get("/health")
    assert res.status_code == 200
    assert any(
        "GET /health ->" in r.message and "rid=" in r.message for r in caplog.records
    )


def test_index_and_session_gauges():
    state = {"count": 12}

    class CountingStore(SessionStore):
        def count(self):
            return state["count"]

    app = create_app(
        bot=FakeBot(),
        session_store=CountingStore(db_path=":memory:"),
        rate_limit=0,
    )
    c = TestClient(app)
    text = c.get("/metrics").text
    assert "esg_index_chunks 350.0" in text
    assert "esg_sessions 12.0" in text


def test_quiet_without_request_id():
    import logging

    rec = logging.LogRecord("n", logging.INFO, "p", 1, "msg", None, None)
    telemetry.RequestIdFilter().filter(rec)
    assert rec.request_id == "-"