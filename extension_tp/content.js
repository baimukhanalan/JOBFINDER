// TP Apply Assist — fills the Teleperformance iCIMS application in-page (this content script runs in
// ALL frames, so inside icims_content_iframe it fills the real form). Ported from the server's
// applier/strategies/icims.py (_tp_fill / _screener_answer / _decline_demographics). It NEVER clicks
// Next/Submit and NEVER touches a captcha — the human does those. Fills on load, on DOM changes
// (debounced), and on the Alt+Shift+F hotkey. Idempotent: only fills empty/placeholder fields.
(function () {
  "use strict";
  if (window.__tpAssistLoaded) return;
  window.__tpAssistLoaded = true;
  const P = (typeof TP_PERSONA !== "undefined") ? TP_PERSONA : {};

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const low = (s) => norm(s).toLowerCase();

  function byId(id) { try { return document.getElementById(id); } catch (e) { return null; } }

  function labelText(el) {
    let t = "";
    const id = el.id;
    if (id) {
      const l = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
      if (l) t = l.innerText;
    }
    if (!t) { const w = el.closest("label"); if (w) t = w.innerText; }
    // iCIMS commonly gives the input NO <label for> — the label is a <span id> referenced by
    // aria-labelledby (or aria-describedby). Resolve those id lists to their text.
    if (!t) {
      for (const attr of ["aria-labelledby", "aria-describedby"]) {
        const ref = el.getAttribute(attr);
        if (!ref) continue;
        const parts = ref.split(/\s+/).map((i) => { const e = byId(i); return e ? e.innerText : ""; }).filter(Boolean);
        if (parts.length) { t = parts.join(" "); break; }
      }
    }
    if (!t) t = el.getAttribute("aria-label") || "";
    if (!t) {
      // climb to a small container and read its text (iCIMS wraps the label in a sibling)
      let box = el.closest("div,li,fieldset,tr,td,section");
      if (box && low(box.innerText).length < 180) t = box.innerText;
    }
    if (!t) t = el.getAttribute("placeholder") || el.getAttribute("title") || (el.name || "");
    return norm(t);
  }

  function setVal(el, val) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value") && Object.getOwnPropertyDescriptor(proto, "value").set;
    if (setter) setter.call(el, val); else el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillText(labelRe, val, overwrite) {
    if (!val) return false;
    const rx = new RegExp(labelRe, "i");
    let any = false;
    // fill EVERY matching field (TP has both a "Login (email)" and a separate "Email" input)
    for (const el of document.querySelectorAll('input[type=text],input[type=email],input[type=tel],input:not([type]),textarea')) {
      const t = (el.type || "").toLowerCase();
      if (["hidden", "submit", "button", "file", "password", "checkbox", "radio"].includes(t)) continue;
      if (!rx.test(labelText(el))) continue;
      if (!overwrite && norm(el.value)) { any = true; continue; }   // leave a value the parser set
      setVal(el, val);
      any = true;
    }
    return any;
  }

  function optMatch(cand, opt) {
    cand = low(cand); opt = low(opt);
    if (!cand || !opt) return false;
    if (cand === opt) return true;
    if (cand.length <= 4) return opt.startsWith(cand + " ") || opt.startsWith(cand + ",") || (" " + opt + " ").includes(" " + cand + " ");
    return opt.includes(cand) || cand.includes(opt);
  }
  const isPlaceholder = (t) => !t || /make a selection|select an option|select a |please select|choose|specify a|select a source/i.test(t);

  function setSelect(el, opt) {
    el.value = opt.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Iterate the <select> ELEMENTS and read each one's label via labelText (which resolves iCIMS's
  // aria-labelledby <span id> labels) — NOT document.querySelectorAll('label'), because iCIMS
  // selects usually have NO <label for>, so a label-first scan misses them entirely.
  function pickSelect(labelRe, valueRe, force) {
    const rx = new RegExp(labelRe, "i");
    for (const el of document.querySelectorAll("select")) {
      if (el.multiple) continue;
      if (!rx.test(labelText(el))) continue;
      const cur = el.options[el.selectedIndex];
      // already answered → try the NEXT select with this label (e.g. phone Type is set; the address
      // Type shares the label "Type" and still needs a value), don't stop here.
      if (!force && el.value && !isPlaceholder(cur && cur.text)) continue;
      const opt = [...el.options].find((o) => o.value && (optMatch(valueRe, o.text) || optMatch(valueRe, o.value)));
      if (!opt) continue;
      setSelect(el, opt);
      return true;
    }
    return false;
  }

  function selectFirstReal(labelRe) {
    const rx = new RegExp(labelRe, "i");
    for (const el of document.querySelectorAll("select")) {
      if (el.multiple) continue;
      if (!rx.test(labelText(el))) continue;
      const cur = el.options[el.selectedIndex];
      if (el.value && !isPlaceholder(cur && cur.text)) return true;
      const opt = [...el.options].find((o) => o.value && !isPlaceholder(o.text));
      if (!opt) continue;
      setSelect(el, opt);
      return true;
    }
    return false;
  }

  function tickRequiredConsent() {
    for (const cb of document.querySelectorAll('input[type=checkbox]')) {
      if (cb.checked) continue;
      const req = cb.required || cb.getAttribute("aria-required") === "true";
      const ctx = low((cb.closest("div,li,fieldset,form,label") || {}).innerText || "");
      if (/newsletter|marketing|promotional|subscribe|contact you about|talent community|opportunities/.test(ctx)) continue;
      if (req || /i have read|i accept|i agree|i understand|i consent|privacy notice/.test(ctx)) {
        cb.checked = true;
        cb.dispatchEvent(new Event("click", { bubbles: true }));
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  }

  function tickAck() {
    for (const el of document.querySelectorAll('input[type=checkbox],input[type=radio]')) {
      if (el.checked) continue;
      const t = low(labelText(el));
      if (/acknowledge|i certify|i attest|i agree|i understand|i confirm/.test(t)) {
        el.checked = true;
        el.dispatchEvent(new Event("click", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  }

  const DECLINE_RE = /not to disclose|choose not|prefer not|decline|do not wish|do not want|don't wish|wish not/i;
  const DEMO_RE = /gender|rac(e|ial)|ethnic|hispanic|latin[ox]?(?!\s*americ)|disabilit|veteran|armed forces|served in the (military|armed forces)|self-?identif|orientation|pronoun/i;
  const DISCLOSE_Q = /choose to disclose|wish to disclose|like to disclose|do you wish/i;

  function declineDemographics() {
    // radio groups
    const groups = {};
    for (const r of document.querySelectorAll('input[type=radio]')) { if (r.name) (groups[r.name] = groups[r.name] || []).push(r); }
    for (const nm in groups) {
      const rs = groups[nm];
      let box = rs[0].parentElement;
      while (box && !rs.every((r) => box.contains(r))) box = box.parentElement;
      const qt = low(box ? box.innerText : "");
      const lab = (r) => low(labelText(r));
      let pick = null;
      if (DISCLOSE_Q.test(qt)) { pick = rs.find((r) => /^\s*no\b/i.test(lab(r))); }
      else if ((DEMO_RE.test(qt) || rs.some((r) => DEMO_RE.test(lab(r)))) && !rs.some((r) => r.checked)) {
        pick = rs.find((r) => DECLINE_RE.test(lab(r)));
      }
      if (pick && !pick.checked) { pick.checked = true; pick.dispatchEvent(new Event("click", { bubbles: true })); pick.dispatchEvent(new Event("change", { bubbles: true })); }
    }
    // demographic selects -> decline / No
    for (const el of document.querySelectorAll("select:not([multiple])")) {
      const cur = el.options[el.selectedIndex];
      if (el.value && cur && !isPlaceholder(cur.text)) continue;
      const lt = low(labelText(el));
      if (!DEMO_RE.test(lt) && ![...el.options].some((o) => DEMO_RE.test(o.text))) continue;
      const o = [...el.options].find((o) => o.value && DECLINE_RE.test(o.text))
        || [...el.options].find((o) => o.value && /^\s*no\b/i.test(o.text))
        || [...el.options].find((o) => o.value && /i do not|not a /i.test(o.text));
      if (o) { el.value = o.value; el.dispatchEvent(new Event("change", { bubbles: true })); }
    }
    // decline checkboxes + disability-form name signature
    for (const c of document.querySelectorAll('input[type=checkbox]')) {
      if (c.checked) continue;
      const t = labelText(c);
      if (/not a protected veteran|do not wish to answer|don't wish to answer|do not wish to self/i.test(t)) {
        c.checked = true; c.dispatchEvent(new Event("click", { bubbles: true })); c.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    if (P.full_name) fillText("your name|employee name|name of employee|signature|please (enter|type) your name", P.full_name, true);
  }

  // deterministic, truthful screener answers (lowercased label) -> ordered candidates, or null
  function screenerAnswer(t) {
    t = low(t);
    if (/acknowledge|i certify|i attest/.test(t)) return null;
    if (/spanish/.test(t)) return P.bilingual ? ["Fluent", "Native", "Advanced", "Bilingual"] : ["None", "No proficiency", "Basic", "Beginner", "Limited"];
    if (/english/.test(t)) return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"];
    if (/highest level of education|education (you have )?achieved|level of education/.test(t)) return [P.education_level || "Bachelor", "Bachelor", "High School", "Associate", "GED"];
    if (/experience.*(customer service|call center|contact center|retail|customer)/.test(t)) return ["5+ years", "5 or more", "More than 5", "6+ years", "5 years", "3-5 years", "3+ years", "1-3 years", "Yes"];
    if (/(supervisor|leadership|management|managerial|team lead).*(experience)|experience.*(supervisor|leadership|manage|team lead)|how (much|many years?).*experience|years of experience/.test(t)) return ["4-5 years", "5+ years", "6+ years", "3-5 years", "5 years", "1-3 years", "Yes"];
    if (/reside|within \d+ ?mile|live within|currently reside|relocat/.test(t)) return ["Yes"];
    if (/(commitment|obligation|conflict).{0,40}(interfere|attendance|schedule|availab|work)|foresee (any )?(commitment|conflict|obligation)|interfere with (your )?(attendance|schedule|work|availab)|impact.*attendance/.test(t)) return ["No"];
    if (/private|secure|quiet|workspace|distraction|free from/.test(t)) return ["Yes"];
    if (/ethernet|hardwired|hard-wired|wired/.test(t)) return ["Yes, my home internet is hardwired", "Yes"];
    if (/download speed|\bmbps\b|high.?speed|cable or fiber|internet|connection/.test(t)) return ["Yes"];
    if (/documentation|diploma or ged|provide.*if needed|verify.*education|able to provide/.test(t)) return ["Yes"];
    if (/18 (years|and older)|older|authorized|eligible to work/.test(t)) return ["Yes"];
    if (/seasonal|interested in (the |this )?(season|temporary|position|role|opportunity)/.test(t)) return ["Yes"];
    if (/\bcitizen(ship)?\b|u\.?s\.? citizen/.test(t)) return ["Yes"];
    if (/require sponsor|need sponsor|visa sponsor/.test(t)) return ["No"];
    if (/able to meet this requirement|do you meet this requirement|meet (this|the) requirement|able to work|\bshift\b|overtime|willing to (work|attend|commit|travel|obtain)|onsite|on-site|in.?office|in person|first week|training|obtain a[n]? .*(clearance|public trust)|public trust|background (check|investigation)/.test(t)) return ["Yes"];
    return null;
  }

  function answerScreeners() {
    // native <select> screeners — iterate the SELECTs and read each label via labelText (iCIMS
    // aria-labelledby), not a <label>-first scan.
    for (const el of document.querySelectorAll("select")) {
      if (el.multiple) continue;
      const cur = el.options[el.selectedIndex];
      if (el.value && !isPlaceholder(cur && cur.text)) continue;
      const lt = labelText(el); if (norm(lt).length < 6) continue;
      let cands = screenerAnswer(lt);
      if (/proficiency|language/i.test(lt) && /english|spanish/i.test(lt) && !cands) {
        const high = /english/i.test(lt) ? true : !!P.bilingual;
        cands = high ? ["Native", "Fluent", "Advanced", "Professional"] : ["None", "No proficiency", "Basic", "Limited"];
      }
      if (!cands) continue;
      for (const c of cands) { const o = [...el.options].find((o) => o.value && optMatch(c, o.text)); if (o) { setSelect(el, o); break; } }
    }
    // radio-group screeners
    const groups = {};
    for (const r of document.querySelectorAll('input[type=radio]')) { if (r.name) (groups[r.name] = groups[r.name] || []).push(r); }
    for (const nm in groups) {
      const rs = groups[nm]; if (rs.some((r) => r.checked)) continue;
      let box = rs[0].parentElement; while (box && !rs.every((r) => box.contains(r))) box = box.parentElement;
      let qt = box ? box.innerText : ""; for (const r of rs) { const t = labelText(r); if (t) qt = qt.split(t).join(" "); }
      if (DEMO_RE.test(low(qt))) continue;   // demographics handled by declineDemographics
      const cands = screenerAnswer(qt); if (!cands) continue;
      let picked = null;
      for (const c of cands) { picked = rs.find((r) => optMatch(c, labelText(r))); if (picked) break; }
      if (picked) { picked.checked = true; picked.dispatchEvent(new Event("click", { bubbles: true })); picked.dispatchEvent(new Event("change", { bubbles: true })); }
    }
  }

  function fillHowHeard() {
    for (const v of (P.how_heard || ["Job Board", "Google Search", "Other/None"])) {
      if (!pickSelect("how did you hear", v, true)) continue;
      // dependent "Please specify further" — pick a real sub-option or type a value
      let ok = selectFirstReal("specify further") || fillText("specify further", "Online", true);
      if (ok) return;
    }
  }

  function fillPasswords() {
    const pws = [...document.querySelectorAll('input[type=password]')];
    for (const el of pws) { if (!norm(el.value)) setVal(el, P.password || "Jf7xQ2wnpkV9!"); }
  }

  let lastCount = 0;
  function fillTP() {
    let n = 0;
    const before = document.querySelectorAll('input,select,textarea').length;
    try {
      // identity (empty-only)
      n += fillText("login.*email|^email", P.email) ? 1 : 0;
      n += fillText("first name|given name", P.first_name) ? 1 : 0;
      n += fillText("last name|surname|family name", P.last_name) ? 1 : 0;
      if (P.middle_name) fillText("middle name", P.middle_name);
      fillPasswords();
      // phone: Type = Mobile (its own select) + Number as digits
      pickSelect("^\\s*type\\b|phone type", P.phone_type || "Mobile");
      fillText("^\\s*number|include country code|^phone|mobile number", P.phone_digits, true);
      // how did you hear + its dependent specify-further
      fillHowHeard();
      // residence: Country FIRST (unlocks State), then State, address Type, then the address fields
      pickSelect("country", P.country || "United States");
      pickSelect("state|province", P.state_full || "Ohio");
      pickSelect("^\\s*type\\b", "Physical");                     // address Type (phone Type already set)
      fillText("^\\s*address\\b(?!\\s*(2|line))|^\\s*street", P.address, false);
      fillText("^\\s*city\\b|city/town|^town\\b", P.city, true);
      fillText("zip|postal", P.zip, true);
      // consents / screeners / EEO
      tickAck();
      tickRequiredConsent();
      answerScreeners();
      declineDemographics();
    } catch (e) { /* keep going */ }
    let unfilled = [];
    try { unfilled = unfilledReport(); } catch (e) {}
    badge(unfilled);
    if (unfilled.length) {
      try { console.log("[TP Assist] НЕ смог заполнить (required):", unfilled); } catch (e) {}
    }
    return n;
  }

  // required, visible, still-empty fields (the ones that block Submit) — for the badge + console
  function unfilledReport() {
    const out = [];
    for (const el of document.querySelectorAll("input,select,textarea")) {
      const t = (el.type || el.tagName).toLowerCase();
      if (["hidden", "submit", "button", "file", "reset"].includes(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 && r.height < 2) continue;
      const req = el.required || el.getAttribute("aria-required") === "true";
      if (!req) continue;
      let empty;
      if (t === "checkbox" || t === "radio") {
        const nm = el.name;
        empty = nm ? ![...document.querySelectorAll('[name="' + (window.CSS && CSS.escape ? CSS.escape(nm) : nm) + '"]')].some((x) => x.checked) : !el.checked;
      } else if (el.tagName === "SELECT") {
        const cur = el.options[el.selectedIndex];
        empty = !el.value || isPlaceholder(cur && cur.text);
      } else empty = !norm(el.value);
      if (!empty) continue;
      const lab = norm(labelText(el)).replace(/\s*\*\s*$/, "").slice(0, 55) || (el.name || "field");
      if (lab && !out.includes(lab)) out.push(lab);
    }
    return out;
  }

  // floating status badge — drawn in the frame that actually holds the form (>=2 visible fields),
  // listing the required fields it couldn't fill (so they're visible + reportable).
  function badge(unfilled) {
    const nFields = [...document.querySelectorAll("input,select,textarea")].filter((e) => {
      const r = e.getBoundingClientRect();
      return r.width > 2 && !["hidden", "submit", "button", "reset"].includes((e.type || "").toLowerCase());
    }).length;
    if (nFields < 2) return;
    let b = document.getElementById("__tpBadge");
    if (!b) {
      b = document.createElement("div"); b.id = "__tpBadge";
      b.style.cssText = "position:fixed;z-index:2147483647;right:10px;bottom:10px;max-width:460px;background:#0c47c2;" +
        "color:#fff;font:12px -apple-system,Segoe UI,Roboto,sans-serif;padding:8px 12px;border-radius:9px;" +
        "box-shadow:0 2px 10px rgba(0,0,0,.35);line-height:1.35";
      document.documentElement.appendChild(b);
    }
    if (unfilled && unfilled.length) {
      b.style.background = "#b3541e";
      b.textContent = "TP Assist: не заполнил " + unfilled.length + " — " +
        unfilled.slice(0, 8).join(" · ") + (unfilled.length > 8 ? " …" : "") + "  (заполни вручную + реши капчу)";
    } else {
      b.style.background = "#1a7f37";
      b.textContent = "TP Assist: всё заполнено ✓ — реши капчу и жми Submit";
    }
  }

  // triggers: on load, on DOM changes (debounced), and on the hotkey
  let deb = null;
  const schedule = () => { clearTimeout(deb); deb = setTimeout(fillTP, 700); };
  const mo = new MutationObserver(schedule);
  try { mo.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) {}
  setTimeout(fillTP, 1200);
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((m) => { if (m && m.type === "fill") fillTP(); });
  }
  window.__tpFill = fillTP;   // test hook (page.evaluate('__tpFill()'))
})();
