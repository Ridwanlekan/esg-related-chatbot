from chatbot.evaluate import (
    aggregate_metrics,
    hit_rate,
    load_eval_set,
    reciprocal_rank,
    recall_at_k,
    run_retrieval_eval,
)


class FakeResults:
    def __init__(self, sources):
        self._sources = sources

    @property
    def source(self):
        return self._sources


def _results(sources):
    return [FakeResults(s) for s in sources]


def test_hit_rate():
    assert hit_rate(["jupiter.txt", "crispr.txt"], ["jupiter.txt"])
    assert not hit_rate(["crispr.txt"], ["jupiter.txt"])


def test_recall_at_k():
    assert recall_at_k(["a", "b"], ["a", "b"]) == 1.0
    assert recall_at_k(["a", "b"], ["a", "c"]) == 0.5
    assert recall_at_k([], ["a"]) == 0.0
    assert recall_at_k(["x"], []) == 1.0


def test_reciprocal_rank():
    assert reciprocal_rank(["a", "b", "c"], ["c"]) == 1 / 3
    assert reciprocal_rank(["x", "y"], ["z"]) == 0.0
    assert reciprocal_rank(["b", "a"], ["a", "b"]) == 1.0


def test_aggregate_metrics():
    metrics = aggregate_metrics(
        [
            {"hit": True, "recall": 1.0, "mrr": 1.0},
            {"hit": False, "recall": 0.0, "mrr": 0.0},
        ]
    )
    assert metrics == {"questions": 2, "hit_rate": 0.5, "recall": 0.5, "mrr": 0.5}


def test_run_retrieval_eval_uses_retrieve_fn():
    def retrieve(question, k):
        return _results(["s1.txt"])

    questions = [{"question": "q1", "expected_sources": ["s1.txt"]}]
    examples = run_retrieval_eval(retrieve, questions, k=3)
    assert examples[0]["hit"] is True
    assert examples[0]["retrieved_sources"] == ["s1.txt"]


def test_load_eval_set(tmp_path):
    p = tmp_path / "set.json"
    p.write_text(
        '{"questions": [{"question": "q", "expected_sources": ["a.txt"]}]}'
    )
    items = load_eval_set(str(p))
    assert items[0]["question"] == "q"
    assert items[0]["expected_sources"] == ["a.txt"]