"""Voice (STT/TTS) providers.

Voice is an input/output adapter around the chat pipeline: speech is
transcribed to text, the user reviews the transcript, the existing chat
pipeline answers, and the answer can be spoken back. Nothing here touches
retrieval, citations or streaming.

Provider construction follows the lazy pattern of rag.py: clients are built
on first use from env vars, so importing this module never requires
faster-whisper, the openai SDK, or any credential, and the app boots with
voice unconfigured. A voice call with no usable provider raises RuntimeError,
which the API layer maps to the same clean 503 the app uses for an
unconfigured LLM.

STT defaults to local faster-whisper (zero marginal cost; the ffmpeg binary
must be installed). VOICE_STT_PROVIDER=openai switches STT to the Whisper API
over the same Azure OpenAI endpoint/key as the LLM — one flag, no new
credential. TTS always uses the OpenAI-compatible speech endpoint over that
same client.
"""

import io
import os
import subprocess
import tempfile
import threading
import wave

# Audio the STT endpoint accepts. Browsers send what MediaRecorder recorded:
# webm/opus on Chrome/Firefox, mp4/aac on Safari; wav/ogg/mpeg round out uploads
# from desktop tools. Parameters (e.g. "audio/webm;codecs=opus") are stripped
# before matching.
STT_MIME_TYPES = frozenset(
    {
        "audio/webm",
        "audio/mp4",
        "audio/x-m4a",
        "audio/aac",
        "audio/wav",
        "audio/ogg",
        "audio/mpeg",
    }
)

# Accepted when a browser sends no content-type at all (some mobile Safari
# builds send an empty or generic type for blob uploads).
AUDIO_EXTENSIONS = (".webm", ".mp4", ".m4a", ".aac", ".wav", ".ogg", ".mp3")

DEFAULT_STT_MODEL = "small"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = "nova"
OPENAI_STT_MODEL = "whisper-1"

# Local Kokoro TTS. Weights are baked into the image at build time (see
# Dockerfile) so the container never downloads at runtime.
DEFAULT_KOKORO_MODEL = "models/tts/kokoro-v1.0.onnx"
DEFAULT_KOKORO_VOICES = "models/tts/voices-v1.0.bin"
DEFAULT_KOKORO_VOICE = "af_heart"

_stt_provider = None
_tts_provider = None


class UndecodableAudio(ValueError):
    """Uploaded bytes are not audio the decoder can read.

    A distinct type so the API can tell a bad upload (422, retryable by the
    user) from an unconfigured provider (503, not retryable without a server
    change). Both are subclassed from ValueError because that is what the
    PyAV/ctranslate2 stack raises underneath.
    """


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy doc/env_example.txt to .env and fill it in."
        )
    return value


def stt_provider_name():
    return os.environ.get("VOICE_STT_PROVIDER", "local").strip().lower()


def stt_model_name():
    """Model label recorded in the usage ledger."""
    if stt_provider_name() == "openai":
        return OPENAI_STT_MODEL
    return f"faster-whisper:{os.environ.get('VOICE_STT_MODEL', DEFAULT_STT_MODEL)}"


def tts_provider_name():
    return os.environ.get("VOICE_TTS_PROVIDER", "kokoro").strip().lower()


def _openai_tts_deployment():
    """Azure deployment name for the cloud speech endpoint."""
    return os.environ.get("VOICE_TTS_MODEL", DEFAULT_TTS_MODEL)


def tts_model_name():
    """Model label recorded in the usage ledger.

    For local Kokoro this is derived from the voice, since a voice is the only
    thing a user actually chooses and there is no per-variant model.
    """
    if tts_provider_name() == "kokoro":
        return f"kokoro:{os.environ.get('VOICE_TTS_VOICE', DEFAULT_KOKORO_VOICE)}"
    return _openai_tts_deployment()


