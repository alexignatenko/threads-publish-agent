# Справочник: CLI и Threads API

Читай, когда нужен точный параметр, имя поля или команда. Для обычной работы хватает
шпаргалки в SKILL.md.

`$T` = `python3 <SKILL_DIR>/scripts/threads.py`. Все команды печатают JSON.
Общие флаги: `--profile <имя>` (несколько аккаунтов), `--home <путь>` (другой каталог данных).

Коды возврата: `0` успех, `1` ошибка, `2` неверные аргументы, `3` профиль не настроен,
`4` нет валидного токена.

---

## Команды CLI

### Настройка

| Команда | Что делает |
| --- | --- |
| `config init [--force]` | Создаёт профиль: `config.json`, `voice.md`, `queue.json`, `credentials.json`. |
| `config show` | Вся конфигурация + секреты в замаскированном виде. |
| `config set КЛЮЧ=ЗНАЧЕНИЕ …` | Меняет настройки. Ключи через точку, списки — как JSON. |
| `config get [КЛЮЧ]` | Читает значение. |
| `config path` | Пути ко всем файлам профиля. |

### Токены

В онбординге используются первые пять команд. Остальные — вход через браузер (OAuth),
скилл по этому пути не ведёт; они оставлены для ручного использования.

| Команда | Что делает |
| --- | --- |
| `auth save-token --token <TOKEN> --no-exchange` | Сохраняет готовый токен как есть. Так сохраняется маркер из генератора: он уже 60-дневный, обменивать не на что. |
| `auth refresh [--force]` | Продлевает 60-дневный токен (токену должно быть ≥24 ч). App Secret не нужен. |
| `auth ensure` | Продлевает только если срок подходит к концу. |
| `auth status` | Состояние токена: срок, возраст, права. |
| `auth logout` | Удаляет токен, оставляет app_id/app_secret. |
| `auth set-app --app-id … --app-secret … --redirect-uri …` | OAuth: сохраняет данные приложения. |
| `auth url [--scopes …] [--state …]` | OAuth: строит ссылку авторизации. |
| `auth exchange --code <CODE>` | OAuth: `code` → короткоживущий → 60-дневный токен. |
| `auth long-lived` | Обменивает короткоживущий токен на 60-дневный. Нужен App Secret. |

### Диагностика

| Команда | Что делает |
| --- | --- |
| `doctor [--no-refresh]` | Полная проверка + автопродление токена. Запускай первым делом. |
| `selftest` | Оффлайн-проверка сборки запросов, сеть не нужна. |
| `me` | Профиль по токену, обновляет `account.*` в конфиге. |
| `quota` | Лимиты API за 24 ч + локальные счётчики. |

### Публикация

Без `--yes` ничего не отправляется: возвращается `"dry_run": true` и поле `would_send`
с точными параметрами, которые ушли бы в API.

| Команда | Что делает |
| --- | --- |
| `post` | Один пост или ответ. |
| `reply --to <ID>` | Ответ (синоним `post --reply-to`). |
| `chain --file chain.json` / `chain --text … --text …` | Цепочка: каждый следующий — ответ на предыдущий. |
| `carousel --image URL --image URL --text …` | Карусель из 2–20 медиа. |

Флаги содержимого. Все перечисленные работают в `post`, `reply` и `queue add`.
В `chain --text` применимы только `--text`, `--reply-to` и `--reply-control`: остальные
относятся к одному посту, а в цепочке их несколько — задавай их у нужного поста
в файле `chain --file`.

| Флаг | Значение |
| --- | --- |
| `--text` | Текст, до 500 символов. `-` читает stdin, `@файл.txt` читает файл. |
| `--image URL` | Публичный HTTPS-URL картинки. |
| `--video URL` | Публичный HTTPS-URL видео. |
| `--alt-text` | Alt-текст к медиа, до 1000 символов. |
| `--link URL` | `link_attachment` — ссылка-превью, только для текстового поста. |
| `--topic-tag` | Тема, одна на пост, без `.` и `&`, символ `#` отрезается. |
| `--reply-to ID` | Публикуем как ответ на этот пост. |
| `--quote ID` | Цитируем этот пост (только `post`). |
| `--reply-control` | `everyone` / `accounts_you_follow` / `mentioned_only`. |
| `--countries RU,KZ` | Гео-гейт по ISO-кодам. |

Флаги публикации: `--yes` (отправить по-настоящему), `--wait N` (пауза перед проверкой
контейнера), `--timeout N` (максимум на обработку медиа), `--delay N` (пауза между постами
цепочки, только `chain`).

### Чтение

| Команда | Что делает |
| --- | --- |
| `list [--limit N] [--since] [--until]` | Мои посты. Даты — ISO или unix. |
| `get --media-id ID` | Один пост со всеми полями. |
| `replies --media-id ID [--conversation]` | Ответы к посту / вся ветка. |
| `replies --mine` / `replies --mentions` | Мои ответы / упоминания меня. |
| `hide --reply-id ID [--unhide] --yes` | Скрыть или вернуть ответ. |
| `insights --media-id ID [--metrics …]` | Метрики поста. |
| `insights [--since] [--until]` | Метрики аккаунта. |
| `history [--limit N]` | Локальный журнал публикаций агента. |

### Очередь

| Команда | Что делает |
| --- | --- |
| `queue add --at ISO [--note …]` + флаги содержимого или `--file` | Поставить публикацию. |
| `queue list [--due] [--status …]` | Показать очередь. |
| `queue rm ID …` | Удалить записи. |
| `queue run [--yes] [--id ID] [--limit N]` | Опубликовать наступившее. |
| `queue cron [--minute N]` | Строка crontab для автозапуска. |

