"""Mandarin (zh-Hans) and Hindi (hi) support across API, RAG and smalltalk.

Covers the language-selection policy (query script wins, UI language is the
fallback), smalltalk localisation, the retrieval translation step and the
system-prompt directive.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot import languages, smalltalk
from chatbot.api import create_app
from chatbot.session_store import SessionStore


class FakeBot:
    def __init__(self):
        self.last_results = []
        self.store = SimpleNamespace(count=lambda: 0)
        self.ask_calls = []

    def retrieve(self, question, k=3, source=None, history=None, usage_sink=None, language=None):
        self.ask_calls.append({"retrieve": question, "language": language})
        return self.last_results

    def ask(self, question, k=3, source=None, history=None, usage_sink=None, language=None):
        self.ask_calls.append({"ask": question, "language": language})
        return f"answer to: {question} [{language}]"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None, language=None):
        self.ask_calls.append({"ask_stream": question, "language": language})
        yield "chunk one"


@pytest.fixture
def client(tmp_path):
    bot = FakeBot()
    store = SessionStore(db_path=str(tmp_path / "sessions.sqlite3"))
    app = create_app(bot=bot, session_store=store, user_store=None)
    yield TestClient(app), bot


class TestLanguageHelpers:
    def test_normalize_maps_tags(self):
        assert languages.normalize_language("zh-Hans") == "zh"
        assert languages.normalize_language("zh_CN") == "zh"
        assert languages.normalize_language("hi-IN") == "hi"
        assert languages.normalize_language("en-GB") == "en"
        assert languages.normalize_language(None) == "en"
        assert languages.normalize_language("fr") == "en"

    def test_detect_by_script(self):
        assert languages.detect_language("你好") == "zh"
        assert languages.detect_language("नमस्ते") == "hi"
        assert languages.detect_language("hello") == "en"
        assert languages.detect_language("") == "en"

    def test_query_script_wins_over_ui(self):
        assert languages.resolve_language("你好", "en") == "zh"
        assert languages.resolve_language("नमस्ते", "zh") == "hi"

    def test_english_follows_ui_language(self):
        assert languages.resolve_language("hello", "zh") == "zh"
        assert languages.resolve_language("hello", "hi") == "hi"
        assert languages.resolve_language("hello", None) == "en"

    def test_directive_is_empty_for_english(self):
        assert languages.language_directive("en") == ""
        assert "Simplified Chinese" in languages.language_directive("zh")
        assert "Hindi" in languages.language_directive("hi")


class TestSmalltalkLocalisation:
    def test_greeting_chinese(self):
        reply = smalltalk.handle("你好", user_name="张三", category="finance")
        assert reply and any(s in reply for s in ("早上好", "下午好", "晚上好"))
        assert "张三" in reply

    def test_greeting_hindi(self):
        reply = smalltalk.handle("नमस्ते", user_name="राज", category="finance")
        assert reply and any(s in reply for s in ("शुभ प्रभात", "शुभ दोपहर", "शुभ संध्या"))

    def test_english_greeting_still_english(self):
        reply = smalltalk.handle("hi", user_name="Alex", category="finance")
        assert any(s in reply for s in ("Good morning", "Good afternoon", "Good evening"))

    def test_english_query_uses_ui_language(self):
        reply = smalltalk.handle("hello", user_name="Alex", category="finance", language="zh")
        assert any(s in reply for s in ("早上好", "下午好", "晚上好"))

    def test_query_language_beats_ui(self):
        reply = smalltalk.handle("你好", user_name="Alex", category="finance", language="hi")
        assert any(s in reply for s in ("早上好", "下午好", "晚上好"))

    def test_help_chinese(self):
        reply = smalltalk.handle("帮助", user_name="张三", category="finance")
        assert "助手" in reply

    def test_thanks_chinese(self):
        reply = smalltalk.handle("谢谢", user_name="张三", category="finance")
        assert "不客气" in reply

    def test_howareyou_hindi(self):
        reply = smalltalk.handle("आप कैसे हैं", user_name="राज", category="finance")
        assert "बढ़िया" in reply

    def test_howareyou_hindi_with_day_word(self):
        for text in ("आज आप कैसे हैं", "क्या हाल है"):
            assert smalltalk.detect(text) == "howareyou", text
        reply = smalltalk.handle("आज आप कैसे हैं", user_name="राज", category="finance")
        assert "बढ़िया" in reply

    def test_howareyou_chinese_with_day_word(self):
        for text in ("你今天怎么样", "你怎么样", "你好吗"):
            assert smalltalk.detect(text) == "howareyou", text
        reply = smalltalk.handle("你今天怎么样", user_name="张三", category="finance")
        assert "我很好" in reply

    def test_howareyou_does_not_swallow_real_questions(self):
        assert smalltalk.detect("你如何计算范围三排放") is None
        assert smalltalk.detect("how are you calculating scope 3 emissions") is None


class TestChatLanguagePlumbing:
    def test_chinese_question_reaches_rag_with_zh(self, client):
        c, bot = client
        c.post("/chat", json={"question": "什么是气候风险？"})
        assert bot.ask_calls[-1]["language"] == "zh"

    def test_hindi_question_reaches_rag_with_hi(self, client):
        c, bot = client
        c.post("/chat", json={"question": "जलवायु जोखिम क्या है?"})
        assert bot.ask_calls[-1]["language"] == "hi"

    def test_english_question_follows_requested_language(self, client):
        c, bot = client
        c.post("/chat", json={"question": "what is climate risk?", "language": "zh-Hans"})
        assert bot.ask_calls[-1]["language"] == "zh"

    def test_default_english_unchanged(self, client):
        c, bot = client
        c.post("/chat", json={"question": "what is climate risk?"})
        assert bot.ask_calls[-1]["language"] == "en"

    def test_greeting_localised_via_api(self, client):
        c, _ = client
        body = c.post("/chat", json={"question": "你好"}).json()
        assert any(s in body["answer"] for s in ("早上好", "下午好", "晚上好"))

    def test_search_passes_language(self, client):
        c, bot = client
        c.post("/search", json={"question": "你好", "k": 2})
        assert bot.ask_calls[-1]["language"] == "zh"

    def test_stream_passes_language(self, client):
        c, bot = client
        c.post("/chat/stream", json={"question": "什么是气候风险？"})
        assert bot.ask_calls[-1]["language"] == "zh"


def _rag_bot(monkeypatch, store_count=0):
    """A RAGBot with the heavy collaborators stubbed out."""
    from chatbot.rag import RAGBot
    from chatbot.vector_store import SearchResult

    bot = RAGBot.__new__(RAGBot)
    bot.system_prompt = "You are a helper."
    bot.max_generation_tokens = 600
    bot.candidate_k = 30
    bot.last_results = []
    if store_count == 0:
        bot.store = SimpleNamespace(count=lambda: 0)
    else:
        bot.store = SimpleNamespace(
            count=lambda: store_count,
            search_hybrid=lambda **kw: [
                SearchResult(chunk_id="c1", source="a.txt", chunk_index=0,
                             content="ctx", distance=0.1, score=0.5)
            ],
            fetch_embeddings=lambda ids: [[0.0] * 4 for _ in ids],
        )
    monkeypatch.setattr(bot, "_embed", lambda texts: [[0.0] * 4 for _ in texts])
    return bot


class TestRagLanguage:
    def test_build_messages_adds_directive_for_chinese(self, monkeypatch):
        bot = _rag_bot(monkeypatch)
        from chatbot.vector_store import SearchResult
        results = [SearchResult(chunk_id="c", source="a.txt", chunk_index=0,
                                content="ctx", distance=0.1, score=0.5)]
        msgs = bot._build_messages("q", results, None, "zh")
        assert "Simplified Chinese" in msgs[0]["content"]

    def test_build_messages_has_no_directive_for_english(self, monkeypatch):
        bot = _rag_bot(monkeypatch)
        from chatbot.vector_store import SearchResult
        results = [SearchResult(chunk_id="c", source="a.txt", chunk_index=0,
                                content="ctx", distance=0.1, score=0.5)]
        msgs = bot._build_messages("q", results, None, "en")
        assert msgs[0]["content"] == bot.system_prompt

    def test_translate_returns_translation(self, monkeypatch):
        bot = _rag_bot(monkeypatch)
        monkeypatch.setattr(
            bot, "_complete",
            lambda kind, messages, **kw: SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="climate risk"))]
            ),
        )
        assert bot.translate_to_english("气候风险") == "climate risk"

    def test_translate_falls_back_on_error(self, monkeypatch):
        bot = _rag_bot(monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("no llm")

        monkeypatch.setattr(bot, "_complete", boom)
        assert bot.translate_to_english("气候风险") == "气候风险"

    def test_retrieve_translates_non_english(self, monkeypatch):
        bot = _rag_bot(monkeypatch, store_count=1)
        monkeypatch.setattr(bot, "translate_to_english", lambda q, sink=None: "climate risk")
        monkeypatch.setattr(bot, "rewrite_question_for_retrieval", lambda q, h=None, sink=None: q)
        monkeypatch.setattr("chatbot.rag.reranker_enabled", lambda: False)
        results = bot.retrieve("气候风险", k=1, language="zh")
        assert len(results) == 1

    def test_retrieve_skips_translation_for_english(self, monkeypatch):
        bot = _rag_bot(monkeypatch, store_count=1)
        called = {"n": 0}

        def spy(q, sink=None):
            called["n"] += 1
            return q

        monkeypatch.setattr(bot, "translate_to_english", spy)
        monkeypatch.setattr(bot, "rewrite_question_for_retrieval", lambda q, h=None, sink=None: q)
        monkeypatch.setattr("chatbot.rag.reranker_enabled", lambda: False)
        bot.retrieve("climate risk", k=1, language="en")
        assert called["n"] == 0

    def test_no_results_reply_localised(self, monkeypatch):
        bot = _rag_bot(monkeypatch)
        assert "不知道" in bot._no_results_reply("zh")
        assert "पता" in bot._no_results_reply("hi")
        assert bot._no_results_reply("en") == "I don't know. No relevant documents were found."


class TestVoiceLanguage:
    def test_kokoro_lang_maps_languages(self, monkeypatch):
        from chatbot import voice

        monkeypatch.delenv("VOICE_TTS_LANG", raising=False)
        assert voice.kokoro_lang("zh") == "cmn"
        assert voice.kokoro_lang("hi") == "hi"
        assert voice.kokoro_lang("en") == "en-gb"
        assert voice.kokoro_lang(None) == "en-gb"

    def test_kokoro_lang_honours_configured_english(self, monkeypatch):
        from chatbot import voice

        monkeypatch.setenv("VOICE_TTS_LANG", "en-us")
        assert voice.kokoro_lang("en") == "en-us"
        assert voice.kokoro_lang("zh") == "cmn"

    def test_kokoro_voice_switches_family(self, monkeypatch):
        from chatbot import voice

        monkeypatch.setenv("VOICE_TTS_VOICE", "af_heart")
        assert voice.kokoro_voice("zh").startswith("zf_")
        assert voice.kokoro_voice("hi").startswith("hf_")
        assert voice.kokoro_voice("en") == "af_heart"

    def test_kokoro_voice_keeps_explicit_language_voice(self, monkeypatch):
        from chatbot import voice

        monkeypatch.setenv("VOICE_TTS_VOICE", "zm_yunxi")
        assert voice.kokoro_voice("zh") == "zm_yunxi"
