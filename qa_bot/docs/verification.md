# Проверки каркаса

## TP-016 timing diagnostic — 2026-09-06

- Платформа показала официальный экран `Your test is now complete. Thank you!`;
  снимок сохранён в `runs/batch-15/TP-016/evidence/completion.png`.
- Попытка остаётся диагностической и не входит в 15 чистых проходов: до установки
  исправления ранний SVAR Section A/B имел пропуски захвата и ответов.
- После стабилизации прямой replay для Listen and Repeat стартовал с задержкой
  0 мс; Free Speech начался примерно через 0,1 мс после `Speak Now`, длина
  ответа 30,456 с при окне 45 с.
- Полностью завершены поздние блоки: WriteX 436/436 символов, Personality 72/72
  вопросов, Basic Analytical Ability 19/19 вопросов.
- В JobFinder профиль Penelope Robinson проверенно отмечен как `Пройдено`.
- Реальный сбой `fetch()` для signed S3 audio воспроизведён. Исправление сначала
  использует `fetch`, затем единственный XHR GET той же allowlisted URL, замечает
  динамический `audio/source src` до `Speak Now`, ограничивает повторы и не пишет
  query-подпись в manifest.
- Отдельный локальный soak из 15 последовательных вопросов дал 15/15 успешных
  replay через наблюдавшийся на живой странице fallback `fetch → XHR`: задержка
  старта 0,2–0,7 мс, средняя 0,453 мс, 15/15 XHR fallbacks, 15/15 prompt
  captures, retries/failures = 0. Отчёт сохранён в
  `runs/validation/2026-09-06-timing-soak-15.json`.
- Если запись началась до завершения prefetch, loopback теперь фиксирует
  `recording_unprepared` и не запускает поздний звук. Полный локальный suite
  после этого ограничения и точного распознавания видимой фазы: 197 tests OK;
  compileall и diff check прошли.

## Strict loop / local STT / exact audio replay — 2026-09-06

- Полный локальный набор: 154 tests OK (23.008 s), без skipped; `compileall` и
  `git diff --check` прошли.
- Реальный локальный macOS TTS smoke без `ELEVENLABS_API_KEY` создал ненулевой
  MP3/WAV. Повтор точного вопроса взят из банка с тем же SHA и с нулём локальных
  и внешних генераций.
- Нативный screen recorder создал читаемый MOV контрольного участка; полный
  TP-015 будет записываться только по области теста без адресной строки и токена.
- Natively runtime + `distil-whisper/distil-small.en` распознали контрольную
  запись дословно: `The world is such a small place.`; Moonshine tiny/base в
  сравнении потеряли последнее слово и не выбраны моделью по умолчанию.
- До STT измеряются peak/RMS/activity; полностью тихий WAV отклоняется без запуска
  модели. Исходный файл сохраняется отдельно от результата распознавания.
- Прямой audio loopback проверен в настоящем локальном Chromium: страница получила
  ненулевой сигнал и ровно один replay без TTS.
- Импорт существующего MP3 проверен: повтор возвращает идентичные байты, а SHA,
  источник профиль/тест/вопрос и число повторов сохраняются.
- Строгий цикл засчитывает вопрос только после полной экстракции, ответа,
  валидации, DOM-ack и перехода на другой identity; отрицательные сценарии не
  вызывают apply и завершаются записью причины.
- Loopback HTTP bridge принимает байты только на `127.0.0.1`, только с указанного
  HTTPS-origin и токеном; положительный и отрицательный сценарии проверены.
- Отдельный Chromium integration test подтвердил HTTPS assessment-origin →
  loopback capture после выдачи браузеру разрешения `local-network-access`;
  сохранённые байты совпали полностью. В живой сессии это разрешение ещё не выдано.
- Из исходных 208 точных текстовых групп resolver безопасно активирует 206:
  2 группы с image-only вариантами остаются выключены до привязки по SHA медиа.
  175 активных групп имеют несколько появлений; конфликтов ответа нет.
