import contextvars
import logging
import time
import uuid

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

_request_id_var = contextvars.ContextVar("request_id", default="-")


def get_request_id():
    return _request_id_var.get()


def new_request_id():
    return uuid.uuid4().hex[:12]


def set_request_id(rid):
    return _request_id_var.set(rid)


def reset_request_id(token):
    _request_id_var.reset(token)


# --- HTTP -------------------------------------------------------------------
http_requests = Counter(
    "esg_http_requests_total",
    "HTTP requests handled",
    ["method", "route", "status"],
)
http_duration = Histogram(
    "esg_http_request_duration_seconds",
    "HTTP request handling time",
    ["method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- LLM --------------------------------------------------------------------
llm_requests = Counter("esg_llm_requests_total", "LLM generation calls", ["kind"])
llm_duration = Histogram(
    "esg_llm_duration_seconds",
    "LLM call duration",
    ["kind"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 90, 180),
)
llm_tokens = Counter(
    "esg_llm_tokens_total",
    "Tokens consumed by LLM calls",
    ["kind", "token_type"],
)
llm_errors = Counter("esg_llm_errors_total", "Failed LLM calls", ["kind", "error"])

# --- Retrieval ---------------------------------------------------------------
retrieval_requests = Counter("esg_retrieval_requests_total", "Retrieval calls")
retrieval_duration = Histogram(
    "esg_retrieval_duration_seconds",
    "Hybrid retrieval time (embed + search)",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

# --- State gauges ------------------------------------------------------------
index_chunks = Gauge("esg_index_chunks", "Number of chunks in the vector index")
sessions = Gauge("esg_sessions", "Number of stored chat sessions")


def observe_http(method, route, status, duration_ms):
    http_requests.labels(method, route, str(status)).inc()
    http_duration.labels(method, route).observe(duration_ms / 1000.0)


def record_llm(kind, duration_seconds, prompt_tokens=None, completion_tokens=None):
    llm_requests.labels(kind).inc()
    llm_duration.labels(kind).observe(duration_seconds)
    if prompt_tokens:
        llm_tokens.labels(kind, "prompt").inc(prompt_tokens)
    if completion_tokens:
        llm_tokens.labels(kind, "completion").inc(completion_tokens)


def record_llm_error(kind, error_type):
    llm_errors.labels(kind, error_type).inc()


def record_retrieval(duration_seconds):
    retrieval_requests.inc()
    retrieval_duration.observe(duration_seconds)


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id_var.get()
        return True


_configured = False


def configure_logging(level=logging.INFO, formatter=None):
    """Attach a request-id-aware stream handler to the root logger (once)."""
    global _configured
    if _configured:
        return
    _configured = True
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        formatter
        or logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


def render_metrics():
    return generate_latest()


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST