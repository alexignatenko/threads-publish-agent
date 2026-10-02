#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка боевого пути публикации с заглушкой HTTP вместо сети.

Запуск из корня репозитория:
    python3 tests/test_publish_flow.py

Сеть не нужна и не используется: http_request подменяется заглушкой, которая
проверяет, какие именно параметры уходили бы в Threads API. Дополняет
`threads.py selftest`, который проверяет только сборку и валидацию запросов.
"""

import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "threads-publishing" / "scripts"))
import threads as th  # noqa: E402

HOME = pathlib.Path(tempfile.mkdtemp(prefix="threads-test-"))

CALLS = []
SEQ = {"container": 0, "media": 0}


def fake_http(method, url, params=None, data=None, timeout=60, retries=3):
    params = params or {}
    data = data or {}
    path = url.split("/v1.0/")[-1]
    sent = {k: v for k, v in list(params.items()) + list(data.items()) if k != "access_token"}
    CALLS.append((method, path, sent))
    if method == "POST" and path.endswith("/threads"):
        SEQ["container"] += 1
        return {"id": "cont_%d" % SEQ["container"]}
    if method == "POST" and path.endswith("/threads_publish"):
        SEQ["media"] += 1
        return {"id": "media_%d" % SEQ["media"]}
    if method == "GET" and str(params.get("fields", "")).startswith("status"):
        return {"id": path, "status": "FINISHED"}
    if method == "GET":
        return {
            "id": path,
            "permalink": "https://www.threads.net/@user/post/%s" % path,
            "timestamp": "2026-09-30T10:00:00+0000",
            "username": "user",
        }
    raise AssertionError("неожиданный вызов %s %s" % (method, url))


def run(argv):
    """Запускает CLI, перехватывая вывод вместо печати в stdout."""
    out = []
    real = th.emit
    th.emit = lambda payload, code=0: (out.append(payload), code)[1]
    try:
        rc = th.main(["--home", str(HOME)] + argv)
    finally:
        th.emit = real
    return rc, (out[-1] if out else None)


def main():
    th.http_request = fake_http
    assert run(["config", "init"])[0] == 0
    th.Store(home=str(HOME)).save_credentials(
        {
            "access_token": "test-token",
            "user_id": "42",
            "app_secret": "test-secret",
            "long_lived": True,
            "obtained_at": th.iso(th.now_utc() - th.timedelta(days=1)),
            "expires_at": th.iso(th.now_utc() + th.timedelta(days=59)),
        }
    )

    # 1. Одиночный пост: контейнер -> публикация -> permalink.
    rc, res = run(["post", "--text", "Первый настоящий пост", "--yes"])
    assert rc == 0 and res["published"] is True, res
    assert res["result"]["media_id"] == "media_1", res
    assert res["result"]["permalink"].endswith("media_1"), res
    print("ok  post ->", res["result"]["permalink"])

    # 2. Без --yes ничего не уходит в API.
    CALLS.clear()
    rc, res = run(["post", "--text", "не должен уйти"])
    assert rc == 0 and res["published"] is False and res["dry_run"] is True, res
    assert CALLS == [], "в режиме предпросмотра не должно быть обращений к API: %s" % CALLS
    print("ok  dry-run -> ни одного обращения к API")

    # 3. Цепочка: reply_to_id указывает на предыдущий опубликованный пост.
    CALLS.clear()
    rc, res = run(["chain", "--text", "A", "--text", "B", "--text", "C", "--yes", "--delay", "0"])
    assert rc == 0 and res["count"] == 3, res
    assert [r["media_id"] for r in res["results"]] == ["media_2", "media_3", "media_4"], res
    creates = [c for c in CALLS if c[0] == "POST" and c[1].endswith("/threads")]
    assert "reply_to_id" not in creates[0][2], creates[0]
    assert creates[1][2]["reply_to_id"] == "media_2", creates[1]
    assert creates[2][2]["reply_to_id"] == "media_3", creates[2]
    print("ok  chain -> reply_to_id связаны верно")

    # 4. Карусель: дети с is_carousel_item и alt_text по порядку, родитель с children.
    CALLS.clear()
    rc, res = run(
        [
            "carousel", "--image", "https://a/1.jpg", "--image", "https://a/2.jpg",
            "--text", "Подпись", "--alt-text", "один", "--alt-text", "два", "--yes",
        ]
    )
    assert rc == 0 and res["published"] is True, res
    creates = [c for c in CALLS if c[0] == "POST" and c[1].endswith("/threads")]
    assert creates[0][2]["is_carousel_item"] == "true", creates[0]
    assert creates[0][2]["alt_text"] == "один" and creates[1][2]["alt_text"] == "два", creates[:2]
    assert creates[2][2]["media_type"] == "CAROUSEL" and "," in creates[2][2]["children"], creates[2]
    assert "is_carousel_item" not in creates[2][2] and creates[2][2]["text"] == "Подпись", creates[2]
    print("ok  carousel -> children =", creates[2][2]["children"])

    # 5. Видео: статус контейнера проверяется до публикации.
    CALLS.clear()
    rc, res = run(["post", "--video", "https://a/v.mp4", "--text", "видео", "--yes", "--wait", "0"])
    assert rc == 0 and res["result"]["container_status"] == "FINISHED", res
    assert any(c[0] == "GET" and str(c[2].get("fields", "")).startswith("status") for c in CALLS), CALLS
    print("ok  video -> статус контейнера проверен до публикации")

    # 6. Локальный суточный лимит останавливает публикацию.
    run(["config", "set", "safety.max_posts_per_day=1"])
    rc, res = run(["post", "--text", "лишний", "--yes"])
    assert rc == 1 and "лимит" in res["error"], res
    print("ok  лимит ->", res["error"])
    run(["config", "set", "safety.max_posts_per_day=50"])

    # 7. Журнал различает посты и ответы.
    rc, res = run(["history"])
    kinds = [r["kind"] for r in res["history"]]
    assert kinds == ["post", "post", "reply", "reply", "post", "post"], kinds
    print("ok  history ->", kinds)

    # 8. Очередь: наступившая запись публикуется и помечается.
    rc, res = run(["queue", "add", "--at", "2020-01-01T00:00:00Z", "--text", "Из очереди"])
    queue_id = res["added"]["id"]
    rc, res = run(["queue", "run", "--yes"])
    assert rc == 0 and res["published_count"] == 1 and res["failed_count"] == 0, res
    rc, res = run(["queue", "list", "--status", "published"])
    assert res["items"][0]["id"] == queue_id and res["items"][0]["results"][0]["permalink"], res
    print("ok  queue ->", queue_id, "опубликована")

    # 9. doctor: незаполненный профиль голоса ловится, заполненный проходит.
    rc, res = run(["doctor", "--no-refresh"])
    assert [c for c in res["checks"] if c["check"] == "voice"][0]["ok"] is False, res["checks"]
    th.Store(home=str(HOME)).voice_path.write_text(
        "# Профиль голоса\n" + "Пишу спокойно, на «ты», без эмодзи. " * 20, encoding="utf-8"
    )
    rc, res = run(["doctor", "--no-refresh"])
    assert rc == 0 and res["ok"] is True, res
    assert all(c["ok"] for c in res["checks"]), res["checks"]
    print("ok  doctor ->", [c["check"] for c in res["checks"]])

    # 10. Токен близок к истечению: allow_refresh решает, трогать его или нет.
    #     Важно для случая, когда тот же токен используют в другом сервисе.
    store = th.Store(home=str(HOME))
    creds = store.load_credentials()
    creds["expires_at"] = th.iso(th.now_utc() + th.timedelta(days=3))
    store.save_credentials(creds)

    run(["config", "set", "auth.allow_refresh=false"])
    CALLS.clear()
    rc, res = run(["doctor"])
    assert not any("refresh_access_token" in c[1] for c in CALLS), CALLS
    assert not any(c["check"] == "token_refresh" for c in res["checks"]), res["checks"]
    validity = [c for c in res["checks"] if c["check"] == "token_validity"][0]
    assert "автопродление выключено" in validity["detail"], validity
    rc, res = run(["auth", "ensure"])
    assert rc == 0 and res["refreshed"] is False and "выключено" in res["reason"], res
    rc, res = run(["auth", "refresh"])
    assert rc == 1 and "выключено" in res["error"], res
    print("ok  allow_refresh=false -> токен не трогается ни doctor, ни ensure, ни refresh")

    # Порог 0 должен работать так же (раньше 0 молча превращался в 10).
    run(["config", "set", "auth.allow_refresh=true", "auth.refresh_when_days_left=0"])
    CALLS.clear()
    run(["doctor"])
    assert not any("refresh_access_token" in c[1] for c in CALLS), CALLS
    print("ok  refresh_when_days_left=0 -> тоже не продлевает")

    # А при разрешённом продлении doctor обязан продлить.
    run(["config", "set", "auth.refresh_when_days_left=10"])
    refreshed = {"done": False}

    def with_refresh(method, url, params=None, data=None, timeout=60, retries=3):
        if "refresh_access_token" in url:
            refreshed["done"] = True
            return {"access_token": "new-token", "expires_in": 5183944}
        return fake_http(method, url, params, data, timeout, retries)

    th.http_request = with_refresh
    rc, res = run(["doctor"])
    assert refreshed["done"] is True, "doctor должен был продлить токен"
    assert [c for c in res["checks"] if c["check"] == "token_refresh"][0]["ok"] is True, res["checks"]
    assert th.Store(home=str(HOME)).load_credentials()["access_token"] == "new-token"
    print("ok  allow_refresh=true -> doctor продлевает и сохраняет новый токен")
    th.http_request = fake_http

    # 11. Оборванная цепочка: partial + подсказка, где продолжить.
    state = {"publishes": 0}

    def flaky(method, url, params=None, data=None, timeout=60, retries=3):
        if method == "POST" and url.endswith("/threads_publish"):
            state["publishes"] += 1
            if state["publishes"] == 2:
                raise th.CliError("API упал", code=1)
        return fake_http(method, url, params, data, timeout, retries)

    th.http_request = flaky
    rc, res = run(["chain", "--text", "X", "--text", "Y", "--text", "Z", "--yes", "--delay", "0"])
    assert rc == 1 and res["partial"] is True and len(res["results"]) == 1, res
    assert "post --reply-to" in res["next"], res
    print("ok  partial ->", res["next"])

    # 12. Карусель сохраняет порядок, в котором перечислены медиа.
    th.http_request = fake_http
    CALLS.clear()
    rc, res = run(
        [
            "carousel", "--image", "A.jpg", "--video", "V.mp4", "--image", "B.jpg",
            "--alt-text", "alt-A", "--alt-text", "alt-V", "--alt-text", "alt-B",
            "--text", "подпись", "--yes",
        ]
    )
    assert rc == 0, res
    children = [
        c[2] for c in CALLS
        if c[0] == "POST" and c[1].endswith("/threads") and c[2].get("is_carousel_item")
    ]
    pairs = [(c.get("image_url") or c.get("video_url"), c.get("alt_text")) for c in children]
    assert pairs == [("A.jpg", "alt-A"), ("V.mp4", "alt-V"), ("B.jpg", "alt-B")], pairs
    print("ok  carousel -> порядок медиа и alt-текстов сохранён")

    # 13. Публикация без id в ответе: понятная ошибка, а не traceback.
    def no_id(method, url, params=None, data=None, timeout=60, retries=3):
        if method == "POST" and url.endswith("/threads_publish"):
            return {}
        return fake_http(method, url, params, data, timeout, retries)

    th.http_request = no_id
    rc, res = run(["post", "--text", "без id", "--yes"])
    assert rc == 1 and res["ok"] is False, res
    assert "не вернул id" in res["error"], res
    print("ok  publish без id -> ошибка, а не падение")

    # 14. Любое неожиданное исключение выходит наружу как JSON.
    def boom(*args, **kwargs):
        raise RuntimeError("что-то сломалось")

    th.http_request = boom
    rc, res = run(["me"])
    assert rc == 1 and res["ok"] is False and "Внутренняя ошибка" in res["error"], res
    print("ok  неожиданное исключение -> JSON, а не traceback")
    th.http_request = fake_http

    # 15. Цепочка через --text отклоняет флаги одного поста, а не теряет их молча.
    rc, res = run(["chain", "--text", "A", "--text", "B", "--image", "https://x/i.jpg"])
    assert rc == 2 and "--image" in res["error"], res
    print("ok  chain --text -> неприменимые флаги названы явно")

    # 16. Отложенная запись сохраняет гео-гейт.
    rc, res = run(["queue", "add", "--at", "2099-01-01T00:00:00Z", "--text", "гео", "--countries", "RU,KZ"])
    assert res["added"]["posts"][0]["allowlisted_country_codes"] == "RU,KZ", res
    print("ok  queue add -> --countries не теряется")

    print("\nВСЕ ПРОВЕРКИ БОЕВОГО ПУТИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
