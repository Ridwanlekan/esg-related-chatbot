"""The owner's usage panel in static/ui.html, exercised under node.

The panel is pure string-building plus one gate, but both are the kind of
logic that is only ever checked by reading it unless it runs: a period the
server has never sent, a free-tier organisation, and a member who must not be
handed the billing numbers. No build step and no JS test runner in the repo,
so the page's own functions are driven against a minimal DOM.
"""

import re
import shutil
import subprocess

import pytest

UI = "src/chatbot/static/ui.html"

HARNESS = r"""
const assert = require("assert");

function el(tag) {
  return {
    tag, className: "", textContent: "", href: "", title: "", value: "",
    innerHTML: "", children: [],
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    addEventListener() {},
    remove() {},
    focus() {},
  };
}
global.document = {
  createElement: el, getElementById: () => el("div"),
  addEventListener() {},
};
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
// The shape acme answers with right now: a paid plan whose period window has
// never been recorded, so metering is idle rather than silently wrong.
const idle = renderUsagePanel({
  plan: "team", billing_status: "active",
  period_start: null, period_end: "2027-09-03T19:33:20+00:00",
  questions: { included: 500, used: 0, overage: 0, metered: true },
  seats: { included_per_workspace: 5, total: 1, extra: 0, reported: 0,
           by_workspace: [{ category: "finance", seats: 1 }] },
  reporting: { configured: true, period_start_set: false },
});
assert(idle.includes("Reporting idle"), "an unknown window must read as idle");
assert(idle.includes("0 of 500"));
assert(idle.includes("finance"));
assert(!idle.includes("past the pool"));

// A window that is set: the on-state the panel exists to show.
const live = renderUsagePanel({
  plan: "team", billing_status: "active",
  period_start: "2026-10-01T00:00:00+00:00",
  period_end: "2026-11-01T00:00:00+00:00",
  questions: { included: 500, used: 503, overage: 3, metered: true },
  seats: { included_per_workspace: 5, total: 7, extra: 2, reported: 1,
           by_workspace: [{ category: "finance", seats: 3 },
                          { category: "hr", seats: 4 }] },
  reporting: { configured: true, period_start_set: true },
});
assert(live.includes("Reporting to Stripe: on"));
assert(live.includes("503 of 500"));
assert(live.includes("3 past the pool"));
assert(live.includes("2 (1 already reported)"));
assert(live.includes("hr"));

// The free sample organisation: no pool, no meter, and no claim otherwise.
const free = renderUsagePanel({
  plan: null, billing_status: null,
  period_start: null, period_end: null,
  questions: { included: null, used: 4, overage: 0, metered: false },
  seats: { included_per_workspace: null, total: 1, extra: 0, reported: 0,
           by_workspace: [{ category: "finance", seats: 1 }] },
  reporting: { configured: false, period_start_set: false },
});
assert(free.includes("Free tier"), "an unmetered pool must not be called a plan");
assert(free.includes("Not connected to Stripe"));
assert(!free.includes("of "));

// A plan bought but not yet paid: counted, never reported.
const unpaid = renderUsagePanel({
  plan: "team", billing_status: null,
  period_start: null, period_end: null,
  questions: { included: 500, used: 12, overage: 0, metered: false },
  seats: { included_per_workspace: 5, total: 1, extra: 0, reported: 0,
           by_workspace: [] },
  reporting: { configured: false, period_start_set: false },
});
assert(unpaid.includes("12 of 500"));
assert(unpaid.includes("No Stripe subscription yet"));

// The gate: owners see the panel, everyone else does not.
me = { organisation: { id: "acme", role: "owner", plan: "team" } };
assert.strictEqual(usageVisible(), true);
me.organisation.role = "member";
assert.strictEqual(usageVisible(), false);
me.organisation = { id: "_sample", system_owned: true, role: "owner" };
assert.strictEqual(usageVisible(), false);
me = null;
assert.strictEqual(usageVisible(), false);

console.log("ui usage panel assertions passed");
// Exit deliberately: the page's init() is suspended on an unresolved fetch.
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
    path = tmp_path / "usage_harness.js"
    path.write_text(HARNESS + _ui_script() + harness_body, encoding="utf-8")
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_usage_panel_renders_each_state(tmp_path):
    out = _run(ASSERTIONS, tmp_path)
    assert "ui usage panel assertions passed" in out
