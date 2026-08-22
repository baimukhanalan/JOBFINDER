"""Review surface for the pre-generated application drafts (job_catalog.draft).

Server-rendered, reuses the mail-CRM shell. Two views:
  * /drafts            — every job that has a generated draft, with a fill bar
  * /drafts/{id}       — the full packet: each question, its filled value, and a
                         badge for HOW it was filled (profile / choice / draft /
                         file / human), plus review flags and the résumé PDF.

Neutral, stack-agnostic labels only (no engine/vendor names) — this is an internal
review page but the branding rule still holds.
"""
from __future__ import annotations

import html
import io
import re

from backend.tools import catalog_db
from backend.tools import mailcrm_ui

esc = html.escape

# Some scraped labels (lever esp.) carry newlines + a required "✱"/"*" marker and
# helper text ("Resume/CV\n✱\nATTACH RESUME/CV"). Collapse to one clean line.
_LBL_WS = re.compile(r"\s+")


def _clean_label(label: str) -> str:
    s = _LBL_WS.sub(" ", (label or "").replace("✱", " ").replace("＊", " ")).strip()
    return s.strip(" *").strip()

# source -> (badge label, css modifier). Deliberately neutral wording.
_SRC = {
    "identity": ("профиль", "s-id"),
    "choice": ("выбор", "s-ch"),
    "llm": ("черновик", "s-dr"),
    "ideal": ("ответ", "s-dr"),
    "file": ("файл", "s-fi"),
    "human": ("вручную", "s-hu"),
    "none": ("вручную", "s-hu"),
}


def _bar(filled: int, review: int, human: int, total: int) -> str:
    total = max(total, 1)
    good = filled - review if filled >= review else 0
    fp = round(good / total * 100)
    rp = round(review / total * 100)
    hp = max(0, 100 - fp - rp)
    return (f'<div class="dfbar" title="{filled}/{total} заполнено, {review} на проверку, '
            f'{human} вручную"><span style="width:{fp}%"></span>'
            f'<span class="rev" style="width:{rp}%"></span>'
            f'<span class="hum" style="width:{hp}%"></span></div>')


# ---- index ------------------------------------------------------------------
def _index_card(d: dict) -> str:
    st = d.get("stats") or {}
    cand = d.get("candidate") or {}
    total = int(st.get("total") or 0)
    filled = int(st.get("filled") or 0)
    review = int(st.get("needs_review") or 0)
    human = int(st.get("human") or 0)
    regions = " ".join(esc(r) for r in (d.get("regions") or []))
    title = esc(d.get("title") or "(без названия)")
    company = esc(d.get("company") or "")
    ms = st.get("match_score")
    mscore = f'<span class="dfscore">fit {int(ms)}%</span>' if ms is not None else ""
    return (
        f'<a class="dfcard" href="/drafts/{d["id"]}">'
        f'<div class="dftop"><span class="dfco">{company}</span>'
        f'<span class="dfats">{esc(d.get("ats") or "")} · {regions}</span></div>'
        f'<div class="dftitle">{title}</div>'
        f'<div class="dfcand">👤 {esc(cand.get("name") or "?")} '
        f'<span class="dfcc">{esc(cand.get("country") or "")}</span>{mscore}</div>'
        f'{_bar(filled, review, human, total)}'
        f'<div class="dfnums"><b>{filled}/{total}</b> заполнено'
        + (f' · <span class="rev">{review} на проверку</span>' if review else "")
        + (f' · <span class="hum">{human} вручную</span>' if human else "")
        + "</div></a>")


