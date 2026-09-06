# База, Answer Engine, речь и staging-исполнение

## Точное повторное использование собранного банка

`HistoricalAnswerResolver` загружает `data/questions.jsonl` и
`SHL_answers_all.csv`, группирует только полностью одинаковые раздел, инструкцию,
текст и порядок вариантов и запрещает загрузку при конфликте ответов. При новом
показе сохранённая подпись варианта сопоставляется с текущим DOM option ID, поэтому
старые технические идентификаторы не переиспользуются вслепую. Fuzzy-поиск служит
только подсказкой и не выдаёт ответ.

Поля `official_answer_key=false` сохраняются в результате: рекомендации банка
не объявляются официальным ключом. Точный повтор означает воспроизводимость
предыдущего выбранного ответа, а не доказанную оценку платформы.

## Граница реализации

Это набор работающих Python API и контрактно проверенных адаптеров, не готовый
автономный клиент SHL. Текущий CLI наблюдения не запускает решение/действия.
Живая проверка выполнена с разрешения пользователя; результат и ограничения:
[live_verification.md](live_verification.md). Прохождение assessment не подтверждено.

Поток: QuestionSpec → exact approved lookup → при отсутствии synthetic LLM →
строгий JSON → Python-вычисления при наличии expression tree → validator →
candidate → ручное approve → повторный exact lookup → staging executor.
Similar search всегда остаётся подсказкой человеку, не маршрутом исполнения.

## База

QuestionBank использует SQLite и versioned SHA-256 канонического JSON.
Нормализация — только Unicode NFC и пробелы. Сохраняются регистр, отрицания,
числа, знаки, единицы, инструкция, порядок/ID вариантов, таблицы, ограничения
и контракт ответа. У медиа обязателен SHA-256 байтов; URL не заменяет хеш.
При поиске дополнительно сравнивается весь canonical JSON, не только digest.
Новый question_id привязывается к ответу лишь после exact hit и новой валидации.
Approved запись защищена от перезаписи candidate. Approve требует reviewer/reason;
это локальный human-review API, не полноценная многопользовательская авторизация.

Импортированы 1251 исходных записей questions.jsonl и рекомендации из
SHL_answers_all.csv в runs/knowledge.sqlite3. Все — candidate; approved=0.
Исходные данные не объявляются официальным ключом. Corpus хранится отдельно
от runtime answers: corpus_candidates(text) помогает review, но не выдаёт
автоматических approved попаданий. Для переноса в runtime нужно явное
сопоставление с проверенным QuestionSpec и проверка медиа.
SQLite, аудио и исходный лог не коммитятся.

Повторяемый импорт:
```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python -m qa_bot.knowledge.import_log --source <questions.jsonl> --answers ../SHL_answers_all.csv --database runs/knowledge.sqlite3
```
Использовать Python из .venv, как в README.

## LLM

AnswerEngine принимает injected client.complete(question, timeout).
Принимается только строгий набор JSON-полей из configs/answer_schema.json.
Нет selector, command или executable code. Unknown/non-synthetic, missing media,
неполный вопрос, неправильный ID/hash, низкая уверенность, ошибка/timeout →
abstain. В LLM payload не входят URL медиа и исходные browser observations.
Self-reported confidence — дополнительный порог, не доказательство правильности.

CodexCLIClient — опциональный транспорт для native Codex executable:
ephemeral, read-only, stdin input, structured output, timeout, без shell interpolation.
Обнаруженная tool activity отклоняет результат. Это не доказательство отсутствия
действий до получения результата: перед реальным использованием нужен отдельный
проверенный изолированный профиль без инструментов/секретов и интеграционный smoke.
На этом этапе проверен только login status (ChatGPT); реальный LLM-вызов не выполнен.
Auth-файлы не читались/копировались. Обычный OpenAI API отдельно не подключён.

