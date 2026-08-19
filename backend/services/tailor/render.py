"""Render a structured résumé dict into ATS-safe plain text and HTML.

Single column, standard section headings, no tables/columns/text-boxes — the layout
rules that ATS parsers handle most reliably.
"""


def _contact_line(pi: dict) -> str:
    parts = [pi.get("email", ""), pi.get("phone", ""), pi.get("location", ""), pi.get("linkedin", "")]
    return "  |  ".join(p for p in parts if p)


def render_text(resume: dict) -> str:
    pi = resume.get("personal_info", {})
    out: list[str] = []
    out.append(pi.get("full_name", "").upper())
    if resume.get("headline"):
        out.append(resume["headline"])
    if resume.get("eligibility"):
        out.append(resume["eligibility"])
    cl = _contact_line(pi)
    if cl:
        out.append(cl)
    out.append("")

    if resume.get("summary"):
        out.append("SUMMARY")
        out.append(resume["summary"])
        out.append("")

    exp = resume.get("experience", [])
    if exp:
        out.append("EXPERIENCE")
        for e in exp:
            head = f"{e.get('title','')} — {e.get('company','')}"
            if e.get("dates"):
                head += f"  ({e['dates']})"
            out.append(head)
            for b in e.get("bullets", []):
                out.append(f"  - {b}")
            out.append("")

    skills = resume.get("skills_grouped", {})
    if skills:
        out.append("SKILLS")
        for grp, items in skills.items():
            out.append(f"{grp}: {', '.join(items)}")
        out.append("")

    certs = resume.get("certifications", [])
    if certs:
        out.append("CERTIFICATIONS")
        for c in certs:
            out.append(f"  - {c}")
        out.append("")

    edu = resume.get("education", [])
    if edu:
        out.append("EDUCATION")
        for e in edu:
            line = f"{e.get('degree','')} — {e.get('school','')}"
            if e.get("year"):
                line += f"  ({e['year']})"
            out.append(line)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(resume: dict) -> str:
    pi = resume.get("personal_info", {})
    p: list[str] = []
    p.append("<!doctype html><html><head><meta charset='utf-8'><style>")
    p.append("body{font-family:Arial,Helvetica,sans-serif;font-size:11pt;color:#111;"
             "max-width:7.5in;margin:0 auto;line-height:1.35}"
             "h1{font-size:18pt;margin:0}h2{font-size:12pt;border-bottom:1px solid #999;"
             "margin:14px 0 6px;text-transform:uppercase}.muted{color:#444}"
             "ul{margin:4px 0 8px 18px}li{margin:2px 0}.role{font-weight:bold}</style></head><body>")
    p.append(f"<h1>{_esc(pi.get('full_name',''))}</h1>")
    if resume.get("headline"):
        p.append(f"<div class='muted'>{_esc(resume['headline'])}</div>")
    if resume.get("eligibility"):
        p.append(f"<div class='muted'><b>{_esc(resume['eligibility'])}</b></div>")
    cl = _contact_line(pi)
    if cl:
        p.append(f"<div class='muted'>{_esc(cl)}</div>")

    if resume.get("summary"):
        p.append("<h2>Summary</h2>")
        p.append(f"<div>{_esc(resume['summary'])}</div>")

    if resume.get("experience"):
        p.append("<h2>Experience</h2>")
        for e in resume["experience"]:
            head = f"{_esc(e.get('title',''))} — {_esc(e.get('company',''))}"
            dates = f" <span class='muted'>({_esc(e.get('dates',''))})</span>" if e.get("dates") else ""
            p.append(f"<div class='role'>{head}{dates}</div>")
            if e.get("bullets"):
                p.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in e["bullets"]) + "</ul>")

    if resume.get("skills_grouped"):
        p.append("<h2>Skills</h2><ul>")
        for grp, items in resume["skills_grouped"].items():
            p.append(f"<li><b>{_esc(grp)}:</b> {_esc(', '.join(items))}</li>")
        p.append("</ul>")

    if resume.get("certifications"):
        p.append("<h2>Certifications</h2><ul>")
        p.extend(f"<li>{_esc(c)}</li>" for c in resume["certifications"])
        p.append("</ul>")

    if resume.get("education"):
        p.append("<h2>Education</h2>")
        for e in resume["education"]:
            yr = f" <span class='muted'>({_esc(e.get('year',''))})</span>" if e.get("year") else ""
            p.append(f"<div>{_esc(e.get('degree',''))} — {_esc(e.get('school',''))}{yr}</div>")

    p.append("</body></html>")
    return "".join(p)
