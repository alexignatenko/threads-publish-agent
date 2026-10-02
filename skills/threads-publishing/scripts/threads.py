#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""threads.py — CLI для Threads Graph API.

Только стандартная библиотека Python 3.8+. Ничего не нужно устанавливать.
Все команды печатают JSON в stdout. Ошибки — JSON в stdout + ненулевой код возврата.

Коды возврата:
    0  — успех
    1  — ошибка (API, сеть, валидация)
    2  — неверные аргументы
    3  — профиль не настроен (запусти `config init`)
    4  — нет валидного токена (запусти `auth ...`)

Где лежат данные:
    $THREADS_AGENT_HOME или ~/.threads-agent
      profiles/<profile>/config.json       — несекретные настройки
      profiles/<profile>/credentials.json  — токены, chmod 600
      profiles/<profile>/voice.md          — профиль голоса
      profiles/<profile>/queue.json        — локальная очередь отложенных публикаций
      profiles/<profile>/history.jsonl     — журнал опубликованного

Переменные окружения переопределяют credentials.json:
    THREADS_ACCESS_TOKEN, THREADS_APP_ID, THREADS_APP_SECRET, THREADS_USER_ID
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "1.5.0"

DEFAULT_API_BASE = "https://graph.threads.net"
DEFAULT_API_VERSION = "v1.0"
AUTHORIZE_URL = "https://threads.net/oauth/authorize"

TEXT_LIMIT = 500
ALT_TEXT_LIMIT = 1000
TOPIC_TAG_LIMIT = 50
CAROUSEL_MIN = 2
CAROUSEL_MAX = 20

ALL_SCOPES = [
    "threads_basic",
    "threads_content_publish",
    "threads_read_replies",
    "threads_manage_replies",
    "threads_manage_insights",
]

REPLY_CONTROLS = ["everyone", "accounts_you_follow", "mentioned_only"]

MEDIA_FIELDS = (
    "id,media_product_type,media_type,media_url,permalink,owner,username,text,"
    "timestamp,shortcode,thumbnail_url,children,is_quote_post,has_replies,"
    "reply_audience"
)
REPLY_FIELDS = (
    "id,text,username,permalink,timestamp,media_type,media_url,shortcode,"
    "is_reply,replied_to,root_post,hide_status,has_replies,reply_audience"
)
MEDIA_METRICS = "views,likes,replies,reposts,quotes,shares"
ACCOUNT_METRICS = "views,likes,replies,reposts,quotes,followers_count"

DEFAULT_CONFIG = {
    "profile": "default",
    "account": {
        "user_id": None,
        "username": None,
    },
    "api": {
        "base_url": DEFAULT_API_BASE,
        "version": DEFAULT_API_VERSION,
    },
    "defaults": {
        "reply_control": "everyone",
        "timezone": "UTC",
        "language": "ru",
        # Пауза между постами одной цепочки, сек. Порядок ответов важен.
        "chain_delay_seconds": 3,
        # Сколько ждать обработки видео перед публикацией, сек.
        "video_wait_seconds": 30,
        "container_timeout_seconds": 300,
    },
    "voice": {
        # Заполняется при онбординге, подробности — в voice.md
        "configured": False,
        "summary": None,
    },
    "safety": {
        # Без --yes команды post/chain/queue run только показывают план.
        "require_yes_flag": True,
        # Локальный лимит поверх лимита API (250 постов / 1000 ответов в 24ч).
        "max_posts_per_day": 10,
        "max_replies_per_day": 50,
        # Слова, при наличии которых публикация блокируется.
        "blocked_words": [],
    },
    "auth": {
        "redirect_uri": None,
        "scopes": ["threads_basic", "threads_content_publish"],
        # За сколько дней до истечения обновлять долгоживущий токен. 0 — не обновлять.
        "refresh_when_days_left": 10,
        # false — никогда не продлевать токен сам. Нужно, когда тот же токен
        # используется другим сервисом и его строку нельзя менять.
        "allow_refresh": True,
    },
    "created_at": None,
    "updated_at": None,
}


# --------------------------------------------------------------------------- #
# Ошибки и вывод
# --------------------------------------------------------------------------- #