Официальные источники:
[вход Codex](https://learn.chatgpt.com/docs/auth),
[неинтерактивный режим](https://learn.chatgpt.com/docs/non-interactive-mode).
Повторный approved hit вообще не вызывает LLM; это кэш/review, не обучение модели.

## Вычисления

Разрешены add/sub/mul/div/percent/proportion, decimal string leaves, bounded tree.
Расчёт Decimal независимо сверяется через Fraction. Единицы: m/km/cm/ft,
s/min/h, kg/g; несовместимые величины отклоняются. table_number читает явно
заданную ячейку; match_number принимает только единственное точное числовое
совпадение. Нет eval/exec, произвольных функций и кода от модели.
Это проверяет арифметику, не правильность перевода условия в expression tree.
Визуальное чтение графиков, сложные единицы и приблизительное округление не заявлены.

## STT/TTS ElevenLabs — только Free

ElevenLabsProvider формирует STT multipart и TTS JSON. Реальные запросы выключены
по умолчанию. Для ElevenLabsHTTP нужны явное enabled, budget 1–10 запросов,
ключ из защищённого окружения либо памяти процесса через diagnostics. Перед generation выполняется
GET /v1/user/subscription: разрешён только free без can/allowed-to-extend;
неизвестные поля/остаток/платный тариф → stop. Консервативный резерв квоты —
3000 для STT и длина текста для TTS, не точный расчёт цены. Покупки,
pay-as-you-go и автоматический upgrade отсутствуют.

Free-план и действующие квоты проверять в
[официальных тарифах](https://elevenlabs.io/pricing/api);
[создание API-ключа](https://elevenlabs.io/docs/api-reference/authentication).
Ключ принят реальным API; Free TTS/STT synthetic round-trip прошёл с точным совпадением 22 слов.
Нужен именно голос, доступный этому Free-аккаунту: произвольный Voice Library ID
не считается разрешённым. Карта/платная подписка не оформлялись.

STT принимает явно разрешённый синтетический WAV. Ключ кэша включает SHA-256
аудио, модель и язык. Сохраняются текст, язык, language probability, word timestamps.
exp(word.logprob) используется как эвристика уверенности, не как калиброванная
вероятность; language probability не подменяет уверенность транскрипции.
Если logprob отсутствует — confidence=None, результат на review.
Одна повторная попытка при низком качестве; ошибки транспорта не повторяются
автоматически. attach_transcript связывает текст только с совпавшим audio SHA.

TTS: exact text + voice + model + settings + format → cache. Только allowlisted voice.
Поддержаны PCM16 mono 16000 Hz, обёрнутый в проверяемый WAV, и MP3 44.1 kHz /
128 kbps. `SpeechReplayBank` сохраняет MP3 на диске, один раз готовит mono PCM16
16 kHz WAV и связывает оба файла с точным вопросом. Повторный вопрос проверяет
контрольные суммы и возвращает запись без запроса к ElevenLabs. Отдельный журнал
сохраняет профиль, тест и вопрос каждого использования.

`ChromiumFakeMicrophone` атомарно ставит WAV по стабильному пути. Интеграционный
тест подтвердил ненулевой `getUserMedia`-сигнал в настоящем Chromium. Это готовая
транспортная часть для разрешённого тестового браузера; обработчик Record/Stop и
проверка принятия записью конкретного live assessment ещё не реализованы.
Автоматического захвата входящих вопросов из аудиопотока assessment нет: нужен
отдельный разрешённый источник аудио.

## Наш mock media API

```powershell
.\.venv\Scripts\python.exe -m qa_bot.staging.media_api
```

Слушает только 127.0.0.1:8766. POST /v1/responses:
question_id, audio_hash, idempotency_key, wav_base64, synthetic=true.
Проверяет WAV/размер/duration/hash. Повтор с тем же ключом возвращает receipt;
изменение question/audio с тем же ключом → 409.
GET /v1/responses/{key} возвращает metadata receipt.
Статус accepted_mock означает приём нашим стендом, не ответ на реальном тесте.
Сервер держит receipts в памяти; перезапуск их очищает. Для удалённого staging
нужны TLS, auth, persistent store и отдельная реализация порта.

## Validator и исполнитель

Проверяются question_id/content_hash, актуальный вопрос/таймер, response kind,
варианты/количество/роли, confidence, минимальное число слов и длина текста.
Неизвестные ограничения поля блокируют действие.
ActionExecutor работает через StagingBrowser только для явно разрешённых
staging origins и qa-v1 marker; production AMCAT запрещён.
Выбор вариантов и заполнение textarea подтверждаются чтением значений.
next/back/submit выбираются из allowlist и доступной navigation. Submit выключен
без отдельного allow_final_submit. Изменение вопроса во время действия →
unknown, автоматических повторов нет. Role-specific SJT controls пока блокируются.
Нельзя считать кнопку next безвредной на реальной платформе — там может быть
неявная отправка; реальный execution adapter не реализован.

AnswerPipeline останавливает candidate до действия. Speak-to-mock принимает
явно reviewed текст, привязанный к текущему question_id/content_hash.
Генерация и утверждение speaking-draft не автоматизированы.

## Что ещё требуется до live

1. Разрешённый источник реального listening: synthetic STT/TTS уже прошли quality gate.
2. Изолированный real LLM transport smoke; сейчас Codex CLI transport не запускался.
3. Проверенные layout/asset/audio adapters реальной платформы; текущий qa-v1
   не превращается в универсальный парсер после добавления базы.
4. Human review/утверждение ответов, особенно неоднозначных и Personality.
5. Полный сценарий на нашем стенде; live-проверка уже разрешена, но не дала готового QuestionSpec.

В свежем Python-контексте открывается Login; никаких ответов не отправлено.
