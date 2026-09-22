from datetime import datetime, timezone

from chatbot import smalltalk


def _freeze(monkeypatch, dt):
    fixed = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    monkeypatch.setattr(smalltalk, "_local_now", lambda tz: fixed)


def test_detect_greetings():
    for text in ("hi", "Hello, there", "good morning", "Hey!", "Good afternoon", "wassup", "whatsup"):
        assert smalltalk.detect(text) == "greeting", text


def test_detect_how_are_you():
    for text in ("how are you", "how are you today", "how are you doing",
                 "how are you doing today", "how's it going", "how's everything",
                 "what's up", "whats up"):
        assert smalltalk.detect(text) == "howareyou", text


def test_detect_thanks_and_help():
    assert smalltalk.detect("thanks a lot") == "thanks"
    assert smalltalk.detect("thank you!") == "thanks"
    assert smalltalk.detect("what can you do?") == "help"
    assert smalltalk.detect("who are you?") == "help"


def test_detect_returns_none_for_questions():
    for text in ("What does IFRS S1 require?", "tell me about climate disclosures", "999", "?"):
        assert smalltalk.detect(text) is None, text


def test_greeting_mentions_day_date_and_persona(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 9, 22, 15, 0))  # Tuesday afternoon UTC
    reply = smalltalk.handle("good afternoon", "Ada Lovelace", "finance", -60)
    assert reply.startswith("Good afternoon, Ada!")
    assert "Today is Tuesday" in reply
    assert "September 22, 2026" in reply
    assert "ESG Finance assistant" in reply
    assert "\U0001F4CA" in reply  # chart emoji


def test_greeting_respects_user_local_hour(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 9, 22, 15, 0))  # 15:00 UTC
    assert smalltalk.handle("hello", "Ada", "finance", 0).startswith("Good afternoon,")
    # +24h shifts the local clock so the salutation must change.
    _freeze(monkeypatch, datetime(2026, 9, 23, 9, 0))
    assert smalltalk.handle("hello", "Ada", "finance", 0).startswith("Good morning,")


def test_help_reply_matches_workspace():
    reply = smalltalk.handle("who are you", "Bob", "hr")
    assert "People & HR assistant" in reply
    assert "\U0001F465" in reply  # people emoji


def test_how_are_you_reply_matches_workspace():
    reply = smalltalk.handle("how are you doing today", "Ada", "finance")
    assert reply.startswith("I'm doing great, Ada!")
    assert "ESG Finance assistant" in reply
    assert "\U0001F4CA" in reply  # chart emoji


def test_thanks_reply_acknowledges():
    reply = smalltalk.handle("thanks", "Ada", "finance")
    assert reply.startswith("You're welcome, Ada!")


def test_handle_passthrough_for_non_smalltalk():
    assert smalltalk.handle("What is IFRS S2 about?", "Ada", "finance") is None


def test_weekday_lines_cover_all_days():
    assert len(smalltalk._WEEKDAY_LINES) == 7