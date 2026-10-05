"""Voice mode: /voice/stt and /voice/tts.

The providers are stubbed. Nothing here touches Azure, faster-whisper, ffmpeg or
a real browser, and no test writes audio to disk.
"""

import io
import os
import sys
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot import voice
from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore

SECRET = "test-secret"
ADMIN_KEY = "secret-admin-key"


class FakeBot:
    def __init__(self):
        self.store = SimpleNamespace(count=lambda: 1)

    def retrieve(self, question, k=3, source=None, history=None, usage_sink=None):
        return []

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        return "answer"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        yield "answer"
        yield "[DONE]"


class FakeSTT:
    """Records the bytes it was handed so tests can assert on the upload."""

    def __init__(self, text="what are the IFRS S1 targets?", duration=4.5):
        self.text = text
        self.duration = duration
        self.seen = []

    def transcribe(self, audio_bytes, filename):
        self.seen.append((audio_bytes, filename))
        return self.text, self.duration


class FakeTTS:
    def __init__(self):
        self.seen = []

    def synthesize(self, text):
        self.seen.append(text)
        return b"ID3-fake-mp3-bytes"


@pytest.fixture(autouse=True)
def _reset_voice_providers():
    """The provider singletons are module-level; never let one leak between tests."""
    voice.reset_providers()
    yield
    voice.reset_providers()


@pytest.fixture
def stt():
    return FakeSTT()


@pytest.fixture
def tts():
    return FakeTTS()


@pytest.fixture
def voice_env(tmp_path, monkeypatch, stt, tts):
    monkeypatch.setattr(voice, "get_stt_provider", lambda: stt)
    monkeypatch.setattr(voice, "get_tts_provider", lambda: tts)
    monkeypatch.setenv("VOICE_STT_MODEL", "small")
    admin_store = AdminStore(str(tmp_path / "workspaces.sqlite3"))
    app = create_app(
        bot=FakeBot(),
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret=SECRET),
        admin_store=admin_store,
        api_key=ADMIN_KEY,
        rate_limit=0,
    )
    c = TestClient(app)
    c.admin_store = admin_store
    res = c.post("/auth/signup", json={
        "email": "u@corp.com", "password": "password123", "name": "User",
        "category": "finance",
    })
    assert res.status_code == 200, res.text
    c.token = res.json()["token"]
    return c


def _auth(c):
    return {"authorization": f"Bearer {c.token}"}


def _wav(seconds=1, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# ------------------------------------------------------------------ /voice/stt


def test_stt_returns_transcript(voice_env, stt):
    res = voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")}, headers=_auth(voice_env)
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"text": "what are the IFRS S1 targets?"}
    assert stt.seen and stt.seen[0][1] == "speech.wav"


def test_stt_requires_authentication(voice_env):
    res = voice_env.post("/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")})
    assert res.status_code == 401


def test_stt_rejects_disallowed_content_type(voice_env, stt):
    res = voice_env.post(
        "/voice/stt", files={"file": ("payload.exe", b"MZ\x90\x00", "application/x-msdownload")},
        headers=_auth(voice_env),
    )
    assert res.status_code == 422
    assert "content type" in res.json()["detail"].lower()
    assert stt.seen == []


def test_stt_accepts_browser_recorders(voice_env):
    """Chrome records webm/opus, Safari mp4/aac; both must reach the provider."""
    for mime, ext in (("audio/webm", "webm"), ("audio/mp4", "mp4"), ("audio/ogg", "ogg")):
        res = voice_env.post(
            "/voice/stt",
            files={"file": ("speech." + ext, b"OggS-not-really", mime)},
            headers=_auth(voice_env),
        )
        assert res.status_code == 200, (mime, res.text)


def test_stt_accepts_extension_when_browser_sends_no_content_type(voice_env):
    res = voice_env.post(
        "/voice/stt",
        files={"file": ("speech.m4a", b"data", "")},
        headers=_auth(voice_env),
    )
    assert res.status_code == 200, res.text


