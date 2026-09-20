REWRITE_SYSTEM_PROMPT = (
    "You are a search query rewriter. Given the conversation history and the "
    "latest user question, produce a standalone question that captures the "
    "user's intent, suitable for retrieving from a document index. Use the "
    "history only for context — do not answer the question. Return only the "
    "rewritten question, no preamble."
)

MAX_HISTORY_TURNS = 6


def rewrite_question(question, history, generate_fn):
    if not history:
        return question

    messages = [{"role": "system", "content": REWRITE_SYSTEM_PROMPT}]
    messages.extend(history[-MAX_HISTORY_TURNS:])
    messages.append({"role": "user", "content": question})

    rewritten = (generate_fn(messages) or "").strip().strip('"'.strip())
    return rewritten or question