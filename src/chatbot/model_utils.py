import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def env_or_local_model(model_name, prefix="sentence-transformers"):
    """Use the vendored model copy if present (models/<name>), else the
    Hugging Face model id so sentence-transformers can download it on
    first use. Names that already carry an org (e.g.
    "cross-encoder/ms-marco-MiniLM-L-6-v2") are returned unchanged."""
    local = os.path.join(BASE_DIR, "models", model_name)
    if os.path.isdir(local):
        return local
    if "/" in model_name:
        return model_name
    return f"{prefix}/{model_name}"