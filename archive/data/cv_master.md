# MASTER CV

> **TL;DR:** Всё ниже уже написано как готовый CV профессионального CS-специалиста с 10+ годами опыта.
> Тебе нужно ТОЛЬКО:
>
> 1. Заменить все `<...UPPERCASE_PLACEHOLDERS>` — это 10 личных фактов
> 2. Удалить bullets, которых у тебя реально не было (по одной строке — Ctrl+D)
> 3. (опционально) проставить реальные числа там где `<METRIC>` — или удалить эти bullets
>
> Что нельзя автоматизировать = personal facts. Всё остальное (структуру, skills, phrasing, bullets) Claude дальше будет тасовать под каждый JD.

---

## ⚠️ FACTS YOU MUST FILL (10 placeholders)

```yaml
# Personal
FULL_NAME:        "<YOUR FULL NAME>"
EMAIL:            "<your@email.com>"
PHONE:            "<+1-XXX-XXX-XXXX>"
LOCATION:         "<City>, <State>, US"
LINKEDIN_URL:     "<https://linkedin.com/in/...>"

# Companies (top 3 most recent — add more as needed)
COMPANY_1_NAME:   "<Company you worked at most recently>"
COMPANY_1_TITLE:  "<Your exact job title there>"
COMPANY_1_DATES:  "<MM/YYYY> – <MM/YYYY or Present>"

COMPANY_2_NAME:   "<Previous company>"
COMPANY_2_TITLE:  "<Title there>"
COMPANY_2_DATES:  "<MM/YYYY> – <MM/YYYY>"

COMPANY_3_NAME:   "<Earlier company>"
COMPANY_3_TITLE:  "<Title>"
COMPANY_3_DATES:  "<MM/YYYY> – <MM/YYYY>"

# Education
DEGREE:           "<Bachelor's in X>"
UNIVERSITY:       "<University Name>"
GRAD_YEAR:        "<YYYY>"
```

---

## 1. Personal Info

```yaml
full_name: "<FULL_NAME>"
email: "<EMAIL>"
phone: "<PHONE>"
location: "<LOCATION>"
linkedin: "<LINKEDIN_URL>"
work_authorization: "US Citizen"
willing_to_relocate: false
remote_preference: "remote"
```

---

## 2. Professional Summary (3 versions — Claude picks one per JD)

### Short (≤ 2 lines, для compact CV)
> Customer Support professional with 10+ years across SaaS, fintech, and consumer products. Specializes in technical troubleshooting, escalation management, and knowledge base ownership.

### Medium (3-4 lines, default)
> Customer Support specialist with 10+ years of experience scaling support operations across SaaS and consumer product companies. Strong track record in Tier 1-3 escalations, CSAT programs, knowledge base development, and cross-functional coordination with Engineering and Product. Proficient with Zendesk, Intercom, Salesforce Service Cloud, HubSpot, and Jira.

### Long (5-7 lines, для senior/lead роли)
> Senior Customer Support professional with 10+ years building and scaling support functions across SaaS, fintech, and consumer companies. Owned end-to-end ticket lifecycle from Tier-1 intake through Tier-3 engineering escalation, with deep experience in Zendesk and Intercom administration, KCS-aligned knowledge base programs, and cross-functional collaboration. Mentored junior agents, authored SOPs, and partnered with Product on customer feedback loops. Looking to bring this experience to a high-growth team where customer experience is treated as a product.

---

## 3. Experience

> ⚠️ Удали bullets, которые НЕ описывают что ты реально делал. Оставь те которые описывают.
> Каждый bullet — действие + контекст + (опционально) метрика.
> Bullets отсортированы тематически: [csat] [tech] [escalation] [process] [training] [tools] [b2b] [b2c] [analytics]

### `<COMPANY_1_NAME>` — `<COMPANY_1_TITLE>` | `<COMPANY_1_DATES>` | Remote

**Context:** SaaS / fintech / e-commerce / consumer (укажи одно).

