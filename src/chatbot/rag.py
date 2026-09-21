import os
import threading
import time

from dotenv import load_dotenv
from openai import AzureOpenAI
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer

from chatbot import model_utils, telemetry
from chatbot.ingest import ingest_documents, recursive_split
from chatbot.model_utils import BASE_DIR, env_or_local_model
from chatbot.reranker import CrossEncoderReranker, mmr_lambda_value, mmr_select, reranker_enabled
from chatbot.rewrite import rewrite_question
from chatbot.vector_store import SQLiteVecStore

load_dotenv()

DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_INDEX_DIR = os.environ.get("INDEX_DIR", os.path.join(BASE_DIR, ".index"))
DEFAULT_STORE_PATH = os.path.join(DEFAULT_INDEX_DIR, "vectors.sqlite3")
EMBEDDING_DIM = 384

SYSTEM_PROMPT = (
    "You are a data extractor expert. Use the given context to "
    "provide a response to the user's query. Cite the source file "
    "in square brackets when you use it. If you don't know, say "
    "'I don't know'. Do not try to make up an answer."
)


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy doc/env_example.txt to .env and fill it in."
        )
    return value


def env_or_local_model(model_name):
    """Deprecated alias kept for back-compat; use model_utils instead."""
    return model_utils.env_or_local_model(model_name)


class RAGBot:

    def __init__(self, store_path=DEFAULT_STORE_PATH, data_dir=DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self._llm_client = None
        self._llm_model = None
        self._llm_lock = threading.Lock()
        embedding_model = env_or_local_model("all-MiniLM-L6-v2")
        self.sentence_transformer = SentenceTransformer(embedding_model)
        self.tokenizer = AutoTokenizer.from_pretrained(embedding_model)
        self.store = SQLiteVecStore(db_path=store_path, dim=EMBEDDING_DIM)
        self.last_results = []
        self._reranker = None
        self.candidate_k = int(os.environ.get("CANDIDATE_K", "30"))
        self.max_generation_tokens = int(os.environ.get("MAX_GENERATION_TOKENS", "600"))

    def _ensure_reranker(self):
        if self._reranker is None:
            self._reranker = CrossEncoderReranker()
        return self._reranker

    def _ensure_llm(self):
        if self._llm_client is None:
            with self._llm_lock:
                if self._llm_client is None:
                    self._llm_client = AzureOpenAI(
                        api_key=_require_env("AZURE_OPENAI_API_KEY"),
                        api_version=_require_env("OPENAI_API_VERSION"),
                        azure_endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
                        timeout=float(os.environ.get("AZURE_OPENAI_TIMEOUT", "120")),
                        max_retries=int(os.environ.get("OPENAI_MAX_RETRIES", "3")),
                    )
                    self._llm_model = _require_env("MODEL_NAME")
        return self._llm_client, self._llm_model

    @property
    def openai_client(self):
        return self._ensure_llm()[0]

    @property
    def model_name(self):
        return self._ensure_llm()[1]

    def _complete(self, kind, messages, *, max_tokens, temperature=None, stream=False):
        """LLM call with telemetry. Returns a response or (when streaming) a
        generator that reports duration + token usage on exhaustion."""
        client, model = self._ensure_llm()
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

        started = time.monotonic()
        try:
            raw = client.chat.completions.create(**kwargs)
        except Exception as exc:
            telemetry.record_llm_error(kind, type(exc).__name__)
            raise
        first_seconds = time.monotonic() - started

        if not stream:
            usage = getattr(raw, "usage", None)
            telemetry.record_llm(
                kind,
                first_seconds,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
            )
            return raw

        def track():
            prompt = completion = None
            for chunk in raw:
                usage = getattr(chunk, "usage", None)
                if usage:
                    prompt = usage.prompt_tokens
                    completion = usage.completion_tokens
                yield chunk
            telemetry.record_llm(
                kind,
                time.monotonic() - started,
                prompt_tokens=prompt,
                completion_tokens=completion,
            )

        return track()
        model_path = os.path.join(BASE_DIR, "models/all-MiniLM-L6-v2")
        self.sentence_transformer = SentenceTransformer(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.store = SQLiteVecStore(db_path=store_path, dim=EMBEDDING_DIM)
        self.last_results = []

    def _embed(self, texts):
        return self.sentence_transformer.encode(list(texts), normalize_embeddings=True)

    def ingest(self, folder_path=None):
        folder = folder_path if folder_path is not None else self.data_dir
        stats = ingest_documents(
            data_dir=folder,
            store=self.store,
            embed_fn=self._embed,
        )
        telemetry.index_chunks.set(self.store.count())
        return stats

    def read_and_embed_data(self, folder_path=None):
        return self.ingest(folder_path=folder_path)

    def _chunk_text(self, text, size=500, overlap=50):
        return recursive_split(text, chunk_size=size, overlap=overlap)

    def rewrite_question_for_retrieval(self, question, history=None):
        if not history:
            return question
        try:
            return rewrite_question(question, history, self._generate_rewrite)
        except Exception:
            return question

    def _generate_rewrite(self, messages):
        response = self._complete(
            "rewrite", messages, max_tokens=80, temperature=0
        )
        return response.choices[0].message.content

    def retrieve(self, question, k=3, source=None, history=None):
        if self.store.count() == 0:
            raise RuntimeError("call ingest() before retrieve()")
        started = time.monotonic()
        rewritten = self.rewrite_question_for_retrieval(question, history)
        query_embedding = self._embed([rewritten])[0]
        results = self.store.search_hybrid(
            query_text=rewritten,
            query_embedding=query_embedding,
            k=self.candidate_k,
            candidate_k=self.candidate_k,
            source=source,
        )
        if reranker_enabled():
            results = self._ensure_reranker().rerank(rewritten, results, k)
        elif len(results) > k:
            results = results[:k]
        if len(results) > k:
            lambda_ = mmr_lambda_value()
            if lambda_ > 0:
                embeddings = self.store.fetch_embeddings([r.chunk_id for r in results])
                results = mmr_select(results, query_embedding, embeddings, k, lambda_)
            else:
                results = results[:k]
        telemetry.record_retrieval(time.monotonic() - started)
        return results

    def _build_messages(self, question, results, history=None):
        context = "\n--\n".join(f"[{r.source}] {r.content}" for r in results)
        if not context:
            context = "No relevant documents were found."
        context = context[:8000]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            for turn in history:
                turn = dict(turn)
                turn["content"] = turn["content"][:2000]
                messages.append(turn)
        messages.append(
            {"role": "user", "content": f"content\n{context}\nQuestion{question}"}
        )
        return messages

    def ask(self, question, k=3, source=None, history=None):
        results = self.retrieve(question, k=k, source=source, history=history)
        self.last_results = results
        if not results:
            return "I don't know. No relevant documents were found."

        response = self._complete(
            "answer", self._build_messages(question, results, history),
            max_tokens=self.max_generation_tokens,
        )
        return response.choices[0].message.content

    def ask_stream(self, question, k=3, source=None, history=None):
        results = self.retrieve(question, k=k, source=source, history=history)
        self.last_results = results
        if not results:
            yield "I don't know. No relevant documents were found."
            return

        stream = self._complete(
            "stream", self._build_messages(question, results, history),
            max_tokens=self.max_generation_tokens,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta