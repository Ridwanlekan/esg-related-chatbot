import os
import threading

from dotenv import load_dotenv
from openai import AzureOpenAI
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer

from chatbot.ingest import ingest_documents, recursive_split
from chatbot.rewrite import rewrite_question
from chatbot.vector_store import SQLiteVecStore

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_STORE_PATH = os.path.join(BASE_DIR, ".index", "vectors.sqlite3")
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


class RAGBot:

    def __init__(self, store_path=DEFAULT_STORE_PATH, data_dir=DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self._llm_client = None
        self._llm_model = None
        self._llm_lock = threading.Lock()
        model_path = os.path.join(BASE_DIR, "models/all-MiniLM-L6-v2")
        self.sentence_transformer = SentenceTransformer(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.store = SQLiteVecStore(db_path=store_path, dim=EMBEDDING_DIM)
        self.last_results = []
        self.max_generation_tokens = int(os.environ.get("MAX_GENERATION_TOKENS", "600"))

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
        model_path = os.path.join(BASE_DIR, "models/all-MiniLM-L6-v2")
        self.sentence_transformer = SentenceTransformer(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.store = SQLiteVecStore(db_path=store_path, dim=EMBEDDING_DIM)
        self.last_results = []

    def _embed(self, texts):
        return self.sentence_transformer.encode(list(texts), normalize_embeddings=True)

    def ingest(self, folder_path=None):
        folder = folder_path if folder_path is not None else self.data_dir
        return ingest_documents(
            data_dir=folder,
            store=self.store,
            embed_fn=self._embed,
        )

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
        client, model = self._ensure_llm()
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=80,
        )
        return response.choices[0].message.content

    def retrieve(self, question, k=3, source=None, history=None):
        if self.store.count() == 0:
            raise RuntimeError("call ingest() before retrieve()")
        rewritten = self.rewrite_question_for_retrieval(question, history)
        query_embedding = self._embed([rewritten])[0]
        return self.store.search_hybrid(
            query_text=rewritten,
            query_embedding=query_embedding,
            k=k,
            source=source,
        )

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

        client, model = self._ensure_llm()
        response = client.chat.completions.create(
            model=model,
            messages=self._build_messages(question, results, history),
            max_tokens=self.max_generation_tokens,
        )
        return response.choices[0].message.content

    def ask_stream(self, question, k=3, source=None, history=None):
        results = self.retrieve(question, k=k, source=source, history=history)
        self.last_results = results
        if not results:
            yield "I don't know. No relevant documents were found."
            return

        client, model = self._ensure_llm()
        stream = client.chat.completions.create(
            model=model,
            messages=self._build_messages(question, results, history),
            stream=True,
            max_tokens=self.max_generation_tokens,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta