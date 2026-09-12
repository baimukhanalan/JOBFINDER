"""System health snapshot for the admin dashboard (`/health`) — the operator's COMPLETE map of the
deployment: every moving part, its live status, why it is fragile and what to do when it breaks.

Groups (each row = {name, status: ok|warn|down|info, detail, hint, [sched]}):
  * pm2      — every jobfinder-* long-running process (status / uptime / restarts / cwd / memory)
  * crons    — EVERY JOBFINDER line of `crontab -l` (parsed at runtime) joined to its log file; the
               known lanes (`_CRONS`) carry an explicit cadence, unknown lines get one derived from
               the cron expression. Error / hung / stale escalation as before.
  * data     — Postgres jobfinder_crm (row counts + the idle-in-transaction / lock-waiter probe that
               would have caught the 2026-09-07..09 outage), the MySQL mailbox backend, the Maildir
               root, disks, the runtime JSON stores, the question bank
  * deps     — the local model, the proxy pool, captcha-solver key, Telegram, Postfix/Dovecot, DNS
               for takhet.com, the :98 display stack, the co-pilot, nginx + the public URL
  * system   — load / RAM / swap / cpu / uptime / top RSS / chromium count / /tmp
  * incidents — a static list of the known fragile points (date of last incident + remediation)

Every probe runs in a worker thread with a hard deadline (`_PROBE_TIMEOUT`), so the page renders in
< 4 s even when MySQL / DNS / the model hang — a timed-out probe shows as «нет ответа за 3 с» (warn).
Read-only except two small state files under logs/ (the alert throttle + pm2 restart samples).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
_LOGS = os.path.join(_ROOT, "logs")
_DATA = os.path.join(_ROOT, "backend", "data")
_ENV_FILE = os.path.join(_ROOT, "backend", ".env")
_MAILDIR_ROOT = "/var/mail/vhosts/takhet.com"
_SERVER_IP = "173.249.18.153"          # the box's public IP (nginx + the takhet.com MX target)
_PUBLIC_LOGIN_URL = "https://jobs.systeam.kz/login"
_COPILOT_STATE_URL = "http://127.0.0.1:8102/state"
_PROBE_TIMEOUT = 3.0                   # per-probe hard deadline (seconds)


# ---- small helpers -------------------------------------------------------------------------------
def _age_str(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs} с назад"
    if secs < 5400:
        return f"{secs // 60} мин назад"
    if secs < 172800:
        return f"{secs // 3600} ч назад"
    return f"{secs // 86400} дн назад"


def _dur_str(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{secs // 60} мин"
    if secs < 172800:
        return f"{secs // 3600} ч"
    return f"{secs // 86400} дн"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 сбой / 2 сбоя / 5 сбоев."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _fmt_mb(b: float) -> str:
    return f"{b / 2**20:.1f} MB" if b < 2**30 else f"{b / 2**30:.2f} GB"


def _tail_lines(path: str, n: int = 12, maxlen: int = 200) -> list[str]:
    """The last `n` non-empty lines of a log (best-effort, reads only an 8 KB tail). Scanning a block
    — not just the final line — catches a run that crashed but printed a benign-looking last line."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", "replace")
        lines = [ln.strip()[:maxlen] for ln in chunk.splitlines() if ln.strip()]
        return lines[-n:]
    except Exception:
        return []


def _tail_line(path: str, maxlen: int = 160) -> str:
    """Last non-empty line of a log (best-effort)."""
    lines = _tail_lines(path, n=1, maxlen=maxlen)
    return lines[-1] if lines else ""


