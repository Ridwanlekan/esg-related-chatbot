"""No duplicate top-level function declarations in the served HTML/JS.

Function declarations are hoisted and the last one wins, so a leftover copy
silently replaces the version you meant to ship. `node --check` only validates
syntax and reports nothing, and this already shipped once: an unguarded
stopIngestWatch shadowed the race fix that was supposed to be there.
"""

import re
from collections import Counter
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src/chatbot/static"
PAGES = ["admin.html", "ui.html"]


def _top_level_functions(script):
    """Function declarations at column 0 (i.e. not nested inside another)."""
    return re.findall(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", script, re.M)


def _scripts(page):
    return re.findall(r"<script>(.*?)</script>", (STATIC / page).read_text(), re.S)


def test_no_page_declares_the_same_function_twice():
    for page in PAGES:
        for index, script in enumerate(_scripts(page)):
            dupes = {name: n for name, n in Counter(_top_level_functions(script)).items() if n > 1}
            assert not dupes, (
                f"{page} script #{index} redeclares {dupes}; "
                "the last declaration silently wins"
            )


def test_no_page_declares_the_same_function_across_scripts():
    for page in PAGES:
        names = [n for script in _scripts(page) for n in _top_level_functions(script)]
        dupes = {name: n for name, n in Counter(names).items() if n > 1}
        assert not dupes, f"{page} declares {dupes} in more than one script"


def test_watch_internals_are_intact():
    """The specific regression: the cancelled flag is what stops an in-flight
    poll re-arming the timer and re-busying a finished button."""
    body = re.search(
        r"function stopIngestWatch[\s\S]*?\n\}", (STATIC / "admin.html").read_text()
    ).group(0)
    assert "state.cancelled = true" in body
    assert ':state' in body