- Batch tracker принимает ровно 15 уникальных профилей и не засчитывает попытку
  при пропуске, случайном ответе, capture gap, незавершённом разделе или отсутствии
  финального сообщения платформы.
- Сквозной аудит реального корпуса построил и провалидировал 1243 предложения:
  Personality 859, Analytical 226, Sales 120, Computer Simulation 32 и WriteX 6;
  ошибок сопоставления/валидатора — 0. Шесть speech-записей отправлены в отдельный
  аудиопоток, две image-only записи оставлены заблокированными.
- Реальный изолированный Codex CLI smoke для нового synthetic-вопроса вернул
  строгое JSON-предложение и корректное дерево `percent(200,15)` с выбором `30`;
  subprocess не сообщил tool/file/web действий и завершился с exit 0.
- Второй реальный smoke проверил именно речевой решатель: для аудиотранскрипта
  о цвете неба возвращено одно актуальное предложение из 8 слов, прошедшее
  content-hash, schema, confidence и word-count gates.
- TP-014 использовался как диагностика: запись вопроса 21 имела ненулевой сигнал,
  но вопросы 14–20 ранее получили пустой микрофон. Поэтому попытка не считается
  чистым проходом. TP-015 дошёл до тестового Read and Speak перед первым
  отправляемым аудиоответом; 15 чистых попыток TP-015–TP-029 ещё не выполнены.

## Diagnostics / live smoke / distribution — 2026-09-06

- Baseline: e8c012e, clean; 96 tests OK (32.507 s).
- Финально: 116 tests OK (32.479 s), pip check / diff check / source secret-marker scan OK.
- Новые проверки: Free account/voice selection, exact HTTP paths, MIME, sanitized
  error, hidden-input contract, budgets, consent/login gates, delayed DOM,
  open Shadow DOM, data sanitization/hash/path guards и ISO language hint/cache.
- Реальный ElevenLabs Free synthetic TTS→STT прошёл: 22/22 слова, язык 1.0,
  word confidence 0.99877, 7.755 s WAV. Пороги 0.9 сохранены.
- По token URL свежий Chromium получает Login; во встроенной сессии виден
  Data Protection Notice. Ответы/согласия/submit не отправлялись.
- Полный source+reference package собирается по allowlist, без secrets/raw
  capture logs/личных SQLite-баз, с manifest/SHA-256/CRC-проверкой.
- Coverage не измерялся; реальные LLM, платформа и полный autonomous loop
  не объявляются проверенными. Подробности: live_verification.md.

## Answer system / speech / staging — 2026-09-06

- Baseline: clean Git eb9bdfd; 73 tests OK (29.313 s).
- Финально: 96 tests OK (30.037 s), без skipped; отдельно 78 unit tests OK.
- +23 теста: candidate/approved, exact/media/order protection, corpus quarantine,
  validator, arithmetic, fake LLM/cache, pipeline candidate stop, STT/TTS/cache,
  Free-only gate, media API/client и Chromium staging actions.
- Импорт: 1251 исходных записей, approved=0; runs/knowledge.sqlite3 исключён из Git.
- Проверена авторизация Codex CLI через ChatGPT (без чтения credential values).
  Реальных запросов к LLM и ElevenLabs не было. Voice ID/API key отсутствуют.
- Chromium подтвердил выбор/чтение значения, заполнение textarea, approved-bank
  pipeline без LLM, блокировку final submit и production origin.
- pip check, diff check и secret-marker scan зелёные.
- API Free-режима подготовлен, но не проверен на реальном ElevenLabs аккаунте.
- Assessment-токен в этом этапе не использовался: требуется отдельное подтверждение.
- Coverage не измерялся; back/next и Codex subprocess transport пока не имеют
  полноценной live-интеграционной проверки. Это не завершённый autonomous test runner.

## OCR — 2026-09-06

- Baseline: clean Git 3401b49; 56 tests OK (29.552 s).
- После OCR: 73 tests OK (28.044 s), без skipped.
- 17 новых тестов: 16 unit/async integration и один Chromium HTTPS test.
- Проверены текст/цифры, PBM table fixture, chart labels, DOM-first,
  confidence/conflict/empty/hash/coordinates, provider replacement и RunState.
