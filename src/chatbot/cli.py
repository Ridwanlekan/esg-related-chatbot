from chatbot.rag import RAGBot


def main():
    print("Initialising RAGBot...\n")
    rag = RAGBot()

    print("Syncing index with source data...\n")
    stats = rag.read_and_embed_data()
    print(
        f"Index ready: {stats.documents_seen} docs found, "
        f"{stats.chunks_upserted} chunks embedded, "
        f"{stats.stale_chunks_removed} stale chunks removed\n"
    )

    print(
        "Hi, I'm your RAGBot. I answer from the documents under data/. "
        "Workspace folders (finance/, hr/, ...) are indexed together here."
    )
    print("Type 'exit' to quit.\n")

    while True:
        question = input("You: ")
        if question.strip().lower() in {"exit", "quit", "q"}:
            print("Goodbye!")
            break
        response = rag.ask(question=question)
        print(f"RAGBot: {response}\n")


if __name__ == "__main__":
    main()