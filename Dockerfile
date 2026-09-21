FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.hf-cache

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
COPY data ./data

RUN pip install -r requirements.txt \
 && pip install . --no-deps

# Bake the embedding + cross-encoder models in at build time so the
# container runs fully offline (no runtime downloads).
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

EXPOSE 8000

CMD ["esg-api"]