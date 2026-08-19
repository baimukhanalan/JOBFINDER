// Apply Assist — content script. Fills the current page's form from window.APPLY_PROFILE.
// Triggered by the popup button, the toolbar, or Alt+Shift+F. Reviews & submits are the user's.
(() => {
  if (window.__applyAssistLoaded) return;
  window.__applyAssistLoaded = true;

  // The manifest injects into ALL frames on ALL urls — Greenhouse/Lever legitimately
  // embed the application form in an iframe and ATS domains are unbounded, so the
  // matches/all_frames stay broad. But most sub-frames (analytics, chat widgets,
  // trackers) hold no form: bail early so fills and messages don't echo from every
  // frame on the page.
  if (window.top !== window && document.querySelectorAll("input, textarea, select").length < 3) return;

  // Baked offline fallback. When the server knows the selected profile, its identity
  // fields override these (see withIdentity below) — the rest stays from profile.js.
  let P = window.APPLY_PROFILE || {};
  const ANSWERS = window.APPLY_ANSWERS || [];

  // Countries that are NOT the US — used to AVOID auto-answering "authorized to work in <X>?".
  const FOREIGN = /\b(canada|canadian|united kingdom|\buk\b|england|ireland|germany|france|spain|italy|netherlands|poland|portugal|romania|india|pakistan|philippines|australia|new zealand|singapore|brazil|mexico|argentina|nigeria|kenya|south africa|egypt|uae|emirates|saudi|israel|japan|china|korea|vietnam|indonesia|malaysia|thailand|europe|emea|apac|eu\b)\b/i;
  const US_RE = /\b(u\.?s\.?a?\.?|united states|america|stateside)\b/i;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  // Build a descriptive label for a field from every available signal.
  function labelText(el) {
    const bits = [];
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) bits.push(lab.textContent);
    }
    let p = el.closest("label");
    if (p) bits.push(p.textContent);
    const al = el.getAttribute("aria-label"); if (al) bits.push(al);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) lb.split(/\s+/).forEach((id) => { const e = document.getElementById(id); if (e) bits.push(e.textContent); });
    // Climb to a wrapping field group and grab its question/legend text.
    let cur = el, hops = 0;
    while (cur && hops < 4) {
      cur = cur.parentElement; hops++;
      if (!cur) break;
      const q = cur.querySelector("label, legend, .label, [class*='label'], [class*='question'], [class*='Question']");
      if (q && !q.contains(el)) { bits.push(q.textContent); break; }
    }
    bits.push(el.getAttribute("placeholder") || "", el.getAttribute("name") || "", el.id || "");
    return norm(bits.join(" ").toLowerCase());
  }

  const looksLikeQuestion = (t) => t.length > 70 || /\?$/.test(t.trim()) || /\b(describe|explain|why|how|what|tell us|give an example|share)\b/.test(t);

  // Decide what to put in a field. Returns {value} | {answer} | {skip} | {needs} | null.
  function decide(el, label, kind) {
    const isText = kind === "text";
    const isArea = kind === "textarea";
    const has = (re) => re.test(label);

    // --- Open-ended narrative: answer bank only, never a bare Yes/No ---
    if (isArea || (isText && looksLikeQuestion(label))) {
      for (const [re, ans] of ANSWERS) if (re.test(label)) return { value: ans };
      // Identity bits still belong in some short text inputs even if long-ish:
      // fall through to identity matching below for plain text inputs.
      if (isArea) return { needs: true };
    }

    // --- Identity / direct profile fields ---
    if (has(/first ?name|given name|fname/) && !has(/last|family/)) return { value: P.first_name };
    if (has(/last ?name|surname|family name|lname/)) return { value: P.last_name };
    if (has(/full ?name|your name|legal name|^name\b|applicant name|candidate name/) && !has(/first|last|user ?name|company|file|account/)) return { value: P.full_name };
    if (has(/e-?mail/)) return { value: P.email };
    if (has(/telegram/)) return { value: P.telegram };
    if (has(/phone|mobile|\btel\b|contact number|cell/)) return { value: P.phone };
    if (has(/linkedin/)) return { value: P.linkedin };
    // Spoken-English level (radio group on many APAC/CIS forms) — pick the value in
    // P.english_level; setRadioGroup matches it against the option wording.
    if (has(/level of (spoken )?english|english (level|proficiency|skill)|spoken english|how.*english/))
      return { value: P.english_level };
    if (has(/portfolio|personal (web)?site|website|github/)) return { value: P.website };
    if (has(/zip|postal/)) return { value: P.zip };
    if (has(/\bcity\b/) && !has(/capacity/)) return { value: P.city };
    if (has(/\bstate\b|province|region/) && !has(/statement|united states/)) return { value: P.state_full };
    if (has(/country/)) return { value: P.country };
    if (has(/street|address|^location$|current location|where.*located|city.*state|location/) && !looksLikeQuestion(label)) return { value: P.address };
    if (has(/years.*(experience|exp)|experience.*years|how many years|yrs of/)) return { value: P.years_experience };
    if (has(/(highest )?(level of )?education|degree|qualification/)) return { value: P.education_level };
    if (has(/desired (salary|pay|comp)|salary (expect|require|range)|expected (salary|pay)|compensation/)) return { value: P.desired_salary };
    if (has(/start date|available.*start|when.*(can you )?start|earliest.*start|notice period/)) return { value: P.available_start };
    if (has(/time ?zone/)) return { value: P.timezone };
    if (has(/how did you hear|how.*find|referr|source/)) return { value: P.source };

    // --- Eligibility yes/no (with foreign work-auth guard) ---
    if (has(/authoriz|eligible to work|legally.*work|right to work|work permit/)) {
      if (FOREIGN.test(label) && !US_RE.test(label)) return { needs: true }; // non-US country → user decides
      return { value: P.work_authorized_us };
    }
    if (has(/sponsor/)) return { value: P.needs_sponsorship };
    if (has(/18 (years|yrs)|over 18|at least 18|age of 18|legal.*age/)) return { value: P.over_18 };
    // Criminal-history questions are NOT consent questions: "do you have a criminal
    // record?" must answer No, never the consent's Yes. Criminal words win when mixed.
    if (has(/criminal|convicted|conviction|felony|misdemeanor/)) return { value: P.criminal_record };
    if (has(/background check|consent to.*check/)) return { value: P.background_check_consent };
    if (has(/relocat/)) return { value: P.willing_relocate };
    if (has(/work (remote|from home)|remote work|comfortable.*remote|fully remote/)) return { value: P.remote_ok };
    if (has(/reliable (internet|computer)|own (a )?(computer|laptop)|equipment|high.?speed internet|quiet (work)?space/)) return { value: P.has_equipment };

    // --- EEO / demographics: never auto-answered — the human decides (yellow).
    // Matches the batch engine, which skips these outright.
    if (has(/gender|sex\b|hispanic|latino|race|ethnicit|veteran|disabilit/)) return { needs: true };

    // --- Short plain text that's actually a question we don't know ---
    if ((isText || isArea) && looksLikeQuestion(label)) return { needs: true };
    return null;
  }

  // ---- setters that trigger framework change events ----
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillSelect(el, value) {
    const v = value.toLowerCase();
    let best = null;
    for (const o of el.options) {
      const t = (o.textContent || "").trim().toLowerCase();
      if (!t) continue;
      if (t === v) { best = o; break; }
      if (!best && (t.includes(v) || v.includes(t)) && t !== "") best = o;
    }
    // Map a few canonical values onto common option wording.
    if (!best && /yes/i.test(value)) best = [...el.options].find((o) => /^yes/i.test(o.textContent.trim()));
    if (!best && /^no$/i.test(value)) best = [...el.options].find((o) => /^no/i.test(o.textContent.trim()));
    if (best) {
      el.value = best.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  // The option's OWN label only (never the group legend — else "...now..." matches "No").
  function optionText(el) {
    let t = el.getAttribute("aria-label") || "";
    if (!t) { const p = el.closest("label"); if (p) t = p.textContent; }
    // Ashby (and other React ATSes) don't wrap the option text in a <label> and reuse
    // one id across the whole group — so climb to the smallest ancestor that holds
    // exactly THIS radio (the option's own row) and take its text.
    if (!t) {
      let cur = el.parentElement;
      for (let hops = 0; cur && hops < 4; hops++, cur = cur.parentElement) {
        if (cur.querySelectorAll('input[type="radio"], input[type="checkbox"]').length !== 1) break;
        const txt = (cur.innerText || cur.textContent || "").trim();
        if (txt) { t = txt; break; }
      }
    }
    if (!t && el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.textContent; }
    if (!t) t = el.value || "";
    return norm(t).toLowerCase();
  }

  // The group question for a radio/checkbox — the heading text of the ancestor that
  // holds more than one same-named option. React ATSes (Ashby) keep it in an
  // unlabelled div, so labelText's class-based climb misses it.
  function radioGroupQuestion(el) {
    if (!el.name) return "";
    let cur = el.parentElement;
    for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
      const same = cur.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length;
      if (same > 1) {
        const q = cur.querySelector("label, legend, h1, h2, h3, h4, [class*='label'], [class*='question'], [class*='title']");
        if (q && q.textContent.trim() && !q.querySelector("input")) return q.textContent;
        return ((cur.innerText || "").trim().split("\n")[0]) || "";
      }
    }
    return "";
  }

  function setRadioGroup(name, scope, value) {
    const radios = (scope || document).querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
    const want = value.toLowerCase();
    let hit = null;
    radios.forEach((r) => {
      if (hit) return;
      const ot = optionText(r);
      const toks = ot.split(/[^a-z0-9]+/).filter(Boolean);
      // `includes` (for want ≥3 chars) handles option wording with punctuation the
      // startsWith check misses — e.g. value "confident b2" vs "Confident B2, ready…".
      if (ot === want || toks.includes(want) || ot.startsWith(want + " ") ||
        (want.length >= 3 && ot.includes(want)) ||
        (want === "yes" && toks[0] === "yes") || (want === "no" && toks[0] === "no")) hit = r;
    });
    if (hit) { hit.click(); return true; }
    return false;
  }

  // ---- server identity: prefer the selected profile's data over the baked one ----
  // Server keys -> the P shape decide() uses. Only identity/contact fields come from
  // the server; eligibility/answers stay from profile.js via the spread fallback.
  function mapIdentity(id) {
    const out = {};
    const set = (k, v) => { if (v) out[k] = String(v); };
    set("first_name", id.first_name);
    set("last_name", id.last_name);
    set("full_name", id.full_name);
    set("email", id.email);
    set("phone", id.phone);
    set("linkedin", id.linkedin);
    set("zip", id.zip);
    set("city", id.city);
    set("country", id.country);
    set("address", id.location);
    if (id.state) { out.state = String(id.state); out.state_full = String(id.state); }
    return out;
  }

  let identityLoaded = false;
  // Fetch identity once per page (background caches it for 24h), then run cb.
  // Any failure -> baked profile.js values; the fill must never hang or break offline.
  function withIdentity(cb) {
    if (identityLoaded) { cb(); return; }
    let done = false;
    const go = () => { if (!done) { done = true; cb(); } };
    try {
      chrome.runtime.sendMessage({ type: "getIdentity" }, (resp) => {
        if (!chrome.runtime.lastError && resp && resp.ok && resp.identity) {
          P = { ...(window.APPLY_PROFILE || {}), ...mapIdentity(resp.identity) };
          identityLoaded = true;
        }
        go();
      });
    } catch (e) { go(); }
    setTimeout(go, 8000); // background aborts its fetch at 6s; this is the backstop
  }

  function mark(el, kind) {
    const c = kind === "ok" ? "#1c5e35" : "#7a5c12";
    const bg = kind === "ok" ? "rgba(40,200,120,.10)" : "rgba(230,180,60,.12)";
    el.style.outline = `2px solid ${c}`;
    el.style.background = bg;
  }

  function run() {
    armSubmitWatch(); // any fill means a human submit may follow — watch for it
    let filled = 0, needs = 0, skipped = 0;
    const radioGroupsDone = new Set();

    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (el.disabled || el.readOnly || el.type === "hidden" || el.offsetParent === null) return;
      const type = (el.type || "").toLowerCase();
      if (["submit", "button", "reset", "file", "image", "search"].includes(type)) return;

      // radios handled per-group
      if (type === "radio") {
        if (radioGroupsDone.has(el.name) || !el.name) return;
        const label = (radioGroupQuestion(el) + " " + labelText(el)).toLowerCase();
        const d = decide(el, label, "radio");
        if (d && d.value) {
          radioGroupsDone.add(el.name);
          if (setRadioGroup(el.name, el.closest("fieldset, form, body"), d.value)) { filled++; }
        } else if (d && d.needs) { needs++; }
        return;
      }
      if (type === "checkbox") {
        const label = labelText(el);
        if (/agree|consent|acknowledge|confirm|terms|certify|accept/.test(label) && !el.checked) {
          el.click(); mark(el, "ok"); filled++;
        }
        return;
      }

      const kind = el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : "text";
      const label = labelText(el);
      const d = decide(el, label, kind === "select" ? "text" : kind);
      if (!d) return;
      if (d.needs) { mark(el, "needs"); needs++; return; }
      if (!d.value) return;

      if (kind === "select") {
        if (fillSelect(el, d.value)) { mark(el, "ok"); filled++; } else { mark(el, "needs"); needs++; }
      } else {
        if (el.value && el.value.trim()) {
          // Already filled — by the ATS résumé parser (parser-first order) or by the
          // human. Don't clobber it; we only top up what's still missing. Just confirm.
          mark(el, "ok");
          return;
        }
        setNative(el, d.value);
        mark(el, "ok"); filled++;
      }
    });

    // Best-effort React-Select / custom comboboxes for eligibility & country.
    fillCustomDropdowns().then((n) => {
      chrome.runtime?.sendMessage?.({ type: "fillResult", filled: filled + n, needs, skipped });
      flash(`Filled ${filled + n} · ${needs} need you`);
    }).catch(() => {
      chrome.runtime?.sendMessage?.({ type: "fillResult", filled, needs, skipped });
      flash(`Filled ${filled} · ${needs} need you`);
    });

    return { filled, needs };
  }

  // React-Select: control + typed value + first matching option. Sequential, best-effort.
  async function fillCustomDropdowns() {
    const controls = document.querySelectorAll("[class*='select__control'], [class*='Select__control']");
    let n = 0;
    for (const ctrl of controls) {
      const wrap = ctrl.closest("[class*='select__container'], [class*='Select'], div");
      if (wrap && /[✓✔]/.test(wrap.getAttribute("data-filled") || "")) continue;
      const label = labelText(ctrl);
      const d = decide(ctrl, label, "select");
      if (!d || !d.value) continue;
      try {
        ctrl.click();
        await new Promise((r) => setTimeout(r, 120));
        const input = document.querySelector("input[id*='react-select'], [class*='select__input'] input");
        if (input) { setNative(input, d.value); await new Promise((r) => setTimeout(r, 180)); }
        const opt = [...document.querySelectorAll("[class*='select__option'], [role='option']")]
          .find((o) => o.textContent.trim().toLowerCase().includes(d.value.toLowerCase().split(" ")[0]));
        if (opt) { opt.click(); n++; mark(ctrl, "ok"); }
        else { ctrl.click(); } // close menu
        await new Promise((r) => setTimeout(r, 80));
      } catch (e) { /* ignore one bad dropdown */ }
    }
    return n;
  }

  function flash(msg, bare) {
    let t = document.getElementById("__apply_assist_toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "__apply_assist_toast";
      t.style.cssText = "position:fixed;z-index:2147483647;bottom:18px;right:18px;background:#171c23;color:#e7ebf0;" +
        "padding:11px 15px;border-radius:10px;border:1px solid #2b3340;font:600 13px -apple-system,Segoe UI,Arial;" +
        "box-shadow:0 6px 24px rgba(0,0,0,.4)";
      document.body.appendChild(t);
    }
    t.textContent = "Apply Assist — " + msg + (bare ? "" : ". Review green, fill yellow, attach résumé, submit.");
    clearTimeout(t._h); t._h = setTimeout(() => t.remove(), 6000);
  }

  // ---- server draft for open-ended questions the deterministic pass left blank ----
  function guessCompany() {
    const og = document.querySelector("meta[property='og:site_name']");
    if (og && og.content) return og.content.trim();
    const h = location.hostname.replace(/^(www|jobs|careers|boards|apply|job-boards)\./, "").split(".")[0];
    return h ? h.charAt(0).toUpperCase() + h.slice(1) : "";
  }

  // Collect empty open-ended fields (textareas + question-like text inputs) needing prose.
  function collectOpen() {
    const items = [];
    const seen = new Set();
    document.querySelectorAll("textarea, input[type='text']").forEach((el) => {
      if (el.disabled || el.readOnly || el.offsetParent === null) return;
      if (el.value && el.value.trim().length > 3) return; // already has content
      const label = labelText(el);
      const isArea = el.tagName === "TEXTAREA";
      if (!isArea && !looksLikeQuestion(label)) return;
      if (isArea && !looksLikeQuestion(label) && label.length < 25) return;
      // skip identity/eligibility — those are the deterministic pass's job
      if (/first ?name|last ?name|e-?mail|phone|linkedin|zip|^city$|country|salary|^state$/.test(label)) return;
      const q = norm(label).replace(/\s*\*?\s*\(?required\)?\s*$/i, "").slice(0, 300);
      if (!q || q.length < 12 || seen.has(q)) return;
      seen.add(q);
      items.push({ el, q });
    });
    return items;
  }

  function runDraft() {
    armSubmitWatch();
    const items = collectOpen();
    if (!items.length) { flash("No open questions to draft"); return; }
    flash(`Drafting ${items.length} answer(s)…`);
    chrome.runtime.sendMessage(
      { type: "draft", questions: items.map((i) => i.q), company: guessCompany(), job_title: document.title.slice(0, 80) },
      (resp) => {
        if (!resp || !resp.ok) { flash("Draft failed — fill manually"); return; }
        const n = applyOpen(items, resp.answers || {}, resp.review || {});
        flash(n.edits ? `Drafted ${n.filled} (${n.edits} need your edit)`
                      : `Drafted ${n.filled} answer(s). Review before submit.`);
      }
    );
  }

  // Fill open answers; the server's review map decides green vs yellow (answers
  // arrive pre-stripped — the review flag is metadata, never part of the text).
  function applyOpen(items, answers, review) {
    let filled = 0, edits = 0;
    for (const it of items) {
      const a = answers[it.q];
      if (!a) continue;
      setNative(it.el, a);
      if (review[it.q]) { mark(it.el, "needs"); edits++; } else { mark(it.el, "ok"); }
      filled++;
    }
    return { filled, edits };
  }

  // ---- closed screeners -> the server (selects / radio / checkbox groups) ----

  // The GROUP question (fieldset legend / question wrapper), never the option's own
  // label — else "Do you ... now?" collapses into the "No" option text.
  function groupQuestion(el) {
    const own = el.closest("label");
    const opt = optionText(el);
    const group = el.closest("fieldset, [role='radiogroup'], [role='group'], " +
      "[class*='question'], [class*='Question'], .field, .form-group, ul");
    if (!group) return "";
    const cands = group.querySelectorAll("legend, [class*='question'] label, [class*='label'], h3, h4");
    for (const c of cands) {
      if (own && (own.contains(c) || c.contains(own))) continue;
      if (c.contains(el)) continue;
      const t = norm(c.textContent);
      if (!t || t.toLowerCase() === opt) continue;
      return t;
    }
    return "";
  }

  const SELECT_PLACEHOLDER = /^(select|choose|please|pick|--|—|\.\.\.)/i;

  // Unanswered closed questions for the server. Anything decide() deterministically
  // filled this run no longer looks unanswered (the select left its placeholder, a
  // radio got checked) so it is skipped naturally. React-Select/custom comboboxes are
  // NOT collected: the local fillCustomDropdowns handles those best-effort and the
  // batch engine owns them server-side — menu-harvesting from here is out of scope.
  function collectClosed() {
    const items = [];
    // (1) native selects still sitting on their placeholder / empty first option
    document.querySelectorAll("select").forEach((el) => {
      if (el.disabled || el.offsetParent === null || el.multiple) return;
      const cur = el.options[el.selectedIndex];
      const answered = cur && cur.value !== "" &&
        !(el.selectedIndex === 0 && SELECT_PLACEHOLDER.test((cur.textContent || "").trim()));
      if (answered) return;
      const options = [];
      for (const o of el.options) {
        const t = (o.textContent || "").trim();
        if (t && o.value !== "") options.push(t);
      }
      // <2 real options = nothing to choose; >40 (countries...) = rule/human territory
      if (options.length < 2 || options.length > 40) return;
      const question = norm(labelText(el)).slice(0, 300);
      if (question.length < 4) return;
      items.push({ kind: "select", el, question, options });
    });
    // (2) radio groups (by name) with no checked option
    const groups = { radio: {}, checkbox: {} };
    document.querySelectorAll("input[type='radio'], input[type='checkbox']").forEach((r) => {
      if (r.disabled || r.offsetParent === null || !r.name) return;
      const bucket = groups[r.type];
      (bucket[r.name] = bucket[r.name] || []).push(r);
    });
    // (3) checkbox groups (same name, >=2) all unchecked — treated like radios
    for (const kind of ["radio", "checkbox"]) {
      for (const name of Object.keys(groups[kind])) {
        const inputs = groups[kind][name];
        if (inputs.length < 2 || inputs.some((r) => r.checked)) continue;
        const question = norm(groupQuestion(inputs[0])).slice(0, 300);
        if (question.length < 4) continue;
        const options = inputs.map((r) => optionText(r) || (r.value || "").trim());
        if (options.some((o) => !o)) continue; // unlabeled options -> useless to choose from
        items.push({ kind, inputs, question, options });
      }
    }
    return items.slice(0, 40); // server caps at 40 — keep indexes aligned
  }

  function applyClosed(item, idx) {
    if (item.kind === "select") {
      const text = item.options[idx];
      return text != null && fillSelect(item.el, text); // exact option text matches first
    }
    const input = item.inputs && item.inputs[idx];
    if (!input) return false;
    if (!input.checked) input.click();
    return true;
  }

  // Ship the leftovers (closed screeners + open questions) to the server in ONE
  // round-trip and apply its picks/drafts. Both collectors skip anything already
  // filled, so the deterministic/pack passes' work is never re-asked or clobbered.
  function assistLeftovers(done) {
    const closed = collectClosed();
    const open = collectOpen();
    if (!closed.length && !open.length) { flash("Nothing left to draft — review and submit"); if (done) done(); return; }
    flash(`Smart fill: checking ${closed.length + open.length} question(s) with the server…`);
    chrome.runtime.sendMessage({
      type: "assist",
      company: guessCompany(),
      job_title: document.title.slice(0, 80),
      closed: closed.map((c) => ({ question: c.question, options: c.options })),
      open: open.map((i) => i.q),
    }, (resp) => {
      if (chrome.runtime.lastError || !resp || !resp.ok) {
        flash("Server unavailable — local fill only");
        if (done) done();
        return;
      }
      let filled = 0, edits = 0;
      (resp.closed || []).forEach((a, i) => {
        const item = closed[i];
        if (!item || !a || a.index == null || !applyClosed(item, a.index)) return;
        filled++;
        const tgt = item.el || item.inputs[a.index];
        if (a.review) { mark(tgt, "needs"); edits++; } else { mark(tgt, "ok"); }
      });
      const o = applyOpen(open, resp.answers || {}, resp.review || {});
      filled += o.filled; edits += o.edits;
      flash(edits ? `Drafted ${filled} (${edits} need your edit)` : `Drafted ${filled}`);
      if (done) done();
    });
  }

  // Smart fill: deterministic pass first, then the single server round-trip.
  function runSmart() {
    withIdentity(() => {
      run(); // deterministic pass (also kicks off the local custom-dropdown pass)
      setTimeout(() => assistLeftovers(), 600); // let the async custom-dropdown pass settle before re-collecting
    });
  }

  // ==== one-click apply (#aa=profile:jid on the dashboard's Apply link) ====

  // The dashboard links apply_url#aa=<profile>:<jid> (both ids are [a-z0-9_-]).
  function parseAaHash(hash) {
    const m = /#aa=([a-z0-9_-]+):([a-z0-9_-]+)/.exec(hash || "");
    return m ? { profile: m[1], jid: m[2] } : null;
  }

  // Pack question -> form field: same containment heuristic in both directions —
  // a match is when one normalized string contains the other's first 60 chars.
  // `items` are {q, el} with q already normalized lowercase (collectPackTargets).
  function matchPackAnswer(question, items) {
    const q = norm(question).toLowerCase();
    if (q.length < 8) return null;
    const qHead = q.slice(0, 60);
    for (const it of items) {
      const l = it.q || "";
      if (l.length < 8) continue; // "city"-sized labels match everything — skip
      if (l.includes(qHead) || q.includes(l.slice(0, 60))) return it;
    }
    return null;
  }

  // Candidate fields for the pack answers: every EMPTY visible text input/textarea,
  // labeled the same way collectOpen labels its fields. No question-likeness filter:
  // the pack's question list came from this very form, so the label decides alone.
  function collectPackTargets() {
    const items = [];
    document.querySelectorAll("textarea, input[type='text'], input:not([type])").forEach((el) => {
      if (el.disabled || el.readOnly || el.offsetParent === null) return;
      if (el.value && el.value.trim().length > 3) return; // already has content
      const q = norm(labelText(el)).replace(/\s*\*?\s*\(?required\)?\s*$/i, "").slice(0, 300);
      if (q.length < 8) return;
      items.push({ el, q });
    });
    return items;
  }

  // Drop the batch-drafted answers into their fields. Yellow when the server says
  // the human must look at it, green otherwise. One answer per field.
  function fillPackAnswers(pack) {
    const answers = pack.answers || {};
    const review = pack.review || {};
    const targets = collectPackTargets();
    const matched = {}; // question -> el (review panel click-to-focus)
    let filled = 0;
    for (const q of Object.keys(answers)) {
      if (!answers[q]) continue;
      const hit = matchPackAnswer(q, targets);
      if (!hit) continue;
      targets.splice(targets.indexOf(hit), 1);
      setNative(hit.el, answers[q]);
      mark(hit.el, review[q] ? "needs" : "ok");
      matched[q] = hit.el;
      filled++;
    }
    return { filled, matched };
  }

  // Resume-input picker over {el, sig} candidates (sig = accept + label, lowercase).
  // Skips photo/avatar/image/cover inputs, prefers a resume/cv-labeled one, else
  // takes the first survivor. Pure — the node sanity checks drive it directly.
  function pickResumeInput(cands) {
    const ok = (cands || []).filter((c) => c && c.sig != null && !/photo|avatar|image|cover/i.test(c.sig));
    if (!ok.length) return null;
    const pref = ok.find((c) => /resume|cv/i.test(c.sig));
    return (pref || ok[0]).el;
  }

  function resumeSig(el) {
    return ((el.getAttribute("accept") || "") + " " + labelText(el)).toLowerCase();
  }

  function bgCall(msg) {
    return new Promise((res) => {
      try {
        chrome.runtime.sendMessage(msg, (r) => {
          if (chrome.runtime.lastError) res({ ok: false, error: chrome.runtime.lastError.message });
          else res(r || { ok: false, error: "no reply" });
        });
      } catch (e) { res({ ok: false, error: String(e) }); }
    });
  }

  // Fetch the tailored PDF via the background and drop it on the form's file input.
  // Hidden inputs count too — every ATS hides the real input behind a styled button.
  async function attachResume(profile, jid) {
    try {
      const inputs = [...document.querySelectorAll("input[type='file']")];
      const el = pickResumeInput(inputs.map((i) => ({ el: i, sig: resumeSig(i) })));
      if (!el) throw new Error("no file input");
      const r = await bgCall({ type: "resumeFile", profile, jid });
      if (!r.ok || !r.b64) throw new Error(r.error || "no file");
      const bytes = Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0));
      const file = new File([bytes], "resume.pdf", { type: "application/pdf" });
      const dt = new DataTransfer();
      dt.items.add(file);
      el.files = dt.files;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      mark(el, "ok");
      return true;
    } catch (e) {
      flash("Couldn't attach the résumé — attach it manually", true);
      return false;
    }
  }

  // Save a durable copy of the tailored résumé to the PC's Downloads folder (via the
  // background service worker + chrome.downloads). Best-effort — a failure never blocks
  // the fill. Named after the company/role so applications don't overwrite each other.
  function saveResumeToDownloads(profile, jid, pack) {
    const raw = ["Resume", pack && pack.company, pack && pack.job_title]
      .filter((s) => s && String(s).trim()).join(" - ");
    const base = (raw || ("resume_" + profile + "_" + jid))
      .replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 120);
    bgCall({ type: "saveResume", profile, jid, filename: base + ".pdf" }).then((r) => {
      if (r && r.ok) flash("Résumé saved to Downloads", true);
    });
  }

  // Floating review panel (bottom-left twin of the toast). Lists the answers the
  // server flagged for a human look (click scrolls to the field) + still-empty
  // questions. Replaces itself on rebuild; ✕ closes it.
  function buildPanel(pack, matched) {
    const old = document.getElementById("__aa_panel");
    if (old) old.remove();
    const p = document.createElement("div");
    p.id = "__aa_panel";
    p.style.cssText = "position:fixed;z-index:2147483647;bottom:18px;left:18px;width:330px;max-height:48vh;overflow:auto;" +
      "background:#171c23;color:#e7ebf0;padding:12px 14px;border-radius:10px;border:1px solid #2b3340;" +
      "font:13px -apple-system,Segoe UI,Arial;box-shadow:0 6px 24px rgba(0,0,0,.4);line-height:1.45";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:4px";
    const title = document.createElement("b");
    title.textContent = "Apply Assist — review";
    const x = document.createElement("span");
    x.textContent = "✕";
    x.style.cssText = "cursor:pointer;color:#8a94a3;padding:0 3px;font-size:14px";
    x.addEventListener("click", () => p.remove());
    head.appendChild(title); head.appendChild(x);
    p.appendChild(head);

    const section = (label) => {
      const s = document.createElement("div");
      s.style.cssText = "margin:8px 0 2px;font:600 11px -apple-system,Segoe UI,Arial;color:#8a94a3;" +
        "text-transform:uppercase;letter-spacing:.4px";
      s.textContent = label;
      p.appendChild(s);
    };
    const clip = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n) + "…" : s; };

    // review rows: explicit review_items first, then answers flagged in the review map
    const seen = new Set();
    const rows = [];
    (pack.review_items || []).forEach((it) => {
      if (it && it.question && !seen.has(it.question)) {
        seen.add(it.question);
        rows.push({ q: it.question, a: it.answer || "" });
      }
    });
    Object.keys(pack.review || {}).forEach((q) => {
      if (!seen.has(q)) { seen.add(q); rows.push({ q, a: (pack.answers || {})[q] || "" }); }
    });
    if (rows.length) {
      section("Check these answers");
      rows.forEach(({ q, a }) => {
        const row = document.createElement("div");
        row.style.cssText = "margin:5px 0;padding:7px 9px;border-radius:7px;background:#1e242d;" +
          "cursor:pointer;border-left:3px solid #c79a2a";
        const qd = document.createElement("div");
        qd.style.cssText = "color:#e6b43c;font-size:12px";
        qd.textContent = clip(q, 90);
        const ad = document.createElement("div");
        ad.style.cssText = "color:#cfd6df;font-size:12px;margin-top:2px";
        ad.textContent = clip(a, 160);
        row.appendChild(qd); row.appendChild(ad);
        row.addEventListener("click", () => focusField(q, matched));
        p.appendChild(row);
      });
    }

    const unfilled = (pack.unfilled || []).filter((u) => u && String(u).trim());
    if (unfilled.length) {
      section("Still empty — fill yourself");
      unfilled.slice(0, 12).forEach((u) => {
        const d = document.createElement("div");
        d.style.cssText = "margin:4px 0;color:#aab3c0;font-size:12px";
        d.textContent = "• " + clip(u, 100);
        p.appendChild(d);
      });
    }

    const st = document.createElement("div");
    st.id = "__aa_panel_status";
    st.style.cssText = "margin-top:8px;color:#46d17f;font-size:12px";
    p.appendChild(st);

    const foot = document.createElement("div");
    foot.style.cssText = "margin-top:8px;padding-top:8px;border-top:1px solid #2b3340;color:#8a94a3;font-size:11px";
    foot.textContent = "Review yellow fields, then click Submit on the form.";
    p.appendChild(foot);

    document.body.appendChild(p);
  }

  function focusField(q, matched) {
    let el = matched && matched[q];
    if (!el || !el.isConnected) {
      const all = [];
      document.querySelectorAll("textarea, input[type='text'], input:not([type]), select").forEach((f) => {
        if (f.offsetParent === null) return;
        const lq = norm(labelText(f)).slice(0, 300);
        if (lq.length >= 8) all.push({ el: f, q: lq });
      });
      const hit = matchPackAnswer(q, all);
      el = hit && hit.el;
    }
    if (!el) return;
    try {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.focus({ preventScroll: true });
      const prev = el.style.boxShadow;
      el.style.boxShadow = "0 0 0 3px rgba(230,180,60,.7)";
      setTimeout(() => { el.style.boxShadow = prev; }, 1600);
    } catch (e) { /* focus is best-effort */ }
  }

  // Submit detection -> auto-mark. We NEVER click anything: we only notice the
  // human's click on a Submit button (capture phase, so page handlers can't eat
  // it), then poll for the confirmation text / URL for up to 120s. The armed
  // state survives a full-page navigation via sessionStorage so the thank-you
  // page (new document, hash gone) can still report the submit.
  const CONFIRM_RE = /(thank you for applying|application (has been )?submitted|successfully submitted|we have received your application|application received)/i;

  // Is this click target the form's REAL submit control? Conservative: wizard
  // steps (Next/Continue/Back) never count, and a bare "Apply" only counts when
  // it is an actual submit button inside a form — an <a> or [role='button'] with
  // that label is navigation, and "Apply filters"-style buttons don't anchor.
  function isSubmitControl(b, t) {
    t = norm(t).toLowerCase();
    if (/next|continue|back/.test(t)) return false;
    if (/submit/.test(t)) return true; // "Submit", "Submit application", ...
    if (/\bsend( my| your| the)? application\b/.test(t)) return true;
    if (/^apply( now| for this (job|position|role|opening))?$/.test(t)) {
      return (b.tagName === "BUTTON" || b.tagName === "INPUT") &&
        (b.type || "").toLowerCase() === "submit" && !!(b.closest && b.closest("form"));
    }
    return false;
  }

  // One watch per document. Re-arming is a no-op, except that a later caller
  // that KNOWS the job (the #aa pack) attaches its ids to an anonymous watch
  // (popup/hotkey fill armed first), so the mark still reaches the dashboard.
  let watchCtx = null;

  function installSubmitWatch(profile, jid, armed) {
    if (window.__aaWatchOn) {
      if (watchCtx && profile && jid && !(watchCtx.profile && watchCtx.jid)) {
        watchCtx.profile = profile;
        watchCtx.jid = jid;
      }
      return;
    }
    window.__aaWatchOn = true;
    const ctx = (watchCtx = { profile: profile || "", jid: jid || "" });
    const startPath = location.pathname;
    let done = false, polls = 0;

    function finish() {
      if (done) return;
      done = true;
      try { sessionStorage.removeItem("__aa_watch"); } catch (e) { /* ignore */ }
      if (!ctx.profile || !ctx.jid) {
        // Anonymous fill (popup/hotkey on a page with no #aa hash): the submit
        // is detected, but there is no dashboard card to flip.
        flash("Submitted — update the dashboard manually", true);
        return;
      }
      bgCall({ type: "markExt", profile: ctx.profile, jid: ctx.jid, to: "submitted" }).then((r) => {
        const note = r.ok ? "Marked as submitted ✓" : "Submitted — update the dashboard manually";
        flash(note, true);
        const st = document.getElementById("__aa_panel_status");
        if (st) st.textContent = note;
      });
    }

    function confirmed() {
      const txt = (document.body && document.body.innerText) || "";
      const moved = location.pathname !== startPath &&
        /confirm|thank|success|submitted/i.test(location.pathname + location.search);
      if (CONFIRM_RE.test(txt) || moved) { finish(); return true; }
      return false;
    }

    function poll() {
      if (done || polls >= 60) return; // every 2s, up to 120s
      polls++;
      setTimeout(() => { if (!done && !confirmed()) poll(); }, 2000);
    }

    if (armed) poll(); // resumed after navigation — the click already happened

    document.addEventListener("click", (ev) => {
      if (done) return;
      const b = ev.target && ev.target.closest ?
        ev.target.closest("button, input[type='submit'], input[type='button'], [role='button'], a") : null;
      if (!b) return;
      const t = (b.textContent || "") + " " + (b.value || "");
      if (!isSubmitControl(b, t)) return;
      try { sessionStorage.setItem("__aa_watch", JSON.stringify({ profile: ctx.profile, jid: ctx.jid, ts: Date.now() })); } catch (e) { /* ignore */ }
      if (polls === 0) poll();
    }, true);
  }

  // Every fill entry point arms the watch: the #aa hash supplies the job ids
  // when present, an anonymous watch otherwise. Install itself is idempotent,
  // so calling this on every trigger is safe.
  function armSubmitWatch() {
    const t = parseAaHash(location.hash);
    installSubmitWatch(t ? t.profile : "", t ? t.jid : "", false);
  }

  // Some ATSes gate the form behind an "Apply for this Job" button that reveals it
  // inline. Click it when the page has no form yet. Returns true if it clicked.
  function revealForm() {
    if (document.querySelectorAll("input, textarea, select").length >= 3) return false;
    const btn = [...document.querySelectorAll("a, button")].find((e) => {
      const t = (e.textContent || "").trim();
      return t.length < 30 && /\bapply\b/i.test(t) && e.offsetParent !== null
        && !/back to|listings|other/i.test(t);
    });
    if (btn) { try { btn.click(); return true; } catch (e) { /* ignore */ } }
    return false;
  }

  // JS-rendered ATSes (Ashby & co) may not have the form at document_idle yet.
  // ~10s of patience + a reveal-click each tick so a slow React SPA still fills.
  function whenFormReady(cb, tries) {
    tries = tries == null ? 20 : tries;
    if (document.querySelectorAll("input, textarea, select").length >= 3 || tries <= 0) { cb(); return; }
    revealForm();
    setTimeout(() => whenFormReady(cb, tries - 1), 500);
  }

  let __aaAutoDone = "";
  function autoRun() {
    if (window.top !== window) return; // 1-click runs only in the top frame
    const t = parseAaHash(location.hash);
    if (!t) return;
    const key = t.profile + ":" + t.jid;
    if (__aaAutoDone === key) return; // once per hash
    __aaAutoDone = key;
    flash("Auto-fill starting…", true);
    bgCall({ type: "jobPack", profile: t.profile, jid: t.jid }).then((resp) => {
      if (!resp.ok || !resp.pack) {
        flash("Couldn't load the saved answers — fill manually (" + (resp.error || "server error") + ")", true);
        return;
      }
      const pack = resp.pack;
      // Order matters: feed the résumé to the ATS's OWN parser FIRST so it pre-fills
      // what it can (name/email/experience on GH/Lever/Workable/Workday; Ashby via its
      // autofill input), THEN top up only the fields it left empty. Mirrors the server
      // runner's "let the ATS parse first, fill the gaps" order and the requested flow:
      // résumé → autofill once, then fill what's missing. A copy is also saved to the PC.
      whenFormReady(() => {
        const fillGaps = () => withIdentity(() => {
          run(); // identity/eligibility — now skips whatever the parser already filled
          setTimeout(() => {
            const res = fillPackAnswers(pack); // drafted answers into still-empty fields
            buildPanel(pack, res.matched);
            installSubmitWatch(t.profile, t.jid, false);
            assistLeftovers(); // closed screeners + leftover opens (skips filled fields)
          }, 700); // let run()'s async custom-dropdown pass settle
        });
        if (pack.has_resume) {
          saveResumeToDownloads(t.profile, t.jid, pack); // durable copy in Downloads
          attachResume(t.profile, t.jid).then((ok) => {
            // give an attachment-triggered ATS parser a beat to populate, then fill gaps
            setTimeout(fillGaps, ok ? 2500 : 0);
          });
        } else {
          fillGaps();
        }
      });
    });
  }

  // After a submit-triggered navigation the hash (and panel) are gone; pick the
  // armed watch back up so the confirmation page can still flip the dashboard.
  function resumeSubmitWatch() {
    if (window.top !== window || window.__aaWatchOn) return;
    let w = null;
    try { w = JSON.parse(sessionStorage.getItem("__aa_watch") || "null"); } catch (e) { /* ignore */ }
    if (!w || !w.profile || !w.jid) return;
    if (Date.now() - (w.ts || 0) > 10 * 60 * 1000) {
      try { sessionStorage.removeItem("__aa_watch"); } catch (e) { /* ignore */ }
      return;
    }
    installSubmitWatch(w.profile, w.jid, true);
  }

  // Triggers: popup message + keyboard command (routed via background) + direct.
  // "fill" (Alt+Shift+F / popup Fill) = deterministic local fill, with the server
  // identity preferred when reachable. "draft-fill" (popup Smart fill) = runSmart.
  chrome.runtime?.onMessage?.addListener((m, _s, reply) => {
    // Wait for the (slow, React-rendered) form to mount before filling, and click
    // an "Apply" gate if the form is hidden — otherwise a click before load fills 0.
    if (m && m.type === "fill") {
      flash("Waiting for the form…", true);
      whenFormReady(() => withIdentity(() => { const r = run(); reply && reply(r); }));
    } else if (m && m.type === "draft-fill") {
      whenFormReady(() => runSmart()); reply && reply({ ok: true });
    }
    return true;
  });
  window.__applyAssistRun = run;
  window.__applyAssistDraft = runDraft;
  window.__applyAssistSmart = runSmart;
  // DOM-event hooks (cross the isolated/main world boundary) — used for e2e + console
  // triggers. Fill stays purely local here so the offline guarantee is testable.
  window.addEventListener("__applyAssistFill", () => run());
  window.addEventListener("__applyAssistDraft", () => runDraft());
  window.addEventListener("__applyAssistSmart", () => runSmart());
  // One-click: fire on load and whenever the #aa fragment appears later.
  window.addEventListener("hashchange", () => autoRun());
  autoRun();
  resumeSubmitWatch();
  // Hooks for the node sanity checks (isolated world — invisible to the page's JS).
  window.__aaTest = { decide, collectClosed, groupQuestion, mapIdentity, labelText,
    parseAaHash, matchPackAnswer, pickResumeInput, isSubmitControl };
})();