def test_stt_rejects_unknown_extension_without_content_type(voice_env, stt):
    res = voice_env.post(
        "/voice/stt", files={"file": ("payload.exe", b"MZ", "")}, headers=_auth(voice_env)
    )
    assert res.status_code == 422
    assert stt.seen == []


def test_stt_rejects_empty_upload(voice_env):
    res = voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", b"", "audio/wav")}, headers=_auth(voice_env)
    )
    assert res.status_code == 422


def test_stt_enforces_upload_size_cap(voice_env, monkeypatch, stt):
    monkeypatch.setenv("VOICE_MAX_UPLOAD_MB", "0")  # 0 MB -> anything is too big
    res = voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")}, headers=_auth(voice_env)
    )
    assert res.status_code == 413
    assert stt.seen == []


def test_stt_undecodable_audio_is_422_not_500(voice_env, monkeypatch):
    """Bytes with an allowed extension but no decodable audio inside.

    PyAV raises InvalidDataError, a ValueError. Unhandled that is a 500 with a
    decoder stack trace; the client needs a message it can act on. av.error
    subclasses ValueError, so the test drives that contract without importing
    PyAV -- the voice tests must not need the extra installed.
    """
    def boom(_audio, _name):
        raise voice.UndecodableAudio("Could not decode the uploaded audio: junk")

    stt = SimpleNamespace(transcribe=boom)
    monkeypatch.setattr(voice, "get_stt_provider", lambda: stt)
    res = voice_env.post(
        "/voice/stt", files={"file": ("speech.webm", b"\x1aE\xdf\xa3junk", "audio/webm")},
        headers=_auth(voice_env),
    )
    assert res.status_code == 422
    assert "could not decode" in res.json()["detail"].lower()


def test_undecodable_audio_is_a_value_error_so_decoder_contracts_are_kept():
    assert issubclass(voice.UndecodableAudio, ValueError)


def test_local_stt_wraps_decoder_value_error_as_undecodable_audio():
    """The provider normalises the decoder's ValueError into its own type."""
    stt = voice.LocalWhisperSTT.__new__(voice.LocalWhisperSTT)

    class FakeModel:
        def transcribe(self, path):
            raise ValueError("Invalid data found when processing input")

    stt._model = FakeModel()
    with pytest.raises(voice.UndecodableAudio, match="Could not decode"):
        stt.transcribe(b"\x1aE\xdf\xa3junk", "clip.webm")


def test_local_stt_deletes_the_temp_file_even_when_decoding_fails():
    """The clip is written to disk for the decoder, so cleanup must hold on error."""
    written = []

    stt = voice.LocalWhisperSTT.__new__(voice.LocalWhisperSTT)

    class FakeModel:
        def transcribe(self, path):
            written.append(path)
            raise ValueError("Invalid data found when processing input")

    stt._model = FakeModel()
    with pytest.raises(voice.UndecodableAudio):
        stt.transcribe(b"junk", "clip.webm")
    assert written and not os.path.exists(written[0])


def test_stt_unconfigured_provider_is_503_not_500(voice_env, monkeypatch):
    def boom():
        raise RuntimeError("Voice STT is not available: faster-whisper is not installed.")

    monkeypatch.setattr(voice, "get_stt_provider", boom)
    res = voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")}, headers=_auth(voice_env)
    )
    assert res.status_code == 503
    assert "faster-whisper" in res.json()["detail"]


# ------------------------------------------------------------------ /voice/tts


