# Browser Controller / Screen Detector

Один вызов --observe открывает страницу, читает её, определяет экран,
сохраняет наблюдение в RunState и выводит JSON-событие в stdout и JSONL.
Браузер закрывается в finally. Кнопки, ответы и Next не нажимаются.
dry-run без --observe сохраняет прежний bootstrap.

Snapshot содержит URL, title, DOM body text, видимые элементы с label/role/type,
enabled и ID; значения полей ввода не извлекаются. Он находится только в памяти.
Журнал содержит origin без path/query/fragment, тип, раздел, причину, размеры
данных и ID наблюдения. Не сериализовать полный RunState через asdict в журнал.

Поддержано семь ScreenType: start, instruction, question, section_transition,
review, complete, unknown. Правила проверены на локальных fixtures на английском;
базовые русские заголовки предусмотрены. Для неоднозначных headings — unknown.
Iframe/canvas и нестандартные SPA требуют дальнейшего профиля и здесь не
объявляются распознанными. Заголовок/текст вопроса не является ключом ответа.

Запросы и redirect разрешены только к явно перечисленным origins; профиль
с авторизацией, trace, HAR и storage_state не сохраняются. Страница может
выполнять собственный JavaScript и запросы: наблюдение не блокирует поведение
сайта. Реальная платформа и присланная авторизационная ссылка не проверялись.

## Запуск локальной демонстрации

Первый терминал из корня проекта:
```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures/screens
```

Второй терминал:
```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:QA_PAGE_URL = 'http://127.0.0.1:8765/question.html'
.\.venv\Scripts\python.exe -m qa_bot --observe --allow-origin http://127.0.0.1:8765
```

Для URL с авторизацией предназначена переменная окружения, а не аргумент URL
или конфигурационный файл. Не помещать credentials в историю команд, Git или
журнал. Реальный токен этим этапом не сохраняется.

Официальные API-источники:
- https://playwright.dev/python/docs/library
- https://playwright.dev/python/docs/api/class-browsercontext
