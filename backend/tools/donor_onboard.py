"""Public donor onboarding — the `/join` page. A single link you share with anyone willing to
lend their phone's internet as a Tailscale EXIT NODE for the egress pool. Flow: install Tailscale →
tap «Стать донором» (this server mints a FRESH single-use Tailscale invite via the API and redirects
them into the app to sign in) → flip the one manual toggle «Exit Node → Run as exit node». The exit
node auto-approves (ACL autoApprovers) and the `*/10 tailscale_egress --sync` cron adds it to the
pool within minutes. See tailscale_egress.py.

SECURITY: donors join as role "member" and the tailnet ACL grants them NOTHING → they are EXIT-ONLY
and cannot reach any node on the tailnet (proven by the ACL's tag/outsider DENY tests). The API token
(`backend/.ts_api_token`) is read server-side ONLY, never sent to the client or logged. The token
EXPIRES (~90 days) → minting stops working until it is refreshed; the durable fix is a scoped OAuth
client (owner, later). Mint is rate-limited per client IP.
"""
from __future__ import annotations

import threading
import time
from html import escape
from pathlib import Path

_TOKEN_PATH = Path(__file__).resolve().parent.parent / ".ts_api_token"
_API = "https://api.tailscale.com/api/v2/tailnet/-/user-invites"
_LOCK = threading.Lock()
_HITS: dict[str, list[float]] = {}      # client-ip -> recent mint timestamps
_MAX_PER_HR = 5
_WINDOW = 3600.0

_IOS_STORE = "https://apps.apple.com/app/tailscale/id1470499037"
_AND_STORE = "https://play.google.com/store/apps/details?id=com.tailscale.ipn"


def _token() -> str:
    try:
        return _TOKEN_PATH.read_text().strip()
    except Exception:
        return ""


def rate_ok(ip: str) -> bool:
    """≤ _MAX_PER_HR mints per IP per hour. Abuse only creates ignorable pending invites, but
    keep it tidy."""
    now = time.time()
    with _LOCK:
        hits = [t for t in _HITS.get(ip or "?", []) if now - t < _WINDOW]
        if len(hits) >= _MAX_PER_HR:
            _HITS[ip or "?"] = hits
            return False
        hits.append(now)
        _HITS[ip or "?"] = hits
        return True


def mint_invite() -> str | None:
    """Mint a fresh single-use Tailscale user-invite; return its URL, or None on any failure.
    In-process httpx (token in the Authorization/basic-auth, never in a process argv). Never raises."""
    tok = _token()
    if not tok:
        return None
    try:
        import httpx
        r = httpx.post(_API, json=[{"role": "member"}], auth=(tok, ""), timeout=12,
                       headers={"Content-Type": "application/json"})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data and isinstance(data[0], dict):
                url = data[0].get("inviteUrl")
                if url and url.startswith("https://login.tailscale.com/"):
                    return url
    except Exception:
        return None
    return None