---

## Настройки config.json

| Ключ | По умолчанию | Смысл |
| --- | --- | --- |
| `account.user_id`, `account.username` | заполняются `me`/`doctor` | Чей аккаунт. |
| `api.base_url`, `api.version` | `https://graph.threads.net`, `v1.0` | Менять не нужно. |
| `defaults.reply_control` | `everyone` | Кто может отвечать на новые посты. |
| `defaults.timezone` | `UTC` | Для расписания. |
| `defaults.language` | `ru` | Язык публикаций. |
| `defaults.chain_delay_seconds` | `3` | Пауза между постами цепочки. |
| `defaults.video_wait_seconds` | `30` | Пауза перед проверкой видео-контейнера. |
| `defaults.container_timeout_seconds` | `300` | Максимум на обработку медиа. |
| `voice.configured`, `voice.summary` | `false`, `null` | Статус профиля голоса. |
| `safety.require_yes_flag` | `true` | Без `--yes` ничего не публикуется. |
| `safety.max_posts_per_day` | `10` | Локальный предохранитель (API даёт 250). |
| `safety.max_replies_per_day` | `50` | Локальный предохранитель (API даёт 1000). |
| `safety.blocked_words` | `[]` | Публикация с этими словами блокируется. |
| `auth.redirect_uri`, `auth.scopes` | — | Данные OAuth. В онбординге не используются. |
| `auth.refresh_when_days_left` | `10` | За сколько дней до конца продлевать токен. `0` — не продлевать. |
| `auth.allow_refresh` | `true` | `false` — никогда не продлевать токен самостоятельно. Нужно, когда тот же токен используется другим сервисом. |

Переменные окружения переопределяют файл с секретами и на диск не записываются:
`THREADS_ACCESS_TOKEN`, `THREADS_APP_ID`, `THREADS_APP_SECRET`, `THREADS_USER_ID`.
Каталог данных — `THREADS_AGENT_HOME` (по умолчанию `~/.threads-agent`).

---

## Что под капотом: Threads Graph API

Знать полезно, чтобы понимать ошибки. Ходить в API напрямую не нужно — это делает CLI.

**База:** `https://graph.threads.net/v1.0`. Токен передаётся параметром `access_token`.

### Публикация — всегда два шага

1. `POST /{threads-user-id}/threads` — создаёт контейнер, возвращает `id`.
   Параметры: `media_type` (`TEXT` / `IMAGE` / `VIDEO` / `CAROUSEL`), `text`, `image_url`,
   `video_url`, `is_carousel_item`, `children`, `reply_to_id`, `quote_post_id`, `reply_control`,
   `link_attachment`, `alt_text`, `topic_tag`, `allowlisted_country_codes`.
2. `POST /{threads-user-id}/threads_publish?creation_id={id}` — публикует, возвращает `id` поста.

Между шагами для видео и карусели нужно дождаться готовности:
`GET /{container-id}?fields=status,error_message` → `status` = `IN_PROGRESS`, `FINISHED`,
`ERROR`, `EXPIRED`, `PUBLISHED`. Публиковать можно только `FINISHED`.

Карусель: сначала создаются дети с `is_carousel_item=true` и `media_type=IMAGE|VIDEO`
(без текста), затем родитель с `media_type=CAROUSEL` и `children=id1,id2,…`.

Цепочка: первый пост обычный, каждый следующий — контейнер с `reply_to_id` предыдущего поста.

### Чтение

- `GET /me?fields=id,username,…` — профиль.
- `GET /{user-id}/threads?fields=…&since=&until=&limit=` — свои посты.
- `GET /{media-id}?fields=…` — один пост.
- `GET /{media-id}/replies` и `/{media-id}/conversation` — ответы и вся ветка.
- `GET /{user-id}/replies`, `GET /{user-id}/mentions` — свои ответы и упоминания.
- `POST /{reply-id}/manage_reply` с `hide=true|false` — скрыть ответ.
- `GET /{media-id}/insights?metric=views,likes,replies,reposts,quotes,shares`.
- `GET /{user-id}/threads_insights?metric=…&since=&until=` — метрики аккаунта.
- `GET /{user-id}/threads_publishing_limit?fields=quota_usage,config,reply_quota_usage,reply_config`.

### Токены

- Авторизация: `https://threads.net/oauth/authorize` с `client_id`, `redirect_uri`, `scope`,
  `response_type=code`. Код живёт 1 час, одноразовый.
- Код → токен: `POST https://graph.threads.net/oauth/access_token`
  (`client_id`, `client_secret`, `grant_type=authorization_code`, `redirect_uri`, `code`).
  Токен короткоживущий — 1 час.
- Короткий → долгий: `GET /access_token?grant_type=th_exchange_token&client_secret=…&access_token=…`
  → 60 дней.
- Продление: `GET /refresh_access_token?grant_type=th_refresh_token&access_token=…`.
  Токену должно быть не меньше 24 часов и он не должен быть просрочен. Не продлённый
  за 60 дней токен умирает окончательно.
- Права: `threads_basic` (обязательно), `threads_content_publish` (публикация),
  `threads_read_replies`, `threads_manage_replies`, `threads_manage_insights`.

### Лимиты

- 250 постов за 24 часа (карусель считается одним постом).
- 1000 ответов за 24 часа.
- 500 символов текста, 1000 символов alt-текста, 2–20 элементов карусели.

### Чего API не делает

Не редактирует и не удаляет посты. Не публикует по расписанию. Не поддерживает опросы.
Не принимает загрузку локальных файлов — только публичные HTTPS-URL. Не даёт публиковать
в аккаунты, не добавленные как тестировщики, пока приложение не прошло App Review.
