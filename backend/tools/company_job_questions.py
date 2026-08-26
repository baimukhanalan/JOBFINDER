"""Read-only application-question acquisition for company-discovery jobs.

This module deliberately has no dependency on ``job_catalog`` or its database.  It
turns an ATS/job URL into an apply-form URL, waits for client-side forms to hydrate,
and returns a JSON-serialisable result with an explicit scrape state.  It never
fills a field and never submits a form.

``complete`` means a stable rendered form was read without known omissions;
``partial`` means useful evidence was captured but the form was empty, unstable,
or contained controls that could not be labelled; ``failed`` means navigation or
extraction failed and no trustworthy question set is available.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

ScrapeState = Literal["complete", "partial", "failed"]

_WS_RE = re.compile(r"\s+")
_REQUIRED_SUFFIX_RE = re.compile(r"\s*(?:\*|\(required\)|required)\s*$", re.I)
_HONEYPOT_LABEL_RE = re.compile(
    r"(?:for\s+(?:robots|bots)\s+only|leave\s+(?:this\s+)?(?:field\s+)?blank|"
    r"do\s+not\s+(?:fill|enter|complete).*(?:human|person))", re.I,
)
_KNOWN_ATS = {
    "ashby", "lever", "workable", "greenhouse", "smartrecruiters", "workday",
    "icims", "successfactors", "oracle", "generic", "custom",
}
def clean_text(value: Any) -> str:
    """Return human text in a stable, single-line Unicode representation."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WS_RE.sub(" ", text).strip()


def normalize_label(value: Any) -> str:
    """Normalize a label for matching while retaining its semantic wording."""
    return _REQUIRED_SUFFIX_RE.sub("", clean_text(value)).strip(" :-")


def normalize_type(value: Any) -> str:
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", clean_text(value))
    raw = raw.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "string": "text", "input": "text", "tel": "phone", "telephone": "phone",
        "dropdown": "select", "single_select": "select", "radio": "choice",
        "radio_button": "choice", "radio_group": "choice",
        "checkbox": "multi_select", "checkbox_group": "multi_select",
        "multiselect": "multi_select", "multi_choice": "multi_select",
        "multi_select_list": "multi_select", "boolean": "choice",
        "long_text": "textarea", "rich_text": "textarea", "file_upload": "file",
    }
    return aliases.get(raw, raw or "text")