- HTTPS token URL проверен с intercepted fixture, без внешней сети;
  токен/распознанный текст не попадают в metadata JSONL.
- pip check и git diff --check зелёные. Coverage не измерялся.
- После уточнения пользователя live-ссылка отдельно открыта встроенным браузером:
  SHL Data Protection Notice доступен. Checkbox/Continue не использовались.
  Это подтверждение доступа, не проверка Python OCR на реальном вопросе.
- Синтетический pixel OCR не является general-purpose screenshot OCR.

## Question Router — 2026-09-06

- Исходный Git HEAD 957fb85, worktree перед этапом чистый.
- Полный набор: 56 tests OK (26.798 s), без skipped; прежде было 42.
- 12 новых unit-тестов: все 25 каталоговых типов и 11 GEN-* расширений,
  ANA-05 ambiguity, contract/media conflicts, confidence, missing adapter,
  stale decision, offline stubs и RunState.
- 2 новых Chromium integration tests: routing/сброс старого состояния и
  CLI → metadata JSONL, exit 0/3, ноль действий.
- GEN-* проверены на синтетических QuestionSpec, не на live DOM.
- Решатели и внешние сервисы не подключены. Процент покрытия не измерялся.

## Question Extractor — 2026-09-06

- Перед изменениями: clean Git, HEAD b4b7362; 27 прежних тестов OK (11.973 s).
- После изменений: 42 tests OK (23.463 s), без skipped.
- 25 типов каталога проверены unit и настоящими Chromium integration subtests.
- 27 HTML question-fixtures: 25 типов, unknown и CSS-hidden; один SVG.
- Проверены обязательные поля, identity, options/count/duplicate IDs, roles,
  таблицы, media refs, ограничения, таймеры, навигация, hash и confidence.
- Unknown формат очищает старый spec, переводит RunState в review_required;
  CLI возвращает 3 и журналирует только metadata/errors.
- pip check и git diff --check зелёные; распространённые secret markers не найдены.
- Поддержан только DOM-профиль qa-v1. OCR/live AMCAT и media playback не проверялись.

## Browser Controller / Screen Detector

- Python 3.14.6, Playwright 1.62.0, Chromium 151.0.7922.34.
- Полный набор: 27 tests, OK (10.382 s), без skipped.
- 8 browser/integration tests; один параметризованный тест проверяет все 7 экранов.
- 9 локальных HTML-fixtures: 7 типов + неоднозначный экран + iframe.
- Проверены URL/title/DOM/elements, enabled/hidden, close и повторный close,
  отказ по origin, redirect failure cleanup, RunState и CLI→JSONL.
- Синтетический токен из query отсутствует в журнале и repr RunState.
- pip check: No broken requirements found; git diff --check без ошибок.
- Live-страница AMCAT не проверялась; предоставленный токен не сохранялся.
- Неподдержанный iframe даёт unknown. Полного SPA/frame/layout coverage пока нет.

## Предыдущий этап: минимальный каркас

- Python 3.14.6, Windows; отдельное .venv создано командой из README.
- `python -m unittest discover -s tests -v`: 19 tests, OK.
- `python -m qa_bot --config configs/offline.toml --mode dry-run`: exit 0,
  skeleton_ready, dry_run_ready, 0 actions, 0 initialized adapters.
- Unit-тест bootstrap запрещает socket.socket; вызовов сети нет.
- Проверены отрицательные сценарии моделей и конфигурации, пропущенный файл,
  неизвестные поля, live-mode, некорректные лимиты и NaN confidence.
- Поиск распространённых secret markers в исходниках/настройках: совпадений нет.
- Build/pip install не проверены: setuptools отсутствует. Запуск и тесты через
  PYTHONPATH не требуют сборки или сторонних пакетов.

Исторические проверки минимального каркаса относятся к моделям и bootstrap.
Текущие Browser/Extractor проверки описаны выше; OCR/LLM/STT/TTS,
решение вопросов и recovery не реализованы.
