#!/usr/bin/env python3
"""Publish the Yuki demo page to Telegra.ph in one command.

Run from a machine with normal internet access. These images must sit next to
the script: cover.png, pipeline_leshik.png, block_leshik.png, block_maria.png,
block_man.png, block_me.png.

    python3 publish_telegraph.py

Creates an anonymous Telegra.ph account, uploads the images, builds the page
and prints the public URL plus the access token (keep it to edit later).
Stdlib only — no API keys, no dependencies.
"""
from __future__ import annotations

import json
import mimetypes
import pathlib
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
API = "https://api.telegra.ph"
UPLOAD = "https://telegra.ph/upload"

TITLE = "Yuki — стикерпак из одного фото"
AUTHOR = "Yuki Stickers"
AUTHOR_URL = "https://t.me/yuki_stickers_bot"

INTRO = (
    "Пришли боту одно фото человека — и через пару минут получишь набор "
    "рисованных стикеров с русскими подписями, опубликованный прямо в Telegram. "
    "Ниже — живые примеры: слева исходное фото, справа готовые стикеры."
)
ORIGIN = (
    "Всё началось с Лёшика. По текстовому описанию нейросеть упорно рисовала "
    "совершенно другого ребёнка — поэтому появился итеративный алгоритм: бот "
    "рисует персонажа шаг за шагом и на каждом шаге сверяется с исходным лицом. "
    "Так сходство не теряется ни на одном фото."
)
STEPS = [
    "Пришли фото человека (лицо покрупнее, один в кадре).",
    "Бот превращает его в нарисованного персонажа в выбранном стиле.",
    "Отмечаешь готовые реакции или описываешь свои подписи.",
    "Получаешь опубликованный пак в Telegram — или скачиваешь архивом.",
]
CTA = "Сделать свой пак: "

IMAGES = {
    "cover": "cover.png",
    "pipeline": "pipeline_leshik.png",
    "leshik": "block_leshik.png",
    "maria": "block_maria.png",
    "man": "block_man.png",
    "me": "block_me.png",
}


def _multipart(path: pathlib.Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), boundary


def upload(path: pathlib.Path) -> str:
    body, boundary = _multipart(path)
    req = urllib.request.Request(
        UPLOAD, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"upload failed for {path.name}: {data['error']}")
    src = data[0]["src"] if isinstance(data, list) else data["src"]
    return f"https://telegra.ph{src}"


def api(method: str, **params) -> dict:
    req = urllib.request.Request(
        f"{API}/{method}", data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"{method} failed: {out.get('error')}")
    return out["result"]


def img(src: str) -> dict:
    return {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": src}}]}


def main() -> None:
    missing = [n for n in IMAGES.values() if not (HERE / n).exists()]
    if missing:
        raise SystemExit(f"Нет файлов рядом со скриптом: {', '.join(missing)}")

    print("· создаю анонимный аккаунт Telegra.ph…")
    token = api("createAccount", short_name="yuki", author_name=AUTHOR, author_url=AUTHOR_URL)[
        "access_token"
    ]

    print("· загружаю картинки…")
    url = {key: upload(HERE / name) for key, name in IMAGES.items()}

    content: list[dict] = [
        img(url["cover"]),
        {"tag": "p", "children": [INTRO]},
        {"tag": "h3", "children": ["С чего всё началось"]},
        {"tag": "p", "children": [ORIGIN]},
        img(url["pipeline"]),
        img(url["leshik"]),
        {"tag": "h3", "children": ["Как это работает"]},
        {"tag": "ol", "children": [{"tag": "li", "children": [s]} for s in STEPS]},
        {"tag": "h3", "children": ["Ещё примеры"]},
        img(url["maria"]),
        img(url["man"]),
        img(url["me"]),
        {
            "tag": "p",
            "children": [
                CTA,
                {"tag": "a", "attrs": {"href": AUTHOR_URL}, "children": ["@yuki_stickers_bot"]},
            ],
        },
    ]

    print("· публикую страницу…")
    page = api(
        "createPage", access_token=token, title=TITLE, author_name=AUTHOR,
        author_url=AUTHOR_URL, content=content, return_content=False,
    )
    print("\n✅ Готово!")
    print(f"   Страница: {page['url']}")
    print(f"   access_token (сохрани, чтобы править позже): {token}")


if __name__ == "__main__":
    main()
