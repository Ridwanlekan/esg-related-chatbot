import os

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


class RAGBot:

    def __init__(self, store_path=DEFAULT_STORE_PATH, data_dir=DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self.openai_client = AzureOpenAI(
            api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            api_version=os.environ.get("OPENAI_API_VERSION"),
            azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
        )
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
        response = self.openai_client.chat.completions.create(
            model=os.environ.get("MODEL_NAME"),
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
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append(
            {"role": "user", "content": f"content\n{context}\nQuestion{question}"}
        )
        return messages

    def ask(self, question, k=3, source=None, history=None):
        results = self.retrieve(question, k=k, source=source, history=history)
        self.last_results = results
        if not results:
            return "I don't know. No relevant documents were found."

        response = self.openai_client.chat.completions.create(
            model=os.environ.get("MODEL_NAME"),
            messages=self._build_messages(question, results, history),
        )
        return response.choices[0].message.content

    def ask_stream(self, question, k=3, source=None, history=None):
        results = self.retrieve(question, k=k, source=source, history=history)
        self.last_results = results
        if not results:
            yield "I don't know. No relevant documents were found."
            return

        stream = self.openai_client.chat.completions.create(
            model=os.environ.get("MODEL_NAME"),
            messages=self._build_messages(question, results, history),
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta