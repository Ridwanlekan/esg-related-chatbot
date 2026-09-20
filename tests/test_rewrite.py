from chatbot.rewrite import rewrite_question, MAX_HISTORY_TURNS


HISTORY = [
    {"role": "user", "content": "Tell me about Jupiter's moons."},
    {"role": "assistant", "content": "Jupiter has 97 known moons."},
]


def _capture(history):
    calls = []

    def generate_fn(messages):
        calls.append(messages)
        return "Which is the largest of Jupiter's moons?"

    return generate_fn, calls


def test_no_history_returns_question_unchanged():
    generate_fn, calls = _capture(HISTORY)
    result = rewrite_question("which is the largest?", [], generate_fn)
    assert result == "which is the largest?"
    assert calls == []


def test_rewrites_with_history():
    generate_fn, calls = _capture(HISTORY)
    result = rewrite_question("which is the largest?", HISTORY, generate_fn)
    assert result == "Which is the largest of Jupiter's moons?"

    messages = calls[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "which is the largest?"}
    assert "history" in messages[0]["content"]


def test_trims_long_history_to_last_n_turns():
    generate_fn, calls = _capture(HISTORY)
    long_history = HISTORY * 10
    rewrite_question("which is the largest?", long_history, generate_fn)
    history_msgs = calls[0][1:-1]
    assert len(history_msgs) == MAX_HISTORY_TURNS


def test_empty_generation_falls_back_to_original():
    result = rewrite_question("which is the largest?", HISTORY, lambda msgs: "")
    assert result == "which is the largest?"


def test_strips_surrounding_quotes():
    result = rewrite_question(
        "which is the largest?", HISTORY, lambda msgs: '"Jupiter moon size"'
    )
    assert result == "Jupiter moon size"