def test_tts_returns_audio(voice_env, tts):
    res = voice_env.post(
        "/voice/tts", json={"text": "Jupiter has many moons."}, headers=_auth(voice_env)
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("audio/mpeg")
    assert res.content == b"ID3-fake-mp3-bytes"
    assert tts.seen == ["Jupiter has many moons."]


def test_tts_requires_authentication(voice_env):
    assert voice_env.post("/voice/tts", json={"text": "hello"}).status_code == 401


def test_tts_rejects_empty_text(voice_env, tts):
    assert voice_env.post("/voice/tts", json={"text": ""}, headers=_auth(voice_env)).status_code == 422
    assert tts.seen == []


def test_tts_enforces_character_cap(voice_env, monkeypatch, tts):
    monkeypatch.setenv("VOICE_TTS_MAX_CHARS", "10")
    res = voice_env.post("/voice/tts", json={"text": "x" * 50}, headers=_auth(voice_env))
    assert res.status_code == 422
    assert tts.seen == []


def _tts_that_fails(exc):
    """A real OpenAITTS wired to a client that raises exc.

    The error is normalised inside the provider, so the endpoint tests must
    drive the real provider rather than a stub that raises raw SDK errors --
    a stub would bypass the wrapping these tests exist to cover.
    """
    class FakeSpeech:
        def create(self, **kw):
            raise exc

    return voice.OpenAITTS(SimpleNamespace(audio=SimpleNamespace(speech=FakeSpeech())))


def test_tts_missing_deployment_is_503_with_the_deployment_name(voice_env, monkeypatch):
    """Azure says 404 DeploymentNotFound; the UI must learn which name failed."""
    monkeypatch.setenv("VOICE_TTS_MODEL", "gpt-4o-mini-tts")

    class NotFound(Exception):
        status_code = 404
        message = "The API deployment for this resource does not exist."

    provider = _tts_that_fails(NotFound())
    monkeypatch.setattr(voice, "get_tts_provider", lambda: provider)
    res = voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "gpt-4o-mini-tts" in detail
    assert "deployment" in detail.lower()


def test_tts_wrong_model_kind_is_503_and_explains_the_operation(voice_env, monkeypatch):
    """A chat deployment answers 400 OperationNotSupported, not 404."""
    monkeypatch.setenv("VOICE_TTS_MODEL", "gpt-4.1-mini-1")

    class NotSupported(Exception):
        # Shaped like openai.BadRequestError: status_code, code, message.
        status_code = 400
        code = "OperationNotSupported"
        message = (
            "The audioSpeech operation does not work with the specified "
            "model, gpt-4.1-mini."
        )

    provider = _tts_that_fails(NotSupported())
    monkeypatch.setattr(voice, "get_tts_provider", lambda: provider)
    res = voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "gpt-4.1-mini-1" in detail
    assert "audioSpeech" in detail


def test_tts_transport_error_still_503s_with_detail(voice_env, monkeypatch):
    """An unexpected SDK failure must not escape as a 500."""

    class Boom(Exception):
        status_code = 503
        message = "service unavailable"

    provider = _tts_that_fails(Boom())
    monkeypatch.setattr(voice, "get_tts_provider", lambda: provider)
    res = voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    assert res.status_code == 503
    assert "service unavailable" in res.json()["detail"]


def test_openai_tts_wraps_sdk_errors_as_runtime_error():
    """The provider itself normalises SDK errors, not just the endpoint."""
    class NotFound(Exception):
        status_code = 404
        message = "deployment missing"

    with pytest.raises(RuntimeError, match="gpt-4o-mini-tts"):
        _tts_that_fails(NotFound()).synthesize("hello")


def test_failed_tts_is_not_metered(voice_env, monkeypatch):
    """A 503 costs nothing, so it must not appear in the usage ledger."""
    def boom(_text):
        raise RuntimeError("Voice TTS is unavailable")

    monkeypatch.setattr(voice, "get_tts_provider",
                        lambda: SimpleNamespace(synthesize=boom))
    voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    stats = voice_env.get("/admin/usage?range=all",
                          headers={"authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert stats["by_kind"] == []


# ----------------------------------------------------------------- TTS provider


def test_default_tts_provider_is_local_kokoro(monkeypatch):
    """No Azure TTS deployment is required for voice out to work."""
    monkeypatch.delenv("VOICE_TTS_PROVIDER", raising=False)
    assert voice.tts_provider_name() == "kokoro"
    assert voice.tts_model_name() == f"kokoro:{voice.DEFAULT_KOKORO_VOICE}"


def test_unknown_tts_provider_is_reported(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "nonsense")
    with pytest.raises(RuntimeError, match="VOICE_TTS_PROVIDER"):
        voice.get_tts_provider()


def test_openai_tts_provider_records_the_deployment_name(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_TTS_MODEL", "my-tts-deployment")
    assert voice.tts_model_name() == "my-tts-deployment"


def test_local_tts_label_tracks_the_chosen_voice(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "kokoro")
    monkeypatch.setenv("VOICE_TTS_VOICE", "bm_george")
    assert voice.tts_model_name() == "kokoro:bm_george"


def _kokoro_importable(monkeypatch):
    """Make `import soundfile` and `from kokoro_onnx import Kokoro` succeed.

    LocalKokoroTTS checks the import *before* it checks the weight files, so a
    test aiming at the file check has to get past the import first.
    """
    monkeypatch.setitem(
        sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=lambda *a, **k: None)
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(write=lambda *a: None))


def test_local_tts_reports_a_missing_model_file(tmp_path, monkeypatch):
    """A container built without the weights must 503, not crash on import.

    Both imports are stubbed so this asserts the same thing whether or not
    kokoro-onnx happens to be installed. Without the stub it passed only on a
    developer machine that had the dependency, and failed in CI.
    """
    _kokoro_importable(monkeypatch)
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "kokoro")
    monkeypatch.setenv("VOICE_TTS_MODEL_PATH", str(tmp_path / "absent.onnx"))
    monkeypatch.setenv("VOICE_TTS_VOICES_PATH", str(tmp_path / "absent.bin"))
    with pytest.raises(RuntimeError, match="model file is missing"):
        voice.get_tts_provider()


def test_local_tts_reports_a_missing_dependency(monkeypatch):
    """Without the extra installed, the error must name the install command.

    A sys.modules entry of None makes the import raise ImportError, which is
    how a machine lacking the `voice` extra behaves.
    """
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "kokoro")
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    monkeypatch.setitem(sys.modules, "soundfile", None)
    with pytest.raises(RuntimeError, match="kokoro-onnx is not installed"):
        voice.get_tts_provider()


def test_local_tts_endpoint_serves_a_playable_wav(voice_env, monkeypatch):
    """A real WAV header, and a media type the browser will accept."""
    import io as _io
    import wave as _wave

    payload = None
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)
    payload = buf.getvalue()

    monkeypatch.setenv("VOICE_TTS_PROVIDER", "kokoro")
    monkeypatch.setattr(
        voice, "get_tts_provider",
        lambda: SimpleNamespace(synthesize=lambda _t: payload, media_type="audio/wav"),
    )
    res = voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("audio/wav")
    assert res.content[:4] == b"RIFF"