def mime_allowed(content_type):
    base = (content_type or "").split(";")[0].strip().lower()
    return base in STT_MIME_TYPES if base else False


def filename_allowed(filename):
    return os.path.splitext((filename or "").lower())[1] in AUDIO_EXTENSIONS


def reset_providers():
    """Drop cached providers. Used by tests and if env changes mid-process."""
    global _stt_provider, _tts_provider
    _stt_provider = None
    _tts_provider = None


class LocalWhisperSTT:
    """On-device transcription via faster-whisper (lazy-imported)."""

    def __init__(self, model_size=None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "Voice STT is not available: faster-whisper is not installed. "
                "Install it with: pip install 'esg-chatbot[voice]' (no external "
                "ffmpeg needed, PyAV bundles it), or set VOICE_STT_PROVIDER=openai "
                "to use the Whisper API over the existing Azure OpenAI endpoint."
            ) from e
        model_size = model_size or os.environ.get("VOICE_STT_MODEL", DEFAULT_STT_MODEL)
        try:
            self._model = WhisperModel(model_size)
        except Exception as e:
            raise RuntimeError(
                f"Voice STT failed to load the faster-whisper model "
                f"{model_size!r}: {e}"
            ) from e

    def transcribe(self, audio_bytes, filename):
        suffix = os.path.splitext(filename or "")[1] or ".audio"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            tmp.write(audio_bytes)
            tmp.close()
            segments, info = self._model.transcribe(tmp.name)
            text = "".join(seg.text for seg in segments).strip()
            duration = float(getattr(info, "duration", 0.0) or 0.0)
            return text, duration
        except ValueError as e:
            # PyAV raises InvalidDataError (a ValueError subclass) for bytes
            # that are not decodable audio. The extension allowlist only checks
            # the name, so a truncated or mislabelled recording reaches here.
            # That is a bad upload, not a broken server, hence its own type so
            # the API can answer 422 instead of an unhandled 500.
            raise UndecodableAudio(f"Could not decode the uploaded audio: {e}") from e
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


class OpenAIWhisperSTT:
    """Whisper API fallback over the shared OpenAI-compatible client."""

    def __init__(self, client):
        self._client = client

    def transcribe(self, audio_bytes, filename):
        name = filename or "audio.webm"
        if "." not in name:
            name += ".webm"
        upload = io.BytesIO(audio_bytes)
        upload.name = name
        response = self._client.audio.transcriptions.create(
            model=OPENAI_STT_MODEL, file=upload
        )
        duration = _audio_duration_seconds(audio_bytes, name)
        return (response.text or "").strip(), duration


def _tts_failure_message(model, exc):
    """Turn a TTS API error into something the UI can act on.

    Azure answers a missing TTS deployment with two different codes depending
    on the model name: DeploymentNotFound (404) when the deployment name does
    not exist at all, OperationNotSupported (400) when it exists but cannot do
    audioSpeech. Both mean the same thing to the user, and neither is fixable
    by retrying, so both become a 503 with the deployment name spelled out.
    """
    status = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "message", "") or exc)
    code = str(getattr(exc, "code", "") or "")
    hint = (
        f"Azure OpenAI has no text-to-speech deployment named {model!r} on "
        "this resource, so answers cannot be spoken. Provision a TTS "
        "deployment (for example gpt-4o-mini-tts) and set VOICE_TTS_MODEL to "
        "its deployment name, or unset it if that name is already correct. "
        "Text input and transcription are unaffected."
    )
    if status == 404:
        return f"Voice TTS is unavailable: no such deployment {model!r}. {hint}"
    if status == 400 and (
        code == "OperationNotSupported"
        or "does not work with the specified model" in detail
    ):
        return (
            f"Voice TTS is unavailable: deployment {model!r} exists but does "
            f"not support audioSpeech. {hint}"
        )
    return f"Voice TTS request failed ({status or type(exc).__name__}): {detail}"


