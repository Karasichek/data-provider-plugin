# Chat API Bridge

Плагин для [exteraGram](https://github.com/exteraSquad) (Python SDK **>= 1.4.5.0**), который
отдаёт содержимое выбранного чата как простой REST API на `127.0.0.1:16660`.

Любой клиент на устройстве (Termux-скрипты, MCP-сервер/клиент, LLM-агент, автоматизации)
может читать чат без прямого доступа к Telegram API.

## Установка

1. Скопируйте `chat_api_bridge.py` в `/data/user/0/com.exteragram.messenger/files/plugins/`
   (или используйте dev-server: `adb forward tcp:42690 tcp:42690` → команда `write_plugin`).
2. Включите плагин в настройках exteraGram.
3. Выберите чат: долгое нажатие на любое сообщение → **«Expose via Chat API»**,
   или «Pick current chat» в настройках плагина, или задайте Dialog ID вручную.

Dialog ID: у пользователей положительный (`123456789`), у групп — отрицательный
(`-987654321`), у каналов — `-100…` (`-1001234567890`). Если ID неизвестен —
откройте `GET /dialogs` и найдите нужный чат.

## Настройки

| Параметр     | Описание |
|--------------|----------|
| `enabled`    | Запуск/остановка HTTP-сервера на 127.0.0.1:16660 |
| `dialog_id`  | Целевой чат (ID) |
| `token`      | Необязательный токен авторизации |
| `allow_send` | Разрешение на реальную отправку через `POST /send` (по умолчанию off) |

Токен передаётся в заголовке `Authorization: Bearer <token>` или `X-Token: <token>`.

## Эндпоинты

```
GET  /health                     статус плагина
GET  /chat                       информация о выбранном чате
GET  /dialogs                    список чатов (для поиска dialog_id)
GET  /messages?limit=&before_id=&after_id=&order=desc|asc   чтение кэша сообщений
GET  /messages/<id>              конкретное сообщение
GET  /search?q=<text>&limit=     поиск по тексту в кэше
POST /refresh                    запросить последние 100 сообщений истории
POST /send  {"text": "...", "reply_to": <id>}   отправка (требует allow_send)

GET  /media/<id>/info            метаданные медиа
GET  /media/<id>                 файл, если скачан
GET  /media/<id>/size            размер скачанного файла
POST /media/<id>/download        запустить скачивание
```

## Медиа

Файлы отдаются из кэша Telegram-клиента (FileLoader):

1. `GET /media/<id>` — если файл уже скачан клиентом → `200`, бинарный поток
   (`Content-Type` по MIME, `Content-Length`, отдаётся чанками по 64 КБ).
   Если не скачан → `409 {"status": "not_downloaded", ...}`.
2. `POST /media/<id>/download` — запускает скачивание через FileLoader → `202`.
   Параллельные повторные запросы дедуплицируются.
3. Клиент опрашивает `GET /media/<id>`; как только файл появился на диске — он
   сразу отдаётся по HTTP, и помечается `downloaded`.

Полезные поля: `/media/<id>/info` возвращает `filename`, `mime`, `bytes_hint`,
`kind` (`document`/`photo`), `local_path`, `downloading`.

## Примеры (curl / Termux)

```bash
curl -s http://127.0.0.1:16660/health
curl -s "http://127.0.0.1:16660/messages?limit=20" | jq .
curl -s "http://127.0.0.1:16660/messages?limit=5&order=asc"
curl -s "http://127.0.0.1:16660/search?q=pizza"

curl -s http://127.0.0.1:16660/media/789/info
curl -s -X POST http://127.0.0.1:16660/media/789/download
curl -s http://127.0.0.1:16660/media/789 --output photo.jpg

curl -s -X POST http://127.0.0.1:16660/send \
  -H "Content-Type: application/json" -d '{"text": "Hello!"}'
```

С токеном:

```bash
curl -s -H "Authorization: Bearer my-token" \
  http://127.0.0.1:16660/messages?limit=1
```

## Архитектура / как это работает

- **Сбор данных.** Плагин хуками перехватывает три источника и пишет в кэш:
  - `TL_messages_getHistory` (ответ, когда клиент загружает историю при открытии/скролле чата);
  - `TL_updateNewMessage` (живые входящие/исходящие);
  - post-обработчики `TL_messages_sendMessage/sendMedia/sendMultiMedia`
    (`TL_updates` / `TL_updateShortMessage` / `TL_updateShortChatMessage` /
    `TL_updateShortSentMessage`).
- **Хранилище.** Два параллельных кэша: текстовые остветы сообщений (`dict` id → entry)
  и raw `TLRPC.Message` для медиа-резолвинга. Текстовые persistятся в
  `files/plugins/chat_api_bridge_data/cache.json` (до 3000 сообщений);
  raw-объекты живут только в памяти.
- **HTTP.** `ThreadingHTTPServer` в daemon-потоке, bind на `127.0.0.1:16660`;
  наружу (LAN/USB) не слушает — только loopback.
- **Потокобезопасность.** Доступ к общим структурам через один `Threading.Lock`;
  все тяжёлые операции идут в фоновых очередях, UI не блокируется.

## Известные ограничения

- Кэшируется не вся история, а только та, что клиент реально получал
  (скролл, обновления, `POST /refresh`).
- После перезапуска exteraGram raw-объекты сообщений теряются: текст остаётся
  в кэше, а для передачи медиа старого сообщения нужно снова открыть чат
  (или `POST /refresh` — подтянет последние 100 свежих сообщений).
- `/send` отправляет реальные сообщения в Telegram (по умолчанию отключено).
- Авторизация — простая сверка строки, без криптографии.
- Скачивание очень крупных файлов отдаётся уже скачанным файлом, а не
  стримится по ходу загрузки; прогресс достигается повторными запросами.
