"""Citation chip behaviour in static/ui.html, exercised under node.

The citation merging logic is plain JavaScript with no build step and no test
runner in the repo, so the alternative is that it is only ever checked by hand
in a browser. These are the cases that were actually wrong once: retrieval
order deciding the page range, and the label disagreeing with the link the
browser follows.
"""

import re
import shutil
import subprocess

import pytest

UI = "src/chatbot/static/ui.html"

HARNESS = r"""
const assert = require("assert");

// Minimal DOM. Only the shapes the citation path touches.
function el(tag) {
  return {
    tag, className: "", textContent: "", href: "", title: "", value: "",
    children: [],
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
    addEventListener() {},
    remove() {},
    focus() {},
  };
}
global.document = { createElement: el, getElementById: () => el("div") };
global.location = { search: "", hash: "", href: "", assign() {} };
global.window = { location: global.location };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.alert = () => {};
// Never resolves: the page's own init() then suspends instead of racing the
// assertions below or logging unhandled rejections into the test output.
global.fetch = () => new Promise(() => {});
global.$ = () => el("div");
global.EventSource = class {};
"""

ASSERTIONS = r"""
function chipsFor(sources) {
  const bot = el("div");
  addSources(bot, sources);
  const box = bot.children[0];
  return box.children.slice(1); // [0] is the "Sources:" label
}

function merge(label, pageList) {
  // Several chunks of one document, one per page, as retrieval returns them.
  return chipsFor(pageList.map((p) => ({
    source: "a.pdf", url: "/documents/download?source=a.pdf&token=t#page=" + p,
    page_start: p, page_end: p, similarity: 0.4,
  })))[0];
}

const url = (p) => "/documents/download?source=a.pdf&token=abc#page=" + p;
function assertIncludes(haystack, needle, what) {
  assert.ok(haystack.includes(needle), what + ": " + haystack);
}

// Retrieval order must not decide the range: page 5 first, then 10.
let chips = chipsFor([
  { source: "a.pdf", url: url(5), page_start: 5, page_end: 5, similarity: 0.4 },
  { source: "a.pdf", url: url(10), page_start: 10, page_end: 10, similarity: 0.3 },
]);
assert.strictEqual(chips.length, 1, "one chip per document");
// Scattered pages are listed, not collapsed into a span that implies contiguity.
assertIncludes(chips[0].title, "pp. 5, 10", "scattered pair");
assert.ok(chips[0].href.endsWith("#page=5"), chips[0].href);

// ...and the same pair in the other order must land identically.
chips = chipsFor([
  { source: "a.pdf", url: url(10), page_start: 10, page_end: 10, similarity: 0.4 },
  { source: "a.pdf", url: url(5), page_start: 5, page_end: 5, similarity: 0.3 },
]);
assertIncludes(chips[0].title, "pp. 5, 10", "scattered pair, reversed");
assert.ok(chips[0].href.endsWith("#page=5"), chips[0].href);

// Consecutive pages still compress to a range.
assertIncludes(merge("contiguous", [7, 8, 9]).title, "pp. 7–9", "contiguous run");

// The shape measured on the real IFRS index: 7,8,9,11,20 -> three runs.
assertIncludes(merge("real", [7, 8, 9, 11, 20]).title, "pp. 7–9, 11, 20", "three runs");

// Five or six scattered pages is the typical case and must be shown in full: the
// whole point of the label is naming them.
assertIncludes(merge("five", [7, 12, 20, 31, 39]).title, "pp. 7, 12, 20, 31, 39", "five runs");
assertIncludes(merge("six", [1, 3, 5, 7, 9, 11]).title, "pp. 1, 3, 5, 7, 9, 11", "six runs");

// Past the cap it abbreviates, and the count keeps the tail honest.
const many = merge("many", [1, 3, 5, 7, 9, 11, 13]);
assertIncludes(many.title, "pp. 1, 3 … 13 (7 pages)", "abbreviated");

// The first citation's page survives even with no duplicate to merge with.
chips = chipsFor([
  { source: "a.pdf", url: url(7), page_start: 7, page_end: 7, similarity: 0.4 },
]);
assertIncludes(chips[0].title, "p. 7", "single page");
assert.ok(chips[0].href.endsWith("#page=7"), chips[0].href);

// One chunk spanning pages keeps its range: that span is contiguous already.
chips = chipsFor([
  { source: "a.pdf", url: url(5), page_start: 5, page_end: 7, similarity: 0.4 },
]);
assertIncludes(chips[0].title, "pp. 5–7", "single chunk range");

// An unpaginated citation gets no page label and no invented fragment.
chips = chipsFor([
  { source: "b.txt", url: "/documents/download?source=b.txt&token=abc",
    page_start: null, page_end: null, similarity: 0.4 },
]);
assert.ok(!chips[0].title.includes("p."), chips[0].title);
assert.strictEqual(chips[0].href, "/documents/download?source=b.txt&token=abc");

// A paginated and an unpaginated chunk of one document merge to the known page.
chips = chipsFor([
  { source: "a.pdf", url: url(3), page_start: 3, page_end: 3, similarity: 0.4 },
  { source: "a.pdf", url: "/documents/download?source=a.pdf&token=abc",
    page_start: null, page_end: null, similarity: 0.3 },
]);
assertIncludes(chips[0].title, "p. 3", "mixed paged and unpaginated");
assert.ok(chips[0].href.endsWith("#page=3"), chips[0].href);

// Sessions stored before signed URLs still render as text-only chips.
chips = chipsFor(["legacy.txt"]);
assert.strictEqual(chips[0].textContent, "legacy.txt");

// Distinct documents stay distinct.
chips = chipsFor([
  { source: "a.pdf", url: url(1), page_start: 1, page_end: 1, similarity: 0.4 },
  { source: "c.pdf", url: url(9), page_start: 9, page_end: 9, similarity: 0.3 },
]);
assert.strictEqual(chips.length, 2);

console.log("ui citation assertions passed");
// The page schedules polling work, so the event loop would stay alive after the
// assertions finish. Exit deliberately: anything that threw already exited.
process.exit(0);
"""


def _ui_script():
    html = open(UI, encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline script found in " + UI
    return "\n".join(blocks)


def _run(harness_body, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    path = tmp_path / "ui_harness.js"
    path.write_text(HARNESS + _ui_script() + harness_body, encoding="utf-8")
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_ui_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    path = tmp_path / "ui.js"
    path.write_text(_ui_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_citation_pages_merge_regardless_of_retrieval_order(tmp_path):
    out = _run(ASSERTIONS, tmp_path)
    assert "ui citation assertions passed" in out
