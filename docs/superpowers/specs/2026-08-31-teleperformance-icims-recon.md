# Teleperformance (iCIMS) apply-flow recon — 2026-08-31

Recon done through the owner's **residential tunnel** (`socks5://127.0.0.1:8120`, egress = his home IP
`178.95.1.166`), headful Playwright on `:98`. Target job 459 = Healthcare CSR (Remote),
`https://careersus-teleperformance.icims.com/jobs/87136/...`.

## TL;DR — the blocker is AWS WAF, not the IP
The residential IP gets the page to LOAD (datacenter IP is Akamai/WAF-blocked outright), but the iCIMS
apply content sits behind an **AWS WAF "Human Verification" image CAPTCHA** that triggers on the
**automated-browser fingerprint** — so it fires *even through the residential IP*. The owner applying
from his own REAL browser on the same home IP does NOT hit it (it's bot-fingerprint-triggered, not
IP-triggered). **The form + the allowed-states list are BEHIND this captcha and were not reached.**

## Flow observed
1. `.../job` → Teleperformance careers **wrapper** page (nav/footer) with the iCIMS job embedded in an
   `iframe` (`?in_iframe=1`). Loads via the residential IP.
2. The iframe content = **AWS WAF challenge**: body `"Temporary error… you need to solve a puzzle…
   Let's confirm you are human"` + a **Begin** button. (Direct-loading the `in_iframe=1` URL just
   redirects back to the wrapper.)
3. Click **Begin** → page **title "Human Verification"**, body: *"Let's confirm you are human. Choose
   all the **hats**. [3×3 image grid 1–9]. Solved: 0 Required: 1. Choose only the images that contain
   the underlined object… Confirm."* Marker string **`AwsWafInt`** in the DOM ⇒ **AWS WAF CAPTCHA**
   (image-classification, "choose all the X"). Screenshot: `scratchpad/tp_06_challenge.png`.
4. (NOT reached) After the WAF captcha: iCIMS would show the real job + **Apply for this job online** →
   iCIMS **account register/login** → profile/résumé upload → **application form + screeners**
   (incl. the **state** selector — the allowed-states list the owner asked for). Account creation
   almost certainly needs an email step, which lands in the persona's `@takhet.com` Maildir.

## What this means for a build
- **The residential proxy is confirmed working and NECESSARY** (page loads via home IP; datacenter is
  hard-blocked). But it is NOT sufficient — AWS WAF still challenges the automated browser.
- To auto-fill Teleperformance you must first PASS the AWS WAF image captcha. Two paths:
  - **A. Solve it programmatically** — CapSolver/2Captcha both support **AwsWaf** captcha
    (token/image). Wire it into `applier/captcha_solver.py` (already scaffolded) and gate on a funded
    `CAPTCHA_SOLVER_KEY`. Then build the iCIMS strategy (register a synth persona, verify via the
    `@takhet.com` mailbox, fill the form, **constrain the persona's STATE to Teleperformance's allowed
    states**, submit). This is the full-auto path — but it depends on the captcha solver working on
    AWS WAF image puzzles (test before committing to the build).
  - **B. Human-in-noVNC hybrid** — the owner solves the WAF captcha once in noVNC (`jobs.systeam.kz/vnc/`),
    then the bot fills the rest. Simpler, but a person is in the loop each session, and AWS WAF may
    re-challenge.
- **Allowed-states data is still unknown** — it lives behind the captcha + account step. Gather it
  either (a) after wiring the captcha solver, or (b) from the owner's own manual session (he can read
  the state dropdown / eligibility text and send it), or (c) web research of TP's remote-hire states.
  Do NOT build the state-constraint until this list is in hand.

## Recommendation
Decide the captcha approach BEFORE building the iCIMS strategy — the whole flow hinges on it. Cheapest
first step: try CapSolver on this exact AWS WAF challenge (one live probe) to see if it solves; if yes,
build path A; if not, path B (human solves entry captcha in noVNC). The residential tunnel already
covers the IP half of the problem.

## Artifacts
- Screenshots: `scratchpad/tp_01_jobpage.png`, `tp_03_icims_job.png` (blank iframe = WAF), `tp_06_challenge.png` (the AWS WAF "choose all the hats" grid).
- Recon scripts: `scratchpad/tp_recon{1,2,3,4}.py`.
