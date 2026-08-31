# Residential proxy — домашний IP ноутбука как egress для сервера

Пускает автоматизацию сервера (co-pilot / заполнение форм) через **домашний интернет ноутбука**,
чтобы дойти до ATS, которые блокируют датацентр-IP сервера (Teleperformance/iCIMS, Kelly/Akamai,
reCAPTCHA-Greenhouse). Maximus/Avature это не нужно — он работает и с сервера.

## Как это устроено
```
Ноут: gost (локальный HTTP-прокси 127.0.0.1:8899)
   └─ обратный SSH-туннель на сервер → сервер слушает 127.0.0.1:8120 (ТОЛЬКО loopback)
Сервер: co-pilot берёт proxy 127.0.0.1:8120 → трафик уходит в интернет ЧЕРЕЗ ноут (residential IP)
```
Пока ноут подключён — сервер предпочитает домашний IP (`proxy_pool.next_proxy()`); отключил ноут —
сервер сам откатывается на Bright Data / прямой. Публичных портов не открывается: серверная сторона
туннеля висит только на 127.0.0.1, а SSH-ключ жёстко ограничен (только этот один обратный туннель).

## Windows (основной)
1. Возьми у меня папку с тремя файлами: `install-proxy-windows.ps1`, `start-proxy-windows.bat`,
   (ключ уже вшит в .ps1).
2. Дважды кликни **`start-proxy-windows.bat`**. Он один раз скачает gost, пропишет автозапуск при
   входе (Task Scheduler) и поднимет туннель. Оставь окно открытым (или он сам поднимется после
   перезагрузки при следующем входе).
3. Проверка (в окне видно `connecting tunnel to proxy.systeam.kz`). Всё — сервер уже ходит через твой
   IP. Если всплывёт капча на сайте — открой noVNC `https://jobs.systeam.kz/vnc/` и реши её; форму
   заполняет бот.

Отключить: закрой окно + `schtasks /Delete /TN JobFinderResidentialProxy /F`.

## Mac (если понадобится)
```bash
bash install-proxy-mac.sh        # скачает gost (Intel/Apple Silicon сам), пропишет launchd, поднимет
bash install-proxy-mac.sh --run  # только туннель (это дергает автозапуск)
```
Отключить: `launchctl unload ~/Library/LaunchAgents/com.jobfinder.residentialproxy.plist`.

## Проверить, что реально идёт через твой IP
На сервере (я): `curl -x http://127.0.0.1:8120 https://ipinfo.io/ip` — должен вернуть **твой домашний
IP**, а не 173.249.18.153. `ss -tlnp | grep 8120` — должен быть только `127.0.0.1:8120` (loopback).

## Файлы
- `install-proxy-*.template` — шаблоны (в git, без ключа).
- `build_installers.py` — вставляет ключ+настройки в шаблоны → `dist/` (gitignored, отдаю тебе готовое).
- `dist/tunnel_key(.pub)` — ключ пары (gitignored). Ротация: пересоздать пару, обновить
  `/home/tunnel/.ssh/authorized_keys`, перегенерить installers.

## Безопасность (см. глобальный CLAUDE.md, инцидент 2026-04-16)
- Серверный конец туннеля — **только 127.0.0.1:8120** (`GatewayPorts no`), недоступен снаружи; порт
  наружу НЕ открыт.
- SSH-юзер `tunnel`: nologin, пароль заблокирован, ключ-only. authorized_keys:
  `restrict,port-forwarding,permitopen="127.0.0.1:1",permitlisten="127.0.0.1:8120",command="/bin/false"`
  → утёкший ключ может СОЗДАТЬ только этот один обратный туннель (ни шелла, ни `-L`, ни другого порта).
- Переподключение — экспоненциальный backoff с потолком 60с (не долбёжка).
