FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.hf-cache \
    VOICE_TTS_MODEL_PATH=/app/models/tts/kokoro-v1.0.onnx \
    VOICE_TTS_VOICES_PATH=/app/models/tts/voices-v1.0.bin

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
COPY data ./data

RUN pip install -r requirements.txt \
 && pip install . --no-deps

# Local voice STT + TTS: the `voice` extra (faster-whisper + Kokoro), with
# Kokoro's weights and the whisper model baked in so the container never
# downloads at runtime. Costs ~350 MB of Kokoro weights and ~490 MB of
# `small` whisper weights. Build with `--build-arg INSTALL_VOICE=false` to skip
# both, which leaves /voice/stt and /voice/tts returning a clean 503.
ARG INSTALL_VOICE=true
ARG KOKORO_RELEASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
# Each python snippet stays on ONE physical line. A real newline inside
# python -c "..." ends the RUN instruction in Docker's parser, so the following
# line is read as its own (invalid) instruction; the `;` separators keep it
# valid Python. Use `\` only at end of line, never inside the quotes.
RUN if [ "$INSTALL_VOICE" = "true" ]; then \
      pip install '.[voice]' \
      && KOKORO_RELEASE="$KOKORO_RELEASE" python -c "import os, urllib.request; d = '/app/models/tts'; os.makedirs(d, exist_ok=True); [urllib.request.urlretrieve(os.environ['KOKORO_RELEASE'] + '/' + n, os.path.join(d, n)) for n in ('kokoro-v1.0.onnx', 'voices-v1.0.bin')]" \
      && HF_HOME=/app/.hf-cache python -c "from faster_whisper import WhisperModel; WhisperModel('small')"; \
    fi

# Optional: audio/video transcription of ingested documents. Requires the
# ffmpeg binary and the `docling[asr]` extra:
# docker build --build-arg INSTALL_ASR=true .
ARG INSTALL_ASR=false
RUN if [ "$INSTALL_ASR" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends ffmpeg \
      && rm -rf /var/lib/apt/lists/* \
      && pip install "docling[asr]"; \
    fi

# Bake the embedding + cross-encoder models in at build time so the
# container runs fully offline (no runtime downloads).
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'))" || exit 1

CMD ["esg-api"]