def _run(cmd: list[str], timeout: float = _PROBE_TIMEOUT, env: dict | None = None) -> str:
    """stdout of a short read-only command ('' on any failure / timeout)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=env).stdout
    except Exception:
        return ""


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _env_value(key: str) -> str:
    """A secret's PRESENCE check: the process env first, then backend/.env (never printed)."""
    v = os.environ.get(key, "")
    if v:
        return v
    try:
        with open(_ENV_FILE, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith(f"{key}="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _row(name: str, status: str, detail: str, hint: str = "", **extra) -> dict:
    r = {"name": name, "status": status, "detail": detail}
    if hint:
        r["hint"] = hint
    r.update(extra)
    return r


# ---- pm2 -----------------------------------------------------------------------------------------
_PM2_EXPECTED = ["jobfinder-alan-dash", "jobfinder-mail-indexer", "jobfinder-alan-copilot",
                 "jobfinder-alan-display", "jobfinder-alan-ivremind"]
_PM2_HINTS = {
    "jobfinder-alan-dash": (
        "Дашборд CRM на 127.0.0.1:8099 (за nginx jobs.systeam.kz). <b>Перезапускать после правок</b> "
        "dashboard_app.py, tools/*_ui.py, mailcrm.py, interviews/*. Внутри него же крутятся фоновые "
        "потоки: ревалидатор прокси, сверка сабмитов по почте, цикл дренажа «Незавершённых». "
        "cwd ОБЯЗАН быть /home/projects/jobfinder (строчные) — до 2026-08-21 он смотрел на пустой "
        "/home/projects/JOBFINDER и отдавал устаревший код. Починить: pm2 restart jobfinder-alan-dash."),
    "jobfinder-mail-indexer": (
        "inotify-наблюдатель за /var/mail/vhosts/takhet.com → Postgres mail_index. <b>Именно он "
        "наполняет вкладку «Кандидаты»</b>: если он лежит, письма доставляются, но в CRM не появляются. "
        "Перезапускать после ЛЮБОЙ правки разбора/классификации в mailcrm.py (иначе новая почта "
        "индексируется старой логикой — инцидент 2026-08-25) и после регистрации новых персон. "
        "Починить: pm2 restart jobfinder-mail-indexer (под sg mail)."),
    "jobfinder-alan-copilot": (
        "Headful Chromium-ко-пилот на 127.0.0.1:8102, DISPLAY=:98 — заполняет формы для «Заполнить» "
        "и «Докрутить». <b>Держит код apply в памяти</b>: после правок applier/, dropdowns.py, "
        "analyzer.py, services/tailor — pm2 restart jobfinder-alan-copilot, иначе 22-часовой код "
        "(инцидент 2026-08-28: 381 задач застряли в needs_review). Headless-воркеры массовой подачи "
        "стартуют заново на каждый запуск и этой проблемы не имеют."),
    "jobfinder-alan-display": (
        "vnc/copilot_display.sh: Xvfb :98 + x11vnc :5901 + noVNC :6090. Без него не стартует ни "
        "ко-пилот, ни headful-лейны (Maximus, TP, Taleo, Workday, харвестер AMCAT). Починить: "
        "pm2 restart jobfinder-alan-display, затем перезапустить ко-пилот."),
    "jobfinder-alan-ivremind": (
        "Демон Telegram-уведомлений о собесах: анонс назначения + напоминания за 60 и 5 минут, "
        "привязка чатов интервьюеров (/start код). Перезапускать после смены IV_BOT_TOKEN в .env "
        "или правок interviews/notify.py. Не трогает Maildir — sg mail не нужен."),
}
_PM2_STATE = os.path.join(_LOGS, "health_pm2_state.json")


def _restart_growth(name: str, restarts: int, now: float, state: dict) -> int:
    """How much the pm2 restart counter grew in the last hour (rolling samples in a state file)."""
    samples = [s for s in state.get(name, []) if isinstance(s, list) and len(s) == 2
               and now - float(s[0]) <= 3600]
    samples.append([int(now), int(restarts)])
    state[name] = samples
    return int(restarts) - min(int(s[1]) for s in samples)


def pm2_services() -> list[dict]:
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT).stdout
        procs = json.loads(out)
    except Exception as exc:
        return [_row("pm2", "down", f"pm2 jlist не отвечает: {str(exc)[:80]}",
                     "pm2 — супервизор всех сервисов JobFinder. Проверить: pm2 ls; pm2 resurrect.")]
    now = time.time()
    state = _load_json(_PM2_STATE)
    rows, seen = [], set()
    for p in procs:
        name = p.get("name", "?")
        if not name.startswith("jobfinder"):
            continue
        seen.add(name)
        env = p.get("pm2_env", {}) or {}
        st = env.get("status", "?")
        up = env.get("pm_uptime")
        restarts = int(env.get("restart_time", 0) or 0)
        cwd = env.get("pm_cwd") or ""
        monit = p.get("monit", {}) or {}
        cpu = monit.get("cpu", 0)
        mem = (monit.get("memory", 0) or 0) / (1024 * 1024)
        up_s = (now * 1000 - up) / 1000 if up else None
        uptime = _dur_str(up_s) if up_s is not None else "?"
        notes = []
        if st == "online":
            status = "ok"
        elif st in ("launching", "one-launch-status"):
            status = "warn"
        else:
            status = "down"
            notes.append(f"статус {st}")
        growth = _restart_growth(name, restarts, now, state)
        if growth >= 2:
            status = "down"
            notes.append(f"{growth} перезапуска за час (падает?)")
        elif growth >= 1 or (restarts and up_s is not None and up_s < 3600):
            if status == "ok":
                status = "warn"
            notes.append("перезапуск " + (_age_str(up_s) if up_s is not None else "за последний час"))
        if cwd and os.path.realpath(cwd) != os.path.realpath(_ROOT):
            status = "down"
            notes.append(f"cwd {cwd} ≠ живой чекаут")
        detail = f"{st} · аптайм {uptime} · {restarts} перезапусков · cpu {cpu}% · {mem:.0f} MB"
        if notes:
            detail = " · ".join(notes) + " · " + detail
        rows.append(_row(name, status, detail, _PM2_HINTS.get(name, "")))
    for name in _PM2_EXPECTED:
        if name not in seen:
            rows.append(_row(name, "down", "не найден в pm2 (процесс удалён?)",
                             _PM2_HINTS.get(name, "") + " Восстановить по инструкции из документации "
                             "проекта, раздел Deploy (pm2 start … --cwd /home/projects/jobfinder), затем pm2 save."))
    _save_json(_PM2_STATE, state)
    if not rows:
        rows.append(_row("pm2", "warn", "процессы jobfinder-* не найдены"))
    return rows


# ---- cron lanes ----------------------------------------------------------------------------------
# (label, log filename under logs/, max age in HOURS before it's considered STALE)
_CRONS = [
    ("Каталог: сбор (nightly)", "catalog.log", 30),
    ("Каталог: регионы", "regions.log", 30),
    ("Каталог: формы", "forms.log", 30),
    ("Каталог: est-comp", "est_comp.log", 30),
    ("Компании: discovery (weekly)", "discovery.log", 24 * 8),
    ("Mass Hiring: сбор", "masshiring.log", 8),      # cron every 6h since 2026-09-09 (no manual button)
    ("Прокси: Bright Data (daily)", "brightdata.log", 30),
    ("Почта: retention", "retention.log", 30),
    ("Почта: health-probe", "health.log", 1),
    ("Prefill retention", "prefill_retention.log", 30),
    ("Apply: Maximus (5×/день)", "mh_apply.log", 8),
    ("Apply: Teleperformance", "tp_apply.log", 8),
    ("Apply: Taleo/TTEC", "taleo_apply.log", 8),
    ("Apply: Kelly", "kelly_apply.log", 8),
    ("Apply: SmartRecruiters", "sr_apply.log", 8),
    ("Apply: Workday/Centene", "workday_apply.log", 8),
    ("Харвестер вопросов (hourly)", "harvest_cron.log", 3),
    ("Health: Telegram-алерт", "health_alert.log", 1),
    ("Кампании: ежедневная подача", "apply_campaigns.log", 8),
]
# Why each lane is fragile + what to do (keyed by log filename; shown on tap).
_CRON_HINTS = {
    "catalog.log": ("Ночной сбор Ashby/Greenhouse/Lever/Workable в job_catalog (remote-only, регионы, "
                    "роли, comp). Если не отработал — каталог протухает, «Подать на все» бьёт по "
                    "мёртвым вакансиям. Ручной прогон: python3 -m backend.tools.catalog_collector."),
    "regions.log": "Дозаполнение regions у строк с NULL (остаток после правил). Некритично, но без него часть вакансий не попадает в фильтр по стране.",
    "forms.log": ("Playwright-скрейп вопросов анкет Ashby/Lever/Workable. Падал ночами на argparse "
                  "(2026-09-09, choices+nargs='*'); чинится без choices. Проверка: catalog_forms --limit 1."),
    "est_comp.log": "Оценка зарплат для новых вакансий (наследует по комбо компания×роль×регион, иначе таблица медиан). Без него новые карточки без «оценки».",
    "discovery.log": "Еженедельное обновление списка компаний/слагов ATS. Раз в неделю — жёлтый между запусками нормален.",
    "masshiring.log": ("Сбор доски Mass Hiring (Amazon/BPO/Workday-страховщики) каждые 6 ч. Висел 2 дня "
                       "в очереди за блокировкой Postgres (2026-09-07..09) — смотри «Блокировки Postgres». "
                       "Ручной прогон: mass_hiring --collect."),
    "brightdata.log": ("Ежедневная перевыпуск 200 сессий Bright Data (зона alibaba_dc). При пустом балансе "
                       "refresh НЕ трогает вчерашний пул. Пополнять баланс в кабинете BD."),
    "retention.log": "Удаление проиндексированной почты старше 30 дней. Некритично.",
    "health.log": "Зонд индексатора/БД каждые 10 мин (mail_health check) → предупреждение в CRM при провале.",
    "prefill_retention.log": "Чистка uploads/prefill старше 20 дней (резюме, скриншоты). Без него диск растёт.",
    "mh_apply.log": ("Реальные сабмиты на Maximus (Avature) 5×/день, каждая — новая синтетическая персона + "
                     "приглашение на SHL-опросник, который добивает этaлон. Ошибка «connection already closed» "
                     "= лейн умер вместе с зачисткой сессий Postgres — пройдёт на следующем запуске."),
    "tp_apply.log": "Teleperformance/iCIMS: hCaptcha на каждом шаге, решает платный ключ NopeCHA. Ключ кончился → 0 сабмитов. 3 воркера, ~5–8 мин/задача.",
    "taleo_apply.log": "TTEC/Taleo без капчи, headful на :98. Список doable-вакансий фиксирован в коде (языковые/лицензионные пропускаются).",
    "kelly_apply.log": "Kelly: сайт за Akamai — ходит ТОЛЬКО через пул прокси. Пустой пул → лейн пропускает всё. Тяжёлый fill (локальная модель).",
    "sr_apply.log": "Sutherland/SmartRecruiters: shadow-DOM форма, NopeCHA. Работает с датацентрового IP.",
    "workday_apply.log": ("Centene (Workday) headful. Лимит ~90 активационных писем/домен: если «Verify your "
                          "candidate account» перестали приходить — переждать, не ретраить."),
    "harvest_cron.log": ("Харвестер AMCAT: один токен в 20 мин, concurrency 1, без прокси. Два дня падал с "
                         "ModuleNotFoundError — строка была без cd (2026-09-09). Всплеск запросов с одного IP "
                         "→ NE500 (лимит) и логаут на секции C."),
    "health_alert.log": "Этот самый зонд: каждые 15 мин gather() + Telegram при любом «сбое» (троттл 4 ч) и одно сообщение о восстановлении.",
    "apply_campaigns.log": "Драйвер ежедневных кампаний (4 запуска/день). Инертен, пока нет ни одной кампании — отсутствие лога до первого запуска нормально.",
    "mailpoll.log": "УСТАРЕВШИЙ mail_sink --poll: пишет uploads/inbox/mail_sink.json, который никто не читает. «Кандидаты» питает mail_indexer, не он. Можно удалить из crontab.",
}
_OPTIONAL_LOGS = {"apply_campaigns.log"}   # a lane that legitimately hasn't run yet
# Error signal in a log tail. `\w*error` matches bare "error" AND CamelCase endings
# (ModuleNotFoundError / KeyError / TimeoutError / …); "no module named" catches the import failure
# whose CamelCase word `\berror\b` used to miss (the harvest-cron incident, 2026-09-09).
_ERR_RE = re.compile(
    r"(?:\w*error|traceback|exception|failed|no fresh|no module named|"
    r"ne500|state_transition|modulenotfound)", re.I)
# Benign phrases that CONTAIN an error word but mean success — apply lanes end with a summary like
# "…errors=0" / "0 errors", and a "still going — exiting" flock-skip is normal. Don't flag those.
_BENIGN_RE = re.compile(r"errors?\s*[=:]\s*0\b|\b0\s+errors?\b|still going\s*[—-]\s*exiting", re.I)


# A run-completion summary line — every cron here ends a successful run on one of these shapes
# (`DONE …`, `FINISHED …`, `stats: {…}`/`collect: {…}`, `catalog counts -> …`, or a bare JSON/dict
# summary). Seen as the newest line it means "the latest run completed", whatever sits above it.
_SUCCESS_RE = re.compile(r"^\s*(?:DONE\b|FINISHED\b|stats:|collect:|catalog counts|\{)", re.I)

_DOW_RU = {0: "вс", 1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб"}


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    vals: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        vals.update(range(a, b + 1, step))
    return vals


def cron_cadence_hours(expr: str) -> float | None:
    """The LONGEST gap (hours) between two consecutive runs of a 5-field cron expression, i.e. the
    lane's expected cadence. A day-of-month/month-restricted line is treated as ~monthly."""
    try:
        m, h, dom, mon, dow = expr.split()[:5]
        if dom != "*" or mon != "*":
            return 24.0 * 31
        mins = _expand_field(m, 0, 59)
        hrs = _expand_field(h, 0, 23)
        dows = {d % 7 for d in _expand_field(dow, 0, 7)}
        slots = sorted(d * 1440 + hh * 60 + mm for d in dows for hh in hrs for mm in mins)
        if not slots:
            return None
        if len(slots) == 1:
            return 24.0 * 7
        gaps = [b - a for a, b in zip(slots, slots[1:])] + [10080 - slots[-1] + slots[0]]
        return max(gaps) / 60
    except Exception:
        return None


def cron_human(expr: str) -> str:
    """Cron expression → short Russian words («каждые 20 мин», «в 05:30», «5×/день: 01:00, …»)."""
    f = expr.split()
    if len(f) < 5:
        return expr
    m, h, dom, mon, dow = f[:5]
    try:
        if dom == "*" and mon == "*":
            if h == "*" and m == "*":
                return "каждую минуту"
            if h == "*" and m.startswith("*/"):
                return f"каждые {int(m[2:])} мин"
            if h.startswith("*/") and re.fullmatch(r"\d+", m):
                return f"каждые {int(h[2:])} ч (в :{int(m):02d})"
            if re.fullmatch(r"\d+", m) and re.fullmatch(r"\d+(,\d+)*", h):
                hours = [int(x) for x in h.split(",")]
                times = ", ".join(f"{hh:02d}:{int(m):02d}" for hh in hours)
                if dow == "*":
                    return f"ежедневно в {times}" if len(hours) == 1 else f"{len(hours)}×/день: {times}"
                days = ", ".join(_DOW_RU.get(int(d) % 7, d) for d in dow.split(","))
                return f"еженедельно ({days}) в {times}"
    except Exception:
        pass
    return expr


_CRON_LINE_RE = re.compile(r"^\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$")
_MODE_FLAGS = ("--poll", "--collect", "--alert", "--refresh", "--drain")


def parse_crontab(text: str, root: str = _ROOT) -> list[dict]:
    """Every ACTIVE crontab line that touches the repo root → {sched, human, cadence_h, cmd, log,
    module, flag, raw}. Comments and other projects' lines are skipped."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or root not in line:
            continue
        mm = _CRON_LINE_RE.match(line)
        if not mm:
            continue
        sched, cmd = mm.group(1), mm.group(2)
        log = re.search(r">>\s*(\S+\.log)", cmd)
        mods = re.findall(r"-m\s+([\w.]+)", cmd)
        scripts = re.findall(r"(\w+)\.py\b", cmd)
        module = mods[-1].split(".")[-1] if mods else (scripts[-1] if scripts else cmd[:40])
        flag = ""
        for fl in re.findall(r"\s(--[\w-]+)", cmd):
            if fl.startswith("--backfill") or fl in _MODE_FLAGS:
                flag = fl
                break
        out.append({"sched": sched, "human": cron_human(sched), "cadence_h": cron_cadence_hours(sched),
                    "cmd": cmd, "log": os.path.basename(log.group(1)) if log else "",
                    "module": module, "flag": flag, "raw": line})
    return out


def _crontab_text() -> str:
    return _run(["crontab", "-l"])


def _lane_row(label: str, fn: str, max_h: float, entry: dict | None, tracked: bool,
              crontab_known: bool) -> dict:
    path = os.path.join(_LOGS, fn)
    sched = entry["human"] if entry else ""
    hint = _CRON_HINTS.get(fn, "")
    if not tracked:
        hint = ("Строка есть в crontab, но не в списке наблюдения health — статус по логу "
                "приблизительный (каденция выведена из расписания). " + hint).strip()
    extra = {"sched": sched, "log": fn}
    if not os.path.exists(path):
        if fn in _OPTIONAL_LOGS:
            return _row(label, "ok", "ещё не запускался (лога нет — это нормально)", hint, **extra)
        return _row(label, "warn", "нет лога (ещё не запускался?)", hint, **extra)
    try:
        age = time.time() - os.path.getmtime(path)
    except Exception:
        age = 0
    tail = _tail_lines(path)
    last = tail[-1] if tail else ""
    stale = age > max_h * 3600
    # A FAILED run ends on its error; a run that completed ends on its summary line. Walk the last
    # few lines NEWEST-first: the first completion summary (or a benign "errors=0" / flock-skip
    # line) means the latest run is alive — anything older belongs to a PRIOR run (e.g. yesterday's
    # traceback still sitting 2 lines under today's `collect:`/`stats:`, which used to keep the
    # lane red after it had recovered); the first error line before any summary = failed.
    err = False
    for ln in reversed(tail[-4:]):
        if not ln.strip():
            continue
        if _SUCCESS_RE.search(ln) or _BENIGN_RE.search(ln):
            break
        if _ERR_RE.search(ln):
            err = True
            break
    # A lane that hasn't written ANYTHING for 2× its cadence is HUNG or never started — RED too.
    # A stuck cron writes no error line at all (the 2026-09-07..09 DB-lock outage: 7 lanes sat
    # silently in a lock queue for ~2 days and only ever showed as yellow "stale"), so silence
    # past 2× the cadence must escalate and alert, not idle as a benign warn.
    hung = age > 2 * max_h * 3600
    # An ERRORED lane is RED (down) so it drives the overall badge red and can't hide among the
    # benign yellow "stale-between-runs" lanes; a merely-stale (but not errored) lane stays warn.
    status = "down" if (err or hung) else ("warn" if stale else "ok")
    note = "ОШИБКА · " if err else ("ЗАВИС/НЕ ЗАПУСКАЛСЯ · " if hung else ("УСТАРЕЛ (STALE) · " if stale else ""))
    if tracked and crontab_known and entry is None:
        note = "нет строки в crontab · " + note
        if status == "ok":
            status = "warn"
    return _row(label, status, f"{note}последний запуск {_age_str(age)} · {last or '—'}", hint, **extra)


def cron_lanes() -> list[dict]:
    text = _crontab_text()
    entries = parse_crontab(text) if text else []
    by_log = {e["log"]: e for e in entries if e["log"]}
    rows, seen = [], set()
    for label, fn, max_h in _CRONS:
        seen.add(fn)
        rows.append(_lane_row(label, fn, max_h, by_log.get(fn), tracked=True, crontab_known=bool(text)))
    # Every OTHER jobfinder crontab line — shown too, with a cadence derived from its schedule.
    for e in entries:
        if e["log"] and e["log"] not in seen:
            seen.add(e["log"])
            cad = e["cadence_h"] or 24.0
            label = f"{e['module']} {e['flag']}".strip() + " (не в списке)"
            rows.append(_lane_row(label, e["log"], max(cad * 1.5, 0.25), e, tracked=False, crontab_known=True))
    return rows


# ---- data stores ---------------------------------------------------------------------------------
_PG_HINT = ("Единственная БД CRM (mail_index, job_catalog, mass_hiring_jobs, iv_*). Пул до 32 "
            "соединений в mail_db (maxconn 8 исчерпывался при массовой подаче — 2026-08-26, не уменьшать). "
            "Если недоступна — падает всё: инбокс, каталог, статистика, лейны. DSN в backend/.env "
            "(CRM_PG_DSN).")
_LOCK_HINT = ("<b>Инцидент 2026-09-07..09:</b> зависшая сессия «idle in transaction» держала блокировку "
              "mass_hiring_jobs, ALTER TABLE … ADD COLUMN из ночного крона встал за ней в очередь "
              "(AccessExclusive), а за ним — каждый SELECT: /mass-hiring и 6 apply-лейнов висели 2 дня, "
              "молча. <b>Лечение:</b> SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
              "state='idle in transaction' AND now()-state_change > interval '5 minutes'; затем "
              "прибить ожидающий ALTER. <b>Профилактика</b> (уже в коде): ensure_schema ALTER'ит только "
              "реально отсутствующие колонки под lock_timeout=15s; на БД стоит "
              "idle_in_transaction_session_timeout=10min.")


def postgres() -> list[dict]:
    rows: list[dict] = []
    try:
        from backend.tools import mail_db
        t0 = time.monotonic()
        with mail_db.conn() as c:      # context-managed pooled connection
            cur = c.cursor()
            cur.execute("SET LOCAL statement_timeout = '2500ms'")
            cur.execute("SELECT (SELECT count(*) FROM mail_index), (SELECT count(*) FROM job_catalog), "
                        "(SELECT count(*) FROM mass_hiring_jobs)")
            n_mail, n_cat, n_mh = cur.fetchone()
            cur.execute(
                "SELECT count(*) FILTER (WHERE state = 'idle in transaction' "
                "                         AND now() - state_change > interval '5 minutes'), "
                "       count(*) FILTER (WHERE wait_event_type = 'Lock'), "
                "       count(*) FILTER (WHERE state = 'active' AND query ILIKE 'ALTER TABLE%'), "
                "       count(*) "
                "FROM pg_stat_activity WHERE datname = current_database()")
            idle_tx, waiters, alters, total = cur.fetchone()
            cur.execute("SHOW idle_in_transaction_session_timeout")
            itx = (cur.fetchone() or [""])[0]
            cur.execute("SELECT max(date_ts) FROM mail_index WHERE NOT outbound")
            last_in = (cur.fetchone() or [None])[0]
            cur.close()
        ms = (time.monotonic() - t0) * 1000
        rows.append(_row("Postgres jobfinder_crm", "ok",
                         f"доступна · {ms:.0f} мс · mail_index {n_mail} · job_catalog {n_cat} · "
                         f"mass_hiring_jobs {n_mh} · {total} сессий", _PG_HINT))
        bad = bool(idle_tx or waiters or alters)
        rows.append(_row("Блокировки Postgres", "down" if bad else "ok",
                         f"idle in transaction >5 мин: {idle_tx} · ждут блокировку: {waiters} · "
                         f"активных ALTER TABLE: {alters}", _LOCK_HINT))
        itx_s = str(itx or "0")
        rows.append(_row("idle_in_transaction_session_timeout", "ok" if itx_s not in ("0", "") else "warn",
                         itx_s if itx_s not in ("0", "") else "0 — выключен (зависшая транзакция будет держать "
                                                              "блокировку вечно)",
                         "Профилактика повторения инцидента с блокировкой: ALTER DATABASE jobfinder_crm SET "
                         "idle_in_transaction_session_timeout='10min'. Ни один легитимный код не держит "
                         "транзакцию простаивающей 10 минут."))
        if last_in:
            age = time.time() - float(last_in)
            st = "down" if age > 12 * 3600 else ("warn" if age > 4 * 3600 else "ok")
            rows.append(_row("Последнее входящее письмо (по индексу)", st, _age_str(age),
                             "Самое свежее входящее в mail_index. Тишина > 12 ч почти наверняка значит, что "
                             "входящая почта СЛОМАНА (DNS/MX — инцидент 2026-09-03, или лежит индексатор), "
                             "а не что никто не пишет: лейны шлют десятки заявок в день и ATS отвечают "
                             "кодами/подтверждениями постоянно. Смотреть «DNS takhet.com», Postfix и "
                             "jobfinder-mail-indexer."))
        else:
            rows.append(_row("Последнее входящее письмо (по индексу)", "warn", "в индексе нет входящих"))
    except Exception as exc:
        rows.append(_row("Postgres jobfinder_crm", "down", f"недоступна: {str(exc)[:90]}", _PG_HINT))
    return rows


def mysql_mailboxes() -> dict:
    hint = ("Общая MySQL amasmail (amasmail.virtual_users) — бэкенд аккаунтов Dovecot/Postfix, НЕ Postgres. "
            "Пароль читается из /home/projects/amaskills/crm/.dbpass. Без неё новым синтетическим персонам "
            "не создаётся ящик (provision_email тихо падает; fill продолжается, но ответ ATS некуда "
            "доставить), а отправка ответов как кандидат не находит пароль.")
    try:
        from backend.tools.provision_mailboxes import DBPASS_FILE
    except Exception:
        DBPASS_FILE = "/home/projects/amaskills/crm/.dbpass"
    try:
        pw = open(DBPASS_FILE, encoding="utf-8").read().strip()
    except Exception as exc:
        return _row("MySQL amasmail (ящики)", "warn", f"нет доступа к .dbpass: {type(exc).__name__}", hint)
    try:
        proc = subprocess.run(["mysql", "-N", "-uamasmail", "amasmail", "-e",
                               "SELECT count(*) FROM virtual_users WHERE domain='takhet.com'"],
                              capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
                              env={**os.environ, "MYSQL_PWD": pw})
    except Exception as exc:
        return _row("MySQL amasmail (ящики)", "down", f"mysql не отвечает: {type(exc).__name__}", hint)
    if proc.returncode != 0:
        return _row("MySQL amasmail (ящики)", "down", f"ошибка: {proc.stderr.strip()[:100]}", hint)
    n = (proc.stdout.strip().splitlines() or ["?"])[-1].strip()
    return _row("MySQL amasmail (ящики)", "ok", f"доступна · {n} ящиков @takhet.com", hint)


def maildir() -> dict:
    hint = ("Все входящие ATS-письма персон лежат тут (vmail:mail 2770 — процессы ходят под sg mail). "
            "<b>Инцидент 2026-09-03:</b> orta.study переехал на AWS и удалил A-запись mail.orta.study "
            "(цель MX takhet.com) — вся входящая почта пропала без единой ошибки в логах, все лейны "
            "потеряли подтверждения. Тишина > 12 ч → проверить DNS-строку ниже и Postfix.")
    root = _MAILDIR_ROOT
    if not os.path.isdir(root):
        return _row("Maildir takhet.com", "down", f"{root} не существует", hint)
    newest, n = 0.0, 0
    try:
        for e in os.scandir(root):
            if not e.is_dir():
                continue
            n += 1
            for sub in ("new", "cur"):
                try:
                    m = os.stat(os.path.join(e.path, sub)).st_mtime
                except Exception:
                    continue
                if m > newest:
                    newest = m
    except PermissionError:
        return _row("Maildir takhet.com", "warn", "нет доступа — процесс без группы mail (sg mail)", hint)
    writable = os.access(root, os.W_OK)
    age = time.time() - newest if newest else None
    st = "ok"
    bits = [f"{n} ящиков", "запись ok" if writable else "нет прав на запись"]
    if not writable:
        st = "warn"
    if age is None:
        st = "warn"
        bits.append("доставок не найдено")
    else:
        bits.append(f"последняя доставка {_age_str(age)}")
        if age > 12 * 3600:
            st = "down"
        elif age > 4 * 3600:
            st = "warn"
    return _row("Maildir takhet.com", st, " · ".join(bits), hint)


def disks() -> list[dict]:
    rows = []
    hint = ("uploads/prefill (резюме/скриншоты на каждую заявку), логи лейнов, Maildir 13k ящиков и "
            "профили Chromium растут постоянно. < 10 % свободно — красный: Postgres и Maildir перестанут "
            "писать. Чистить: prefill_retention, mail_retention, /tmp профили playwright.")
    seen_dev = None
    for label, path in (("Диск /", _ROOT), ("Диск Maildir", _MAILDIR_ROOT)):
        try:
            if not os.path.exists(path):
                continue
            dev = os.stat(path).st_dev
            du = shutil.disk_usage(path)
            free_pct = du.free / du.total * 100
            st = "down" if free_pct < 10 else ("warn" if free_pct < 20 else "ok")
            detail = f"{100 - free_pct:.0f}% занято · {du.free / 1e9:.0f} GB свободно из {du.total / 1e9:.0f} GB"
            if seen_dev is not None and dev == seen_dev:
                detail = "тот же раздел, что / · " + detail
            seen_dev = dev
            rows.append(_row(label, st, detail, hint))
        except Exception as exc:
            rows.append(_row(label, "warn", f"не удалось прочитать: {str(exc)[:60]}", hint))
    return rows


_STORES = [
    ("logs/unfinished.json", "Ledger «Незавершённые»",
     "bulk_log: заявки без подтверждения. Пишется атомарно под RLock из N потоков массовой подачи (2026-08-26 гонка теряла записи). Читается /unfinished и дренажем.", False),
    ("logs/submitted_jobids.json", "submitted_jobids (анти-повтор)",
     "Множество jobid с подтверждённым сабмитом — «Подать на все» исключает их. Потеря файла = повторные заявки на те же вакансии (job 20219 подавался 10×).", False),
    ("backend/data/demo_personas.json", "Реестр синтетических персон",
     "mailcrm.candidates() читает отсюда, кого индексировать. Потерянная запись = письма персоны лежат в Maildir, но в CRM не видны. НИКОГДА не TRUNCATE mail_index — восстановить можно только перерегистрацией.", False),
    ("backend/data/proxies.json", "Пул прокси",
     "Пул Bright Data + курсоры. Перевыпускается кроном в 04:45; ревалидатор в дашборде выкидывает прокси после 3 подряд провалов.", False),
    ("backend/data/apply_campaigns.json", "Кампании подачи",
     "Ежедневные кампании (имя + запрос + N/день). Пустой/отсутствующий файл = кампаний нет, крон инертен.", True),
    ("backend/data/assessment_bank.json", "Банк вопросов ассессментов",
     "Единый банк харвестера (AMCAT/SHL) + ключ ответов. Пишется атомарно (pid-tmp). Гитигнорирован — бэкапить отдельно.", False),
]


def json_stores() -> list[dict]:
    rows = []
    for rel, label, hint, optional in _STORES:
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            rows.append(_row(label, "ok" if optional else "warn",
                             "нет файла" + (" (ещё не создавался — нормально)" if optional else ""), hint, path=rel))
            continue
        try:
            stt = os.stat(path)
            size = stt.st_size
            age = time.time() - stt.st_mtime
            if size <= 40 * 2**20:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    # a wrapper dict ({"proxies": [...], "cursor": …} / {"items": {…}, "schema_version": …})
                    # is counted by its payload, not its top-level keys
                    payload = next((d[k] for k in ("proxies", "items", "personas", "jobs", "campaigns", "questions")
                                    if isinstance(d.get(k), (list, dict))), d)
                    n = len(payload)
                elif isinstance(d, list):
                    n = len(d)
                else:
                    n = 1
                shape = f"{n} записей" if isinstance(d, (dict, list)) else "скаляр"
            else:
                shape = "слишком большой для разбора на лету"
            rows.append(_row(label, "ok", f"{_fmt_mb(size)} · {shape} · изменён {_age_str(age)}", hint, path=rel))
        except Exception as exc:
            rows.append(_row(label, "down", f"НЕ ЧИТАЕТСЯ: {type(exc).__name__}: {str(exc)[:60]}", hint, path=rel))
    return rows


def bank_stats() -> dict:
    hint = ("Банк вопросов харвестера + покрытие ключом ответов (доля MCQ, у которых есть ответ). Ниже 90 % "
            "— харвестер начнёт отвечать наугад на незнакомые вопросы.")
    try:
        from backend.tools.assessment_harvester import bank, answer_key
        bank.reload()
        total = bank.size()
        cov = answer_key.coverage()
        path = bank._BANK_PATH
        age = _age_str(time.time() - os.path.getmtime(path)) if os.path.exists(path) else "?"
        keyed, mcq = cov.get("keyed", 0), cov.get("mcq", 0)
        status = "ok" if (mcq and keyed / max(1, mcq) >= 0.9) else "warn"
        return _row("Банк вопросов + ключ ответов", status,
                    f"{total} вопросов · ключ {keyed}/{mcq} MCQ · обновлён {age}", hint)
    except Exception as exc:
        return _row("Банк вопросов + ключ ответов", "warn", f"n/a: {str(exc)[:90]}", hint)


# ---- external dependencies -----------------------------------------------------------------------
def llm() -> dict:
    hint = ("Локальная модель на 127.0.0.1:8080 (отдельный pm2-процесс вне jobfinder-*). От неё зависит "
            "КАЖДЫЙ fill: тейлоринг резюме, подбор ответов, синтез персон, харвестер. Один экземпляр: при "
            "> 6 воркерах она сериализуется, fill растёт до 240 с и вылетает по таймауту "
            "(инцидент 2026-08-26 → _ADAPT_MAX=6, не поднимать).")
    try:
        import httpx
        from backend.config import settings
        base = (settings.llm_url or "").rstrip("/")
        t0 = time.monotonic()
        r = httpx.get(base + "/models", headers={"Authorization": f"Bearer {settings.llm_key}"},
                      timeout=_PROBE_TIMEOUT)
        ms = (time.monotonic() - t0) * 1000
        host = re.sub(r"^https?://", "", base).split("/")[0]
        if r.status_code != 200:
            return _row("Локальная модель", "down", f"{host} · HTTP {r.status_code}", hint)
        models = [m for m in (r.json().get("data") or []) if isinstance(m, dict)]
        ids = [m.get("id") for m in models]
        aliases = {a for m in models for a in (m.get("aliases") or [])}
        have = settings.llm_model in ids or settings.llm_model in aliases
        return _row("Локальная модель", "ok" if have else "warn",
                    f"{host} · {ms:.0f} мс · {len(ids)} моделей · настроенная модель "
                    f"{'в списке' if have else 'НЕ в списке /models (ни id, ни alias — проверить имя модели в .env)'}",
                    hint)
    except Exception as exc:
        return _row("Локальная модель", "down", f"не отвечает: {type(exc).__name__}", hint)


def proxy_rows() -> list[dict]:
    rows = []
    hint = ("Ротация egress-IP на заявку (Bright Data, зона в .env). Пустой пул: Kelly пропускается "
            "(Akamai 403), остальное идёт напрямую с датацентрового IP. Прошлый провайдер умер молча (407) "
            "→ 12/100 заполнено. Баланс BD маленький — пополнять в кабинете; refresh при пустом балансе "
            "не трогает вчерашний пул.")
    try:
        from backend.tools import proxy_pool
        s = proxy_pool.summary()
        cnt = int(s.get("count") or 0)
        lc = s.get("last_check")
        lc_s = _age_str(time.time() - float(lc)) if lc else "ещё не проверялся"
        st = "down" if cnt == 0 else ("warn" if (not lc or time.time() - float(lc) > 24 * 3600) else "ok")
        rows.append(_row("Пул прокси", st, f"{cnt} живых · проверка {lc_s}", hint))
    except Exception as exc:
        rows.append(_row("Пул прокси", "warn", f"n/a: {str(exc)[:70]}", hint))
    # the pool of phones (their mobile IPs) on the project Tailscale tailnet (mobile_proxy.py)
    mname = "Мобильные прокси (телефоны)"
    mhint = ("Телефоны в сети Tailscale отдают SOCKS-прокси (мобильные IP) — единственный egress, "
             "который Ashby не помечает как спам (с датацентрового IP отбивает все подачи). Пул находит "
             "все онлайн-телефоны сам и раскидывает подачи по кругу; ни одного онлайн — откат на пул/напрямую. "
             "Настройка: Каталог → Фильтры → Прокси → «Мобильные прокси», проверка: `mobile_proxy --check`.")
    try:
        from backend.tools import mobile_proxy
        ms = mobile_proxy.status()
        tn = ms.get("tailnet") or "?"
        n_on, n_cfg = int(ms.get("n_online") or 0), int(ms.get("n_configured") or 0)
        if not ms.get("configured"):
            rows.append(_row(mname, "info", f"не настроен · tailnet {tn}", mhint))
        elif not ms.get("enabled"):
            rows.append(_row(mname, "info", f"выключен · {n_cfg} эндпоинтов · tailnet {tn}", mhint))
        elif n_on:
            ips = ", ".join(ms.get("egress_samples") or []) or "—"
            rows.append(_row(mname, "ok", f"{n_on} онлайн из {n_cfg} · IP {ips} · tailnet {tn}", mhint))
        else:
            rows.append(_row(mname, "warn",
                             f"настроено {n_cfg}, но ни один телефон не онлайн · tailnet {tn}", mhint))
    except Exception as exc:
        rows.append(_row(mname, "warn", f"n/a: {str(exc)[:70]}", mhint))
    # phones/laptops that are Tailscale EXIT NODES, bridged to local SOCKS (tailscale_egress.py)
    ename = "Exit-node мост (телефоны)"
    ehint = ("Телефоны-exit-node (в т.ч. iPhone, который SOCKS-сервер держать не может) через свой "
             "userspace-tailscaled → локальный SOCKS → в тот же пул. Крон `*/10 tailscale_egress --sync` "
             "поднимает/убирает слоты по мере появления телефонов. Проверка: `tailscale_egress --check`. "
             "Устройства в ОДНОЙ WiFi дают ОДИН IP — для разнообразия держать телефоны на сотовой.")
    try:
        from backend.tools import tailscale_egress
        es = tailscale_egress.status()
        n_run, n_sl = int(es.get("n_running") or 0), int(es.get("n_slots") or 0)
        exits = ", ".join(s.get("exit_ip") for s in (es.get("slots") or []) if s.get("running")) or "—"
        if not es.get("enabled"):
            rows.append(_row(ename, "info", f"выключен · {n_sl} слотов", ehint))
        elif n_run:
            rows.append(_row(ename, "ok", f"{n_run} слот(ов) активно · exit-узлы {exits}", ehint))
        elif n_sl:
            rows.append(_row(ename, "warn", f"{n_sl} слотов, но ни один демон не жив (телефон отвалился?)", ehint))
        else:
            rows.append(_row(ename, "info", "включён, телефонов-exit-node сейчас нет", ehint))
    except Exception as exc:
        rows.append(_row(ename, "warn", f"n/a: {str(exc)[:70]}", ehint))
    try:
        from backend.config import settings
        zone = settings.brightdata_zone or "—"
        path = os.path.join(_LOGS, "brightdata.log")
        lines = _tail_lines(path, n=1, maxlen=4000)      # the JSON summary line is ~400 chars
        last = lines[-1] if lines else ""
        if not os.path.exists(path):
            rows.append(_row("Bright Data", "warn", f"зона {zone} · лога обновления нет", hint))
        else:
            age = time.time() - os.path.getmtime(path)
            info = ""
            try:
                d = json.loads(last)
                info = f" · {d.get('pool_count', '?')} сессий · egress {d.get('probe_ip', '?')}"
            except Exception:
                info = f" · {last[:80]}"
            bad = bool(re.search(r"error|abort|traceback|407", last, re.I))
            st = "down" if bad else ("warn" if age > 30 * 3600 else "ok")
            rows.append(_row("Bright Data", st, f"зона {zone} · обновление {_age_str(age)}{info}", hint))
    except Exception as exc:
        rows.append(_row("Bright Data", "warn", f"n/a: {str(exc)[:70]}", hint))
    return rows


def nopecha() -> dict:
    hint = ("Платный ключ решателя капч (hCaptcha на Teleperformance/iCIMS и SmartRecruiters). Без ключа "
            "TP-лейн не проходит ни одного шага; бесплатный тариф банит IP (error 12). Кончился кредит — "
            "лейн сабмитит 0, в логе tp_apply.log таймауты.")
    v = _env_value("NOPECHA_KEY")
    if not v:
        return _row("Ключ решателя капч", "down", "NOPECHA_KEY не задан", hint)
    return _row("Ключ решателя капч", "ok", f"задан ({len(v)} символов)", hint)


def telegram() -> dict:
    hint = ("Токен бота уведомлений (health --alert, mail_health, бот собесов). Если отправок ещё не было, "
            "алерты о сбоях могут не доходить — проверить TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env.")
    try:
        from backend.config import settings
        tok, chat = settings.telegram_bot_token, settings.telegram_chat_id
    except Exception:
        tok, chat = _env_value("TELEGRAM_BOT_TOKEN"), _env_value("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return _row("Telegram-уведомления", "down", "токен или chat_id не заданы", hint)
    # A cheap getMe proves the token is live WITHOUT sending anything (the token never leaves this
    # probe — no URL is logged).
    status, bits = "ok", []
    try:
        import httpx
        r = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=_PROBE_TIMEOUT)
        if r.status_code == 200 and (r.json().get("result") or {}).get("username"):
            bits.append(f"бот @{r.json()['result']['username']} отвечает")
        else:
            status = "down"
            bits.append(f"токен отклонён (HTTP {r.status_code})")
    except Exception as exc:
        status = "warn"
        bits.append(f"api.telegram.org не отвечает: {type(exc).__name__}")
    bits.append("iv-бот отдельный" if _env_value("IV_BOT_TOKEN") else "iv-бот на общем токене")
    st = _load_json(_ALERT_STATE)
    ok_ts = st.get("last_ok_send")
    if ok_ts:
        bits.append(f"последняя удачная отправка алерта {_age_str(time.time() - float(ok_ts))}")
    if st.get("active"):
        bits.append("сейчас активен алерт")
    return _row("Telegram-уведомления", status, " · ".join(bits), hint)


def _pgrep(pat: str) -> int:
    try:
        out = subprocess.run(["pgrep", "-fc", pat], capture_output=True, text=True,
                             timeout=_PROBE_TIMEOUT).stdout.strip()
        return int(out or "0")
    except Exception:
        return 0


def _unit_active(unit: str) -> str:
    return _run(["systemctl", "is-active", unit]).strip()


def mail_daemons() -> list[dict]:
    rows = []
    hints = {
        "postfix": ("Принимает входящие по MX (25) и отправляет ответы персон через submission 587 (SASL, "
                    "DKIM). systemd-юнит, не pm2. Если лежит — ни одно письмо ATS не доходит и ни один ответ "
                    "не уходит. sudo systemctl restart postfix."),
        "dovecot": ("IMAP/LMTP-доступ к Maildir и SASL-аутентификация для Postfix. systemd-юнит. "
                    "sudo systemctl restart dovecot."),
    }
    for unit, pat in (("postfix", "postfix"), ("dovecot", "dovecot")):
        act = _unit_active(unit)
        n = _pgrep(pat)
        alive = act == "active" or n > 0
        rows.append(_row(unit.capitalize(), "ok" if alive else "down",
                         f"systemd: {act or '?'} · {n} проц.", hints[unit]))
    return rows


def dns_takhet() -> dict:
    hint = ("takhet.com MX → mail.takhet.com → " + _SERVER_IP + ". <b>Инцидент 2026-09-03 13:22:</b> MX "
            "указывал на mail.orta.study; orta.study переехал на AWS и удалил эту A-запись — входящая почта "
            "всех персон терялась молча (ни ошибки, ни отказа), все лейны остались без подтверждений. "
            "Починка: у регистратора A mail.takhet.com → " + _SERVER_IP + " (DNS-only) + MX takhet.com → "
            "mail.takhet.com. Это НЕ баг бота — только DNS.")
    try:
        import dns.resolver
        res = dns.resolver.Resolver()
        res.lifetime = res.timeout = _PROBE_TIMEOUT - 0.5
        mx = sorted((r.preference, str(r.exchange).rstrip(".")) for r in res.resolve("takhet.com", "MX"))
        if not mx:
            return _row("DNS takhet.com (MX)", "down", "MX-записей нет", hint)
        target = mx[0][1]
        ips = [str(a) for a in res.resolve(target, "A")]
        st = "ok" if _SERVER_IP in ips else "warn"
        note = "" if _SERVER_IP in ips else f" (ожидался {_SERVER_IP})"
        return _row("DNS takhet.com (MX)", st, f"MX {target} → {', '.join(ips)}{note}", hint)
    except Exception as exc:
        return _row("DNS takhet.com (MX)", "down", f"не резолвится: {type(exc).__name__}: {str(exc)[:70]}", hint)


def display_stack() -> list[dict]:
    hint = ("Виртуальный дисплей :98 из pm2 jobfinder-alan-display. Xvfb — сам экран; x11vnc :5901 — "
            "VNC-сервер; websockify/noVNC :6090 — то, что открывает /vnc/ в браузере. Без Xvfb падают все "
            "headful-браузеры; без noVNC человек не может «докрутить» капчу.")
    checks = [("Xvfb :98", "Xvfb.*:98"), ("x11vnc :5901", "x11vnc"), ("noVNC :6090", "websockify|novnc")]
    rows = []
    for label, pat in checks:
        n = _pgrep(pat)
        rows.append(_row(label, "ok" if n else "down", f"{n} проц." if n else "не запущен", hint))
    return rows


def copilot() -> dict:
    hint = ("HTTP-API ко-пилота (backend/copilot.py, порт 8102): /load /release /state. Через него идут "
            "«Заполнить» в каталоге, «Докрутить», Mass Hiring dry-run и последовательные кампании. "
            "Если /state молчит, а pm2 говорит online — процесс завис в Playwright: pm2 restart "
            "jobfinder-alan-copilot.")
    try:
        import httpx
        t0 = time.monotonic()
        r = httpx.get(_COPILOT_STATE_URL, timeout=_PROBE_TIMEOUT)
        ms = (time.monotonic() - t0) * 1000
        if r.status_code != 200:
            return _row("Ко-пилот :8102 (/state)", "down", f"HTTP {r.status_code}", hint)
        try:
            url = (r.json().get("url") or "")[:70]
        except Exception:
            url = ""
        return _row("Ко-пилот :8102 (/state)", "ok", f"отвечает · {ms:.0f} мс" + (f" · {url}" if url else ""), hint)
    except Exception as exc:
        return _row("Ко-пилот :8102 (/state)", "down", f"не отвечает: {type(exc).__name__}", hint)


def nginx() -> list[dict]:
    rows = []
    hint = ("nginx-vhost jobs.systeam.kz: / → 8099 (вход в приложении), /copilot/ → 8102 и /vnc/ → 6090 "
            "(basic-auth). Обязателен default_server-блок 00-default-drop (return 444) — без него чужой "
            "домен на этот IP увидит дашборд (инцидент 2026-04-17). Сертификат — certbot.")
    act = _unit_active("nginx")
    rows.append(_row("nginx", "ok" if act == "active" else "down", f"systemd: {act or '?'}", hint))
    try:
        import httpx
        t0 = time.monotonic()
        r = httpx.get(_PUBLIC_LOGIN_URL, timeout=_PROBE_TIMEOUT, follow_redirects=False)
        ms = (time.monotonic() - t0) * 1000
        ok = r.status_code in (200, 301, 302, 303)
        rows.append(_row("jobs.systeam.kz/login", "ok" if ok else "down", f"HTTP {r.status_code} · {ms:.0f} мс", hint))
    except Exception as exc:
        rows.append(_row("jobs.systeam.kz/login", "down", f"не отвечает: {type(exc).__name__}", hint))
    return rows


# ---- system --------------------------------------------------------------------------------------
def _meminfo() -> dict:
    mt = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, _, v = ln.partition(":")
            mt[k.strip()] = int(v.split()[0]) if v.split() else 0
    return mt


def system() -> list[dict]:
    rows = []
    cores = os.cpu_count() or 1
    try:
        l1, l5, l15 = os.getloadavg()
        st = "ok" if l1 < cores * 1.5 else ("warn" if l1 < cores * 3 else "down")
        rows.append(_row("Load average", st, f"{l1:.1f} / {l5:.1f} / {l15:.1f} ({cores} ядер)",
                         "Массовая подача сама добавляет воркеров по load-average (порог 1.2×ядер). "
                         "Load > 3×ядер — воркеры и локальная модель дерутся за CPU, fill вылетает по таймауту."))
    except Exception:
        pass
    try:
        mt = _meminfo()
        total, avail = mt.get("MemTotal", 1), mt.get("MemAvailable", 0)
        pct = (1 - avail / total) * 100
        st = "ok" if pct < 85 else ("warn" if pct < 95 else "down")
        rows.append(_row("Память", st, f"{pct:.0f}% занято · {avail / 1e6:.1f} GB свободно из {total / 1e6:.0f} GB",
                         "Каждый headless-воркер ~216 MB + Chromium; адаптивный ramp останавливается при < 6 GB свободно."))
        stot, sfree = mt.get("SwapTotal", 0), mt.get("SwapFree", 0)
        if stot:
            spct = (1 - sfree / stot) * 100
            st = "ok" if spct < 80 else ("warn" if spct < 95 else "down")
            rows.append(_row("Swap", st, f"{spct:.0f}% занято · {(stot - sfree) / 1e6:.1f} / {stot / 1e6:.1f} GB",
                             "Инцидент 2026-08-26: при 12–18 воркерах массовой подачи swap ушёл в 100 %, fill'ы "
                             "по 240 с и ложные error. > 80 % — снизить число воркеров (_ADAPT_MAX=6)."))
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        rows.append(_row("Сервер", "ok", f"{cores} ядер · аптайм {_dur_str(up)}",
                         "Один VPS на все проекты (Contabo, датацентровый IP — отсюда спам-флаги Ashby и "
                         "капчи Lever/Workable). После ребута pm2 resurrect поднимает сервисы, UFW деним всё "
                         "кроме 22/25/80/443/587/993."))
    except Exception:
        pass
    try:
        import psutil
        procs, ours, browsers, chrom_total = [], 0, 0, 0
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                nm = p.info.get("name") or ""
                mi = p.info.get("memory_info")
                if mi:
                    procs.append((mi.rss, nm))
                if "chrom" in nm.lower() and "crashpad" not in nm.lower():
                    chrom_total += 1
                    if "--type=" in " ".join(p.cmdline()):
                        continue                      # renderer/gpu/utility child, not a browser
                    browsers += 1
                    if _is_our_browser(p):
                        ours += 1
            except Exception:
                continue
        top = sorted(procs, reverse=True)[:3]
        rows.append(_row("Топ-3 по памяти", "ok",
                         " · ".join(f"{nm} {rss / 2**20:.0f} MB" for rss, nm in top),
                         "Обычно node (pm2/фронты), clamd, локальная модель и Chromium. Резкий рост одного "
                         "процесса — утечка."))
        st = "ok" if ours <= 8 else ("warn" if ours <= 16 else "down")
        rows.append(_row("Chromium (браузеры JobFinder)", st,
                         f"наших {ours} · всего на сервере {browsers} браузеров / {chrom_total} процессов",
                         "Норма: ко-пилот (1) + харвестер (≤1) + текущие лейны (1–4) + воркеры массовой подачи "
                         "(≤6). Больше 8 наших браузеров вне запуска — осиротевшие headless-воркеры/recon: "
                         "pkill -f COPILOT_HEADLESS или перезапуск ко-пилота. Чужие браузеры (другие проекты "
                         "на этом сервере) в порог не входят."))
    except Exception as exc:
        rows.append(_row("Процессы", "warn", f"psutil: {str(exc)[:60]}"))
    try:
        du = shutil.disk_usage("/tmp")
        n = sum(1 for _ in os.scandir("/tmp"))
        rows.append(_row("/tmp", "ok" if du.free / du.total > 0.1 else "warn",
                         f"{n} записей верхнего уровня · {du.free / 1e9:.0f} GB свободно на разделе",
                         "Профили playwright/patchright (icims/orc recon, харвестер) и tmp-файлы ledger'ов. "
                         "Очищается при ребуте (venv в /tmp умирают — инцидент в соседнем проекте 2026-09-05)."))
    except Exception:
        pass
    return rows


_OURS_RE = re.compile(r"backend\.|/home/projects/jobfinder\b")


def _is_our_browser(p) -> bool:
    """A Chromium whose ancestry (≤3 levels: chrome ← playwright node driver ← our python) carries a
    JobFinder marker — other projects on this box run their own browsers and must not count."""
    try:
        cur = p
        for _ in range(3):
            cur = cur.parent()
            if cur is None:
                return False
            if _OURS_RE.search(" ".join(cur.cmdline())):
                return True
    except Exception:
        pass
    return False


# ---- incidents / fragile points (static, from CLAUDE.md) -----------------------------------------
_FRAGILE = [
    ("Postgres: «idle in transaction» → блокировка таблицы", "2026-09-07..09",
     "Зависшая сессия держала lock на mass_hiring_jobs; ALTER TABLE из крона встал в очередь, за ним все "
     "SELECT — /mass-hiring и 6 apply-лейнов висели 2 дня. Лечение: pg_terminate_backend для idle-in-tx > 5 мин "
     "+ ожидающего ALTER. Профилактика: idle_in_transaction_session_timeout=10min, ensure_schema без холостых ALTER."),
    ("takhet.com: MX/DNS — входящая почта пропала молча", "2026-09-03",
     "MX вёл на mail.orta.study, чью A-запись удалили при переезде. Ни одной ошибки в логах — только тишина. "
     "Починка у регистратора: A mail.takhet.com → " + _SERVER_IP + " + MX → mail.takhet.com. Индикатор здесь: "
     "«Последнее входящее письмо» и «DNS takhet.com»."),
    ("Крон харвестера без cd → ModuleNotFoundError 2 дня", "2026-09-09",
     "cron стартует в $HOME; PYTHONPATH=. указывал не туда. Правило: КАЖДАЯ строка crontab делает "
     "cd /home/projects/jobfinder (строчные). Редактировать crontab только через crontab - < file."),
    ("pm2 cwd на мёртвый /home/projects/JOBFINDER", "2026-08-21",
     "Все 4 сервиса стартовали из пустого uppercase-каталога: отдавали устаревший код и читали пустой uploads/. "
     "Пересоздавать процессы с --cwd /home/projects/jobfinder, затем pm2 save. Здесь проверяется автоматически."),
    ("AMCAT NE500 — лимит запросов на IP", "2026-09-09",
     "3 токена подряд с одного IP → NE500 и логаут на секции C/D (≈2/3 попыток). Крон де-бёрстнут до одного "
     "токена в 20 мин, concurrency 1. Прокси не помогают (ASN помечен). Не поднимать частоту."),
    ("Ко-пилот держит старый код в памяти", "2026-08-28",
     "Правки applier/, dropdowns, analyzer, tailor не применяются, пока не сделан pm2 restart jobfinder-alan-copilot; "
     "381 задач застряли на 22-часовом коде. Диагноз: один реальный /load и смотреть choice_picks[].backed."),
    ("Ashby: датацентровый IP = «flagged as possible spam»", "2026-08-25",
     "Часть сабмитов Ashby отклоняется и напрямую, и через датацентровый прокси. Лечится только residential "
     "(зона alibaba_res, $4/GB) — по решению владельца принимаем потери; такие задачи не паркуются в «Незавершённые»."),
    ("Workday: лимит активационных писем (~90/домен)", "2026-09-03",
     "После ~90 регистраций «Verify your candidate account» перестают приходить, лейн Centene стоит на sign-in. "
     "Переждать (часы), не ретраить — ретраи только продлевают троттл."),
    ("Пул соединений Postgres исчерпан → медленный дисковый скан", "2026-08-26",
     "maxconn 8 не выдерживал 12 воркеров + 3 демона; PoolError ронял инбокс на скан Maildir с диска. Сейчас "
     "maxconn 32 + ожидание до 5 с. Не уменьшать."),
    ("Прокси-провайдер умер молча (407)", "2026-08-25",
     "Аккаунт прокси протух — 88 из 100 задач упали на Page.goto. Заменён на Bright Data с ежедневным "
     "перевыпуском; баланс маленький. Смотреть «Пул прокси»/«Bright Data»."),
]


def incidents() -> list[dict]:
    return [_row(name, "info", f"последний раз: {date}", hint) for name, date, hint in _FRAGILE]


# ---- gather (parallel, bounded) ------------------------------------------------------------------
# (section key, section title, [(probe label, probe fn), ...]) — every probe runs in its own thread
# under the shared deadline; a probe may return one row (dict) or many (list).
_GROUPS = [
    ("pm2", "Сервисы (pm2)", [("pm2", pm2_services)]),
    ("crons", "Кроны", [("crontab", cron_lanes)]),
    ("data", "Данные", [("Postgres jobfinder_crm", postgres), ("MySQL amasmail (ящики)", mysql_mailboxes),
                        ("Maildir takhet.com", maildir), ("Диски", disks), ("JSON-хранилища", json_stores),
                        ("Банк вопросов", bank_stats)]),
    ("deps", "Внешние зависимости", [("Локальная модель", llm), ("Прокси", proxy_rows),
                                     ("Ключ решателя капч", nopecha), ("Telegram", telegram),
                                     ("Postfix/Dovecot", mail_daemons), ("DNS takhet.com", dns_takhet),
                                     ("Дисплей :98", display_stack), ("Ко-пилот :8102", copilot),
                                     ("nginx", nginx)]),
    ("system", "Система", [("Система", system)]),
    ("incidents", "Инциденты и хрупкие места", [("Инциденты", incidents)]),
]


def _as_rows(res, label: str) -> list[dict]:
    if res is None:
        return []
    if isinstance(res, dict):
        return [res]
    return [r for r in res if isinstance(r, dict)]


def _safe_probe(fn, label: str) -> list[dict]:
    try:
        return _as_rows(fn(), label)
    except Exception as exc:
        return [_row(label, "warn", f"проверка упала: {type(exc).__name__}: {str(exc)[:70]}")]


def safe_one(fn) -> dict:
    """Back-compat: run a single-row probe, never raise."""
    try:
        r = fn()
        return r if isinstance(r, dict) else (r[0] if r else _row(fn.__name__, "warn", "пусто"))
    except Exception as exc:
        return _row(fn.__name__, "warn", f"проверка упала: {str(exc)[:80]}")


def gather(timeout: float = _PROBE_TIMEOUT) -> dict:
    """Full health snapshot. Every probe runs in parallel under ONE deadline (`timeout` seconds after
    start, +0.5 s grace), so the page renders in < 4 s whatever hangs; a late probe shows as a warn row
    «нет ответа за N с». Returns {sections[], counts, overall, ts, elapsed} plus one key per group."""
    t0 = time.monotonic()
    probes = [(gk, label, fn) for gk, _title, plist in _GROUPS for label, fn in plist]
    timings: dict[str, float] = {}

    def _timed(fn, label):
        ts = time.monotonic()
        try:
            return _safe_probe(fn, label)
        finally:
            timings[label] = round(time.monotonic() - ts, 2)

    ex = ThreadPoolExecutor(max_workers=max(1, len(probes)), thread_name_prefix="health")
    futs = [(gk, label, ex.submit(_timed, fn, label)) for gk, label, fn in probes]
    deadline = t0 + timeout + 0.5
    results: dict[str, list[dict]] = {gk: [] for gk, _t, _p in _GROUPS}
    for gk, label, fut in futs:
        try:
            rows = fut.result(timeout=max(0.05, deadline - time.monotonic()))
        except FutTimeout:
            rows = [_row(label, "warn", f"нет ответа за {int(timeout)} с",
                         "Зонд не уложился в лимит — сама подсистема, скорее всего, висит или очень медленная. "
                         "Открыть страницу ещё раз; если повторяется — смотреть эту подсистему напрямую.")]
        except Exception as exc:
            rows = [_row(label, "warn", f"проверка упала: {str(exc)[:70]}")]
        results[gk].extend(rows)
    ex.shutdown(wait=False)

    sections = []
    counts = {"ok": 0, "warn": 0, "down": 0}
    for gk, title, _plist in _GROUPS:
        rows = results[gk]
        sc = {"ok": 0, "warn": 0, "down": 0}
        for r in rows:
            st = r.get("status", "warn")
            if st in sc:
                sc[st] += 1
                counts[st] += 1
        worst = "down" if sc["down"] else ("warn" if sc["warn"] else ("ok" if sc["ok"] else "info"))
        sections.append({"key": gk, "title": title, "rows": rows, "counts": sc, "status": worst})
    overall = "down" if counts["down"] else ("warn" if counts["warn"] else "ok")
    out = {"sections": sections, "counts": counts, "overall": overall,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed": round(time.monotonic() - t0, 2),
           "timings": dict(sorted(timings.items(), key=lambda kv: -kv[1]))}
    out.update(results)
    out["stores"] = results.get("data", [])          # back-compat aliases
    out["display"] = [r for r in results.get("deps", []) if r.get("name", "").split(" ")[0] in ("Xvfb", "x11vnc", "noVNC")]
    return out


# ---- auto-alert (push, so nobody has to open /health to notice a failure) -----------------------
# Own throttle state (kept separate from mail_health's so its recovery-key logic isn't disturbed).
_ALERT_STATE = os.path.join(_LOGS, "health_alert_state.json")


def _tg(text: str) -> bool:
    try:
        import httpx

        from backend.config import settings
        tok, chat = settings.telegram_bot_token, settings.telegram_chat_id
        if not tok or not chat:
            return False
        r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                       data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"})
        return r.status_code < 300
    except Exception:
        return False


def check_and_alert(cooldown: int = 14400) -> dict:
    """Run gather(); Telegram the owner when anything is DOWN (throttled per `cooldown`), and send ONE
    recovery note when it clears. This is the PUSH the pull-only /health tab lacked — a failing service
    or cron now pings the owner within a cron tick instead of waiting to be noticed. Never raises.
    Every RED row of every group (pm2 / crons / data / deps / system) is included — the new probes
    (DB locks, Maildir silence, DNS, the model, nginx…) alert the same way the crons do."""
    snap = gather()
    down = [f"{r.get('name')}: {re.sub(r'<[^>]+>', '', str(r.get('detail', '')))}"
            for sec in snap["sections"] for r in sec["rows"] if r.get("status") == "down"]
    st = _load_json(_ALERT_STATE)
    now = int(time.time())

    def _save(s: dict) -> None:
        _save_json(_ALERT_STATE, s)

    def _send(text: str) -> None:
        if _tg(text):
            st["last_ok_send"] = now

    if down:
        if now - int(st.get("last", 0)) >= cooldown:
            body = (f"🔴 <b>JobFinder health</b> — {_plural(len(down), 'сбой', 'сбоя', 'сбоев')} ({snap['ts']})\n\n"
                    + "\n".join("• " + d for d in down[:12]))
            _send(body)
            print(f"[health] ALERT: {len(down)} down: {down}", flush=True)
            st["last"] = now
        st["active"] = True
        _save(st)
    else:
        if st.get("active"):
            _send(f"🟢 <b>JobFinder health</b> — всё восстановилось ({snap['ts']})")
            print("[health] RECOVERED", flush=True)
        st["active"] = False
        _save(st)
    return {"overall": snap["overall"], "down": down}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Health snapshot + optional Telegram alert.")
    ap.add_argument("--alert", action="store_true",
                    help="check and Telegram the owner if anything is DOWN (cron entry point)")
    ap.add_argument("--cooldown", type=int, default=14400, help="alert throttle seconds (default 14400)")
    args = ap.parse_args()
    if args.alert:
        res = check_and_alert(cooldown=args.cooldown)
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(json.dumps(gather(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