def render_index(q: str = "", limit: int = 300) -> str:
    q = (q or "").strip()
    drafts = catalog_db.list_drafts(q=q or None, limit=limit, offset=0)
    total = catalog_db.drafts_count()
    cards = "".join(_index_card(d) for d in drafts)
    empty = ('<div class="empty">Черновики ещё не сгенерированы</div>'
             if not drafts else "")
    search = (
        '<form class="cat-search" method="get" action="/drafts">'
        f'<input type="search" name="q" value="{esc(q)}" '
        'placeholder="Поиск: должность, компания…">'
        '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/drafts">Сброс</a>' if q else "")
        + "</form>")
    head = (
        '<div class="cat-head"><div class="cat-h-row">'
        f'<div class="cat-h-title">Черновики заявок <span class="cat-h-n">{total}</span></div>'
        '</div>'
        '<div class="dfhint">Предзаполнено под каждую вакансию: резюме + ответы на все '
        'вопросы. Проверь, что и где не заполнилось.</div>'
        f'{search}</div>')
    body = _CSS + head + f'<div class="dflist">{cards}</div>{empty}'
    return mailcrm_ui._page("catalog", body)


# ---- detail -----------------------------------------------------------------
def _answer_row(a: dict) -> str:
    if not a:
        return ""
    src = a.get("source") or "none"
    label = esc(_clean_label(a.get("label")) or "(без текста)")
    req = '<span class="dreq" title="обязательный">*</span>' if a.get("required") else ""
    badge_txt, badge_cls = _SRC.get(src, ("вручную", "s-hu"))
    rev = '<span class="dbadge s-rev">на проверку</span>' if a.get("needs_review") else ""
    note = f'<span class="dnote">{esc(a["note"])}</span>' if a.get("note") else ""
    qtype = esc(a.get("type") or "")
    val = a.get("value")
    if isinstance(val, list):
        val = ", ".join(str(x) for x in val)
    val = (val or "").strip()
    if val:
        vhtml = f'<div class="dval">{esc(val)}</div>'
    else:
        vhtml = '<div class="dval empty">— пусто, заполнит человек</div>'
    status_cls = "d-ok" if a.get("status") == "filled" else "d-hu"
    return (
        f'<div class="drow {status_cls}">'
        f'<div class="dhead"><span class="dlabel">{label}{req}</span>'
        f'<span class="dbadges">{rev}<span class="dbadge {badge_cls}">{badge_txt}</span>'
        + (f'<span class="dtype">{qtype}</span>' if qtype else "")
        + f'</span></div>{vhtml}{note}</div>')


def render_detail(job_id: int) -> str | None:
    job = catalog_db.get_job(job_id)
    if not job or not job.get("draft"):
        return None
    d = job["draft"]
    st = d.get("stats") or {}
    cand = d.get("candidate") or {}
    answers = d.get("answers") or []
    total = int(st.get("total") or 0)
    filled = int(st.get("filled") or 0)
    review = int(st.get("needs_review") or 0)
    human = int(st.get("human") or 0)
    ms = st.get("match_score")
    ats = (st.get("ats_score") or {}).get("score") if isinstance(st.get("ats_score"), dict) else None

    url = (job.get("url") or "").strip()
    open_link = (f'<a class="ghost" href="{esc(url)}" target="_blank" rel="noopener">Вакансия ↗</a>'
                 if url else "")
    resume_link = f'<a class="ghost" href="/drafts/{job_id}/resume.pdf" target="_blank">Резюме (PDF)</a>'

    scores = []
    if ms is not None:
        scores.append(f'<span class="dfscore">соответствие {int(ms)}%</span>')
    if ats is not None:
        scores.append(f'<span class="dfscore">ATS {int(ats)}%</span>')

    rows = "".join(_answer_row(a) for a in answers)
    cover = (d.get("cover_letter") or "").strip()
    cover_block = (
        '<details class="cat-det"><summary>Сопроводительное письмо</summary>'
        f'<div class="dcover">{esc(cover)}</div></details>') if cover else ""

    head = (
        '<div class="dhdr">'
        f'<a class="dback" href="/drafts">← Все черновики</a>'
        f'<div class="cat-h-title">{esc(job.get("title") or "")}</div>'
        f'<div class="dsub">{esc(job.get("company") or "")} · '
        f'{esc(job.get("ats") or "")} · {" ".join(esc(r) for r in (job.get("regions") or []))}</div>'
        f'<div class="dcandbox">Подаём как: <b>{esc(cand.get("name") or "?")}</b> '
        f'<span class="dcc">{esc(cand.get("country") or "")} · {esc(cand.get("work_authorization") or "")}</span></div>'
        f'{_bar(filled, review, human, total)}'
        f'<div class="dfnums"><b>{filled}/{total}</b> заполнено'
        + (f' · <span class="rev">{review} на проверку</span>' if review else "")
        + (f' · <span class="hum">{human} вручную</span>' if human else "")
        + "".join(f' · {s}' for s in scores)
        + '</div>'
        f'<div class="dactions">{resume_link}{open_link}</div>'
        '</div>')
    body = _CSS + head + cover_block + f'<div class="dansw">{rows}</div>'
    return mailcrm_ui._page("catalog", body)


