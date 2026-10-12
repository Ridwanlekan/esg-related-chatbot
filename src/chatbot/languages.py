"""Language helpers shared by the API, RAG and smalltalk layers.

Kept dependency-free so it can be imported without pulling in the heavy
embedding/LLM stack that rag.py needs.
"""

import re

SUPPORTED_LANGUAGES = ("en", "zh", "hi")

LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Simplified Chinese (简体中文)",
    "hi": "Hindi (हिन्दी)",
}

_ZH_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_HI_CHAR_RE = re.compile(r"[\u0900-\u097f]")


def normalize_language(value):
    """Map a request language tag onto one of the supported languages.

    English, Simplified Chinese and Hindi are supported; anything else
    (including a missing value) falls back to English so the default path is
    unchanged.
    """
    if not value:
        return "en"
    tag = str(value).strip().lower().replace("_", "-")
    if tag.startswith("zh"):
        return "zh"
    if tag.startswith("hi"):
        return "hi"
    return "en"


def detect_language(text):
    """Detect zh/hi by script; everything else (including English) is 'en'."""
    if not text:
        return "en"
    if _ZH_CHAR_RE.search(text):
        return "zh"
    if _HI_CHAR_RE.search(text):
        return "hi"
    return "en"


def resolve_language(question, requested=None):
    """Pick the answer language: query script wins, the UI is the fallback.

    A question written in Chinese or Hindi is answered in that language; an
    English question follows the requested (UI) language.
    """
    detected = detect_language(question)
    if detected != "en":
        return detected
    return normalize_language(requested)


def language_directive(language):
    """Instruction appended to a system prompt to pin the answer language."""
    lang = normalize_language(language)
    if lang == "en":
        return ""
    name = LANGUAGE_NAMES[lang]
    return (
        f" Answer in {name}, matching the user's language. Keep source "
        f"filenames and citations unchanged."
    )