def _clean_options(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if isinstance(value, Mapping):
            value = value.get("label", value.get("name", value.get(
                "text", value.get("title", value.get("value", "")))))
        option = clean_text(value)
        key = option.casefold()
        if option and key not in seen:
            seen.add(key)
            out.append(option)
    return out


def question_fingerprint(question: Mapping[str, Any]) -> str:
    """Stable semantic ID, independent of DOM ids, selectors, and display order."""
    payload = {
        "label": normalize_label(question.get("label")).casefold(),
        "type": normalize_type(question.get("type")),
        "section": clean_text(question.get("section")).casefold(),
        # Options are deliberately excluded: providers reorder choices and add an
        # option over time, but that must remain the same longitudinal question.
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_question(question: Mapping[str, Any], *, order: int = 0,
                       source: str = "rendered_form") -> dict[str, Any] | None:
    """Normalize one provider/DOM question into the acquisition schema."""
    label = normalize_label(question.get("label") or question.get("displayName")
                            or question.get("question") or question.get("question_text")
                            or question.get("title") or question.get("prompt")
                            or question.get("text"))
    if not label or _HONEYPOT_LABEL_RE.search(label):
        return None
    options = _clean_options(question.get("options") or question.get("values")
                             or question.get("choices"))
    required = bool(question.get("required")) or bool(
        _REQUIRED_SUFFIX_RE.search(clean_text(question.get("label"))))
    normalized: dict[str, Any] = {
        "label": label,
        "type": normalize_type(question.get("type") or question.get("inputType")
                               or question.get("fieldType") or question.get("controlType")),
        "required": required,
        "options": options,
        "order": int(question.get("order", order)),
        "section": clean_text(question.get("section")),
        "source_question_id": clean_text(question.get("source_question_id")
                                         or question.get("id") or question.get("name")),
        "validation": dict(question.get("validation") or {}),
        "raw_evidence": dict(question.get("raw_evidence") or {
            "source": source, "payload": dict(question)}),
    }
    normalized["fingerprint"] = question_fingerprint(normalized)
    return normalized


def normalize_questions(questions: Iterable[Mapping[str, Any]], *,
                        source: str = "rendered_form") -> list[dict[str, Any]]:
    """Normalize and de-duplicate questions, keeping the richest first-seen record."""
    ordered: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for raw in questions:
        item = normalize_question(raw, order=len(ordered), source=source)
        if item is None:
            continue
        key = item["fingerprint"]
        if key not in positions:
            positions[key] = len(ordered)
            ordered.append(item)
            continue
        current = ordered[positions[key]]
        current["required"] = current["required"] or item["required"]
        current["options"] = _clean_options([*current["options"], *item["options"]])
        # Preserve all distinct DOM evidence for audit/debugging.
        evidence = current["raw_evidence"]
        if evidence != item["raw_evidence"]:
            variants = evidence.setdefault("duplicate_evidence", [])
            if item["raw_evidence"] not in variants:
                variants.append(item["raw_evidence"])
    for order, item in enumerate(ordered):
        item["order"] = order
    return ordered


def normalize_greenhouse_questions(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Greenhouse ``?questions=true`` JSON without requiring a browser."""
    raw_questions = payload.get("questions", []) if isinstance(payload, Mapping) else payload
    expanded: list[dict[str, Any]] = []
    for q_index, question in enumerate(raw_questions or []):
        fields = question.get("fields") or [{}]
        # A Greenhouse question can expose more than one real field.  Preserve each
        # field, suffixing its field label only when it differs from the parent label.
        for f_index, field in enumerate(fields):
            parent_label = clean_text(question.get("label"))
            field_label = clean_text(field.get("label"))
            label = field_label if field_label and field_label.casefold() != parent_label.casefold() else parent_label
            values = field.get("values") or question.get("values") or []
            expanded.append({
                "label": label,
                "type": field.get("type", question.get("type", "text")),
                "required": bool(question.get("required") or field.get("required")),
                "options": values,
                "source_question_id": field.get("name") or question.get("id"),
                "order": len(expanded),
                "section": question.get("section", ""),
                "raw_evidence": {
                    "source": "greenhouse_api", "question_index": q_index,
                    "field_index": f_index, "field_name": field.get("name", ""),
                    "question": dict(question),
                },
            })
    return normalize_questions(expanded, source="greenhouse_api")


def build_application_url(ats: str, url: str) -> str:
    """Build the read-only apply-form URL for a supported ATS."""
    ats_key = clean_text(ats).lower()
    if ats_key not in _KNOWN_ATS:
        ats_key = "generic"
    parts = urlsplit(clean_text(url))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("application URL must be an absolute http(s) URL")
    path = parts.path.rstrip("/") or "/"
    suffix = {"ashby": "/application", "lever": "/apply", "workable": "/apply",
              "workday": "/apply/applyManually"}.get(ats_key)
    if suffix and not path.lower().endswith(suffix):
        path = path.rstrip("/") + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


# One read-only DOM pass.  No value, checked state, or user-entered content is read.
# The evidence is intentionally bounded to attributes and a short HTML fragment.
_EXTRACT_JS = r"""() => {
 const clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const visible=el=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
 const textOnly=el=>{if(!el)return '';const c=el.cloneNode(true);c.querySelectorAll('input,select,textarea,button,option,svg').forEach(n=>n.remove());return clean(c.innerText||c.textContent);};
 const idText=ids=>(ids||'').split(/\s+/).map(id=>document.getElementById(id)).filter(Boolean).map(textOnly).filter(Boolean).join(' ');
 const labelFor=el=>{
   let s=idText(el.getAttribute('aria-labelledby'));if(s)return s;
   if(el.id){const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`);if(l&&(s=textOnly(l)))return s;}
   const wrap=el.closest('label');if(wrap&&(s=textOnly(wrap)))return s;
   if((s=clean(el.getAttribute('aria-label'))))return s;
   const c=el.closest('.ashby-application-form-field-entry,[class*="fieldEntry"],.application-question,[class*="application-question"],[data-automation-id*="question" i],[class*="form-field" i],[class*="field-container" i],fieldset,[role="group"],[role="radiogroup"],li');
   if(c){const l=c.querySelector('.ashby-application-form-question-title,.application-label,.field-label,.questionText,legend,[data-automation-id*="label" i],[class*="question-title"],[class*="QuestionTitle"],[class*="_heading_"],label');if(l)return textOnly(l);}
   return '';
 };
 const containerFor=el=>el.closest('.ashby-application-form-field-entry,[class*="fieldEntry"],.application-question,[class*="application-question"],[data-automation-id*="question" i],[class*="form-field" i],[class*="field-container" i],fieldset,[role="group"],[role="radiogroup"],li')||el.parentElement;
 const sectionFor=el=>{
   let n=containerFor(el);for(let i=0;i<6&&n;i++,n=n.parentElement){
     const own=n.querySelector(':scope > legend,:scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > [data-section-title]');
     if(own){const t=textOnly(own);if(t&&t!==labelFor(el))return t;}
   } return '';
 };
 const selectorFor=el=>el.id?`#${CSS.escape(el.id)}`:(el.name?`${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`:el.tagName.toLowerCase());
 const evidenceFor=el=>({source:'rendered_form',tag:el.tagName.toLowerCase(),input_type:el.type||'',id:el.id||'',name:el.name||'',role:el.getAttribute('role')||'',aria_labelledby:el.getAttribute('aria-labelledby')||'',selector:selectorFor(el),html:(el.outerHTML||'').slice(0,1000)});
 const validationFor=el=>{const out={};for(const name of ['min','max','minlength','maxlength','pattern','step','accept']){const value=el.getAttribute(name);if(value!==null&&value!=='')out[name]=value;}if(el.type==='email'||el.type==='url'||el.type==='number'||el.type==='date')out.format=el.type;return out;};
 const optionLabel=el=>{const w=el.closest('label');if(w){const t=textOnly(w);if(t)return t;}if(el.id){const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`);if(l)return textOnly(l);}return clean(el.getAttribute('aria-label')||el.value);};
 const controls=[...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]),select,textarea,[role=combobox],[role=textbox],[contenteditable=true]')].filter(el=>visible(el)||el.type==='file'||((el.type==='radio'||el.type==='checkbox')&&visible(containerFor(el))));
 const out=[], consumed=new Set();
 const groups=new Map();
 controls.filter(el=>el.matches('input[type=radio],input[type=checkbox]')).forEach(el=>{
   const c=containerFor(el);const key=el.type==='radio'&&el.name?`radio:${el.name}`:c;
   if(!groups.has(key))groups.set(key,{el,label:labelFor(el),options:[],required:false,type:el.type==='checkbox'?'multi_select':'choice',members:[]});
   const g=groups.get(key);const o=optionLabel(el);if(o&&!g.options.includes(o))g.options.push(o);g.required=g.required||el.required||el.getAttribute('aria-required')==='true';g.members.push(el);
 });
 groups.forEach(g=>{g.members.forEach(el=>consumed.add(el));out.push({label:g.label,type:g.type,required:g.required,options:g.options,section:sectionFor(g.el),validation:validationFor(g.el),raw_evidence:{...evidenceFor(g.el),group_size:g.members.length}});});
 controls.filter(el=>!consumed.has(el)).forEach(el=>{
   const tag=el.tagName.toLowerCase();const role=el.getAttribute('role');
   const type=tag==='select'||role==='combobox'?'select':tag==='textarea'||el.isContentEditable?'textarea':(el.type||'text');
   const options=tag==='select'?[...el.options].map(o=>clean(o.textContent)).filter(Boolean):[];
   out.push({label:labelFor(el),type,required:!!(el.required||el.getAttribute('aria-required')==='true'),options,section:sectionFor(el),validation:validationFor(el),raw_evidence:evidenceFor(el)});
 });
 // Ashby and custom forms sometimes implement choices as buttons only.
 document.querySelectorAll('.ashby-application-form-field-entry,[class*="fieldEntry"],[data-automation-id*="question" i],[class*="form-field" i],[role=radiogroup]').forEach(c=>{
   const buttons=[...c.querySelectorAll('button')].filter(visible).map(b=>clean(b.textContent)).filter(t=>t&&!/^(submit|apply|next|back|upload)/i.test(t));
   if(buttons.length<2)return;const anchor=c.querySelector('button');const label=labelFor(anchor);if(label)out.push({label,type:'choice',required:c.getAttribute('aria-required')==='true',options:[...new Set(buttons)],section:sectionFor(anchor),raw_evidence:{...evidenceFor(anchor),group_size:buttons.length}});
 });
 const unlabeled=out.filter(q=>!clean(q.label)).length;
 const actionable=[...document.querySelectorAll('button,input[type=button],input[type=submit]')].filter(visible);
 const nextCount=actionable.filter(el=>/^(next|continue|save and continue)$/i.test(clean(el.innerText||el.value))).length;
 const finalCount=actionable.filter(el=>/^(submit|submit application|apply|finish|send application|complete application)$/i.test(clean(el.innerText||el.value))).length;
 const bodyText=(document.body.innerText||'').slice(0,10000);
 const challenge=!!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],[class*="captcha" i]')||/captcha|verify you are human|access denied/i.test(document.title+' '+bodyText.slice(0,2000));
 const providerMultistep=/step\s+\d+\s+(?:of|\/)+\s*\d+/i.test(bodyText)||!!document.querySelector('[aria-current=step],[role=progressbar],[data-automation-id*="progress" i]');
 const passwordVisible=[...document.querySelectorAll('input[type=password]')].some(visible);
 const accountGate=passwordVisible||!!document.querySelector('[data-automation-id*="createAccount" i],[class*="create-account" i]')||/(?:sign|log)\s+in\s+(?:to|and)\s+(?:apply|continue)|create\s+(?:an?\s+)?account\s+(?:to|and)\s+(?:apply|continue)/i.test(bodyText);
 const consentControl=controls.some(el=>el.type==='checkbox'&&(/consent|privacy|terms|data processing|agree/i.test(labelFor(el))));
 const consentGate=consentControl||/(?:must|required to)\s+(?:agree|consent|accept)|consent\s+(?:is\s+)?required/i.test(bodyText);
 const reviewBoundary=/(?:review|check)\s+(?:your\s+)?application/i.test(bodyText)||!!document.querySelector('[data-automation-id*="review" i],[aria-current=step][data-automation-id*="review" i]');
 return {questions:out, evidence:{url:location.href,title:document.title,form_count:document.forms.length,visible_control_count:controls.length,unlabeled_control_count:unlabeled,submit_control_count:document.querySelectorAll('button[type=submit],input[type=submit]').length,next_control_count:nextCount,final_action_control_count:finalCount,review_boundary_detected:reviewBoundary,challenge_detected:challenge,provider_multistep:providerMultistep,account_gate_detected:accountGate,consent_gate_detected:consentGate}};
}"""


# The only mutating DOM action available to this collector is a bounded click on
# an unambiguously labelled navigation control. A capture-phase guard blocks real
# HTML form submission, including requestSubmit()/submit(), and final-action labels
# are an explicit stop boundary. No value is read, synthesized, or entered.
_SAFE_NEXT_JS = r"""() => {
 const clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const visible=el=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
 const label=el=>clean(el.innerText||el.value||el.getAttribute('aria-label'));
 const finalRe=/^(submit|submit application|apply|finish|send application|complete application)$/i;
 const nextRe=/^(next|continue|save and continue)$/i;
 if(!window.__questionCollectorSubmitGuard){
   window.__questionCollectorSubmitGuard={blocked:0};
   document.addEventListener('submit',event=>{window.__questionCollectorSubmitGuard.blocked++;event.preventDefault();event.stopImmediatePropagation();},true);
   const nativeSubmit=HTMLFormElement.prototype.submit;
   const nativeRequestSubmit=HTMLFormElement.prototype.requestSubmit;
   HTMLFormElement.prototype.submit=function(){window.__questionCollectorSubmitGuard.blocked++;};
   if(nativeRequestSubmit)HTMLFormElement.prototype.requestSubmit=function(){window.__questionCollectorSubmitGuard.blocked++;};
   window.__questionCollectorSubmitGuard.restore=()=>{HTMLFormElement.prototype.submit=nativeSubmit;if(nativeRequestSubmit)HTMLFormElement.prototype.requestSubmit=nativeRequestSubmit;};
 }
 const controls=[...document.querySelectorAll('button,input[type=button],input[type=submit],[role=button]')].filter(visible);
 if(controls.some(el=>finalRe.test(label(el))))return {status:'boundary',reason:'final_action_control'};
 if(/(?:review|check)\s+(?:your\s+)?application/i.test((document.body.innerText||'').slice(0,10000)))return {status:'boundary',reason:'review_step'};
 const candidates=controls.filter(el=>nextRe.test(label(el)));
 if(!candidates.length)return {status:'not_found'};
 const enabled=candidates.filter(el=>!el.disabled&&el.getAttribute('aria-disabled')!=='true');
 if(!enabled.length)return {status:'blocked',reason:'navigation_control_disabled'};
 const preferred=enabled.find(el=>/bottom-navigation-next-button|navigation-next/i.test(el.getAttribute('data-automation-id')||''))||enabled[0];
 const actionLabel=label(preferred);
 preferred.click();
 return {status:'clicked',label:actionLabel,submit_guard_blocks:window.__questionCollectorSubmitGuard.blocked||0};
}"""


def _snapshot_signature(snapshot: Mapping[str, Any]) -> str:
    questions = normalize_questions(snapshot.get("questions") or [])
    evidence = dict(snapshot.get("evidence") or {})
    return hashlib.sha256(json.dumps(
        {
            "questions": [(q["fingerprint"], q["required"], q["options"])
                          for q in questions],
            # A wizard may repeat a field on its review step. Boundary/gate state
            # distinguishes that legitimate transition from a stuck Next button.
            "navigation": [
                int(evidence.get("next_control_count") or 0),
                int(evidence.get("final_action_control_count") or 0),
                bool(evidence.get("review_boundary_detected")),
                bool(evidence.get("account_gate_detected")),
                bool(evidence.get("consent_gate_detected")),
            ],
        },
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _merge_frame_snapshots(snapshots: Sequence[tuple[int, str, Mapping[str, Any]]],
                           *, frame_errors: int = 0) -> dict[str, Any]:
    """Merge bounded, read-only snapshots from a page and its application frames."""
    questions: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "frame_count": len(snapshots) + frame_errors,
        "frames_read": len(snapshots),
        "frame_error_count": frame_errors,
        "frame_urls": [],
    }
    count_fields = (
        "form_count", "visible_control_count", "unlabeled_control_count",
        "submit_control_count", "next_control_count", "final_action_control_count",
    )
    bool_fields = (
        "challenge_detected", "provider_multistep", "account_gate_detected",
        "consent_gate_detected", "review_boundary_detected",
    )
    for frame_index, frame_url, snapshot in snapshots:
        frame_evidence = dict(snapshot.get("evidence") or {})
        actual_url = clean_text(frame_evidence.get("url") or frame_url)
        if actual_url and actual_url not in evidence["frame_urls"]:
            evidence["frame_urls"].append(actual_url)
        evidence.setdefault("url", actual_url)
        evidence.setdefault("title", clean_text(frame_evidence.get("title")))
        for field in count_fields:
            evidence[field] = int(evidence.get(field) or 0) + int(
                frame_evidence.get(field) or 0)
        for field in bool_fields:
            evidence[field] = bool(evidence.get(field) or frame_evidence.get(field))
        for raw in snapshot.get("questions") or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            raw_evidence = dict(item.get("raw_evidence") or {})
            raw_evidence.update({"frame_index": frame_index, "frame_url": actual_url})
            item["raw_evidence"] = raw_evidence
            questions.append(item)
    return {"questions": questions, "evidence": evidence}


def _evaluate_all_frames(page: Any) -> dict[str, Any]:
    """Evaluate the extraction pass in every readable frame, without DOM mutation."""
    page_frames = getattr(page, "frames", None)
    frames = list(page_frames) if isinstance(page_frames, Sequence) else [page]
    if not frames:
        frames = [page]
    snapshots: list[tuple[int, str, Mapping[str, Any]]] = []
    errors = 0
    last_error: Exception | None = None
    for frame_index, frame in enumerate(frames[:20]):
        try:
            snapshot = frame.evaluate(_EXTRACT_JS) or {"questions": [], "evidence": {}}
            if not isinstance(snapshot, Mapping):
                raise TypeError("frame snapshot is not an object")
            snapshots.append((frame_index, clean_text(getattr(frame, "url", "")), snapshot))
        except Exception as exc:
            errors += 1
            last_error = exc
    if not snapshots:
        raise RuntimeError("all frame extractions failed") from last_error
    return _merge_frame_snapshots(snapshots, frame_errors=errors)


def _poll_hydration(page: Any, *, cap_s: float = 12.0, interval_s: float = 0.5,
                    sleep: Callable[[float], None] | None = None,
                    clock: Callable[[], float] = time.monotonic) -> tuple[dict[str, Any], bool, int]:
    """Return the richest snapshot and whether two consecutive reads matched."""
    sleep = sleep or (lambda seconds: page.wait_for_timeout(int(seconds * 1000)))
    started = clock()
    best: dict[str, Any] = {"questions": [], "evidence": {}}
    previous = ""
    reads = 0
    while True:
        snapshot = _evaluate_all_frames(page)
        reads += 1
        if len(snapshot.get("questions") or []) >= len(best.get("questions") or []):
            best = snapshot
        signature = _snapshot_signature(snapshot)
        if snapshot.get("questions") and signature == previous:
            return best, True, reads
        previous = signature
        if clock() - started >= cap_s:
            return best, False, reads
        sleep(interval_s)


def _safe_next(page: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Click one enabled Next/Continue control, never a final action."""
    page_frames = getattr(page, "frames", None)
    frames = list(page_frames) if isinstance(page_frames, Sequence) else [page]
    question_frame_indexes = sorted({
        raw.get("raw_evidence", {}).get("frame_index")
        for raw in snapshot.get("questions") or [] if isinstance(raw, Mapping)
        and isinstance(raw.get("raw_evidence"), Mapping)
        and isinstance(raw.get("raw_evidence", {}).get("frame_index"), int)
    })
    if question_frame_indexes:
        frames = [frames[index] for index in question_frame_indexes
                  if 0 <= index < len(frames)]
    blocked: dict[str, Any] | None = None
    for frame in frames[:20] or [page]:
        try:
            result = frame.evaluate(_SAFE_NEXT_JS)
        except Exception:
            continue
        if not isinstance(result, Mapping):
            continue
        status = clean_text(result.get("status"))
        if status in {"clicked", "boundary"}:
            return dict(result)
        if status == "blocked":
            blocked = dict(result)
    return blocked or {"status": "not_found"}


def _tag_step(snapshot: Mapping[str, Any], step: int) -> dict[str, Any]:
    tagged = {"questions": [], "evidence": dict(snapshot.get("evidence") or {})}
    for raw in snapshot.get("questions") or []:
        if not isinstance(raw, Mapping):
            continue
        question = dict(raw)
        raw_evidence = dict(question.get("raw_evidence") or {})
        raw_evidence["application_step"] = step
        question["raw_evidence"] = raw_evidence
        tagged["questions"].append(question)
    return tagged


def _traverse_application_steps(page: Any, initial: Mapping[str, Any], *,
                                cap_s: float, interval_s: float,
                                sleep: Callable[[float], None] | None,
                                clock: Callable[[], float],
                                max_steps: int = 12) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect reachable wizard steps without entering data or crossing final action."""
    snapshots = [_tag_step(initial, 0)]
    signatures = {_snapshot_signature(initial)}
    clicks = 0
    reads = 0
    status = "not_started"
    stop_reason = ""
    for step in range(1, max(1, max_steps) + 1):
        current_evidence = snapshots[-1]["evidence"]
        if current_evidence.get("challenge_detected"):
            status, stop_reason = "blocked", "anti_bot_challenge_detected"
            break
        if current_evidence.get("account_gate_detected"):
            status, stop_reason = "blocked", "account_gate_not_traversed"
            break
        if current_evidence.get("consent_gate_detected"):
            status, stop_reason = "blocked", "consent_gate_not_accepted"
            break
        if current_evidence.get("review_boundary_detected") or int(
                current_evidence.get("final_action_control_count") or 0):
            status, stop_reason = "boundary_reached", "review_or_submit_boundary"
            break
        action = _safe_next(page, snapshots[-1])
        action_status = clean_text(action.get("status"))
        if action_status == "boundary":
            status, stop_reason = "boundary_reached", clean_text(action.get("reason"))
            break
        if action_status == "blocked":
            status, stop_reason = "blocked", clean_text(action.get("reason"))
            break
        if action_status != "clicked":
            status, stop_reason = "blocked", "navigation_control_not_found"
            break
        clicks += 1
        try:
            next_snapshot, _stable, step_reads = _poll_hydration(
                page, cap_s=min(cap_s, 6.0), interval_s=interval_s,
                sleep=sleep, clock=clock)
        except Exception as exc:
            status, stop_reason = "blocked", f"step_extraction_failed:{type(exc).__name__}"
            break
        reads += step_reads
        signature = _snapshot_signature(next_snapshot)
        if signature in signatures:
            status, stop_reason = "blocked", "navigation_made_no_progress"
            break
        signatures.add(signature)
        snapshots.append(_tag_step(next_snapshot, step))
    else:
        status, stop_reason = "blocked", "step_limit_reached"
    return snapshots, {
        "status": status,
        "stop_reason": stop_reason,
        "navigation_controls_clicked": clicks,
        "additional_hydration_reads": reads,
        "steps_captured": len(snapshots),
        "max_steps": max_steps,
    }


def _result(ats: str, source_url: str, form_url: str, state: ScrapeState,
            questions: list[dict[str, Any]], reasons: list[str],
            evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    scrape_evidence = dict(evidence or {})
    # Machine-auditable safety invariant: no field-fill or final-submit path exists.
    scrape_evidence["form_submission_attempted"] = False
    scrape_evidence.setdefault("action_controls_clicked", 0)
    result = {
        "ats": clean_text(ats).lower() or "generic",
        "source_url": source_url,
        "form_url": form_url,
        "scrape_state": state,
        "questions": questions,
        "question_count": len(questions),
        "reasons": reasons,
        "scrape_evidence": scrape_evidence,
    }
    # Short aliases make the contract convenient for queue/CLI callers while the
    # more descriptive names remain self-documenting in stored acquisition JSON.
    result["state"] = state
    result["error"] = ";".join(reasons) if state == "failed" else None
    return result


def _harvest_combobox_options(page: Any, raw_questions: list[dict[str, Any]], *,
                              cap_s: float = 12.0,
                              clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Open rendered comboboxes and read visible options without entering a value."""
    unresolved: list[str] = []
    deadline = clock() + max(0.0, cap_s)
    for question in raw_questions:
        evidence = question.get("raw_evidence") or {}
        if normalize_type(question.get("type")) != "select" or question.get("options"):
            continue
        if evidence.get("role") != "combobox":
            continue
        selector = evidence.get("selector")
        options: list[str] = []
        if clock() >= deadline:
            unresolved.append(normalize_label(question.get("label")))
            evidence["combobox_opened"] = False
            evidence["option_count_captured"] = 0
            continue
        try:
            target = page
            page_frames = getattr(page, "frames", None)
            frame_index = evidence.get("frame_index")
            if isinstance(page_frames, Sequence) and isinstance(frame_index, int) \
                    and 0 <= frame_index < len(page_frames):
                target = page_frames[frame_index]
            box = target.query_selector(selector)
            if box is None:
                raise LookupError("combobox disappeared")
            # Playwright's default click wait is 30 seconds per control.  A
            # provider can expose dozens of decorative/inert comboboxes, so use
            # a short per-control wait plus a total budget for the whole form.
            remaining_ms = max(100, min(1_500, int((deadline - clock()) * 1000)))
            box.click(timeout=remaining_ms)
            page.wait_for_timeout(250)
            for node in target.query_selector_all(
                    "[role='listbox'] [role='option'], [role='option']")[:100]:
                text = clean_text(node.inner_text())
                if text and text not in options:
                    options.append(text)
            page.keyboard.press("Escape")
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        evidence["combobox_opened"] = True
        evidence["option_count_captured"] = len(options)
        if options:
            question["options"] = options
        else:
            unresolved.append(normalize_label(question.get("label")))
    return unresolved


def scrape_questions_with_page(page: Any, ats: str, url: str, *,
                               cap_s: float = 12.0, interval_s: float = 0.5,
                               sleep: Callable[[float], None] | None = None,
                               clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Scrape with an injected Playwright-like page (also useful for deterministic tests)."""
    try:
        form_url = build_application_url(ats, url)
    except Exception as exc:
        return _result(ats, url, "", "failed", [], [f"invalid_url:{type(exc).__name__}"])
    navigation_error = ""
    try:
        # ATS pages commonly keep analytics/streaming requests open forever.
        # DOM readiness plus the bounded hydration poll below is both faster and
        # more reliable than waiting for Playwright's network-idle heuristic.
        page.goto(form_url, wait_until="domcontentloaded", timeout=12_000)
    except Exception as exc:
        navigation_error = type(exc).__name__
        try:
            page.goto(form_url, wait_until="commit", timeout=5_000)
        except Exception as fallback_exc:
            return _result(ats, url, form_url, "failed", [], [
                f"navigation_failed:{navigation_error}",
                f"navigation_fallback_failed:{type(fallback_exc).__name__}",
            ])
    try:
        snapshot, stable, reads = _poll_hydration(
            page, cap_s=cap_s, interval_s=interval_s, sleep=sleep, clock=clock)
    except Exception as exc:
        return _result(ats, url, form_url, "failed", [],
                       [f"extraction_failed:{type(exc).__name__}"])
    traversal: dict[str, Any] | None = None
    step_snapshots = [_tag_step(snapshot, 0)]
    initial_evidence = dict(snapshot.get("evidence") or {})
    if int(initial_evidence.get("next_control_count") or 0) or initial_evidence.get(
            "provider_multistep"):
        step_snapshots, traversal = _traverse_application_steps(
            page, snapshot, cap_s=cap_s, interval_s=interval_s,
            sleep=sleep, clock=clock)
    raw_questions = [question for step_snapshot in step_snapshots
                     for question in step_snapshot.get("questions") or []]
    unresolved_combos = _harvest_combobox_options(page, raw_questions)
    questions = normalize_questions(raw_questions)
    evidence = dict(initial_evidence)
    if len(step_snapshots) > 1:
        final_evidence = dict(step_snapshots[-1].get("evidence") or {})
        evidence.update(final_evidence)
        for flag in ("challenge_detected", "provider_multistep", "account_gate_detected",
                     "consent_gate_detected", "review_boundary_detected"):
            evidence[flag] = any(bool(step.get("evidence", {}).get(flag))
                                 for step in step_snapshots)
    evidence.update({"hydration_stable": stable, "hydration_reads": reads})
    if traversal is not None:
        evidence["step_traversal"] = traversal
        evidence["action_controls_clicked"] = traversal["navigation_controls_clicked"]
    if clean_text(ats).lower() == "workday":
        evidence["direct_apply_url_used"] = True
    reasons: list[str] = []
    if navigation_error:
        reasons.append(f"domcontentloaded_failed:{navigation_error}")
    if not questions:
        reasons.append("no_labelled_questions_detected")
    if not stable:
        reasons.append("hydration_not_stable")
    if int(evidence.get("unlabeled_control_count") or 0):
        reasons.append("unlabelled_controls_present")
    if unresolved_combos:
        reasons.append("combobox_options_unavailable")
        evidence["comboboxes_without_options"] = unresolved_combos
    if traversal is not None and traversal.get("status") != "boundary_reached":
        stop_reason = clean_text(traversal.get("stop_reason"))
        if stop_reason == "navigation_control_disabled":
            reasons.append("multi_step_navigation_blocked_without_input")
        elif stop_reason == "navigation_made_no_progress":
            reasons.append("multi_step_navigation_no_progress")
        elif stop_reason == "navigation_control_not_found":
            reasons.append("multi_step_navigation_unavailable")
        elif stop_reason not in {"account_gate_not_traversed", "consent_gate_not_accepted",
                                "anti_bot_challenge_detected"}:
            reasons.append(f"multi_step_traversal_stopped:{stop_reason or 'unknown'}")
    if evidence.get("account_gate_detected"):
        reasons.append("account_gate_not_traversed")
    if evidence.get("consent_gate_detected"):
        reasons.append("consent_gate_not_accepted")
    if int(evidence.get("frame_error_count") or 0):
        reasons.append("embedded_frame_unreadable")
    if evidence.get("challenge_detected"):
        reasons.append("anti_bot_challenge_detected")
    gate_reasons = [reason for reason in reasons if reason in {
        "multi_step_navigation_blocked_without_input", "multi_step_navigation_no_progress",
        "multi_step_navigation_unavailable", "account_gate_not_traversed",
        "consent_gate_not_accepted", "embedded_frame_unreadable",
        "anti_bot_challenge_detected",
    } or reason.startswith("multi_step_traversal_stopped:")]
    if gate_reasons:
        evidence["coverage_scope"] = "visible_steps_only"
        evidence["gate_reasons"] = gate_reasons
    elif traversal is not None and traversal.get("status") == "boundary_reached":
        evidence["coverage_scope"] = "all_reachable_steps_to_review_boundary"
    visible = int(evidence.get("visible_control_count") or 0)
    if visible > len(questions) and not evidence.get("unlabeled_control_count"):
        # Groups legitimately collapse several controls into one question, so this is
        # audit evidence only; do not mark a radio-heavy form partial for that reason.
        evidence["controls_grouped_or_deduplicated"] = visible - len(questions)
    partial_reasons = {
        "unlabelled_controls_present", "hydration_not_stable",
        "combobox_options_unavailable", "multi_step_navigation_blocked_without_input",
        "multi_step_navigation_no_progress", "multi_step_navigation_unavailable",
        "account_gate_not_traversed", "consent_gate_not_accepted",
        "embedded_frame_unreadable", "anti_bot_challenge_detected",
    }
    has_partial_reason = any(
        reason in partial_reasons or reason.startswith("multi_step_traversal_stopped:")
        for reason in reasons)
    state: ScrapeState = "complete" if questions and stable and not has_partial_reason \
        else "partial"
    return _result(ats, url, form_url, state, questions, reasons, evidence)


def scrape_application_questions(ats: str, url: str, *, headless: bool = True) -> dict[str, Any]:
    """Launch Chromium and capture application questions; never fill or submit."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            try:
                return scrape_questions_with_page(page, ats, url)
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        form_url = ""
        try:
            form_url = build_application_url(ats, url)
        except Exception:
            pass
        return _result(ats, url, form_url, "failed", [],
                       [f"browser_failed:{type(exc).__name__}"])


def scrape_questions(ats: str, apply_url: str, *, page: Any | None = None,
                     headless: bool = True) -> dict[str, Any]:
    """Public integration entrypoint.

    Returns a dict containing ``state``/``scrape_state``, ``questions``, ``error``,
    ``reasons``, and URL/evidence metadata.  Supplying ``page`` reuses an existing
    Playwright page; otherwise an isolated Chromium context is created.
    """
    if page is not None:
        return scrape_questions_with_page(page, ats, apply_url)
    return scrape_application_questions(ats, apply_url, headless=headless)