- [csat] Resolved customer inquiries across email, live chat, and phone via Zendesk, consistently meeting SLA targets for first-response and resolution time
- [csat] Maintained customer satisfaction (CSAT) at <NN%> across <NNN+> tickets/month — *(replace number or remove)*
- [tech] Diagnosed and triaged technical issues, reproducing bugs and submitting structured reports to Engineering with logs, repro steps, and impact assessment
- [tech] Investigated API errors using Postman, browser DevTools, and product logs to identify root cause before escalation
- [escalation] Managed Tier-2 escalation queue, partnering directly with Engineering and Product to resolve customer-impacting incidents
- [escalation] Acted as customer-facing lead during P1/P2 incidents, drafting status communications and post-incident summaries
- [process] Authored and maintained <NN+> knowledge base articles in Confluence/Notion, reducing repeat ticket volume on top issues
- [process] Built Zendesk macros, triggers, and automations to streamline common workflows (refunds, password resets, account changes)
- [training] Onboarded and mentored new hires using KCS-aligned documentation; created training materials and shadowing programs
- [tools] Administered Zendesk: configured ticket forms, business hours, SLA policies, custom views, and routing rules
- [b2b] Served as named support contact for enterprise accounts, conducting regular check-ins and tracking account health
- [analytics] Built and maintained support dashboards in Looker / Zendesk Explore tracking CSAT, FRT, ticket volume, and agent productivity
- [process] Partnered with Product team to surface recurring customer pain points; submitted feature requests with quantified user impact
- [csat] Handled escalated customer complaints with focus on de-escalation and root-cause resolution
- [process] Contributed to weekly team retrospectives, surfacing process improvements adopted across the team

**Tools used:** Zendesk, Intercom, Slack, Jira, Confluence, Notion, Looker, Salesforce Service Cloud, HubSpot, Postman

---

### `<COMPANY_2_NAME>` — `<COMPANY_2_TITLE>` | `<COMPANY_2_DATES>` | Remote

**Context:** SaaS / fintech / e-commerce (укажи одно).

- [csat] Owned Tier-1 customer support queue, handling email and chat tickets across diverse customer base
- [tech] Resolved technical issues spanning account access, billing, integrations, and product functionality
- [escalation] Coordinated with Engineering on bug reports, providing reproducible repro steps and customer impact context
- [process] Contributed to internal knowledge base, writing articles for top customer questions
- [tools] Used Intercom for inbound chat support and Salesforce Service Cloud for case management
- [training] Mentored junior agents on triage, tone, and product knowledge
- [analytics] Tracked individual ticket metrics (CSAT, resolution time) and used data to improve handling patterns
- [b2c] Handled high volume of consumer-facing tickets with focus on empathy and clear written communication
- [process] Identified workflow inefficiencies and proposed Zendesk/Intercom configuration changes
- [csat] Consistently exceeded team CSAT and SLA targets

**Tools used:** Intercom, Salesforce Service Cloud, Zendesk, Slack, Jira, Notion

---

### `<COMPANY_3_NAME>` — `<COMPANY_3_TITLE>` | `<COMPANY_3_DATES>` | Remote / Onsite

**Context:** Consumer / B2B / e-commerce (укажи).

- [csat] Provided front-line customer support across email, chat, and phone channels
- [tech] Troubleshot product issues, account problems, and order-related questions
- [process] Used Zendesk / Freshdesk / HubSpot Service Hub for ticket management *(оставь только тот которым реально пользовался)*
- [training] Participated in product training and certification programs
- [escalation] Routed complex cases to senior team members and engineering
- [csat] Maintained professional, empathetic tone across high-volume queues
- [process] Followed established SOPs and contributed to documentation updates

**Tools used:** Zendesk / Freshdesk / HubSpot Service Hub *(удали ненужные)*, Slack, Google Workspace

---

## 4. Certifications

> ⚠️ Оставь ТОЛЬКО те, которые реально проходил. Удали остальные.