# ---- résumé PDF (reportlab) -------------------------------------------------
def resume_pdf(job_id: int) -> bytes | None:
    job = catalog_db.get_job(job_id)
    if not job or not job.get("draft"):
        return None
    resume = (job["draft"] or {}).get("resume") or {}
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                    Spacer)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch, leftMargin=0.7 * inch,
                            rightMargin=0.7 * inch, title="Resume")
    ss = getSampleStyleSheet()
    name_s = ParagraphStyle("nm", parent=ss["Title"], fontSize=18, spaceAfter=2,
                            alignment=TA_LEFT)
    muted_s = ParagraphStyle("mut", parent=ss["Normal"], fontSize=9.5, textColor="#444444")
    h2_s = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=10,
                          spaceAfter=3, textColor="#111111")
    body_s = ParagraphStyle("bd", parent=ss["Normal"], fontSize=10, leading=13.5)
    role_s = ParagraphStyle("rl", parent=ss["Normal"], fontSize=10, leading=13,
                            spaceBefore=4)
    bul_s = ParagraphStyle("bu", parent=ss["Normal"], fontSize=9.5, leading=12.5,
                           leftIndent=12, bulletIndent=2)

    def P(t, s):
        return Paragraph(esc(str(t or "")), s)

    pi = resume.get("personal_info", {})
    flow = [P(pi.get("full_name", ""), name_s)]
    if resume.get("headline"):
        flow.append(P(resume["headline"], muted_s))
    if resume.get("eligibility"):
        flow.append(P("<b>" + esc(resume["eligibility"]) + "</b>", muted_s))
    contact = "  |  ".join(x for x in (pi.get("email", ""), pi.get("phone", ""),
                                       pi.get("location", ""), pi.get("linkedin", "")) if x)
    if contact:
        flow.append(P(contact, muted_s))
    flow.append(Spacer(1, 4))
    flow.append(HRFlowable(width="100%", thickness=0.6, color="#999999"))

    if resume.get("summary"):
        flow += [P("SUMMARY", h2_s), P(resume["summary"], body_s)]
    if resume.get("experience"):
        flow.append(P("EXPERIENCE", h2_s))
        for e in resume["experience"]:
            head = f"<b>{esc(e.get('title',''))}</b> — {esc(e.get('company',''))}"
            if e.get("dates"):
                head += f"  <font color='#666666'>({esc(e['dates'])})</font>"
            flow.append(Paragraph(head, role_s))
            for b in e.get("bullets", []):
                flow.append(Paragraph("• " + esc(b), bul_s))
    if resume.get("skills_grouped"):
        flow.append(P("SKILLS", h2_s))
        for grp, items in resume["skills_grouped"].items():
            flow.append(Paragraph(f"<b>{esc(grp)}:</b> {esc(', '.join(items))}", body_s))
    if resume.get("certifications"):
        flow.append(P("CERTIFICATIONS", h2_s))
        for c in resume["certifications"]:
            flow.append(Paragraph("• " + esc(c), bul_s))
    if resume.get("education"):
        flow.append(P("EDUCATION", h2_s))
        for e in resume["education"]:
            line = f"{esc(e.get('degree',''))} — {esc(e.get('school',''))}"
            if e.get("year"):
                line += f"  <font color='#666666'>({esc(str(e['year']))})</font>"
            flow.append(Paragraph(line, body_s))

    doc.build(flow)
    return buf.getvalue()


