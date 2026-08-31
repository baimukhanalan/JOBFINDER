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
      // table layout: the label often sits in the PREVIOUS cell of the same row (<td>State</td><td><select></td>)
      const cell = el.closest("td,th");
      const prev = cell && cell.previousElementSibling;
      if (prev && low(prev.innerText).length < 80) t = prev.innerText;
    }
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
    // no native <select> matched → try a custom dropdown widget (best-effort)
    for (const v of (P.how_heard || ["Job Board", "Google Search", "Other/None"])) {
      if (openAndPickCustom("how did you hear", v)) {
        openAndPickCustom("specify further", "Online") || fillText("specify further", "Online", true);
        return;
      }
    }
  }

  function fillPasswords() {
    const pws = [...document.querySelectorAll('input[type=password]')];
    for (const el of pws) { if (!norm(el.value)) setVal(el, P.password || "Jf7xQ2wnpkV9!"); }
  }

  // Auto-click "Apply for this job online" (a plain navigation, NOT captcha-gated) so the form opens
  // on its own. Never clicks Next/Submit — those trigger the hCaptcha and must be your real click.
  let _applyClicked = false;
  function clickApplyIfNeeded() {
    if (_applyClicked) return;
    if (document.querySelector('input[type=email],input[type=password]')) return;  // already on the form
    for (const el of document.querySelectorAll('a,button,input[type=submit],[role=button]')) {
      const txt = norm(el.innerText || el.value || el.getAttribute("aria-label") || "");
      if (/^apply for this job online$|^apply online$|^apply now$|^apply$/i.test(txt)) {
        _applyClicked = true;
        try { el.click(); } catch (e) {}
        return;
      }
    }
  }

  // DEBUG: dump the exact structure of every required field still empty, so the real iCIMS markup
  // can be seen (why a select/label isn't matching) and fixed precisely.
  function debugUnfilled() {
    const out = [];
    for (const el of document.querySelectorAll("input,select,textarea")) {
      const t = (el.type || el.tagName).toLowerCase();
      if (["hidden", "submit", "button", "file", "reset"].includes(t)) continue;
      const r = el.getBoundingClientRect(); if (r.width < 2 && r.height < 2) continue;
      const req = el.required || el.getAttribute("aria-required") === "true"; if (!req) continue;
      let empty;
      if (t === "checkbox" || t === "radio") empty = !el.checked;
      else if (el.tagName === "SELECT") { const c = el.options[el.selectedIndex]; empty = !el.value || isPlaceholder(c && c.text); }
      else empty = !norm(el.value);
      if (!empty) continue;
      out.push({ tag: el.tagName, type: t, id: el.id || "", name: el.name || "",
        arialb: el.getAttribute("aria-labelledby") || "", label: labelText(el).slice(0, 70),
        opts: el.tagName === "SELECT" ? [...el.options].slice(0, 5).map((o) => o.text).join("|") : undefined });
    }
    return out;
  }

  // ---- custom (non-native) dropdown support + a full DOM report -----------------------------------
  function vis2(el) { try { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; } catch (e) { return false; } }
  // Is a control (native OR custom [role=combobox]/[role=listbox]) still empty / on its placeholder?
  function comboEmpty(el, t) {
    t = t || (el.type || el.tagName).toLowerCase();
    if (t === "checkbox" || t === "radio") return !el.checked;
    if (el.tagName === "SELECT") { const c = el.options[el.selectedIndex]; return !el.value || isPlaceholder(c && c.text); }
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "combobox" || role === "listbox") { const d = norm(el.value || el.innerText); return !d || isPlaceholder(d); }
    return !norm(el.value);
  }
  function elClass(el) { try { return (el.className && el.className.toString ? el.className.toString() : "").slice(0, 90); } catch (e) { return ""; } }
  function labelSource(el) {
    const id = el.id;
    if (id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]')) return "label[for]";
    if (el.closest("label")) return "wrapping-label";
    if (el.getAttribute("aria-labelledby")) return "aria-labelledby";
    if (el.getAttribute("aria-label")) return "aria-label";
    if (el.getAttribute("placeholder")) return "placeholder";
    return "container/name";
  }
  // Is this element a dropdown-like WIDGET (not a native <select>)? iCIMS "iForms" often render
  // Country/State/How-heard as a styled control + popup list instead of a <select>.
  function isDropdownWidget(el) {
    if (el.tagName === "SELECT") return false;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "combobox" || role === "listbox") return true;
    const hp = (el.getAttribute("aria-haspopup") || "").toLowerCase();
    if (hp === "listbox" || hp === "true" || hp === "menu") return true;
    const cls = elClass(el).toLowerCase();
    if (/(^|[-_ ])(select|dropdown|combo|chosen|typeahead|autocomplete|picklist)/.test(cls)) return true;
    // Bootstrap dropdowns: a .dropdown-toggle button / [data-toggle=dropdown] (what iCIMS TP uses —
    // the report showed `class="btn customizarBotao dropdown-toggle"`).
    try { if (el.matches('.dropdown-toggle,[data-toggle*="dropdown"],[data-bs-toggle*="dropdown"]')) return true; } catch (e) {}
    // a readonly text input that opens a list is the classic select-replacement
    if (el.tagName === "INPUT" && (el.readOnly || el.getAttribute("readonly") !== null) &&
        (el.getAttribute("role") === "combobox" || hp)) return true;
    return false;
  }
  // ONE click-equivalent (mousedown→mouseup→click) — do NOT also call el.click(), which would fire a
  // SECOND click and toggle a Bootstrap dropdown open→closed. mousedown covers select2-style widgets.
  function clickOpen(el) {
    for (const type of ["mousedown", "mouseup", "click"]) {
      try { el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window })); } catch (e) {}
    }
  }
  // Find a dropdown TRIGGER near a label matching labelRe, even when the trigger itself carries no
  // resolvable label (iCIMS puts the label in a sibling, the trigger is a bare <button>/<div>).
  function findFieldTrigger(labelRe) {
    const rx = new RegExp(labelRe, "i");
    for (const lab of document.querySelectorAll("label,span,td,th,legend,div,p")) {
      if (!vis2(lab)) continue;
      const own = norm(lab.innerText);
      if (own.length > 70 || !rx.test(own)) continue;   // this node IS a short label matching labelRe
      let box = lab.closest("div,li,fieldset,tr,section,td") || lab.parentElement;
      for (let up = 0; up < 3 && box; up++) {
        const trig = box.querySelector('.dropdown-toggle,[data-toggle*="dropdown"],[data-bs-toggle*="dropdown"],[role=combobox],[role=listbox],[aria-haspopup],input[readonly],select,button');
        if (trig && vis2(trig)) return trig;
        box = box.parentElement;
      }
    }
    return null;
  }
  // Best-effort: open a custom dropdown for labelRe and click the option matching valueRe by TEXT.
  // Only a FALLBACK when the native <select> path found nothing. Text-matched, so it never sets a
  // wrong value — if no option text matches, it leaves the field blank.
  function openAndPickCustom(labelRe, valueRe) {
    const rx = new RegExp(labelRe, "i");
    const triggers = [];
    for (const el of document.querySelectorAll('.dropdown-toggle,[data-toggle*="dropdown"],[data-bs-toggle*="dropdown"],[role=combobox],[role=listbox],[aria-haspopup],input[readonly],button,a[role=button]')) {
      if (vis2(el) && isDropdownWidget(el) && rx.test(labelText(el))) triggers.push(el);
    }
    const t2 = findFieldTrigger(labelRe);
    if (t2 && !triggers.includes(t2)) triggers.push(t2);
    for (const el of triggers) {
      const disp = norm(el.innerText || el.value || "");
      if (disp && !isPlaceholder(disp) && optMatch(valueRe, disp)) return true;   // already right
      clickOpen(el);
      // options render as Bootstrap .dropdown-menu items, ARIA options, select2 results, or a portal list
      const opts = [...document.querySelectorAll('.dropdown-menu li a,.dropdown-menu li,.dropdown-menu a,[role=option],ul[role=listbox] li,li[role],.select2-results__option,.dropdown-item,li,a')]
        .filter((o) => vis2(o) && norm(o.innerText || o.textContent));
      const matches = opts.filter((o) => optMatch(valueRe, norm(o.innerText || o.textContent)));
      // querySelectorAll returns DOCUMENT order, so a wrapper <li> precedes its child <a>; clicking the
      // <li> won't fire the <a>'s handler. Prefer an actual <a>/[role=option], else a leaf, and always
      // click the innermost anchor/option of a wrapper.
      let opt = matches.find((o) => o.tagName === "A" || o.getAttribute("role") === "option")
        || matches.find((o) => !o.querySelector("a,[role=option],li")) || matches[0];
      if (opt) {
        const inner = opt.querySelector("a,[role=option]");
        clickOpen(inner && vis2(inner) ? inner : opt);
        return true;
      }
      clickOpen(el);   // no match — close it back so we don't leave a menu open
    }
    return false;
  }

  // Full structural dump of THIS frame's form — native selects (with options), custom dropdown
  // widgets, and the outerHTML of every required-but-empty field. This is the ground truth that
  // pins down why a dropdown won't fill; the badge copies it to the clipboard on click.
  function ctrlLine(el, i) {
    return "[" + i + "] <" + el.tagName.toLowerCase() + (el.type ? " type=" + el.type : "") + ">" +
      " id=" + (el.id || "-") + " name=" + (el.name || "-").slice(0, 30) +
      " class=" + JSON.stringify(elClass(el)) + " role=" + (el.getAttribute("role") || "") +
      " arialb=" + (el.getAttribute("aria-labelledby") || "") +
      " req=" + !!(el.required || el.getAttribute("aria-required") === "true") +
      " label=" + JSON.stringify(labelText(el).slice(0, 45)) +
      " val=" + JSON.stringify(norm(el.value || "").slice(0, 30));
  }
  // Smallest visible element whose text contains kw AND that holds a control/trigger — its outerHTML
  // shows the exact real widget for a failing field.
  function findLabelContainer(kw) {
    kw = kw.toLowerCase();
    let best = null, bestLen = 1e9;
    for (const el of document.querySelectorAll("div,li,fieldset,tr,section,td")) {
      if (!vis2(el)) continue;
      if (!low(el.innerText).includes(kw)) continue;
      if (!el.querySelector('input,select,textarea,button,a[role],[role],.dropdown-toggle,[data-toggle],[data-bs-toggle]')) continue;
      const len = (el.outerHTML || "").length;
      if (len < bestLen) { bestLen = len; best = el; }
    }
    return best;
  }
  function buildReport() {
    const L = [];
    L.push("=== TP Assist DOM report v1.5 ===");
    L.push("url: " + location.href.slice(0, 150));
    const ctrls = [...document.querySelectorAll("input,select,textarea")];
    const visc = ctrls.filter(vis2);
    L.push("controls: " + ctrls.length + " total, " + visc.length + " visible");
    L.push("");
    L.push("--- visible controls (" + visc.length + ") ---");
    visc.slice(0, 60).forEach((el, i) => L.push(ctrlLine(el, i)));
    const sels = [...document.querySelectorAll("select")].filter(vis2);
    if (sels.length) {
      L.push(""); L.push("--- <select> options ---");
      sels.forEach((s, i) => L.push("[sel " + i + "] " + JSON.stringify(labelText(s).slice(0, 40)) + ": " +
        [...s.options].slice(0, 16).map((o) => norm(o.text)).filter(Boolean).join(" | ")));
    }
    const ws = [], seen = new Set();
    for (const el of document.querySelectorAll('.dropdown-toggle,[data-toggle],[data-bs-toggle],[role=combobox],[role=listbox],[aria-haspopup],[class*="select"],[class*="dropdown"],[class*="combo"],button')) {
      if (!vis2(el)) continue;
      const cls = elClass(el).toLowerCase();
      if (!(isDropdownWidget(el) || /dropdown|select|combo/.test(cls) || el.getAttribute("data-toggle") || el.getAttribute("data-bs-toggle"))) continue;
      const k = el.tagName + "|" + el.id + "|" + cls + "|" + labelText(el).slice(0, 30);
      if (seen.has(k)) continue; seen.add(k);
      ws.push(el); if (ws.length >= 30) break;
    }
    L.push(""); L.push("--- dropdown-ish widgets (" + ws.length + ") ---");
    ws.forEach((el, i) => L.push("[w " + i + "] <" + el.tagName.toLowerCase() + "> class=" + JSON.stringify(elClass(el)) +
      " id=" + el.id + " role=" + (el.getAttribute("role") || "") +
      " toggle=" + (el.getAttribute("data-toggle") || el.getAttribute("data-bs-toggle") || "") +
      " label=" + JSON.stringify(labelText(el).slice(0, 40)) + " text=" + JSON.stringify(norm(el.innerText || "").slice(0, 35))));
    // TARGETED: the outerHTML around each failing field — the decisive evidence
    L.push(""); L.push("--- field containers by label (outerHTML) ---");
    for (const kw of ["country", "state", "how did you hear", "please specify", "phone", "zip", "city"]) {
      const el = findLabelContainer(kw);
      L.push("[" + kw + "] " + (el ? (el.outerHTML || "").replace(/\s+/g, " ").slice(0, 700) : "(label not found)"));
    }
    return L.join("\n");
  }

  // Copy the report to the clipboard (execCommand under a real user gesture works even in iCIMS's
  // iframe, where navigator.clipboard is often policy-blocked) AND show it in a selectable overlay
  // so it can always be copied by hand + a "закрыть" button.
  function copyReport() {
    const rep = buildReport();
    try { console.log("[TP Assist] REPORT:\n" + rep); } catch (e) {}
    let ok = false;
    let host = document.getElementById("__tpReport");
    if (host) host.remove();
    host = document.createElement("div");
    host.id = "__tpReport";
    host.style.cssText = "position:fixed;z-index:2147483647;left:10px;right:10px;bottom:10px;max-height:60vh;" +
      "background:#0c1116;color:#e6edf3;border:2px solid #0c47c2;border-radius:10px;padding:10px;" +
      "font:12px -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.5);display:flex;flex-direction:column";
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:6px";
    const msg = document.createElement("b"); msg.textContent = "Отчёт скопирован — вставь его мне в чат";
    const x = document.createElement("button");
    x.textContent = "закрыть"; x.style.cssText = "border:0;background:#233;color:#fff;padding:5px 10px;border-radius:7px;cursor:pointer";
    x.onclick = () => host.remove();
    bar.appendChild(msg); bar.appendChild(x);
    const ta = document.createElement("textarea");
    ta.readOnly = true; ta.value = rep;
    ta.style.cssText = "flex:1;min-height:180px;width:100%;box-sizing:border-box;background:#06090d;color:#cfe;" +
      "border:1px solid #234;border-radius:7px;font:11px ui-monospace,Menlo,Consolas,monospace;white-space:pre;overflow:auto";
    host.appendChild(bar); host.appendChild(ta);
    document.documentElement.appendChild(host);
    try { ta.focus(); ta.select(); ok = document.execCommand("copy"); } catch (e) {}
    try { if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(rep).catch(() => {}); } catch (e) {}
    msg.textContent = ok ? "✓ Отчёт скопирован — вставь его мне в чат"
                         : "Выдели весь текст ниже (Ctrl+A) и скопируй (Ctrl+C) — вставь мне в чат";
    return ok;
  }

  let lastCount = 0;
  function fillTP() {
    let n = 0;
    clickApplyIfNeeded();
    const before = document.querySelectorAll('input,select,textarea').length;
    try {
      // identity (empty-only)
      n += fillText("login.*email|^email", P.email) ? 1 : 0;
      n += fillText("first name|given name", P.first_name) ? 1 : 0;
      n += fillText("last name|surname|family name", P.last_name) ? 1 : 0;
      if (P.middle_name) fillText("middle name", P.middle_name);
      fillPasswords();
      // phone: Type = Mobile (its own select) + Number as digits. Fall back to a custom dropdown
      // widget when there is no native <select> (iCIMS iForms render these as widgets).
      if (!pickSelect("^\\s*type\\b|phone type", P.phone_type || "Mobile")) openAndPickCustom("^\\s*type\\b|phone type", P.phone_type || "Mobile");
      fillText("^\\s*number|include country code|^phone|mobile number", P.phone_digits, true);
      // how did you hear + its dependent specify-further
      fillHowHeard();
      // residence: Country FIRST (unlocks State), then State, address Type, then the address fields
      if (!pickSelect("country", P.country || "United States")) openAndPickCustom("country", P.country || "United States");
      if (!pickSelect("state|province", P.state_full || "Ohio")) openAndPickCustom("state|province", P.state_full || "Ohio");
      if (!pickSelect("^\\s*type\\b", "Physical")) openAndPickCustom("^\\s*type\\b", "Physical");   // address Type (phone Type already set)
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
      try {
        console.log("[TP Assist] НЕ смог заполнить (required):", unfilled);
        console.log("[TP Assist] DEBUG структура незаполненных → пришли эту строку:",
                    JSON.stringify(debugUnfilled()));
      } catch (e) {}
    }
    return n;
  }

  // required, visible, still-empty fields (the ones that block Submit) — for the badge + console
  function unfilledReport() {
    const out = [];
    for (const el of document.querySelectorAll("input,select,textarea,[role=combobox],[role=listbox]")) {
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
      } else empty = comboEmpty(el, t);
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
      b.style.cursor = "pointer";
      b.textContent = "TP Assist: не заполнил " + unfilled.length + " — " +
        unfilled.slice(0, 8).join(" · ") + (unfilled.length > 8 ? " …" : "") +
        "  ▸ НАЖМИ, чтобы отправить структуру формы (или Alt+Shift+D)";
      b.onclick = () => { try { copyReport(); } catch (e) {} };
    } else {
      b.style.background = "#1a7f37";
      b.style.cursor = "default";
      b.textContent = "TP Assist: всё заполнено ✓ — реши капчу и жми Submit";
      b.onclick = null;
    }
  }

  // triggers: on load, on DOM changes (debounced), and on the hotkey
  let deb = null;
  const schedule = () => { clearTimeout(deb); deb = setTimeout(fillTP, 700); };
  const mo = new MutationObserver(schedule);
  try { mo.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) {}
  // Several bounded initial passes: the first render, then the Country->State + how-heard->specify
  // cascades (State's options only appear AFTER Country is set), then any late-loading iframe form.
  setTimeout(fillTP, 1200);
  setTimeout(fillTP, 2800);
  setTimeout(fillTP, 4600);
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((m) => {
      if (!m) return;
      if (m.type === "fill") fillTP();
      else if (m.type === "report") { try { copyReport(); } catch (e) {} }
    });
  }
  // Alt+Shift+D → copy the DOM report (a real keydown gesture in THIS frame, so clipboard works even
  // inside the iCIMS iframe). Click into any form field first so the form frame has focus.
  window.addEventListener("keydown", (e) => {
    if (e.altKey && e.shiftKey && (e.code === "KeyD" || (e.key || "").toLowerCase() === "d")) {
      e.preventDefault(); try { copyReport(); } catch (err) {}
    }
  }, true);
  window.__tpFill = fillTP;       // test hook (page.evaluate('__tpFill()'))
  window.__tpReport = buildReport; // test hook — returns the report string
})();