def test_tts_media_type_falls_back_to_mp3_when_a_provider_omits_it(voice_env, monkeypatch):
    """A provider that forgets to declare one still gets a playable type."""
    monkeypatch.setattr(
        voice, "get_tts_provider",
        lambda: SimpleNamespace(synthesize=lambda _t: b"ID3-fake-mp3-bytes"),
    )
    res = voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("audio/mpeg")


def test_tts_unconfigured_provider_is_503(voice_env, monkeypatch):
    def boom():
        raise RuntimeError("Missing required environment variable AZURE_OPENAI_API_KEY.")

    monkeypatch.setattr(voice, "get_tts_provider", boom)
    res = voice_env.post("/voice/tts", json={"text": "hi"}, headers=_auth(voice_env))
    assert res.status_code == 503


# --------------------------------------------------------------------- metering


def test_voice_calls_are_metered(voice_env):
    voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")}, headers=_auth(voice_env)
    )
    voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))

    stats = voice_env.get("/admin/usage?range=all", headers={"authorization": f"Bearer {ADMIN_KEY}"}).json()
    kinds = {d["key"]: d for d in stats["by_kind"]}
    assert set(kinds) == {"stt", "tts"}
    assert kinds["stt"]["units"] == pytest.approx(4.5)   # seconds of audio
    assert kinds["tts"]["units"] == pytest.approx(5)     # characters
    assert stats["totals"]["voice_seconds"] == pytest.approx(4.5)
    assert stats["totals"]["voice_chars"] == pytest.approx(5)
    assert stats["totals"]["voice_cost"] > 0