def _platform(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "android"
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "ios"
    return "other"


_CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#f4f6fb;color:#12172a}.wrap{max-width:520px;margin:0 auto;padding:24px 18px 56px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}.logo{width:38px;height:38px;border-radius:10px;
background:#0c47c2;color:#fff;display:grid;place-items:center;font-weight:800;font-family:Georgia,serif;font-size:18px}
h1{font-size:22px;margin:0 0 6px}.lead{margin:0 0 22px;color:#556}
.step{background:#fff;border:1px solid #e4e8f2;border-radius:16px;padding:16px 16px;margin:12px 0;display:flex;gap:13px}
.n{flex:0 0 28px;height:28px;border-radius:50%;background:#eef2fb;color:#0c47c2;font-weight:700;display:grid;
place-items:center;font-size:14px}.step h3{margin:2px 0 4px;font-size:16px}.step p{margin:0;color:#556;font-size:14.5px}
.btn{display:block;text-align:center;text-decoration:none;border-radius:14px;padding:15px 18px;font-weight:700;
font-size:16.5px;margin:8px 0}.btn.primary{background:#0c47c2;color:#fff;box-shadow:0 6px 18px rgba(12,71,194,.28)}
.btn.ghost{background:#fff;color:#0c47c2;border:1.5px solid #cdd8f0}.tip{background:#eef7ee;border:1px solid #cfe8cf;
color:#255f2b;border-radius:12px;padding:12px 14px;font-size:14px;margin:16px 0}.warn{background:#fff5e6;
border:1px solid #f0d9a8;color:#7a5306}.small{color:#8a93a8;font-size:12.5px;text-align:center;margin-top:22px}
code{background:#eef2fb;border-radius:6px;padding:1px 6px;font-size:13px}
"""


def render_join_page(user_agent: str = "", error: str = "") -> str:
    plat = _platform(user_agent)
    store = _IOS_STORE if plat == "ios" else _AND_STORE if plat == "android" else ""
    store_label = ("Установить Tailscale (App Store)" if plat == "ios"
                   else "Установить Tailscale (Google Play)" if plat == "android"
                   else "Установить Tailscale")
    if plat == "ios":
        install_btn = f'<a class="btn ghost" href="{store}">{store_label}</a>'
        toggle = ("Открой приложение <b>Tailscale</b> → нижнее меню <b>Exit Node</b> → "
                  "<b>Run Exit Node</b> → подтверди.")
    elif plat == "android":
        install_btn = f'<a class="btn ghost" href="{store}">{store_label}</a>'
        toggle = ("Открой приложение <b>Tailscale</b> → меню (☰) → <b>Exit Node</b> → "
                  "<b>Run as exit node</b>.")
    else:
        install_btn = (f'<a class="btn ghost" href="{_IOS_STORE}">App Store (iPhone)</a>'
                       f'<a class="btn ghost" href="{_AND_STORE}">Google Play (Android)</a>')
        toggle = ("В приложении <b>Tailscale</b>: <b>Exit Node</b> → <b>Run as exit node</b>.")

    err_html = ""
    if error == "rate":
        err_html = ('<div class="tip warn">Слишком много попыток за час. Подожди немного и обнови '
                    'страницу.</div>')
    elif error == "mint":
        err_html = ('<div class="tip warn">Не удалось создать ссылку-приглашение прямо сейчас. '
                    'Попробуй позже или напиши владельцу.</div>')

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0c47c2"><title>Стать донором</title><style>{_CSS}</style></head>
<body><div class="wrap">
  <div class="brand"><div class="logo">JF</div><b>Донор интернета</b></div>
  <h1>Поделись интернет-выходом телефона</h1>
  <p class="lead">Займёт пару минут. Твой телефон становится «выходом в интернет» — к твоим данным,
  файлам и переписке никто доступа не получает. Можно отключить в любой момент.</p>
  {err_html}
  <div class="step"><div class="n">1</div><div><h3>Установи Tailscale</h3>
    <p>Бесплатное приложение.</p></div></div>
  {install_btn}
  <div class="step"><div class="n">2</div><div><h3>Войди по кнопке</h3>
    <p>Нажми ниже — откроется вход в Tailscale. Войди своим Google/Apple. Готово — ты в сети.</p></div></div>
  <a class="btn primary" href="/join/go">Стать донором →</a>
  <div class="step"><div class="n">3</div><div><h3>Включи «Exit Node»</h3>
    <p>{toggle}</p></div></div>
  <div class="tip">Безопасно: ты отдаёшь только интернет-выход. Ты <b>не видишь</b> чужие устройства
  и <b>не имеешь</b> доступа к чужой сети — только твой телефон служит выходом. Хочешь разный IP —
  включи <b>мобильные данные</b> (не общий Wi-Fi).</div>
  <p class="small">После шага 3 подключение добавится автоматически в течение ~10 минут.</p>
</div></body></html>"""
