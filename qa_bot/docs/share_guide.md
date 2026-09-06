# Начните здесь — QA-бот и банк заданий

Это переносимый комплект для разработки и синтетического QA, **не готовый
автоматический исполнитель реального assessment**. Ограничения и живой результат
смотрите в `qa_bot/docs/live_verification.md`.

## Что включено

- `qa_bot/`: исходный Python-проект, тесты, локальные fixtures, конфигурация без секретов.
- `SHL_QA_architecture_RU.md`, `SHL_QA_task_type_catalog_RU.md`: архитектура и каталог.
- `SHL_answers_RU.md`, `SHL_answers_all.csv`: подготовленные рекомендации.
- `data/questions.jsonl`: 1251 содержательная запись, все `candidate`.
- `data/assets/`: исходные скриншоты; индекс связывает их с вопросами и SHA-256.
- `MANIFEST.json`: полный состав и контрольные суммы файлов.

Это не официальный ключ ответов. В архиве нет утверждённых runtime-ответов.
Personality содержит условный умеренный профиль, не статистическую норму.
Similar search не разрешает автоматически отвечать на похожий вопрос.

## Установка на Windows

Распакуйте архив, откройте PowerShell в папке `qa_bot`. Нужен Python 3.11+;
фактическая проверка проведена на Windows с Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m qa_bot --config configs/offline.toml
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Установка загружает зависимости и Chromium, но не запускает assessment и не
подключает платные сервисы. Для Linux/macOS используйте `.venv/bin/python` и
`PYTHONPATH=src`. Эта платформа отдельно не проверялась.

## Восстановить базу из переносимых данных

```powershell
.\.venv\Scripts\python.exe -m qa_bot.knowledge.import_log --source ../data/questions.jsonl --answers ../SHL_answers_all.csv --database runs/knowledge.sqlite3
```

Локальная SQLite-база создаётся у получателя, а не копируется из личного окружения
автора. Импортированные записи не получают `approved`. Утверждать ответ можно
только после проверки соответствия конкретному QuestionSpec и медиа.

## Безопасная диагностика

```powershell
.\.venv\Scripts\python.exe -m qa_bot.diagnostics page --allow-origin https://YOUR-AUTHORIZED-HOST --seconds 120
.\.venv\Scripts\python.exe -m qa_bot.diagnostics speech --seconds 120
```

URL и ключ вводятся в скрытом запросе программы, не как аргументы команды.
Не вставляйте секреты в файлы, терминальную историю или переписку. Ключ держится
в памяти процесса; долговременного хранилища секретов в пакете нет.
Для автоматизированного запуска предусмотрен `--stdin-secrets` через приватный
pipe с одной JSON-записью (`url` либо `api_key`), без сохранения входа.

Speech по умолчанию выполняет только read-only проверку аккаунта и голосов.
`--generate` отдельно разрешает максимум два синтетических запроса TTS/STT,
которые расходуют бесплатную квоту. Платный тариф/перерасход/нехватка квоты
останавливают работу; автоматических покупок нет. Значение остатка в preflight
относится к моменту **до** генерации, не гарантирует остаток после неё.
Короткие operation timeout и watchdog остаются; общего ограничения 20 минут нет.

## Наш тестовый media API

```powershell
.\.venv\Scripts\python.exe -m qa_bot.staging.media_api
```

Сервер доступен только на `127.0.0.1:8766`; не открывайте этот mock в Интернет.
Схема запросов, idempotency и ответов: `qa_bot/docs/answer_system.md`.
Приём mock-сервером не равен отправке ответа в SHL.

## Чего в архиве нет

Ключей, токенов, cookies, сессий, личных runtime-баз, `.venv`, бинарников браузера,
сырых логов действий и внутренних capture/transcript-идентификаторов.
Вместо raw capture logs включены тексты заданий, варианты, графические источники
и рекомендации. Скриншоты сохранены без изменения пикселей; это не гарантия
отсутствия любой персональной информации внутри изображений. Перед передачей
третьим лицам дополнительно проверьте изображения и право распространять материалы.
Архив подготовлен локально; в Интернет ничего не опубликовано.

## Следующие работы

1. Рабочая авторизация в свежей сессии, затем read-only профиль реального layout.
2. Реальная проверка LLM в изолированном окружении без инструментов и секретов.
3. Разрешённый источник listening и speaking API стенда; synthetic STT/TTS уже проверены.
4. Human review и преобразование candidate corpus в runtime QuestionSpec.
5. General-purpose OCR для изображений; сейчас OCR поддерживает synthetic-5x7-v1.
6. Recovery и полноценный сценарий на staging; в production executor стоит запрет.