class CliError(Exception):
    """Ошибка, которую печатаем как JSON и выходим с кодом."""

    def __init__(self, message, code=1, hint=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.details = details


def emit(payload, code=0):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    return code


def mask(value, keep=4):
    if not value:
        return None
    value = str(value)
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * 6 + "(%d симв.)" % len(value)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    """Парсит ISO-8601, включая суффикс Z. Наивное время считаем UTC."""
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise CliError("Не разобрал дату/время: %r (нужен ISO-8601)" % value, code=2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Хранилище профиля
# --------------------------------------------------------------------------- #


class Store:
    def __init__(self, home=None, profile="default"):
        base = home or os.environ.get("THREADS_AGENT_HOME") or "~/.threads-agent"
        self.home = Path(base).expanduser()
        self.profile = profile
        self.dir = self.home / "profiles" / profile

    # --- пути ---
    @property
    def config_path(self):
        return self.dir / "config.json"

    @property
    def credentials_path(self):
        return self.dir / "credentials.json"

    @property
    def voice_path(self):
        return self.dir / "voice.md"

    @property
    def queue_path(self):
        return self.dir / "queue.json"

    @property
    def history_path(self):
        return self.dir / "history.jsonl"

    @property
    def plan_path(self):
        return self.dir / "plan.md"

    def paths(self):
        return {
            "home": str(self.home),
            "profile": self.profile,
            "profile_dir": str(self.dir),
            "config": str(self.config_path),
            "credentials": str(self.credentials_path),
            "voice": str(self.voice_path),
            "plan": str(self.plan_path),
            "queue": str(self.queue_path),
            "history": str(self.history_path),
        }

    # --- config ---
    def exists(self):
        return self.config_path.exists()

    def load_config(self, required=True):
        if not self.config_path.exists():
            if required:
                raise CliError(
                    "Профиль %r не настроен (нет %s)" % (self.profile, self.config_path),
                    code=3,
                    hint="Запусти: threads.py config init",
                )
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with self.config_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg)

    def save_config(self, cfg):
        self.dir.mkdir(parents=True, exist_ok=True)
        cfg["updated_at"] = iso(now_utc())
        write_json_atomic(self.config_path, cfg)

    # --- credentials ---
    def load_credentials(self):
        creds = {}
        if self.credentials_path.exists():
            with self.credentials_path.open("r", encoding="utf-8") as fh:
                creds = json.load(fh)
        for env_key, key in (
            ("THREADS_ACCESS_TOKEN", "access_token"),
            ("THREADS_APP_ID", "app_id"),
            ("THREADS_APP_SECRET", "app_secret"),
            ("THREADS_USER_ID", "user_id"),
        ):
            value = os.environ.get(env_key)
            if value:
                creds[key] = value
                creds.setdefault("source", {})[key] = env_key
        return creds

    def save_credentials(self, creds):
        self.dir.mkdir(parents=True, exist_ok=True)
        # Не записываем на диск то, что пришло из окружения.
        clean = {k: v for k, v in creds.items() if k != "source"}
        for key, env_key in (creds.get("source") or {}).items():
            if os.environ.get(env_key) == clean.get(key):
                clean.pop(key, None)
        write_json_atomic(self.credentials_path, clean)
        try:
            os.chmod(self.credentials_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    # --- queue ---
    def load_queue(self):
        if not self.queue_path.exists():
            return []
        with self.queue_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("items", data) if isinstance(data, dict) else data

    def save_queue(self, items):
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.queue_path, {"items": items})

    # --- history ---
    def append_history(self, entry):
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_history(self, limit=None):
        if not self.history_path.exists():
            return []
        rows = []
        with self.history_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        if limit:
            rows = rows[-limit:]
        return rows

    def count_since(self, hours, kind=None):
        cutoff = now_utc() - timedelta(hours=hours)
        count = 0
        for row in self.read_history():
            if kind and row.get("kind") != kind:
                continue
            ts = row.get("published_at")
            dt = parse_iso(ts) if ts else None
            if dt and dt >= cutoff:
                count += 1
        return count


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def deep_merge(base, override):
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_request(method, url, params=None, data=None, timeout=60, retries=3):
    """Запрос к Graph API. Возвращает распарсенный JSON или бросает CliError."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    body = None
    headers = {"Accept": "application/json", "User-Agent": "threads-publish-agent/%s" % __version__}
    if data is not None:
        payload = {k: v for k, v in data.items() if v is not None}
        body = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            parsed = None
            try:
                parsed = json.loads(raw)
            except ValueError:
                pass
            api_error = (parsed or {}).get("error") or {}
            message = api_error.get("message") or raw[:500] or exc.reason
            retryable = exc.code in (429, 500, 502, 503, 504)
            last_error = CliError(
                "Threads API вернул %s: %s" % (exc.code, message),
                code=1,
                hint=auth_hint(exc.code, api_error),
                details={
                    "status": exc.code,
                    "error": api_error or raw[:500],
                    "url": strip_secrets(url),
                },
            )
            if not retryable or attempt == retries - 1:
                raise last_error
        except urllib.error.URLError as exc:
            last_error = CliError(
                "Сеть недоступна: %s" % exc.reason,
                code=1,
                hint="Проверь интернет и прокси (HTTPS_PROXY).",
                details={"url": strip_secrets(url)},
            )
            if attempt == retries - 1:
                raise last_error
        except ValueError as exc:
            raise CliError("Не разобрал ответ API: %s" % exc, code=1)
        time.sleep((2 ** attempt) + random.random())
    raise last_error  # pragma: no cover


def auth_hint(status, api_error):
    subcode = str(api_error.get("error_subcode") or "")
    etype = str(api_error.get("type") or "")
    if status in (400, 401) and ("OAuth" in etype or subcode in ("463", "467")):
        return "Токен истёк или отозван. Запусти: threads.py auth refresh (или auth save-token заново)."
    if status == 403:
        return (
            "Нет прав. Проверь, что в приложении добавлено нужное разрешение "
            "(threads_content_publish для публикации) и что аккаунт принял приглашение "
            "тестировщика. После изменения разрешений выпусти маркер заново."
        )
    if status == 429:
        return "Достигнут лимит API. Проверь: threads.py quota"
    return None


def strip_secrets(url):
    return re.sub(r"(access_token|client_secret)=[^&]+", r"\1=***", url)


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #


class Threads:
    def __init__(self, store, cfg, creds):
        self.store = store
        self.cfg = cfg
        self.creds = creds
        api = cfg.get("api", {})
        self.base = (api.get("base_url") or DEFAULT_API_BASE).rstrip("/")
        self.version = api.get("version") or DEFAULT_API_VERSION

    # --- вспомогательное ---
    @property
    def token(self):
        token = self.creds.get("access_token")
        if not token:
            raise CliError(
                "Нет access_token",
                code=4,
                hint="Сгенерируй маркер в настройках приложения Threads, раздел "
                "«Генератор маркеров пользователя», и сохрани: "
                "threads.py auth save-token --token … --no-exchange",
            )
        return token

    @property
    def user_id(self):
        uid = (
            self.creds.get("user_id")
            or (self.cfg.get("account") or {}).get("user_id")
            or "me"
        )
        return uid

    def url(self, path):
        return "%s/%s/%s" % (self.base, self.version, path.lstrip("/"))

    def get(self, path, params=None):
        params = dict(params or {})
        params["access_token"] = self.token
        return http_request("GET", self.url(path), params=params)

    def post(self, path, data=None, params=None):
        data = dict(data or {})
        data["access_token"] = self.token
        return http_request("POST", self.url(path), params=params, data=data)

    # --- профиль / лимиты ---
    def me(self):
        return self.get(
            "me",
            {"fields": "id,username,name,threads_profile_picture_url,threads_biography"},
        )

    def publishing_limit(self):
        return self.get(
            "%s/threads_publishing_limit" % self.user_id,
            {"fields": "quota_usage,config,reply_quota_usage,reply_config"},
        )

    # --- публикация ---
    def create_container(self, params):
        return self.post("%s/threads" % self.user_id, data=params)

    def publish_container(self, creation_id):
        return self.post(
            "%s/threads_publish" % self.user_id, data={"creation_id": creation_id}
        )

    def container_status(self, container_id):
        return self.get(container_id, {"fields": "status,error_message,id"})

    def media(self, media_id, fields=MEDIA_FIELDS):
        return self.get(media_id, {"fields": fields})

    def wait_for_container(self, container_id, wait_seconds, timeout_seconds):
        """Ждём, пока контейнер перейдёт в FINISHED. Возвращает последний статус."""
        deadline = time.time() + max(timeout_seconds, 1)
        interval = 3
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, timeout_seconds))
        last = {}
        while True:
            last = self.container_status(container_id)
            status = (last.get("status") or "").upper()
            if status in ("FINISHED", "PUBLISHED"):
                return last
            if "status" not in last:
                # API не сообщает прогресс — ждать нечего, иначе висим до таймаута.
                return last
            if status in ("ERROR", "EXPIRED"):
                raise CliError(
                    "Контейнер %s в статусе %s: %s"
                    % (container_id, status, last.get("error_message") or "без деталей"),
                    code=1,
                    details=last,
                )
            if time.time() >= deadline:
                raise CliError(
                    "Контейнер %s не обработался за %s сек (статус %s)"
                    % (container_id, timeout_seconds, status or "неизвестен"),
                    code=1,
                    details=last,
                )
            time.sleep(interval)
            interval = min(interval * 1.5, 15)

    # --- чтение ---
    def list_threads(self, limit=25, since=None, until=None, fields=MEDIA_FIELDS):
        return self.get(
            "%s/threads" % self.user_id,
            {"fields": fields, "limit": limit, "since": since, "until": until},
        )

    def replies(self, media_id, conversation=False, limit=50):
        edge = "conversation" if conversation else "replies"
        return self.get(
            "%s/%s" % (media_id, edge),
            {"fields": REPLY_FIELDS, "limit": limit, "reverse": "false"},
        )

    def user_replies(self, limit=50):
        return self.get("%s/replies" % self.user_id, {"fields": REPLY_FIELDS, "limit": limit})

    def mentions(self, limit=25):
        return self.get("%s/mentions" % self.user_id, {"fields": REPLY_FIELDS, "limit": limit})

    def manage_reply(self, reply_id, hide=True):
        return self.post("%s/manage_reply" % reply_id, data={"hide": "true" if hide else "false"})

    def media_insights(self, media_id, metrics=MEDIA_METRICS):
        return self.get("%s/insights" % media_id, {"metric": metrics})

    def account_insights(self, metrics=ACCOUNT_METRICS, since=None, until=None):
        return self.get(
            "%s/threads_insights" % self.user_id,
            {"metric": metrics, "since": since, "until": until},
        )


# --------------------------------------------------------------------------- #
# Валидация и сборка контейнера
# --------------------------------------------------------------------------- #


def check_text(text, label="text"):
    if text is None:
        return None
    length = len(text)
    if length > TEXT_LIMIT:
        raise CliError(
            "%s длиной %d символов, лимит Threads — %d. Сократи или разбей на цепочку."
            % (label, length, TEXT_LIMIT),
            code=2,
        )
    return text


def check_blocked_words(text, blocked):
    if not text or not blocked:
        return
    # Строку в настройках трактуем как одно слово, а не как набор букв:
    # иначе safety.blocked_words="реклама" заблокировал бы любой текст.
    if isinstance(blocked, str):
        blocked = [w.strip() for w in blocked.split(",") if w.strip()]
    lowered = text.lower()
    hits = [w for w in blocked if w and w.lower() in lowered]
    if hits:
        raise CliError(
            "Текст содержит запрещённые слова из safety.blocked_words: %s" % ", ".join(hits),
            code=2,
            hint="Поправь текст или измени список: threads.py config set safety.blocked_words='[]'",
        )


def build_container_params(spec, cfg, is_carousel_item=False):
    """spec — dict с полями поста. Возвращает params для POST /threads."""
    text = spec.get("text")
    image_url = spec.get("image_url")
    video_url = spec.get("video_url")
    children = spec.get("children")

    if children:
        media_type = "CAROUSEL"
    elif video_url:
        media_type = "VIDEO"
    elif image_url:
        media_type = "IMAGE"
    else:
        media_type = "TEXT"

    if media_type == "TEXT" and not (text or "").strip():
        raise CliError("Пустой пост: нужен --text или медиа.", code=2)

    check_text(text)
    check_blocked_words(text, ((cfg.get("safety") or {}).get("blocked_words") or []))

    alt_text = spec.get("alt_text")
    if alt_text and len(alt_text) > ALT_TEXT_LIMIT:
        raise CliError("alt_text длиннее %d символов." % ALT_TEXT_LIMIT, code=2)

    topic_tag = spec.get("topic_tag")
    if topic_tag:
        topic_tag = topic_tag.lstrip("#").strip()
        if not topic_tag or len(topic_tag) > TOPIC_TAG_LIMIT:
            raise CliError("topic_tag должен быть 1–%d символов." % TOPIC_TAG_LIMIT, code=2)
        if "." in topic_tag or "&" in topic_tag:
            raise CliError("В topic_tag нельзя использовать '.' и '&'.", code=2)

    reply_control = spec.get("reply_control")
    if reply_control and reply_control not in REPLY_CONTROLS:
        raise CliError(
            "reply_control должен быть одним из: %s" % ", ".join(REPLY_CONTROLS), code=2
        )

    params = {"media_type": media_type}
    if text:
        params["text"] = text
    if media_type == "IMAGE":
        params["image_url"] = image_url
    if media_type == "VIDEO":
        params["video_url"] = video_url
    if media_type == "CAROUSEL":
        if len(children) < CAROUSEL_MIN or len(children) > CAROUSEL_MAX:
            raise CliError(
                "В карусели должно быть от %d до %d элементов." % (CAROUSEL_MIN, CAROUSEL_MAX),
                code=2,
            )
        params["children"] = ",".join(children)

    if is_carousel_item:
        params["is_carousel_item"] = "true"
        # У элемента карусели нет собственного текста и настроек ответов.
        params.pop("text", None)
    else:
        if reply_control:
            params["reply_control"] = reply_control
        if spec.get("reply_to_id"):
            params["reply_to_id"] = spec["reply_to_id"]
        if spec.get("quote_post_id"):
            params["quote_post_id"] = spec["quote_post_id"]
        if spec.get("link_attachment") and media_type == "TEXT":
            params["link_attachment"] = spec["link_attachment"]
        if topic_tag:
            params["topic_tag"] = topic_tag
        countries = spec.get("allowlisted_country_codes")
        if countries:
            if isinstance(countries, (list, tuple)):
                countries = ",".join(countries)
            params["allowlisted_country_codes"] = countries

    if alt_text and media_type in ("IMAGE", "VIDEO"):
        params["alt_text"] = alt_text

    return params


def publish_one(client, spec, cfg, wait=None, timeout=None, kind="post"):
    """Создаёт контейнер, ждёт готовности, публикует. Возвращает dict с результатом."""
    defaults = cfg.get("defaults") or {}
    params = build_container_params(spec, cfg)
    container = client.create_container(params)
    container_id = container.get("id")
    if not container_id:
        raise CliError("API не вернул id контейнера.", code=1, details=container)

    media_type = params["media_type"]
    if wait is None:
        wait = defaults.get("video_wait_seconds", 30) if media_type == "VIDEO" else 0
    if timeout is None:
        timeout = defaults.get("container_timeout_seconds", 300)

    status = {}
    if media_type in ("VIDEO", "CAROUSEL") or wait:
        status = client.wait_for_container(container_id, wait, timeout)

    published = client.publish_container(container_id)
    media_id = published.get("id")
    if not media_id:
        raise CliError(
            "API не вернул id опубликованного поста. Контейнер %s мог опубликоваться — "
            "проверь ленту перед повторной попыткой." % container_id,
            code=1,
            details={"container_id": container_id, "response": published},
        )

    permalink = None
    try:
        info = client.media(media_id, "id,permalink,timestamp,text,media_type")
        permalink = info.get("permalink")
    except CliError:
        info = {}

    entry = {
        "kind": "reply" if spec.get("reply_to_id") else kind,
        "media_id": media_id,
        "container_id": container_id,
        "media_type": media_type,
        "permalink": permalink,
        "text": spec.get("text"),
        "reply_to_id": spec.get("reply_to_id"),
        "published_at": iso(now_utc()),
    }
    client.store.append_history(entry)
    result = dict(entry)
    result["container_status"] = status.get("status") if status else None
    return result


# --------------------------------------------------------------------------- #
# Проверки безопасности перед публикацией
# --------------------------------------------------------------------------- #


def safety_report(store, cfg, specs, confirmed):
    safety = cfg.get("safety") or {}
    posts = sum(1 for s in specs if not s.get("reply_to_id"))
    replies = sum(1 for s in specs if s.get("reply_to_id"))
    used_posts = store.count_since(24, "post")
    used_replies = store.count_since(24, "reply")
    max_posts = safety.get("max_posts_per_day")
    max_replies = safety.get("max_replies_per_day")

    blockers = []
    if max_posts and used_posts + posts > max_posts:
        blockers.append(
            "локальный лимит постов: уже %d за 24ч, хотим ещё %d, максимум %d"
            % (used_posts, posts, max_posts)
        )
    if max_replies and used_replies + replies > max_replies:
        blockers.append(
            "локальный лимит ответов: уже %d за 24ч, хотим ещё %d, максимум %d"
            % (used_replies, replies, max_replies)
        )

    return {
        "items": len(specs),
        "posts": posts,
        "replies": replies,
        "used_posts_24h": used_posts,
        "used_replies_24h": used_replies,
        "max_posts_per_day": max_posts,
        "max_replies_per_day": max_replies,
        "require_yes_flag": bool(safety.get("require_yes_flag", True)),
        "confirmed": bool(confirmed),
        "blockers": blockers,
    }


def gate(store, cfg, specs, confirmed):
    """Возвращает (можно_публиковать, report). Бросает CliError при жёстком блоке."""
    report = safety_report(store, cfg, specs, confirmed)
    if report["blockers"]:
        raise CliError(
            "Публикация остановлена: " + "; ".join(report["blockers"]),
            code=1,
            hint="Подними лимит: threads.py config set safety.max_posts_per_day=20",
            details=report,
        )
    if report["require_yes_flag"] and not confirmed:
        return False, report
    return True, report


# --------------------------------------------------------------------------- #
# Команды: config
# --------------------------------------------------------------------------- #


def cmd_config_init(args, store):
    if store.exists() and not args.force:
        cfg = store.load_config()
        return emit(
            {
                "ok": True,
                "already_configured": True,
                "paths": store.paths(),
                "config": cfg,
                "next": "Отредактируй значения через `config set` или запусти `doctor`.",
            }
        )
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["profile"] = store.profile
    cfg["created_at"] = iso(now_utc())
    store.save_config(cfg)
    if not store.voice_path.exists():
        store.voice_path.write_text(VOICE_STUB, encoding="utf-8")
    if not store.queue_path.exists():
        store.save_queue([])
    if not store.credentials_path.exists():
        store.save_credentials({})
    return emit(
        {
            "ok": True,
            "created": True,
            "paths": store.paths(),
            "config": cfg,
            "next": [
                "Заполни voice.md (профиль голоса).",
                "Сгенерируй маркер в настройках приложения Threads и сохрани: "
                "auth save-token --token … --no-exchange",
                "Проверь: threads.py doctor",
            ],
        }
    )


def coerce(value):
    """Строку из CLI приводим к JSON-типу, если получается."""
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.strip().startswith(("[", "{", '"')):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def set_dotted(cfg, dotted, value):
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def refresh_policy(cfg):
    """Возвращает (разрешено_ли_продление, порог_в_днях).

    Порог 0 и allow_refresh=false означают «не продлевать». Считаем отдельно,
    потому что `значение or 10` превратило бы осознанный 0 в 10.
    """
    allowed = get_dotted(cfg, "auth.allow_refresh")
    allowed = True if allowed is None else bool(allowed)
    threshold = get_dotted(cfg, "auth.refresh_when_days_left")
    if threshold is None:
        threshold = 10
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 10
    if threshold <= 0:
        allowed = False
    return allowed, threshold


def get_dotted(cfg, dotted, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def cmd_config_set(args, store):
    cfg = store.load_config()
    changed = {}
    for pair in args.assignments:
        if "=" not in pair:
            raise CliError("Ожидал КЛЮЧ=ЗНАЧЕНИЕ, получил %r" % pair, code=2)
        key, raw = pair.split("=", 1)
        value = raw if args.raw else coerce(raw)
        set_dotted(cfg, key.strip(), value)
        changed[key.strip()] = value
    store.save_config(cfg)
    return emit({"ok": True, "changed": changed, "config": cfg})


def cmd_config_get(args, store):
    cfg = store.load_config()
    if args.key:
        return emit({"ok": True, "key": args.key, "value": get_dotted(cfg, args.key)})
    return emit({"ok": True, "config": cfg})


def cmd_config_show(args, store):
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    return emit(
        {
            "ok": True,
            "configured": store.exists(),
            "paths": store.paths(),
            "config": cfg,
            "credentials": {
                "app_id": creds.get("app_id"),
                "app_secret": mask(creds.get("app_secret")),
                "access_token": mask(creds.get("access_token")),
                "user_id": creds.get("user_id"),
                "obtained_at": creds.get("obtained_at"),
                "expires_at": creds.get("expires_at"),
                "from_env": sorted((creds.get("source") or {}).keys()),
            },
            "voice_present": store.voice_path.exists(),
            "voice_bytes": store.voice_path.stat().st_size if store.voice_path.exists() else 0,
        }
    )


def cmd_config_path(args, store):
    return emit({"ok": True, "paths": store.paths()})


# --------------------------------------------------------------------------- #
# Команды: auth
# --------------------------------------------------------------------------- #


def cmd_auth_url(args, store):
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    app_id = args.app_id or creds.get("app_id")
    redirect_uri = args.redirect_uri or get_dotted(cfg, "auth.redirect_uri")
    if not app_id:
        raise CliError(
            "Нужен app_id (Threads App ID из Meta App Dashboard).",
            code=2,
            hint="Передай --app-id или сохрани: threads.py auth set-app --app-id … --app-secret …",
        )
    if not redirect_uri:
        raise CliError(
            "Нужен redirect_uri — ровно тот, что прописан в настройках приложения Threads.",
            code=2,
            hint="Передай --redirect-uri: только https, и домен должен реально существовать "
            "(Meta отклоняет example.com и выдуманные адреса). Подойдёт "
            "https://oauth.pstmn.io/v1/callback или своя страница на GitHub Pages.",
        )
    scopes = args.scopes or get_dotted(cfg, "auth.scopes") or ["threads_basic", "threads_content_publish"]
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise CliError(
            "Неизвестные scope: %s. Доступны: %s" % (", ".join(unknown), ", ".join(ALL_SCOPES)),
            code=2,
        )
    query = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
    }
    if args.state:
        query["state"] = args.state
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)
    return emit(
        {
            "ok": True,
            "authorize_url": url,
            "scopes": scopes,
            "redirect_uri": redirect_uri,
            "next": [
                "Открой ссылку в браузере под нужным аккаунтом Threads и подтверди доступ.",
                "Тебя перебросит на redirect_uri с параметром ?code=… (страница может не открыться — это нормально, код видно в адресной строке).",
                "Скопируй значение code (без завершающего '#_') и выполни: threads.py auth exchange --code <CODE>",
                "Код живёт 1 час и одноразовый.",
            ],
        }
    )


def cmd_auth_set_app(args, store):
    creds = store.load_credentials()
    if args.app_id:
        creds["app_id"] = args.app_id
    if args.app_secret:
        creds["app_secret"] = args.app_secret
    store.save_credentials(creds)
    cfg = store.load_config(required=False)
    if args.redirect_uri:
        set_dotted(cfg, "auth.redirect_uri", args.redirect_uri)
        store.save_config(cfg)
    return emit(
        {
            "ok": True,
            "app_id": creds.get("app_id"),
            "app_secret": mask(creds.get("app_secret")),
            "redirect_uri": get_dotted(cfg, "auth.redirect_uri"),
        }
    )


def _store_token(store, token_payload, app_secret=None, long_lived=False):
    creds = store.load_credentials()
    creds["access_token"] = token_payload["access_token"]
    expires_in = token_payload.get("expires_in")
    creds["obtained_at"] = iso(now_utc())
    creds["expires_at"] = (
        iso(now_utc() + timedelta(seconds=int(expires_in))) if expires_in else None
    )
    creds["long_lived"] = bool(long_lived or (expires_in and int(expires_in) > 86400 * 2))
    if app_secret:
        creds["app_secret"] = app_secret
    store.save_credentials(creds)
    return creds


def exchange_for_long_lived(store, cfg, short_token, app_secret):
    base = (get_dotted(cfg, "api.base_url") or DEFAULT_API_BASE).rstrip("/")
    return http_request(
        "GET",
        base + "/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
    )


def cmd_auth_exchange(args, store):
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    app_id = args.app_id or creds.get("app_id")
    app_secret = args.app_secret or creds.get("app_secret")
    redirect_uri = args.redirect_uri or get_dotted(cfg, "auth.redirect_uri")
    missing = [
        name
        for name, value in (
            ("app_id", app_id),
            ("app_secret", app_secret),
            ("redirect_uri", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise CliError(
            "Не хватает: %s" % ", ".join(missing),
            code=2,
            hint="Сохрани один раз: threads.py auth set-app --app-id … --app-secret … --redirect-uri …",
        )
    code = args.code.strip()
    # Meta иногда добавляет '#_' в конец адресной строки — отрезаем.
    code = code.split("#")[0]

    base = (get_dotted(cfg, "api.base_url") or DEFAULT_API_BASE).rstrip("/")
    short = http_request(
        "POST",
        base + "/oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    if "access_token" not in short:
        raise CliError("API не вернул access_token.", code=1, details=short)

    result = {"ok": True, "short_lived": True, "user_id": short.get("user_id")}
    token_payload = short
    long_lived = False
    if not args.no_long_lived:
        try:
            long_payload = exchange_for_long_lived(store, cfg, short["access_token"], app_secret)
            if long_payload.get("access_token"):
                token_payload = long_payload
                long_lived = True
                result["short_lived"] = False
        except CliError as exc:
            result["long_lived_exchange_error"] = exc.message

    creds = _store_token(store, token_payload, app_secret=app_secret, long_lived=long_lived)
    if short.get("user_id"):
        creds["user_id"] = str(short["user_id"])
        store.save_credentials(creds)

    result.update(
        {
            "long_lived": long_lived,
            "expires_at": creds.get("expires_at"),
            "access_token": mask(creds.get("access_token")),
            "next": "Проверь: threads.py doctor",
        }
    )
    return emit(result)


def cmd_auth_save_token(args, store):
    """Вставить готовый токен (например, сгенерированный в Meta App Dashboard)."""
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    token = args.token.strip()
    app_secret = args.app_secret or creds.get("app_secret")

    payload = {"access_token": token}
    if args.expires_in:
        payload["expires_in"] = args.expires_in
    long_lived = bool(args.long_lived)

    warnings = []
    if not long_lived and app_secret and not args.no_exchange:
        try:
            exchanged = exchange_for_long_lived(store, cfg, token, app_secret)
            if exchanged.get("access_token"):
                payload = exchanged
                long_lived = True
        except CliError as exc:
            warnings.append("Не удалось обменять на долгоживущий токен: %s" % exc.message)
    elif not long_lived and not app_secret:
        warnings.append(
            "app_secret не сохранён — токен остался как есть. Долгоживущий токен (60 дней) "
            "можно получить позже: auth set-app --app-secret … && auth long-lived"
        )

    creds = _store_token(store, payload, app_secret=app_secret, long_lived=long_lived)
    return emit(
        {
            "ok": True,
            "long_lived": long_lived,
            "expires_at": creds.get("expires_at"),
            "access_token": mask(creds.get("access_token")),
            "warnings": warnings,
            "next": "Проверь: threads.py doctor",
        }
    )


def cmd_auth_long_lived(args, store):
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    token = creds.get("access_token")
    app_secret = args.app_secret or creds.get("app_secret")
    if not token:
        raise CliError("Нет сохранённого токена.", code=4)
    if not app_secret:
        raise CliError("Нужен app_secret.", code=2, hint="threads.py auth set-app --app-secret …")
    payload = exchange_for_long_lived(store, cfg, token, app_secret)
    if not payload.get("access_token"):
        raise CliError("Обмен не дал токена.", code=1, details=payload)
    creds = _store_token(store, payload, app_secret=app_secret, long_lived=True)
    return emit(
        {
            "ok": True,
            "long_lived": True,
            "expires_at": creds.get("expires_at"),
            "access_token": mask(creds.get("access_token")),
        }
    )


def token_age_hours(creds):
    obtained = parse_iso(creds.get("obtained_at")) if creds.get("obtained_at") else None
    if not obtained:
        return None
    return (now_utc() - obtained).total_seconds() / 3600.0


def days_left(creds):
    expires = parse_iso(creds.get("expires_at")) if creds.get("expires_at") else None
    if not expires:
        return None
    return (expires - now_utc()).total_seconds() / 86400.0


def do_refresh(store, cfg, force=False):
    creds = store.load_credentials()
    token = creds.get("access_token")
    if not token:
        raise CliError("Нет сохранённого токена.", code=4)
    allowed, _ = refresh_policy(cfg)
    if not allowed and not force:
        raise CliError(
            "Продление токена выключено в настройках (auth.allow_refresh=false "
            "или auth.refresh_when_days_left=0).",
            code=1,
            hint="Так настроено специально, если этот же токен использует другой сервис. "
            "Продлить всё равно: auth refresh --force.",
        )
    age = token_age_hours(creds)
    if age is not None and age < 24 and not force:
        raise CliError(
            "Токен младше 24 часов (%.1f ч) — Threads не даст его обновить." % age,
            code=1,
            hint="Подожди или запусти с --force, если уверен.",
        )
    base = (get_dotted(cfg, "api.base_url") or DEFAULT_API_BASE).rstrip("/")
    payload = http_request(
        "GET",
        base + "/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
    )
    if not payload.get("access_token"):
        raise CliError("Обновление не дало токена.", code=1, details=payload)
    return _store_token(store, payload, long_lived=True)


def cmd_auth_refresh(args, store):
    cfg = store.load_config(required=False)
    creds = do_refresh(store, cfg, force=args.force)
    return emit(
        {
            "ok": True,
            "refreshed": True,
            "expires_at": creds.get("expires_at"),
            "days_left": round(days_left(creds) or 0, 1),
        }
    )


def cmd_auth_ensure(args, store):
    cfg = store.load_config(required=False)
    creds = store.load_credentials()
    if not creds.get("access_token"):
        raise CliError("Нет сохранённого токена.", code=4, hint="Запусти auth exchange / auth save-token.")
    allowed, threshold = refresh_policy(cfg)
    left = days_left(creds)
    age = token_age_hours(creds)
    if not allowed:
        return emit(
            {
                "ok": True,
                "refreshed": False,
                "reason": "продление выключено в настройках (auth.allow_refresh / refresh_when_days_left)",
                "days_left": round(left, 1) if left is not None else None,
            }
        )
    if left is None:
        return emit({"ok": True, "refreshed": False, "reason": "срок жизни токена неизвестен"})
    if left <= 0:
        raise CliError(
            "Токен истёк %s." % creds.get("expires_at"),
            code=4,
            hint="Продлить нельзя. Сгенерируй маркер заново в настройках приложения "
            "Threads и сохрани: auth save-token --token … --no-exchange",
        )
    if left > threshold:
        return emit(
            {"ok": True, "refreshed": False, "days_left": round(left, 1), "threshold_days": threshold}
        )
    if age is not None and age < 24:
        return emit(
            {
                "ok": True,
                "refreshed": False,
                "days_left": round(left, 1),
                "reason": "токен младше 24 часов, обновление недоступно",
            }
        )
    creds = do_refresh(store, cfg, force=False)
    return emit(
        {"ok": True, "refreshed": True, "expires_at": creds.get("expires_at"), "days_left": round(days_left(creds) or 0, 1)}
    )


def cmd_auth_status(args, store):
    creds = store.load_credentials()
    return emit(
        {
            "ok": True,
            "has_token": bool(creds.get("access_token")),
            "access_token": mask(creds.get("access_token")),
            "long_lived": creds.get("long_lived"),
            "obtained_at": creds.get("obtained_at"),
            "expires_at": creds.get("expires_at"),
            "days_left": round(days_left(creds), 1) if days_left(creds) is not None else None,
            "age_hours": round(token_age_hours(creds), 1) if token_age_hours(creds) is not None else None,
            "app_id": creds.get("app_id"),
            "app_secret_saved": bool(creds.get("app_secret")),
            "from_env": sorted((creds.get("source") or {}).keys()),
        }
    )


def cmd_auth_logout(args, store):
    creds = store.load_credentials()
    keep = {k: creds.get(k) for k in ("app_id", "app_secret") if creds.get(k)}
    store.save_credentials(keep)
    return emit({"ok": True, "cleared": ["access_token", "expires_at", "obtained_at"], "kept": sorted(keep)})


# --------------------------------------------------------------------------- #
# Команды: аккаунт и диагностика
# --------------------------------------------------------------------------- #


def client_for(store, require_config=True):
    cfg = store.load_config(required=require_config)
    creds = store.load_credentials()
    return Threads(store, cfg, creds), cfg, creds


def cmd_me(args, store):
    client, cfg, creds = client_for(store)
    profile = client.me()
    if profile.get("id"):
        creds["user_id"] = str(profile["id"])
        store.save_credentials(creds)
        set_dotted(cfg, "account.user_id", str(profile["id"]))
        set_dotted(cfg, "account.username", profile.get("username"))
        store.save_config(cfg)
    return emit({"ok": True, "profile": profile})


def cmd_quota(args, store):
    client, _, _ = client_for(store)
    data = client.publishing_limit()
    rows = (data.get("data") or [{}])[0] if isinstance(data.get("data"), list) else data
    return emit({"ok": True, "api_quota": rows, "local_24h": {
        "posts": store.count_since(24, "post"),
        "replies": store.count_since(24, "reply"),
    }})


def cmd_doctor(args, store):
    checks = []
    overall_ok = True

    def add(name, ok, detail=None, hint=None):
        nonlocal overall_ok
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "hint": hint})
        if not ok:
            overall_ok = False

    configured = store.exists()
    add(
        "config",
        configured,
        str(store.config_path) if configured else "нет файла конфигурации",
        None if configured else "Запусти: threads.py config init",
    )
    if not configured:
        return emit({"ok": False, "checks": checks, "paths": store.paths()}, code=3)

    cfg = store.load_config()
    creds = store.load_credentials()

    voice_text = store.voice_path.read_text(encoding="utf-8") if store.voice_path.exists() else ""
    voice_ok = bool(voice_text) and VOICE_STUB_MARKER not in voice_text and len(voice_text) > 300
    add(
        "voice",
        voice_ok,
        "%s (%d байт)" % (store.voice_path, len(voice_text.encode("utf-8"))),
        None
        if voice_ok
        else "Профиль голоса ещё шаблон — проведи интервью и перепиши voice.md "
        "(структура в references/voice.md скилла).",
    )

    has_token = bool(creds.get("access_token"))
    add(
        "token",
        has_token,
        {"long_lived": creds.get("long_lived"), "expires_at": creds.get("expires_at")},
        None
        if has_token
        else "Сгенерируй маркер в настройках приложения Threads, раздел «Генератор "
        "маркеров пользователя», и сохрани: auth save-token --token … --no-exchange",
    )
    if not has_token:
        return emit({"ok": False, "checks": checks, "paths": store.paths()}, code=4)

    left = days_left(creds)
    if left is not None:
        allowed, threshold = refresh_policy(cfg)
        if left <= 0:
            add("token_validity", False, "истёк %s" % creds.get("expires_at"),
                "Продлить нельзя. Сгенерируй маркер заново и сохрани: "
                "auth save-token --token … --no-exchange")
            return emit({"ok": False, "checks": checks, "paths": store.paths()}, code=4)
        if left <= threshold and allowed and not args.no_refresh:
            age = token_age_hours(creds)
            if age is None or age >= 24:
                try:
                    creds = do_refresh(store, cfg, force=False)
                    left = days_left(creds)
                    add("token_refresh", True, "обновлён, осталось %.1f дн." % (left or 0))
                except CliError as exc:
                    add("token_refresh", False, exc.message, exc.hint)
        add(
            "token_validity",
            True,
            "осталось %.1f дн.%s" % (left, "" if allowed else " (автопродление выключено)"),
        )
    else:
        # Срока нет, когда токен пришёл без метаданных — например, через
        # THREADS_ACCESS_TOKEN. Автопродление в этом случае не срабатывает никогда.
        allowed, _ = refresh_policy(cfg)
        from_env = "access_token" in (creds.get("source") or {})
        add(
            "token_validity",
            True,
            "срок неизвестен%s — автопродление не сработает%s"
            % (
                " (токен передан через THREADS_ACCESS_TOKEN)" if from_env else "",
                "" if allowed else "; оно и выключено в настройках",
            ),
        )

    client = Threads(store, cfg, creds)
    try:
        profile = client.me()
        add("api_me", True, {"id": profile.get("id"), "username": profile.get("username")})
        if profile.get("id") and str(profile["id"]) != str(creds.get("user_id") or ""):
            creds["user_id"] = str(profile["id"])
            store.save_credentials(creds)
            set_dotted(cfg, "account.user_id", str(profile["id"]))
            set_dotted(cfg, "account.username", profile.get("username"))
            store.save_config(cfg)
    except CliError as exc:
        add("api_me", False, exc.message, exc.hint)
        return emit({"ok": False, "checks": checks, "paths": store.paths()}, code=exc.code)

    try:
        limit = client.publishing_limit()
        rows = (limit.get("data") or [{}])[0] if isinstance(limit.get("data"), list) else limit
        add("api_quota", True, rows)
    except CliError as exc:
        # threads_publishing_limit требует scope threads_content_publish.
        add("api_quota", False, exc.message,
            "Проверь, что у токена есть scope threads_content_publish.")

    queue = store.load_queue()
    pending = [q for q in queue if q.get("status") == "pending"]
    due = [q for q in pending if parse_iso(q["publish_at"]) <= now_utc()]
    add("queue", True, {"pending": len(pending), "due_now": len(due)})

    return emit(
        {
            "ok": overall_ok,
            "version": __version__,
            "profile": store.profile,
            "paths": store.paths(),
            "account": cfg.get("account"),
            "safety": cfg.get("safety"),
            "defaults": cfg.get("defaults"),
            "local_24h": {"posts": store.count_since(24, "post"), "replies": store.count_since(24, "reply")},
            "checks": checks,
        },
        code=0 if overall_ok else 1,
    )


# --------------------------------------------------------------------------- #
# Команды: публикация
# --------------------------------------------------------------------------- #


def read_text_arg(value):
    """--text '-' читает stdin; --text @file.txt читает файл."""
    if value is None:
        return None
    if value == "-":
        return sys.stdin.read().rstrip("\n")
    if value.startswith("@") and len(value) > 1:
        path = Path(value[1:]).expanduser()
        if not path.exists():
            raise CliError(
                "Файл не найден: %s" % path,
                code=2,
                hint="Запись '@путь' читает текст из файла. Если нужен текст, "
                "начинающийся с '@', передай его через stdin: --text -",
            )
        return path.read_text(encoding="utf-8").rstrip("\n")
    return value


def spec_from_args(args, cfg):
    spec = {
        "text": read_text_arg(args.text),
        "image_url": args.image,
        "video_url": args.video,
        "alt_text": args.alt_text,
        "link_attachment": args.link,
        "topic_tag": args.topic_tag,
        "reply_to_id": args.reply_to,
        "quote_post_id": getattr(args, "quote", None),
        "reply_control": args.reply_control or get_dotted(cfg, "defaults.reply_control"),
        "allowlisted_country_codes": args.countries,
    }
    return {k: v for k, v in spec.items() if v is not None}


def cmd_post(args, store):
    client, cfg, _ = client_for(store)
    spec = spec_from_args(args, cfg)
    if args.image and args.video:
        raise CliError("Одновременно --image и --video нельзя. Для нескольких медиа используй carousel.", code=2)
    params = build_container_params(spec, cfg)  # валидация до любых запросов
    allowed, report = gate(store, cfg, [spec], args.yes)
    if not allowed:
        return emit(
            {
                "ok": True,
                "published": False,
                "dry_run": True,
                "reason": "Ничего не опубликовано. Добавь --yes, когда текст согласован.",
                "would_send": params,
                "safety": report,
            }
        )
    result = publish_one(client, spec, cfg, wait=args.wait, timeout=args.timeout)
    return emit({"ok": True, "published": True, "result": result, "safety": report})


def cmd_reply(args, store):
    args.reply_to = args.to
    args.quote = None
    return cmd_post(args, store)


def load_chain(args, cfg):
    """Возвращает список spec для цепочки."""
    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            raise CliError("Файл не найден: %s" % path, code=2)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"posts": data}
        posts = data.get("posts") or []
        if not posts:
            raise CliError("В файле нет ключа 'posts' с непустым списком.", code=2)
        common = {
            "reply_control": data.get("reply_control") or get_dotted(cfg, "defaults.reply_control"),
        }
        specs = []
        for item in posts:
            if isinstance(item, str):
                item = {"text": item}
            spec = dict(common)
            spec.update(item)
            specs.append({k: v for k, v in spec.items() if v is not None})
        return specs
    texts = [read_text_arg(t) for t in (args.text or [])]
    if not texts:
        raise CliError("Нужен --file chain.json или несколько --text.", code=2)

    # Эти флаги относятся к одному посту, а в цепочке их несколько — непонятно,
    # к какому применять. Раньше они молча терялись.
    per_post = [
        name
        for name, value in (
            ("--image", getattr(args, "image", None)),
            ("--video", getattr(args, "video", None)),
            ("--alt-text", getattr(args, "alt_text", None)),
            ("--link", getattr(args, "link", None)),
            ("--topic-tag", getattr(args, "topic_tag", None)),
            ("--countries", getattr(args, "countries", None)),
        )
        if value
    ]
    if per_post:
        raise CliError(
            "В цепочке через --text нельзя задать %s: непонятно, к какому посту." % ", ".join(per_post),
            code=2,
            hint="Опиши цепочку файлом и укажи эти поля у нужного поста: "
            "chain --file chain.json (пример — assets/chain.example.json).",
        )

    control = args.reply_control or get_dotted(cfg, "defaults.reply_control")
    return [{"text": t, "reply_control": control} for t in texts]


def cmd_chain(args, store):
    client, cfg, _ = client_for(store)
    specs = load_chain(args, cfg)
    if args.reply_to:
        specs[0]["reply_to_id"] = args.reply_to

    # Валидируем всю цепочку до первого запроса: частично опубликованная цепочка — плохо.
    previews = []
    for index, spec in enumerate(specs):
        probe = dict(spec)
        if index > 0:
            probe["reply_to_id"] = "PREVIOUS"
        previews.append(build_container_params(probe, cfg))

    # Первый пост цепочки — обычный пост, остальные — ответы.
    gate_specs = [dict(s) for s in specs]
    for index in range(1, len(gate_specs)):
        gate_specs[index]["reply_to_id"] = "PREVIOUS"
    allowed, report = gate(store, cfg, gate_specs, args.yes)
    if not allowed:
        return emit(
            {
                "ok": True,
                "published": False,
                "dry_run": True,
                "reason": "Ничего не опубликовано. Добавь --yes, когда цепочка согласована.",
                "would_send": previews,
                "safety": report,
            }
        )

    delay = args.delay if args.delay is not None else get_dotted(cfg, "defaults.chain_delay_seconds") or 3
    results = []
    previous_id = None
    try:
        for index, spec in enumerate(specs):
            current = dict(spec)
            if previous_id:
                current["reply_to_id"] = previous_id
            result = publish_one(client, current, cfg, wait=args.wait, timeout=args.timeout)
            results.append(result)
            previous_id = result["media_id"]
            if index < len(specs) - 1 and delay:
                time.sleep(delay)
    except CliError as exc:
        return emit(
            {
                "ok": False,
                "published": bool(results),
                "partial": True,
                "error": exc.message,
                "hint": exc.hint,
                "results": results,
                "next": "Цепочка опубликована частично. Продолжи вручную: post --reply-to %s" % (previous_id or "<id>"),
            },
            code=1,
        )
    return emit({"ok": True, "published": True, "count": len(results), "results": results, "safety": report})


def cmd_carousel(args, store):
    client, cfg, _ = client_for(store)
    # args.media собран одним списком в порядке, в котором переданы --image/--video:
    # раньше картинки шли первыми, видео следом, и alt-тексты съезжали.
    items = [
        {"image_url": m["url"], "type": "IMAGE"}
        if m["kind"] == "IMAGE"
        else {"video_url": m["url"], "type": "VIDEO"}
        for m in (args.media or [])
    ]
    if len(items) < CAROUSEL_MIN:
        raise CliError("Карусели нужно минимум %d медиа (--image/--video)." % CAROUSEL_MIN, code=2)
    if len(items) > CAROUSEL_MAX:
        raise CliError("Максимум %d элементов в карусели." % CAROUSEL_MAX, code=2)

    alts = args.alt_text or []
    text = read_text_arg(args.text)
    parent_spec = {
        "text": text,
        "children": ["PLACEHOLDER"] * len(items),
        "reply_control": args.reply_control or get_dotted(cfg, "defaults.reply_control"),
        "reply_to_id": args.reply_to,
        "topic_tag": args.topic_tag,
    }
    parent_spec = {k: v for k, v in parent_spec.items() if v is not None}
    preview = build_container_params(parent_spec, cfg)

    allowed, report = gate(store, cfg, [parent_spec], args.yes)
    if not allowed:
        return emit(
            {
                "ok": True,
                "published": False,
                "dry_run": True,
                "reason": "Ничего не опубликовано. Добавь --yes.",
                "would_send": {"parent": preview, "children": items},
                "safety": report,
            }
        )

    child_ids = []
    for index, item in enumerate(items):
        child_spec = {"image_url": item.get("image_url"), "video_url": item.get("video_url")}
        if index < len(alts):
            child_spec["alt_text"] = alts[index]
        child_params = build_container_params(
            {k: v for k, v in child_spec.items() if v is not None}, cfg, is_carousel_item=True
        )
        created = client.create_container(child_params)
        if not created.get("id"):
            raise CliError("Не создался элемент карусели #%d." % (index + 1), code=1, details=created)
        child_ids.append(created["id"])

    timeout = args.timeout if args.timeout is not None else get_dotted(cfg, "defaults.container_timeout_seconds") or 300
    for child_id in child_ids:
        client.wait_for_container(child_id, 0, timeout)

    parent_spec["children"] = child_ids
    result = publish_one(client, parent_spec, cfg, wait=args.wait, timeout=args.timeout, kind="post")
    result["children"] = child_ids
    return emit({"ok": True, "published": True, "result": result, "safety": report})


# --------------------------------------------------------------------------- #
# Команды: чтение и аналитика
# --------------------------------------------------------------------------- #


def cmd_list(args, store):
    client, _, _ = client_for(store)
    since = iso_to_unix(args.since)
    until = iso_to_unix(args.until)
    data = client.list_threads(limit=args.limit, since=since, until=until)
    return emit({"ok": True, "count": len(data.get("data") or []), "threads": data.get("data") or [], "paging": data.get("paging")})


def iso_to_unix(value):
    if not value:
        return None
    if re.fullmatch(r"\d{9,11}", str(value).strip()):
        return int(value)
    return int(parse_iso(value).timestamp())


def cmd_get(args, store):
    client, _, _ = client_for(store)
    return emit({"ok": True, "media": client.media(args.media_id)})


def cmd_replies(args, store):
    client, _, _ = client_for(store)
    if args.mine:
        data = client.user_replies(limit=args.limit)
    elif args.mentions:
        data = client.mentions(limit=args.limit)
    elif args.media_id:
        data = client.replies(args.media_id, conversation=args.conversation, limit=args.limit)
    else:
        raise CliError("Укажи --media-id, либо --mine, либо --mentions.", code=2)
    return emit({"ok": True, "count": len(data.get("data") or []), "replies": data.get("data") or [], "paging": data.get("paging")})


def cmd_hide(args, store):
    client, cfg, _ = client_for(store)
    if not args.yes and (cfg.get("safety") or {}).get("require_yes_flag", True):
        return emit({"ok": True, "changed": False, "dry_run": True,
                     "would": {"reply_id": args.reply_id, "hide": not args.unhide},
                     "reason": "Добавь --yes, чтобы применить."})
    data = client.manage_reply(args.reply_id, hide=not args.unhide)
    return emit({"ok": True, "changed": True, "reply_id": args.reply_id, "hidden": not args.unhide, "response": data})


def cmd_insights(args, store):
    client, _, _ = client_for(store)
    if args.media_id:
        data = client.media_insights(args.media_id, args.metrics or MEDIA_METRICS)
        return emit({"ok": True, "scope": "media", "media_id": args.media_id, "insights": data.get("data") or data})
    since = iso_to_unix(args.since)
    until = iso_to_unix(args.until)
    data = client.account_insights(args.metrics or ACCOUNT_METRICS, since=since, until=until)
    return emit({"ok": True, "scope": "account", "insights": data.get("data") or data})


def cmd_history(args, store):
    rows = store.read_history(limit=args.limit)
    return emit({"ok": True, "count": len(rows), "history": rows})


# --------------------------------------------------------------------------- #
# Команды: очередь (локальное планирование)
# --------------------------------------------------------------------------- #


def new_queue_id():
    return "q_%s_%04d" % (now_utc().strftime("%Y%m%dT%H%M%S"), random.randint(0, 9999))


def cmd_queue_add(args, store):
    cfg = store.load_config()
    when = parse_iso(args.at)
    if args.file:
        specs = load_chain(argparse.Namespace(file=args.file, text=None, reply_control=None), cfg)
    else:
        text = read_text_arg(args.text)
        if not text and not (args.image or args.video):
            raise CliError("Нужен --text, --file или медиа.", code=2)
        spec = {
            "text": text,
            "image_url": args.image,
            "video_url": args.video,
            "alt_text": args.alt_text,
            "link_attachment": args.link,
            "topic_tag": args.topic_tag,
            "reply_to_id": args.reply_to,
            "reply_control": args.reply_control or get_dotted(cfg, "defaults.reply_control"),
            "allowlisted_country_codes": args.countries,
        }
        specs = [{k: v for k, v in spec.items() if v is not None}]

    for index, spec in enumerate(specs):
        probe = dict(spec)
        if index > 0:
            probe["reply_to_id"] = "PREVIOUS"
        build_container_params(probe, cfg)  # валидация заранее

    item = {
        "id": new_queue_id(),
        "publish_at": iso(when),
        "status": "pending",
        "note": args.note,
        "chain": len(specs) > 1,
        "posts": specs,
        "created_at": iso(now_utc()),
    }
    queue = store.load_queue()
    queue.append(item)
    queue.sort(key=lambda q: q.get("publish_at") or "")
    store.save_queue(queue)
    return emit({"ok": True, "added": item, "pending": sum(1 for q in queue if q.get("status") == "pending")})


def cmd_queue_list(args, store):
    queue = store.load_queue()
    if args.status:
        queue = [q for q in queue if q.get("status") == args.status]
    if args.due:
        queue = [q for q in queue if q.get("status") == "pending" and parse_iso(q["publish_at"]) <= now_utc()]
    return emit({"ok": True, "count": len(queue), "items": queue, "now": iso(now_utc())})


def cmd_queue_rm(args, store):
    queue = store.load_queue()
    before = len(queue)
    kept = [q for q in queue if q.get("id") not in set(args.ids)]
    if len(kept) == before:
        raise CliError("Не нашёл записи: %s" % ", ".join(args.ids), code=2)
    store.save_queue(kept)
    return emit({"ok": True, "removed": before - len(kept), "remaining": len(kept)})


def cmd_queue_run(args, store):
    client, cfg, _ = client_for(store)
    queue = store.load_queue()
    due = [
        q
        for q in queue
        if q.get("status") == "pending" and parse_iso(q["publish_at"]) <= now_utc()
    ]
    if args.id:
        due = [q for q in queue if q.get("id") in set(args.id) and q.get("status") == "pending"]
    if args.limit:
        due = due[: args.limit]

    if not due:
        return emit({"ok": True, "published": False, "due": 0, "message": "Нечего публиковать.", "now": iso(now_utc())})

    flat_specs = []
    for item in due:
        for index, spec in enumerate(item["posts"]):
            probe = dict(spec)
            if index > 0:
                probe["reply_to_id"] = "PREVIOUS"
            flat_specs.append(probe)

    allowed, report = gate(store, cfg, flat_specs, args.yes)
    if not allowed:
        return emit(
            {
                "ok": True,
                "published": False,
                "dry_run": True,
                "due": len(due),
                "items": due,
                "reason": "Добавь --yes, чтобы опубликовать наступившие записи.",
                "safety": report,
            }
        )

    published, failed = [], []
    for item in due:
        previous_id = None
        results = []
        try:
            for index, spec in enumerate(item["posts"]):
                current = dict(spec)
                if previous_id:
                    current["reply_to_id"] = previous_id
                result = publish_one(client, current, cfg)
                results.append(result)
                previous_id = result["media_id"]
                if index < len(item["posts"]) - 1:
                    time.sleep(get_dotted(cfg, "defaults.chain_delay_seconds") or 3)
            item["status"] = "published"
            item["results"] = results
            item["published_at"] = iso(now_utc())
            published.append(item)
        except CliError as exc:
            item["status"] = "failed"
            item["error"] = exc.message
            item["results"] = results
            item["failed_at"] = iso(now_utc())
            failed.append(item)
    store.save_queue(queue)
    return emit(
        {
            "ok": not failed,
            "published": bool(published),
            "published_count": len(published),
            "failed_count": len(failed),
            "published_items": published,
            "failed_items": failed,
        },
        code=0 if not failed else 1,
    )


def cmd_queue_cron(args, store):
    script = str(Path(__file__).resolve())
    python = sys.executable or "python3"
    minute = args.minute if args.minute is not None else 5
    default_home = Path("~/.threads-agent").expanduser()
    home_flag = "" if store.home == default_home else ' --home "%s"' % store.home
    line = '%d * * * * "%s" "%s"%s --profile %s queue run --yes >> "%s" 2>&1' % (
        minute,
        python,
        script,
        home_flag,
        store.profile,
        store.dir / "cron.log",
    )
    return emit(
        {
            "ok": True,
            "crontab_line": line,
            "install": "crontab -l 2>/dev/null | { cat; echo '%s'; } | crontab -" % line,
            "note": [
                "Threads API не умеет отложенные публикации — очередь локальная, её должен кто-то запускать.",
                "Пока машина выключена, публикации не уйдут.",
                "Проверить вручную: threads.py queue run --yes",
            ],
        }
    )


# --------------------------------------------------------------------------- #
# selftest — оффлайн-проверка без сети
# --------------------------------------------------------------------------- #


def cmd_selftest(args, store):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    results = []

    def case(name, fn, expect_error=False):
        try:
            value = fn()
            results.append({"case": name, "ok": not expect_error, "value": value})
        except CliError as exc:
            results.append({"case": name, "ok": expect_error, "error": exc.message})
        except Exception as exc:  # noqa: BLE001
            results.append({"case": name, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})

    case("text post", lambda: build_container_params({"text": "привет"}, cfg))
    case("image post", lambda: build_container_params({"text": "a", "image_url": "https://x/i.jpg"}, cfg))
    case("video post", lambda: build_container_params({"video_url": "https://x/v.mp4"}, cfg))
    case(
        "carousel",
        lambda: build_container_params({"text": "a", "children": ["1", "2"]}, cfg),
    )
    case(
        "carousel item drops text",
        lambda: "text" not in build_container_params(
            {"text": "a", "image_url": "https://x/i.jpg"}, cfg, is_carousel_item=True
        ),
    )
    case("empty post rejected", lambda: build_container_params({}, cfg), expect_error=True)
    case(
        "too long rejected",
        lambda: build_container_params({"text": "x" * (TEXT_LIMIT + 1)}, cfg),
        expect_error=True,
    )
    case(
        "bad reply_control rejected",
        lambda: build_container_params({"text": "a", "reply_control": "nobody"}, cfg),
        expect_error=True,
    )
    case(
        "topic_tag with dot rejected",
        lambda: build_container_params({"text": "a", "topic_tag": "a.b"}, cfg),
        expect_error=True,
    )
    case("topic_tag strips hash", lambda: build_container_params({"text": "a", "topic_tag": "#ai"}, cfg)["topic_tag"] == "ai")
    case(
        "carousel too small rejected",
        lambda: build_container_params({"text": "a", "children": ["1"]}, cfg),
        expect_error=True,
    )
    case("iso roundtrip", lambda: iso(parse_iso("2026-10-01T09:00:00Z")) == "2026-10-01T09:00:00Z")
    case("iso to unix", lambda: iso_to_unix("1970-01-02T00:00:00Z") == 86400)
    case("coerce types", lambda: [coerce("true"), coerce("7"), coerce("[]"), coerce("hi")] == [True, 7, [], "hi"])
    case("mask hides token", lambda: "secretsecret" not in str(mask("secretsecret")))
    case(
        "blocked words",
        lambda: build_container_params(
            {"text": "это реклама"}, deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), {"safety": {"blocked_words": ["реклама"]}})
        ),
        expect_error=True,
    )
    case("strip secrets", lambda: "abc" not in strip_secrets("https://x/?access_token=abc&y=1"))
    case(
        "blocked_words строкой — одно слово, не буквы",
        lambda: build_container_params(
            {"text": "совершенно безобидный текст"},
            deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), {"safety": {"blocked_words": "реклама"}}),
        ),
    )
    case(
        "blocked_words строкой всё же срабатывает",
        lambda: build_container_params(
            {"text": "тут реклама"},
            deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), {"safety": {"blocked_words": "реклама"}}),
        ),
        expect_error=True,
    )
    case("@несуществующий файл отклоняется", lambda: read_text_arg("@/нет/такого/файла.txt"), expect_error=True)
    case("refresh policy default", lambda: refresh_policy(cfg) == (True, 10))
    case(
        "refresh policy: порог 0 выключает",
        lambda: refresh_policy(
            deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), {"auth": {"refresh_when_days_left": 0}})
        ) == (False, 0),
    )
    case(
        "refresh policy: allow_refresh=false выключает",
        lambda: refresh_policy(
            deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), {"auth": {"allow_refresh": False}})
        )[0] is False,
    )
    case(
        "dotted config",
        lambda: (lambda c: (set_dotted(c, "a.b.c", 1), get_dotted(c, "a.b.c"))[1])({}) == 1,
    )

    failed = [r for r in results if not r["ok"]]
    return emit(
        {
            "ok": not failed,
            "version": __version__,
            "python": sys.version.split()[0],
            "total": len(results),
            "failed": len(failed),
            "results": results,
        },
        code=0 if not failed else 1,
    )


# --------------------------------------------------------------------------- #
# Шаблон voice.md
# --------------------------------------------------------------------------- #

VOICE_STUB_MARKER = "НЕ ЗАПОЛНЕНО"

VOICE_STUB = """# Профиль голоса

> НЕ ЗАПОЛНЕНО. Пройди онбординг: ИИ задаст вопросы и перепишет этот файл целиком.
Структура — в references/voice.md скилла.

## Кто пишет
## Для кого
## Темы
## Тон и правила
## Формат
## Запреты
## Примеры
"""


# --------------------------------------------------------------------------- #
# Парсер аргументов
# --------------------------------------------------------------------------- #


class MediaOrderAction(argparse.Action):
    """Складывает --image и --video в один список, сохраняя порядок ввода."""

    def __call__(self, parser, namespace, values, option_string=None):
        items = list(getattr(namespace, self.dest, None) or [])
        kind = "VIDEO" if (option_string or "").endswith("video") else "IMAGE"
        items.append({"kind": kind, "url": values})
        setattr(namespace, self.dest, items)


def add_post_content_args(parser, multiple_text=False):
    if multiple_text:
        parser.add_argument("--text", action="append", help="Текст поста. Повторяй для цепочки. '-' = stdin, '@file' = файл.")
    else:
        parser.add_argument("--text", help="Текст поста (до %d символов). '-' = stdin, '@file.txt' = файл." % TEXT_LIMIT)
    parser.add_argument("--image", help="Публичный HTTPS-URL картинки (JPEG/PNG).")
    parser.add_argument("--video", help="Публичный HTTPS-URL видео (MP4/MOV).")
    parser.add_argument("--alt-text", dest="alt_text", help="Alt-текст для медиа (до %d символов)." % ALT_TEXT_LIMIT)
    parser.add_argument("--link", help="link_attachment — ссылка-превью (только для текстового поста).")
    parser.add_argument("--topic-tag", dest="topic_tag", help="Тема поста, одна на пост, без '.' и '&'.")
    parser.add_argument("--reply-to", dest="reply_to", help="ID поста, к которому это ответ.")
    parser.add_argument("--reply-control", dest="reply_control", choices=REPLY_CONTROLS, help="Кто может отвечать.")
    parser.add_argument("--countries", help="Гео-гейт: ISO-коды через запятую, например RU,KZ.")


def add_publish_args(parser):
    parser.add_argument("--yes", action="store_true", help="Действительно публиковать. Без флага — только показать план.")
    parser.add_argument("--wait", type=int, help="Сколько секунд ждать перед проверкой контейнера.")
    parser.add_argument("--timeout", type=int, help="Максимум секунд на обработку контейнера.")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="threads.py",
        description="CLI для Threads Graph API. Вывод — JSON.",
    )
    parser.add_argument("--home", help="Каталог данных (по умолчанию $THREADS_AGENT_HOME или ~/.threads-agent).")
    parser.add_argument("--profile", default="default", help="Профиль/аккаунт (по умолчанию 'default').")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    # config
    config = sub.add_parser("config", help="Настройки профиля.").add_subparsers(dest="sub")
    p = config.add_parser("init", help="Создать профиль.")
    p.add_argument("--force", action="store_true", help="Перезаписать существующий config.json.")
    p.set_defaults(func=cmd_config_init)
    p = config.add_parser("set", help="Изменить значения: safety.max_posts_per_day=20")
    p.add_argument("assignments", nargs="+", metavar="КЛЮЧ=ЗНАЧЕНИЕ")
    p.add_argument("--raw", action="store_true", help="Не приводить тип, сохранить как строку.")
    p.set_defaults(func=cmd_config_set)
    p = config.add_parser("get", help="Прочитать значение.")
    p.add_argument("key", nargs="?")
    p.set_defaults(func=cmd_config_get)
    p = config.add_parser("show", help="Вся конфигурация + замаскированные секреты.")
    p.set_defaults(func=cmd_config_show)
    p = config.add_parser("path", help="Где лежат файлы профиля.")
    p.set_defaults(func=cmd_config_path)

    # auth
    auth = sub.add_parser("auth", help="Токены и доступ.").add_subparsers(dest="sub")
    p = auth.add_parser("set-app", help="Сохранить app_id / app_secret / redirect_uri.")
    p.add_argument("--app-id")
    p.add_argument("--app-secret")
    p.add_argument("--redirect-uri")
    p.set_defaults(func=cmd_auth_set_app)
    p = auth.add_parser("url", help="Построить ссылку авторизации.")
    p.add_argument("--app-id")
    p.add_argument("--redirect-uri")
    p.add_argument("--scopes", nargs="+", choices=ALL_SCOPES)
    p.add_argument("--state")
    p.set_defaults(func=cmd_auth_url)
    p = auth.add_parser("exchange", help="Обменять ?code=… на токен (и сразу на долгоживущий).")
    p.add_argument("--code", required=True)
    p.add_argument("--app-id")
    p.add_argument("--app-secret")
    p.add_argument("--redirect-uri")
    p.add_argument("--no-long-lived", action="store_true", help="Не обменивать на 60-дневный токен.")
    p.set_defaults(func=cmd_auth_exchange)
    p = auth.add_parser("save-token", help="Вставить готовый токен (например, из Meta App Dashboard).")
    p.add_argument("--token", required=True)
    p.add_argument("--app-secret")
    p.add_argument("--expires-in", type=int, help="Срок жизни в секундах, если известен.")
    p.add_argument("--long-lived", action="store_true", help="Токен уже долгоживущий, не обменивать.")
    p.add_argument("--no-exchange", action="store_true", help="Не пытаться обменять на долгоживущий.")
    p.set_defaults(func=cmd_auth_save_token)
    p = auth.add_parser("long-lived", help="Обменять текущий токен на 60-дневный.")
    p.add_argument("--app-secret")
    p.set_defaults(func=cmd_auth_long_lived)
    p = auth.add_parser("refresh", help="Продлить долгоживущий токен.")
    p.add_argument("--force", action="store_true", help="Игнорировать правило «токену должно быть ≥24ч».")
    p.set_defaults(func=cmd_auth_refresh)
    p = auth.add_parser("ensure", help="Продлить, только если срок подходит к концу.")
    p.set_defaults(func=cmd_auth_ensure)
    p = auth.add_parser("status", help="Состояние токена.")
    p.set_defaults(func=cmd_auth_status)
    p = auth.add_parser("logout", help="Удалить токен (app_id/app_secret остаются).")
    p.set_defaults(func=cmd_auth_logout)

    # аккаунт / диагностика
    p = sub.add_parser("me", help="Профиль Threads по токену.")
    p.set_defaults(func=cmd_me)
    p = sub.add_parser("quota", help="Лимиты публикации (API + локальный счётчик).")
    p.set_defaults(func=cmd_quota)
    p = sub.add_parser("doctor", help="Полная проверка: конфиг, голос, токен, API, лимиты, очередь.")
    p.add_argument("--no-refresh", action="store_true", help="Не продлевать токен автоматически.")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("selftest", help="Оффлайн-проверка сборки запросов. Сеть не нужна.")
    p.set_defaults(func=cmd_selftest)

    # публикация
    p = sub.add_parser("post", help="Опубликовать один пост или ответ.")
    add_post_content_args(p)
    p.add_argument("--quote", help="ID поста, который цитируем.")
    add_publish_args(p)
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("reply", help="Ответить на пост (синоним post --reply-to).")
    p.add_argument("--to", required=True, help="ID поста или ответа.")
    add_post_content_args(p)
    add_publish_args(p)
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("chain", help="Цепочка: каждый следующий пост — ответ на предыдущий.")
    p.add_argument("--file", help="JSON: {\"posts\": [{\"text\": …}, …]}")
    add_post_content_args(p, multiple_text=True)
    p.add_argument("--delay", type=int, help="Пауза между постами цепочки, сек.")
    add_publish_args(p)
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("carousel", help="Карусель из 2–20 медиа.")
    p.add_argument("--image", dest="media", action=MediaOrderAction,
                   help="URL картинки, повторяй. Порядок слайдов — порядок флагов.")
    p.add_argument("--video", dest="media", action=MediaOrderAction,
                   help="URL видео, повторяй. Порядок слайдов — порядок флагов.")
    p.add_argument("--text", help="Подпись к карусели.")
    p.add_argument("--alt-text", dest="alt_text", action="append", help="Alt-текст по порядку медиа.")
    p.add_argument("--topic-tag", dest="topic_tag")
    p.add_argument("--reply-to", dest="reply_to")
    p.add_argument("--reply-control", dest="reply_control", choices=REPLY_CONTROLS)
    add_publish_args(p)
    p.set_defaults(func=cmd_carousel)

    # чтение
    p = sub.add_parser("list", help="Мои посты.")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--since", help="ISO-дата или unix-время.")
    p.add_argument("--until", help="ISO-дата или unix-время.")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("get", help="Один пост по id.")
    p.add_argument("--media-id", dest="media_id", required=True)
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("replies", help="Ответы к посту, мои ответы или упоминания.")
    p.add_argument("--media-id", dest="media_id")
    p.add_argument("--conversation", action="store_true", help="Вся ветка, а не только верхний уровень.")
    p.add_argument("--mine", action="store_true", help="Мои ответы.")
    p.add_argument("--mentions", action="store_true", help="Упоминания меня.")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_replies)
    p = sub.add_parser("hide", help="Скрыть/показать ответ под моим постом.")
    p.add_argument("--reply-id", dest="reply_id", required=True)
    p.add_argument("--unhide", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_hide)
    p = sub.add_parser("insights", help="Метрики поста или аккаунта.")
    p.add_argument("--media-id", dest="media_id", help="Без него — метрики аккаунта.")
    p.add_argument("--metrics", help="Список метрик через запятую.")
    p.add_argument("--since")
    p.add_argument("--until")
    p.set_defaults(func=cmd_insights)
    p = sub.add_parser("history", help="Что уже опубликовано этим агентом.")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    # очередь
    queue = sub.add_parser("queue", help="Локальная очередь отложенных публикаций.").add_subparsers(dest="sub")
    p = queue.add_parser("add", help="Поставить публикацию на время.")
    p.add_argument("--at", required=True, help="Когда публиковать, ISO-8601 (с часовым поясом).")
    p.add_argument("--note", help="Пометка для себя.")
    p.add_argument("--file", help="JSON цепочки, как в `chain --file`.")
    add_post_content_args(p)
    p.set_defaults(func=cmd_queue_add)
    p = queue.add_parser("list", help="Показать очередь.")
    p.add_argument("--status", choices=["pending", "published", "failed"])
    p.add_argument("--due", action="store_true", help="Только те, чьё время пришло.")
    p.set_defaults(func=cmd_queue_list)
    p = queue.add_parser("rm", help="Удалить записи по id.")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_queue_rm)
    p = queue.add_parser("run", help="Опубликовать наступившие записи.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--id", action="append", help="Только эти записи, независимо от времени.")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_queue_run)
    p = queue.add_parser("cron", help="Строка для crontab, чтобы очередь публиковалась сама.")
    p.add_argument("--minute", type=int, help="Минута часа (по умолчанию 5).")
    p.set_defaults(func=cmd_queue_cron)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    store = Store(home=args.home, profile=args.profile)
    try:
        return args.func(args, store) or 0
    except CliError as exc:
        payload = {"ok": False, "error": exc.message}
        if exc.hint:
            payload["hint"] = exc.hint
        if exc.details:
            payload["details"] = exc.details
        return emit(payload, code=exc.code)
    except KeyboardInterrupt:
        return emit({"ok": False, "error": "Прервано пользователем."}, code=1)
    except Exception as exc:  # noqa: BLE001 — наружу всегда уходит JSON, не traceback
        import traceback

        return emit(
            {
                "ok": False,
                "error": "Внутренняя ошибка: %s: %s" % (type(exc).__name__, exc),
                "hint": "Это баг скрипта. Покажи поле details целиком.",
                "details": {"traceback": traceback.format_exc().splitlines()[-6:]},
            },
            code=1,
        )


if __name__ == "__main__":
    sys.exit(main())