_CSS = """<style>
.dfhint{font-size:13px;color:var(--ink-mute);line-height:1.5}
.dflist{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.dfcard{display:block;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:12px 13px;text-decoration:none;color:var(--ink)}
.dfcard:hover{border-color:var(--accent);text-decoration:none}
.dftop{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.dfco{font-size:11.5px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em}
.dfats{font-size:10.5px;color:var(--ink-mute);font-family:var(--ff-mono,monospace);white-space:nowrap}
.dftitle{font-size:14.5px;font-weight:600;line-height:1.3;margin:3px 0 5px}
.dfcand{font-size:12.5px;color:var(--ink-soft);margin-bottom:8px}
.dfcc{color:var(--ink-mute)}
.dfscore{margin-left:6px;font-size:11px;font-weight:700;color:#188038}
.dfbar{display:flex;height:7px;border-radius:999px;overflow:hidden;background:var(--line);margin:2px 0 6px}
.dfbar>span{background:#34a853;display:block}
.dfbar>span.rev{background:#f9ab00}.dfbar>span.hum{background:var(--line-strong,#ccc)}
.dfnums{font-size:12px;color:var(--ink-soft)}
.dfnums .rev{color:#b06000;font-weight:600}.dfnums .hum{color:var(--ink-mute)}
.empty{color:var(--ink-mute);text-align:center;padding:44px 0}
/* detail */
.dhdr{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 15px;margin-bottom:12px}
.dback{font-size:13px;color:#1a73e8;text-decoration:none;display:inline-block;margin-bottom:8px}
.dsub{font-size:12.5px;color:var(--ink-mute);margin:2px 0 8px}
.dcandbox{font-size:13px;color:var(--ink);background:var(--bg-app);border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:10px}
.dcandbox .dcc{color:var(--ink-mute);font-size:12px}
.dactions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.dansw{display:flex;flex-direction:column;gap:8px}
.drow{background:var(--panel);border:1px solid var(--line);border-left:3px solid #34a853;border-radius:10px;padding:10px 13px}
.drow.d-hu{border-left-color:var(--line-strong,#ccc)}
.dhead{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}
.dlabel{font-size:13.5px;font-weight:600;color:var(--ink);line-height:1.35;min-width:0}
.dreq{color:#d93025;font-weight:700;margin-left:3px}
.dbadges{display:flex;gap:5px;align-items:center;flex-wrap:wrap;flex:0 0 auto}
.dbadge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap;border:1px solid transparent}
.s-id{color:#188038;background:#e6f4ea}.s-ch{color:#1a56c4;background:#e8f0fe}
.s-dr{color:#7b1fa2;background:#f3e8fd}.s-fi{color:#8a6d00;background:#fef3e0}
.s-hu{color:var(--ink-mute);background:var(--bg-app);border-color:var(--line)}
.s-rev{color:#b06000;background:#fff3e0;border-color:#fadfb0}
.dtype{font-size:10px;color:var(--ink-mute);font-family:var(--ff-mono,monospace);border:1px solid var(--line);border-radius:6px;padding:1px 6px}
.dval{font-size:13.5px;color:var(--ink);line-height:1.5;margin-top:5px;white-space:pre-wrap;word-break:break-word}
.dval.empty{color:var(--ink-mute);font-style:italic}
.dnote{display:inline-block;margin-top:5px;font-size:11.5px;color:#b06000}
.dcover{font-size:13px;line-height:1.6;white-space:pre-wrap;border:1px solid var(--line);border-radius:8px;padding:11px;background:var(--bg-app)}
@media(max-width:760px){.cat-search{display:none}.dflist{grid-template-columns:1fr}}
</style>"""
