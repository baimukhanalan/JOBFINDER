"""Regression tests for the TTEC "Quick Questions" restricted-state screener (Q2/Q3) and the
insurance-license screener + its synthetic license-number follow-up.

BUG (2026-09-20): TTEC's Remote-USA *insurance* reqs (jobs 509/3510) auto-rejected EVERY application
on "minimum requirements". Root cause: prescreen Q2 — "Are you planning to work from Alaska, ...,
the STATE of Washington, or Washington D.C.?" — literally contains the word "state", so the
`/state|province/` residence branch in `taleo._BASICS_JS` SHADOWED the intended restricted-state
screener branch → `firstValid` picked the first option ("Yes") → a synthetic Ohio persona FALSELY
claimed to work from a restricted state → knockout. Fix: test the restricted-state screener BEFORE
the /country/ and /state|province/ branches.

POLICY (2026-09-20): the sibling insurance-LICENSE screener ("Do you currently hold a valid license
to sell health insurance in the state you reside?") is answered SYNTHETICALLY "Yes" (a synthetic
persona already transmits a synthetic SSN/DOB/phone) AND its conditionally-required "If yes, please
provide your license number." text follow-up is filled with a deterministic fabricated number — so
these insurance reqs are ATTEMPTED and COMPLETE, instead of being knocked out by a truthful "No" or
stalled by a blank license number. These tests run the real `_BASICS_JS` (extracted from source)
under node so a re-ordering / re-answer regression fails loudly.
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


# ---- structural guard: the restricted-state + license screeners must win over /state|province/ ----

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


def test_license_screener_answers_yes_and_fills_number_in_source():
    """The insurance-license SELECT branch answers SYNTHETIC "Yes" (not the old truthful "No"), and a
    text-input branch fills the "provide your license number" follow-up with the fabricated `lic`."""
    js = _extract_basics_js()
    # isolate the license SELECT branch (from its regex up to the next `else if`)
    lic_start = js.find("licen[sc]e to sell")
    nxt = js.find("else if", lic_start)
    lic_branch = js[lic_start:nxt]
    assert "yes" in lic_branch.lower() and "set(sel,/^\\s*yes" in lic_branch, (
        f"license SELECT must be answered synthetic Yes, branch was: {lic_branch!r}")
    assert "set(sel,/^\\s*no" not in lic_branch, (
        "license SELECT must NOT be answered No any more (synthetic policy)")
    # the license-number text follow-up must be filled with the fabricated value
    assert "provide your licen[sc]e number" in js, "license-number text-fill branch missing"
    assert "([zc,city,st,edu,lic])" in js, "_BASICS_JS must accept the license-number arg `lic`"


# ---- functional: run the real _BASICS_JS under node against fixture selects + text inputs ----------

_HARNESS = r"""
const BASICS = %(js)s;
const SCEN = %(scen)s;

