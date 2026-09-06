# Python QA-бот — development preview

Python 3.11+, Playwright/Chromium, unittest. Набор модулей для синтетического QA
и read-only диагностики. **Автономное прохождение реального assessment не готово.**

## Быстрый запуск (PowerShell)

Из этой папки:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m qa_bot --config configs/offline.toml
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Активация окружения не нужна. Установка скачивает зависимости и Chromium;
тесты используют синтетические fixtures и локальный HTTP, не живой assessment.
Для пакетной установки доступен `pyproject.toml`; build backend устанавливается
отдельно. Проверенный способ запуска — через PYTHONPATH, без сборки EXE.

## Что реализовано

- QuestionSpec, AnswerProposal, ActionPlan, ActionResult, RunState и порты адаптеров.
- Read-only Browser Controller, Screen Detector, extractor профиля qa-v1,
  маршрутизация 25 каталоговых типов в 12 адаптеров-заглушек.
- DOM-first OCR для synthetic-5x7-v1, confidence/provenance/conflict checks.
- SQLite exact hash + canonical match, candidate/approved и отдельный source corpus.
- Strict AnswerEngine с injected LLM, `abstain`, Decimal/Fraction calculator,
  validator и single-question pipeline. Реальный Codex LLM transport не проверен.
- Локальный macOS TTS, Natively/Distil-Whisper STT, MP3/WAV-кэш, точная память
  речевых ответов, журнал профилей, Free-only gate и наш mock media API.
- Подготовка WAV как тестового микрофона Chromium; реальный Chromium получил
  и измерил ненулевой сигнал из подготовленного файла.
- Opt-in прямой loopback для точного `Listen and Repeat`; синтетический Chromium
  подтвердил один полный повтор без пустой записи.
- Строгий контроллер полного прохода ведёт append-only журнал и засчитывает вопрос
  лишь после проверки ответа и доказанного перехода на новый экран. Любая
  неопределённость останавливает попытку вместо случайного клика или пропуска.
- Исторический resolver использует только точное совпадение раздела, инструкции,
  текста и порядка вариантов. Он проверяет конфликт ответов между прошлыми
  появлениями и привязывает сохранённый ответ к текущим DOM option ID.
- Batch tracker засчитывает ровно 15 уникальных тестов только при полном совпадении
  запланированных/пройденных разделов, равенстве answered/captured, нуле пропусков,
  нуле случайных ответов и наличии финального подтверждения платформы.
- Staging executor: выбор/ввод/проверка значения. Production origins заблокированы.
- Скрытый ввод credentials, ограниченные диагностические subprocess и безопасный
  сборщик полного локального архива с manifest/hashes.

CLI `--observe`, `--extract`, `--route` только читает страницу и останавливается.
Неизвестные frame/shadow/layout/низкая уверенность требуют review. Чтение текста
из открытого Shadow DOM не означает поддержку его QuestionSpec-профиля.
Финальный submit выключен; никаких LLM-команд в executor нет.

## Диагностика

```powershell
.\.venv\Scripts\python.exe -m qa_bot.diagnostics page --allow-origin https://YOUR-AUTHORIZED-HOST
.\.venv\Scripts\python.exe -m qa_bot.diagnostics speech
```

Программа сама запросит URL/ключ скрытым вводом. Не передавайте секреты через
аргументы команд или файлы. `--stdin-secrets` предназначен только для приватного
pipe. `--report runs/reports/result.json` сохраняет метаданные без DOM/секретов.
`--generate` для speech включает до двух коротких запросов за счёт бесплатной
квоты; без него выполняются только GET-проверки. Покупок/апгрейда нет.
`--seconds` — watchdog конкретного запуска, общий лимит 20 минут снят.
Exit 0 — соответствующая диагностика успешна, 3 — stop/review, 2 — ошибка входа.

## Документация

- [Результат живой проверки](docs/live_verification.md)
- [Полная инструкция для получателя архива](docs/share_guide.md)
- [База, LLM, расчёты, STT/TTS, staging](docs/answer_system.md)
- [Точная память MP3 и браузерный аудиовход](docs/speech_replay.md)
- [Каталог](docs/task_catalog.md) и [архитектура](docs/architecture.md)
- [Browser Controller](docs/browser_controller.md), [extractor](docs/question_extractor.md),
  [router](docs/question_router.md), [OCR](docs/ocr.md)
- [История проверок](docs/verification.md)

В полном комплекте соседние `data/` и `SHL_answers_all.csv` позволяют восстановить
1251 candidate-запись. Runtime-базы, исходные capture logs и секреты не включены.
Рекомендации не являются официальным ключом; approved=0. Сначала human review,
не автоматическое повышение статуса.
