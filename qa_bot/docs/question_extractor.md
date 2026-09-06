# Question Extractor — DOM profile qa-v1

Поддержан явный синтетический DOM-профиль qa-v1: 25 type ID каталога,
5 выходных ResponseKind. Это контракт для локальных fixtures/staging;
разметка живой AMCAT не исследовалась и не считается поддержанной.

Контейнер main: data-qa-profile, data-type-id, data-question-id,
data-attempt-id, data-question-number, data-interaction.
Поля data-field: section, instruction, text, response, timer, context.
Варианты: data-option-id; порядок — DOM-порядок.
Response: data-kind, data-option-count, data-min-selections,
data-max-selections, data-roles, data-min-words.
Навигация: data-nav=submit/next/back/skip/record и уникальный id.

Извлекаются section, question ID/номер, инструкция/текст, options и их image_ref,
response contract, обычные прямоугольные HTML-таблицы, media URL из img/audio/video
и помеченных ссылок, required/minlength/maxlength/min/max/step/pattern,
таймер в секундах или MM:SS/HH:MM:SS и доступность кнопок.
Относительные media URL разрешаются относительно URL документа.
Медиа-ссылки не скачиваются и не проверяются на доступность: sample.wav
в SVAR-fixture — намеренная тестовая ссылка, не запись речи.

QuestionSpec дополнен field_constraints, remaining_seconds, navigation,
extraction_confidence и completeness. RunState хранит spec только в памяти.
ExtractionResult.ready разрешён только при отсутствии структурных ошибок.
На ошибке spec=None, состояние review_required и CLI exit 3, без продолжения.

Confidence — детерминированная оценка полноты извлечения профиля, не
статистическая вероятность: 1.0 при полной структуре, 0.9 без таймера,
0 при блокирующей ошибке. Отсутствующий таймер — None, а не 0; нулевой
таймер останавливает этап. Источник confidence и ограничения описаны здесь.

Content hash включает содержание/порядок опций/контракты/URL media; исключает
таймер, runtime identity, navigation state и confidence. Хэш не доказывает
неизменность содержимого файла по тому же URL.

DOM снимается без кликов. Элементы hidden/aria-hidden и скрытые через CSS
исключаются; варианты ниже viewport собираются, если уже существуют в DOM.
Виртуализированные/незагруженные варианты вызывают mismatch ожидаемого count.
Canvas, iframe, merged-cell tables, неизвестный профиль/тип/response и
неполная структура блокируются. OCR, прокрутка с подгрузкой, решения,
автоматические ответы и переходы в этот этап не входят.

## Локальный запуск

Первый терминал из корня проекта:
```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures/questions
```

Второй терминал:
```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:QA_PAGE_URL = 'http://127.0.0.1:8765/ANA-02.html'
.\.venv\Scripts\python.exe -m qa_bot --extract --allow-origin http://127.0.0.1:8765
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

JSONL содержит только metadata и коды ошибок, без question text, media URL,
токенов и полного spec. Старый --observe остаётся режимом классификации экрана.
