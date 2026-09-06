# Question Router

Router выбирает адаптер, но не решает вопрос и не вызывает handle.
Существующий QuestionSpec и его ResponseContract сохранены:
смысловой тип не равен формату ответа (numerical может быть single_choice).

```text
Browser → ScreenDetector → QuestionExtractor → QuestionSpec
                                              ↓
                                        QuestionRouter
                                              ↓
                           RouteDecision → RunState → metadata JSONL
                                              ↓
                            остановка (handle не вызывается)
```

## Контракты

- QuestionAdapter: kind: AdapterKind; async handle(QuestionSpec) → AdapterResult.
- Все 12 регистраций используют StubQuestionAdapter: not_implemented,
  answer=None; никаких вычислений, кликов, файловых или сетевых операций.
- QuestionRouter.route(spec) → RouteDecision(adapter, confidence, reason,
  question_id, content_hash).
- resolve(decision, spec) повторно проверяет маршрут и identity/hash, возвращает
  адаптер без вызова. Изменённый или заблокированный маршрут отклоняется.
- route_state(state) сохраняет selected_adapter, routing_confidence,
  routing_reason и phase=routed либо review_required.
- Новое наблюдение очищает предыдущий выбор. processed_count не увеличивается.

## Таблица маршрутов

| Каталог / явный синтетический тип | Адаптер |
| --- | --- |
| GEN-SINGLE | single_choice |
| GEN-MULTIPLE | multiple_choice |
| ANA-01, ANA-03, GEN-NUMERICAL | numerical |
| ANA-05 с фразой word pair/analogy, GEN-VERBAL | verbal |
| ANA-05 с фразой unlike the others/order would, GEN-LOGICAL | logical |
| ANA-02, ANA-04, GEN-TABLE | table_chart |
| WRITEX-01, GEN-TEXT | free_text |
| GEN-AUDIO: audio input → text response | audio |
| SVAR-01, GEN-SPEAKING: audio response | speaking |
| GEN-CODING | coding |
| PERS-01, SALES-01, GEN-SJT | sjt_personality |
| COMP-01 … COMP-16 | ui_simulation |

GEN-* — расширение для синтетических QuestionSpec на уровне router.
DOM-extractor их пока не принимает; поддержка реальных заданий этих категорий
не заявляется. UI simulation сохранён для совместимости с 16 типами каталога.
GEN-NUMERICAL использует существующий TEXT; числовая валидация — будущий этап.
GEN-SJT моделирует именно BEST/WORST, не произвольный формат SJT.

## Защитные проверки

Unknown ID не направляется в общий single choice по догадке. Проверяются
completeness, unresolved_regions, extraction_confidence ≥ 0.8, ненулевой
известный остаток времени, соответствие раздела и response kind.
Для соответствующих типов обязательны таблица, image/audio ref или BEST/WORST.
Для ANA-05 отсутствие либо конфликт текстовых признаков означает остановку.
Это ограниченные правила синтетического профиля, не универсальный классификатор.

Confidence = min(extraction_confidence, confidence правила); 1.0 для явного
контракта, 0.85 для ANA-05, 0.0 при остановке. Это оценка правила, не вероятность
правильного ответа. Неизвестный таймер допускается, истёкший — нет.

## Локальный запуск

В первом терминале из корня проекта:
```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures/questions
```

Во втором:
```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:QA_PAGE_URL = "http://127.0.0.1:8765/ANA-01.html"
.\.venv\Scripts\python.exe -m qa_bot --route --allow-origin http://127.0.0.1:8765
```

CLI завершится после журнала: exit 0 при выбранном адаптере, 3 при остановке,
2 при ошибке конфигурации/наблюдения. Ни adapter.handle, ни действия не вызываются.

## Будущие сервисы — только план

Внедрять реализации через существующие ports и конструкторы конкретных
адаптеров, собирая registry в composition root. Router не должен создавать
сервисных клиентов или содержать credentials.

- OCR: до routing, на этапе perception; добавить provenance и confidence,
  затем повторить completeness. Не выдавать незаполненный spec за готовый.
- LLM: после routing, только в адаптерах задач, которым он нужен; вернуть
  AnswerProposal по строгой схеме, затем независимый validator.
- STT: для input audio; проверить разрешение/доступность медиа, получить
  транскрипт с provenance. Speaking не означает обязательный STT.
- TTS: только mock/staging API для синтетических speaking-ответов; не
  подключать виртуальный микрофон и не отправлять ответ автоматически.

Сначала fake-реализации, contract tests, timeout/cancellation и ошибки сервиса;
затем отдельное разрешение на интеграцию, лимиты и защищённое хранение секретов.
Содержимое вопроса остаётся данными, не инструкциями для инструментов.
Ни один из перечисленных сервисов на этом этапе не подключён.