- **Zendesk Support Administrator Expert** — Zendesk, 2024
- **Zendesk Customer Service Professional** — Zendesk, 2023
- **Intercom Foundations Certified** — Intercom, 2023
- **HubSpot Service Hub Certification** — HubSpot Academy, 2024
- **HubSpot Customer Service Software** — HubSpot Academy, 2023
- **Salesforce Certified Service Cloud Consultant** — Salesforce, 2023
- **Salesforce Administrator (ADM 201)** — Salesforce, 2022
- **ITIL 4 Foundation** — Axelos / PeopleCert, 2022
- **KCS v6 Practitioner** — Consortium for Service Innovation, 2023
- **Google Project Management Certificate** — Coursera, 2022
- **CCSP — Certified Customer Service Professional** — HDI, 2021

---

## 5. Skills

> ⚠️ Удали из каждой группы то, чем НЕ пользовался. Оставь то, что используешь хотя бы на уровне Familiar.

### Customer Support Platforms
Zendesk (Admin), Intercom, Freshdesk, HubSpot Service Hub, Salesforce Service Cloud, Help Scout, Front, Kustomer, Gorgias

### CRM
Salesforce, HubSpot CRM, Pipedrive, Close, Microsoft Dynamics 365

### Communication & Collaboration
Slack, Microsoft Teams, Zoom, Google Meet, Loom, Discord

### Knowledge Base & Documentation
Confluence, Notion, GitBook, Document360, HelpJuice, Slab

### Project & Issue Tracking
Jira, Linear, Asana, ClickUp, Trello, Monday.com, GitHub Issues

### Analytics & Reporting
Looker, Zendesk Explore, Mixpanel, Amplitude, Tableau, Metabase, Google Analytics, Excel/Sheets (advanced)

### Methodologies & Frameworks
KCS (Knowledge-Centered Service), ITIL 4, Agile/Scrum, OKRs, SLA management, Tier 1-3 escalation models, Voice of Customer (VoC)

### Technical Skills
SQL (basic queries, joins), HTML/CSS (read & edit), JSON / API debugging (Postman, browser DevTools), Git basics, Markdown, Regex, Webhook debugging

### Languages (spoken)
- English — Fluent (C1/C2)
- Russian — Native
- *(добавь другие если есть)*

### Soft Skills
De-escalation, written communication, async / distributed team coordination, customer empathy, cross-functional collaboration, stakeholder management

---

## 6. Education

### `<DEGREE>` — `<UNIVERSITY>` | `<GRAD_YEAR>`
- *(опционально: relevant coursework, honors, GPA если ≥ 3.5/4.0)*

---

## 7. Notable Projects / Initiatives

> ⚠️ Удали те, которых не было. Заполни своими реальными проектами если есть.

- **Knowledge base build-out** — Authored / restructured KB at one of the above companies, contributing to deflection of repeat tickets
- **Onboarding program** — Built or contributed to new-hire training curriculum, reducing ramp time for incoming agents
- **Tool migration** — Participated in migration between support platforms (e.g., Freshdesk → Zendesk), including team training
- **SLA dashboard** — Built or maintained reporting dashboard tracking team SLA and CSAT performance
- **Escalation framework** — Designed or improved Tier-2/Tier-3 escalation workflow with Engineering

---

## 8. Volunteer / Community (optional — delete if not relevant)

- *(если был moderator subreddit / Discord / open-source — пиши тут. Если нет — удали секцию.)*

---

## 9. Tailoring metadata (НЕ рендерится в PDF, только для resume_tailor.py)

```yaml
preferred_titles:
  - "Customer Support Specialist"
  - "Customer Support Engineer"
  - "Senior Customer Support Specialist"
  - "Customer Success Specialist"
  - "Technical Support Specialist"
  - "Customer Experience Specialist"
  - "Support Team Lead"

industries_strong:
  - SaaS B2B
  - Consumer SaaS
  - Fintech
  - E-commerce

industries_avoid:
  - Gambling
  - Adult content

role_levels_acceptable:
  - mid
  - senior
  - lead

avoid_keywords: []

salary_min_usd: 60000
salary_target_usd: 80000
salary_max_usd: 110000
```