function run(scenario) {
  const LABELS = {};
  const SELECTS = (scenario.selects || []).map((s, i) => {
    const id = "sel" + i;
    LABELS[id] = s.label;
    const options = s.opts.map(o => ({ text: o[0], value: o[1] }));
    return {
      id, multiple: false, options, selectedIndex: 0, value: "", type: "select-one",
      getAttribute() { return null; },
      closest() { return null; },
      get previousElementSibling() { return null; },
      get parentElement() { return null; },
      dispatchEvent() { return true; },
    };
  });
  const TEXTS = (scenario.texts || []).map((t, i) => {
    const id = "txt" + i;
    LABELS[id] = t.label;
    return {
      id, type: "text", value: "",
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
    querySelectorAll(sel) {
      if (sel === "select") return SELECTS;
      if (sel.indexOf("input[type=text]") === 0) return TEXTS;
      return [];
    },
    querySelector(sel) {
      const m = sel.match(/for="([^"]+)"/);
      if (m && LABELS[m[1]] !== undefined) return { innerText: LABELS[m[1]] };
      return null;
    },
    getElementById() { return null; },
  };
  BASICS([scenario.zip, scenario.city, scenario.state, scenario.edu, scenario.lic || ""]);
  return { selects: SELECTS.map(s => s.value), texts: TEXTS.map(t => t.value) };
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
# Basics-page insurance-license screener (job 509/3510): answered SYNTHETIC "Yes" + number filled
_LICENSE_LABEL = ("Do you currently hold a valid license to sell health insurance in the state you "
                  "reside?")
_LICENSE_OPTS = [["Not Specified", ""], ["Yes", "Yes"], ["No", "No"]]
# the conditionally-required license-# follow-up text input (statically present on the Basics page)
_LICENSE_NUM_LABEL = "If yes, please provide your license number."
_ZIP_LABEL = "Zip/Postal Code"
_SYNTH_LIC = "26482271"


# a "Which state will you work from?" REQUIRED select (statically a US-states dropdown)
_WORKSTATE_LABEL = "Which state will you work from?"
_WORKSTATE_OPTS = [["Select One", ""], ["Alaska", "AK"], ["California", "CA"], ["Illinois", "IL"],
                   ["New York", "NY"], ["Ohio", "OH"], ["Texas", "TX"], ["Washington", "WA"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_basics_js_answers_restricted_state_and_synthetic_license():
    js = _extract_basics_js()
    scenarios = {
        # Ohio persona (default for Remote-USA reqs): the restricted-state screeners Q2/Q3 -> "No" (the
        # persona works from a permitted state), the insurance-license screener is answered SYNTHETIC
        # "Yes" and the required license-number follow-up is filled with the fabricated number.
        "ohio": {"zip": "43215", "city": "Columbus", "state": "Ohio", "edu": "", "lic": _SYNTH_LIC,
                 "selects": [{"label": _Q2_REAL, "opts": _Q2_OPTS},
                             {"label": _Q3_REAL, "opts": _Q3_OPTS},
                             {"label": _LICENSE_LABEL, "opts": _LICENSE_OPTS}],
                 "texts": [{"label": _LICENSE_NUM_LABEL}, {"label": _ZIP_LABEL}]},
        # OWNER POLICY: even a persona whose HOME state is a restricted one answers the restricted-state
        # screener "No" (never claim a restricted WORK state -> never auto-rejected).
        "restricted_home": {"zip": "98101", "city": "Seattle", "state": "Washington", "edu": "",
                            "lic": "",
                            "selects": [{"label": _Q2_REAL, "opts": _Q2_OPTS}]},
        # a REQUIRED "which state will you work from?" pick -> an ALLOWED state: the Ohio persona keeps
        # Ohio; a California-placed persona falls back to Ohio (NEVER a restricted state).
        "workstate_ohio": {"zip": "43215", "city": "Columbus", "state": "Ohio", "edu": "", "lic": "",
                           "selects": [{"label": _WORKSTATE_LABEL, "opts": _WORKSTATE_OPTS}]},
        "workstate_restricted": {"zip": "90012", "city": "Los Angeles", "state": "California",
                                 "edu": "", "lic": "",
                                 "selects": [{"label": _WORKSTATE_LABEL, "opts": _WORKSTATE_OPTS}]},
    }
    harness = _HARNESS % {"js": js, "scen": json.dumps(scenarios)}
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # restricted-state screeners Q2/Q3 stay "No" (the pre-fix firstValid "Yes" knocked the application
    # out); the insurance-license screener is answered synthetic "Yes".
    assert out["ohio"]["selects"] == ["No", "No", "Yes"], (
        f"Ohio: Q2/Q3 restricted-state must be No/No, insurance-license must be synthetic Yes, "
        f"got {out['ohio']['selects']}")
    # the conditionally-required license-number follow-up must carry the fabricated value (a blank one
    # is the old "we need more information" stall); zip filled too.
    assert out["ohio"]["texts"] == [_SYNTH_LIC, "43215"], (
        f"Ohio: license-number must be the synthetic {_SYNTH_LIC}, zip filled, got {out['ohio']['texts']}")
    # a restricted-home persona STILL answers the restricted-state screener "No" (works from a permitted
    # state), so the application is never auto-rejected on the restricted-state knockout.
    assert out["restricted_home"]["selects"] == ["No"], (
        f"restricted-home persona must answer No on the restricted-state screener, got "
        f"{out['restricted_home']}")
    # a required work-state pick picks an ALLOWED (non-restricted) state
    assert out["workstate_ohio"]["selects"] == ["OH"], (
        f"work-state pick: Ohio persona should pick Ohio, got {out['workstate_ohio']}")
    assert out["workstate_restricted"]["selects"] == ["OH"], (
        f"work-state pick: a California-placed persona must fall back to an ALLOWED state (Ohio), never "
        f"a restricted one, got {out['workstate_restricted']}")
