"""Regression tests for the TTEC "Quick Questions" restricted-state screener (Q2/Q3).

BUG (2026-09-20): TTEC's Remote-USA *insurance* reqs (jobs 509/3510) auto-rejected EVERY application
on "minimum requirements". Root cause: prescreen Q2 — "Are you planning to work from Alaska, ...,
the STATE of Washington, or Washington D.C.?" — literally contains the word "state", so the
`/state|province/` residence branch in `taleo._BASICS_JS` SHADOWED the intended restricted-state
screener branch → `firstValid` picked the first option ("Yes") → a synthetic Ohio persona FALSELY
claimed to work from a restricted state → knockout. Fix: test the restricted-state screener BEFORE
the /country/ and /state|province/ branches. These tests run the real `_BASICS_JS` (extracted from
source) under node so a re-ordering regression fails loudly.
"""
import ast
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_TALEO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "applier", "strategies", "taleo.py")


def _extract_basics_js() -> str:
    """The EXACT runtime `_BASICS_JS` string (Python has already decoded the source escapes)."""
    with open(_TALEO_SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_BASICS_JS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_BASICS_JS not found in taleo.py")


# ---- structural guard: the restricted-state screener must win over /state|province/ + /country/ ----

def test_restricted_state_branch_precedes_state_branch_in_source():
    js = _extract_basics_js()
    # the select-loop restricted-state branch (label var is `lab`, not the radio-loop `glab`)
    restrict = js.find("RESTRICT_Q.test(lab) && RESTRICT_PLACES.test(lab)")
    state = js.find("/state|province/.test(lab)")
    country = js.find("/country/.test(lab)")
    license_ = js.find("licen[sc]e to sell")
    assert restrict != -1, "select-loop restricted-state branch missing"
    assert license_ != -1, "select-loop insurance-license branch missing"
    assert state != -1 and country != -1
    # both the restricted-state screener AND the insurance-license screener carry the word "state"
    # ("the state of Washington" / "the state you reside") and MUST win over /state|province/.
    assert restrict < country < state, (
        "restricted-state screener must be tested BEFORE /country/ and /state|province/ "
        f"(restrict={restrict} country={country} state={state})")
    assert license_ < state, (
        "insurance-license screener must be tested BEFORE /state|province/ "
        f"(license={license_} state={state})")


# ---- functional: run the real _BASICS_JS under node against fixture selects -----------------------

_HARNESS = r"""
const BASICS = %(js)s;
const SCEN = %(scen)s;

function run(scenario) {
  const LABELS = {};
  const SELECTS = scenario.selects.map((s, i) => {
    const id = "sel" + i;
    LABELS[id] = s.label;
    const options = s.opts.map(o => ({ text: o[0], value: o[1] }));
    return {
      id, multiple: false, options, selectedIndex: 0, value: "",
      getAttribute() { return null; },
      closest() { return null; },
      get previousElementSibling() { return null; },
      get parentElement() { return null; },
      dispatchEvent() { return true; },
    };
  });
  global.window = { CSS: null };
  global.CSS = null;
  global.Event = function () {};
  global.document = {
    querySelectorAll(sel) { return sel === "select" ? SELECTS : []; },
    querySelector(sel) {
      const m = sel.match(/for="([^"]+)"/);
      if (m && LABELS[m[1]] !== undefined) return { innerText: LABELS[m[1]] };
      return null;
    },
    getElementById() { return null; },
  };
  BASICS([scenario.zip, scenario.city, scenario.state, scenario.edu]);
  return SELECTS.map(s => s.value);
}

const out = {};
for (const name of Object.keys(SCEN)) out[name] = run(SCEN[name]);
console.log(JSON.stringify(out));
"""

_Q2_REAL = ("2. Are you planning to work from Alaska, California, Colorado, Hawaii, Illinois, "
            "Massachusetts, Minnesota, Montana, New Jersey, New York, the state of Washington, "
            "or Washington D.C.? . Required")
_Q3_REAL = ("3. Are you planning to work from one of the following territories: American Samoa, "
            "Guam, Northern Mariana Islands, Puerto Rico, or U.S. Virgin Islands? . Required")
# option orders EXACTLY as observed live on job 509 (04CXS): Q2 offers Yes before No, Q3 No before Yes
_Q2_OPTS = [["No Selection", ""], ["Yes", "Yes"], ["No", "No"]]
_Q3_OPTS = [["No Selection", ""], ["No", "No"], ["Yes", "Yes"]]
# Basics-page insurance-license screener (job 509/3510): a synthetic persona holds NO license -> "No"
_LICENSE_LABEL = ("Do you currently hold a valid license to sell health insurance in the state you "
                  "reside?")
_LICENSE_OPTS = [["Not Specified", ""], ["Yes", "Yes"], ["No", "No"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_basics_js_answers_restricted_state_truthfully():
    js = _extract_basics_js()
    scenarios = {
        # Ohio persona (default for Remote-USA reqs): NOT in the restricted set -> both "No";
        # and it holds no insurance license -> "No" (never the fabricated "Yes")
        "ohio": {"zip": "43215", "city": "Columbus", "state": "Ohio", "edu": "",
                 "selects": [{"label": _Q2_REAL, "opts": _Q2_OPTS},
                             {"label": _Q3_REAL, "opts": _Q3_OPTS},
                             {"label": _LICENSE_LABEL, "opts": _LICENSE_OPTS}]},
        # A persona placed IN a listed state (a state-specific req) -> truthful "Yes" on Q2
        "washington": {"zip": "98101", "city": "Seattle", "state": "Washington", "edu": "",
                       "selects": [{"label": ("are you planning to work from alaska or the state "
                                              "of washington?"), "opts": _Q2_OPTS}]},
    }
    harness = _HARNESS % {"js": js, "scen": json.dumps(scenarios)}
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # THE FIX: Q2 (contains "the state of Washington") must be "No" for an out-of-list Ohio persona,
    # not "Yes" (the pre-fix firstValid pick that knocked the application out).
    assert out["ohio"] == ["No", "No", "No"], (
        f"Ohio persona: Q2/Q3 restricted-state + insurance-license must all be No, got {out['ohio']}")
    # a persona whose own state IS listed answers Yes truthfully (e.g. a state-specific req)
    assert out["washington"] == ["Yes"], f"Washington persona should answer Yes, got {out['washington']}"