def test_voice_cost_uses_its_own_rates_not_token_rates(voice_env, monkeypatch):
    monkeypatch.setenv("VOICE_STT_PRICE_PER_MIN", "60")  # $1 per second of audio
    monkeypatch.setenv("VOICE_TTS_PRICE_PER_M", "1000000")  # $1 per character
    voice_env.post(
        "/voice/stt", files={"file": ("speech.wav", _wav(), "audio/wav")}, headers=_auth(voice_env)
    )
    voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    stats = voice_env.get("/admin/usage?range=all", headers={"authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert stats["totals"]["cost"] == pytest.approx(4.5 + 5)


def test_token_rows_are_unaffected_by_voice_rates(voice_env, monkeypatch):
    monkeypatch.setenv("VOICE_STT_PRICE_PER_MIN", "60")
    voice_env.post("/chat", json={"question": "targets?"}, headers=_auth(voice_env))
    stats = voice_env.get("/admin/usage?range=all", headers={"authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert stats["totals"]["voice_cost"] == 0
    assert stats["totals"]["input_cost"] >= 0


def test_voice_usage_csv_has_units_column(voice_env):
    voice_env.post("/voice/tts", json={"text": "hello"}, headers=_auth(voice_env))
    res = voice_env.get("/admin/usage/export.csv?range=all", headers={"authorization": f"Bearer {ADMIN_KEY}"})
    assert res.status_code == 200
    header = res.text.splitlines()[0]
    assert header.split(",")[6].strip('"') == "units"
    assert any(",tts," in line for line in res.text.splitlines()[1:])


def test_legacy_usage_rows_without_units_still_aggregate(tmp_path):
    """A usage table written by an older build opens untouched (NULL units)."""
    store = AdminStore(str(tmp_path / "old.sqlite3"))
    store.record_usage(kind="stream", prompt_tokens=1_000_000, completion_tokens=0)
    stats = store.usage_stats()
    assert stats["totals"]["cost"] == pytest.approx(0.40)
    assert stats["totals"]["units"] == 0.0
    assert stats["totals"]["voice_cost"] == 0.0


# --------------------------------------------------------------- provider units


def test_mime_allowlist_ignores_codec_parameters():
    assert voice.mime_allowed("audio/webm;codecs=opus")
    assert voice.mime_allowed("AUDIO/MP4")
    assert not voice.mime_allowed("audio/flac")
    assert not voice.mime_allowed("")


def test_duration_of_a_real_wav_is_measured_without_tools():
    secs = voice._audio_duration_seconds(_wav(seconds=2, rate=8000), "a.wav")
    assert secs == pytest.approx(2.0, abs=0.01)


def test_duration_is_zero_rather_than_raising_on_garbage():
    assert voice._audio_duration_seconds(b"not audio at all", "a.wav") == 0.0


def test_unknown_stt_provider_is_reported(monkeypatch):
    monkeypatch.setenv("VOICE_STT_PROVIDER", "nonsense")
    with pytest.raises(RuntimeError, match="VOICE_STT_PROVIDER"):
        voice.get_stt_provider()


def test_openai_stt_provider_sends_the_openai_model_name(monkeypatch):
    monkeypatch.setenv("VOICE_STT_PROVIDER", "openai")
    assert voice.stt_model_name() == "whisper-1"


def test_local_stt_model_label_is_distinguishable_in_the_ledger(monkeypatch):
    monkeypatch.setenv("VOICE_STT_PROVIDER", "local")
    monkeypatch.setenv("VOICE_STT_MODEL", "medium")
    assert voice.stt_model_name() == "faster-whisper:medium"