class OpenAITTS:
    """Cloud TTS over the shared OpenAI-compatible client. Returns mp3."""

    media_type = "audio/mpeg"

    def __init__(self, client):
        self._client = client

    def synthesize(self, text):
        # The deployment name this provider actually called, not the ledger
        # label: the two differ when the env selects a provider other than
        # this one, and the error message must name what was requested.
        model = _openai_tts_deployment()
        try:
            response = self._client.audio.speech.create(
                model=model,
                voice=os.environ.get("VOICE_TTS_VOICE", DEFAULT_TTS_VOICE),
                input=text,
                response_format="mp3",
            )
        except Exception as e:
            raise RuntimeError(_tts_failure_message(model, e)) from e
        return response.content


class LocalKokoroTTS:
    """On-device TTS via kokoro-onnx (lazy-imported).

    Chosen over the reference `kokoro` package because that one requires
    Python <3.13, while CI and the container both pin 3.13. kokoro-onnx runs
    the same v1.0 weights through onnxruntime, which drops the torch
    dependency and the licence entanglement that came with it.

    Output is WAV rather than mp3: onnxruntime ships no mp3 encoder, and every
    browser plays WAV natively, so this needs no system codec.
    """

    media_type = "audio/wav"

    def __init__(self, model_path=None, voices_path=None):
        model_path = model_path or os.environ.get(
            "VOICE_TTS_MODEL_PATH", DEFAULT_KOKORO_MODEL
        )
        voices_path = voices_path or os.environ.get(
            "VOICE_TTS_VOICES_PATH", DEFAULT_KOKORO_VOICES
        )
        try:
            import soundfile
            from kokoro_onnx import Kokoro
        except ImportError as e:
            raise RuntimeError(
                "Voice TTS is not available: kokoro-onnx is not installed. "
                "Install it with: pip install 'esg-chatbot[voice]' (adds "
                "kokoro-onnx and soundfile; espeak-ng comes bundled via "
                "espeakng-loader, so no system package is needed), or set "
                "VOICE_TTS_PROVIDER=openai to use the Azure OpenAI speech "
                "endpoint instead."
            ) from e
        if not os.path.exists(model_path):
            raise RuntimeError(
                f"Voice TTS is unavailable: the Kokoro model file is missing "
                f"at {model_path!r}. It is fetched at image build time; "
                "override the path with VOICE_TTS_MODEL_PATH."
            )
        if not os.path.exists(voices_path):
            raise RuntimeError(
                f"Voice TTS is unavailable: the Kokoro voice pack is missing "
                f"at {voices_path!r}. It is fetched at image build time; "
                "override the path with VOICE_TTS_VOICES_PATH."
            )
        self._sf = soundfile
        self._model_path = model_path
        self._voices_path = voices_path
        # Synthesis is CPU-bound and the ONNX session is not re-entrant, so
        # concurrent /voice/tts requests serialise here rather than corrupting
        # the session or thrashing the CPU.
        self._lock = threading.Lock()
        self._kokoro = None

    def _ensure_model(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro

            try:
                self._kokoro = Kokoro(self._model_path, self._voices_path)
            except Exception as e:
                raise RuntimeError(
                    f"Voice TTS failed to load the Kokoro model at "
                    f"{self._model_path!r}: {e}"
                ) from e
        return self._kokoro

    def voices(self):
        return list(self._ensure_model().get_voices())

    def synthesize(self, text):
        voice = os.environ.get("VOICE_TTS_VOICE", DEFAULT_KOKORO_VOICE)
        speed = float(os.environ.get("VOICE_TTS_SPEED", "1.0"))
        lang = os.environ.get("VOICE_TTS_LANG", "en-gb")
        with self._lock:
            model = self._ensure_model()
            try:
                samples, sample_rate = model.create(
                    text, voice=voice, speed=speed, lang=lang
                )
            except Exception as e:
                raise RuntimeError(
                    f"Voice TTS failed while synthesizing with voice "
                    f"{voice!r} (lang {lang!r}): {e}"
                ) from e
            # int16 keeps the payload at 48 KB/s, which matters because the
            # whole clip crosses the wire on every answer read-aloud.
            pcm = (samples * 32767).astype("int16")
            buf = io.BytesIO()
            self._sf.write(buf, pcm, sample_rate, format="WAV", subtype="PCM_16")
            return buf.getvalue()


_client = None
_client_lock = threading.Lock()


def _ensure_openai_client():
    """Mirror of rag.py's client construction: same endpoint/key as the LLM.

    Azure Foundry endpoints (/api/projects/) take an OpenAI-compatible
    base_url; classic Azure OpenAI takes the AzureOpenAI wrapper. The openai
    SDK is lazy-imported so this module stays importable without it.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        from openai import AzureOpenAI, OpenAI

        endpoint = _require_env("AZURE_OPENAI_ENDPOINT").strip()
        api_key = _require_env("AZURE_OPENAI_API_KEY")
        timeout = float(os.environ.get("AZURE_OPENAI_TIMEOUT", "120"))
        max_retries = int(os.environ.get("OPENAI_MAX_RETRIES", "3"))
        if "/api/projects/" in endpoint:
            base_url = endpoint.rstrip("/")
            if base_url.endswith("/responses"):
                base_url = base_url[: -len("/responses")]
            base_url = base_url.rstrip("/") + "/"
            _client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=max_retries,
            )
        else:
            _client = AzureOpenAI(
                api_key=api_key,
                api_version=_require_env("OPENAI_API_VERSION"),
                azure_endpoint=endpoint,
                timeout=timeout,
                max_retries=max_retries,
            )
    return _client


def get_stt_provider():
    global _stt_provider
    if _stt_provider is None:
        name = stt_provider_name()
        if name == "local":
            provider = LocalWhisperSTT()
        elif name == "openai":
            provider = OpenAIWhisperSTT(_ensure_openai_client())
        else:
            raise RuntimeError(
                f"Unknown VOICE_STT_PROVIDER {name!r} (expected 'local' or 'openai')"
            )
        _stt_provider = provider
    return _stt_provider


def get_tts_provider():
    """Build the TTS provider named by VOICE_TTS_PROVIDER, once.

    Defaults to local Kokoro: the Azure speech endpoint needs a TTS deployment
    provisioned on the resource, which is an operational prerequisite the local
    default avoids. Each provider declares its own media type, so the endpoint
    does not have to know which one it got.
    """
    global _tts_provider
    if _tts_provider is None:
        name = tts_provider_name()
        if name == "kokoro":
            _tts_provider = LocalKokoroTTS()
        elif name == "openai":
            _tts_provider = OpenAITTS(_ensure_openai_client())
        else:
            raise RuntimeError(
                f"Unknown VOICE_TTS_PROVIDER {name!r} "
                f"(expected 'kokoro' or 'openai')"
            )
    return _tts_provider


def _audio_duration_seconds(audio_bytes, filename):
    """Best-effort audio duration for the usage ledger.

    The Whisper API response carries no duration, so it is measured locally:
    stdlib wave for .wav, soundfile when installed (webm/mp4/ogg), ffprobe as
    a last resort. Returns 0.0 when nothing can parse the bytes — cost is
    then simply under-recorded, never a failed request.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".wav":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if rate:
                    return frames / float(rate)
        except (wave.Error, EOFError):
            return 0.0
    try:
        import soundfile

        with soundfile.SoundFile(io.BytesIO(audio_bytes)) as f:
            return float(len(f)) / f.samplerate if f.samplerate else 0.0
    except Exception:
        pass
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "csv=p=0", "-i", "pipe:0",
            ],
            input=audio_bytes,
            capture_output=True,
            timeout=10,
        )
        return float(out.stdout.decode("utf-8", "replace").strip() or 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0
