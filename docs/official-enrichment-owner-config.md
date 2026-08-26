# Official enrichment owner configuration

The preflight checks only whether `SAM_API_KEY` and `SEC_USER_AGENT` are present and
have a plausible format. It never prints their values and does not read or modify
`backend/.env`.

Run from the repository root:

```bash
.venv/bin/python -m backend.tools.employer_official_config_preflight
```

Use the strict exit code in deployment or an owner handoff. Exit code `2` means at
least one required owner setting is missing or malformed:

```bash
.venv/bin/python -m backend.tools.employer_official_config_preflight --require-ready
```

Configure values only in the protected process environment or secret manager. Do
not put a key on the command line, in source control, screenshots, tickets, or logs.
The SAM key must be a non-placeholder value of 16–256 characters without whitespace.
The SEC user agent must be a one-line application or organization identity followed
by a monitored contact email, for example the shape `Organization contact@example.com`.

After preflight succeeds, resume exact-ID enrichment with these commands. Replace
only the entity-ID placeholders; credentials are read from the protected environment:

```bash
.venv/bin/python -m backend.tools.employer_official_enrichment sam --uei '<12_CHARACTER_UEI>'
.venv/bin/python -m backend.tools.employer_official_enrichment sec --cik '<CIK>'
```

SAM enrichment is bound to an existing exact UEI. SEC enrichment is bound to an
existing exact CIK. Neither connector performs a name-only join.

FDIC BankFind and IRS 990 public data do not require an owner API key:

```bash
.venv/bin/python -m backend.tools.employer_official_enrichment fdic --cert '<FDIC_CERT>'
.venv/bin/python -m backend.tools.employer_official_enrichment fdic-linked --limit 50 --min-interval .1
.venv/bin/python -m backend.tools.employer_official_enrichment irs-xml --ein '<EIN>' --url '<OFFICIAL_IRS_XML_URL>'
```

The IRS command requires an exact EIN and an official `apps.irs.gov` filing XML URL.
The FDIC batch operates only on records already linked by exact FDIC certificate.
