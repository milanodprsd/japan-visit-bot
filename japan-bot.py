#!/usr/bin/env python3
"""Телеграм-бот для планирования поездки: общий список мест с геопозицией и приоритетом.

Без внешних зависимостей. Запуск:
    export BOT_TOKEN="токен от @BotFather"
    export ALLOWED_IDS="111111,222222"   # необязательно: id тебя и девушки (узнать: команда /id)
    python3 bot.py
"""
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.environ.get("DB_PATH", "places.db")

CATS = {
    "shop": "🛍 Аниме-магазины",
    "place": "⛩ Места",
    "cafe": "☕ Кафе",
}
PRIO_HINT = {1: "если будет время", 2: "было бы неплохо", 3: "хочется", 4: "очень хочется", 5: "обязательно!"}

BTN_ADD = "➕ Добавить"
BTN_LIST = "📋 Список"

db = sqlite3.connect(DB_PATH)
db.execute(
    """CREATE TABLE IF NOT EXISTS places (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        name TEXT NOT NULL,
        lat REAL, lon REAL,
        priority INTEGER NOT NULL DEFAULT 3,
        added_by INTEGER,
        created INTEGER
    )"""
)
db.execute("CREATE TABLE IF NOT EXISTS state (chat_id INTEGER PRIMARY KEY, data TEXT)")
db.commit()


# ---------- Telegram API ----------
def api(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print("API error", method, e.read().decode()[:300], file=sys.stderr)
        return {"ok": False}


def send(chat, text, kb=None):
    return api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
               disable_web_page_preview=True, reply_markup=kb)


def inline(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


MAIN_KB = {"keyboard": [[{"text": BTN_ADD}, {"text": BTN_LIST}]], "resize_keyboard": True}


# ---------- Состояние диалога ----------
def get_state(chat):
    row = db.execute("SELECT data FROM state WHERE chat_id=?", (chat,)).fetchone()
    return json.loads(row[0]) if row else None


def set_state(chat, st):
    db.execute("INSERT OR REPLACE INTO state(chat_id,data) VALUES(?,?)", (chat, json.dumps(st)))
    db.commit()


def clear_state(chat):
    db.execute("DELETE FROM state WHERE chat_id=?", (chat,))
    db.commit()


# ---------- Геопозиция ----------
COORD_PATTERNS = [
    r"@(-?\d+\.\d+),(-?\d+\.\d+)",
    r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
    r"[?&](?:q|ll|query|destination)=(-?\d+\.\d+)(?:,|%2C)(-?\d+\.\d+)",
    r"^\s*(-?\d{1,2}\.\d+)[,\s]+(-?\d{1,3}\.\d+)\s*$",
]


def find_coords(text):
    for p in COORD_PATTERNS:
        m = re.search(p, text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


def parse_location_text(text):
    text = text.strip()
    c = find_coords(text)
    if c:
        return c
    m = re.search(r"https?://\S+", text)
    if m:  # короткие ссылки (maps.app.goo.gl) раскрываем редиректом
        try:
            req = urllib.request.Request(m.group(0), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return find_coords(urllib.parse.unquote(r.geturl())) or find_coords(
                    r.read(300000).decode("utf-8", "ignore"))
        except Exception as e:
            print("resolve failed", e, file=sys.stderr)
    return None


def map_url(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


# ---------- Добавление места ----------
def stars(p):
    return "★" * p + "☆" * (5 - p)


def prompt(chat, st):
    """Спрашивает следующее недостающее поле или сохраняет место."""
    if not st.get("category"):
        send(chat, "Что это?", inline([[(CATS[k], f"cat:{k}")] for k in CATS]))
    elif not st.get("name"):
        send(chat, "Как называется?")
    elif not st.get("geo_done"):
        send(chat, "Пришли геопозицию: 📎 → Геопозиция (можно выбрать заведение из списка), "
                   "или ссылку из Google Maps, или координаты.",
             inline([[("Пропустить", "skipgeo"), ("Отмена", "cancel")]]))
    elif not st.get("priority"):
        send(chat, "Насколько хочется?",
             inline([[(f"{n}★", f"prio:{n}") for n in range(1, 6)],
                     [("1 — если будет время", "noop"), ("5 — обязательно!", "noop")]]))
    else:
        db.execute(
            "INSERT INTO places(category,name,lat,lon,priority,added_by,created) VALUES(?,?,?,?,?,?,?)",
            (st["category"], st["name"], st.get("lat"), st.get("lon"), st["priority"], st.get("by"), int(time.time())),
        )
        db.commit()
        clear_state(chat)
        geo = "📍 с геопозицией" if st.get("lat") is not None else "без геопозиции"
        send(chat, f"Добавлено: <b>{html.escape(st['name'])}</b>\n{CATS[st['category']]} · {stars(st['priority'])} · {geo}",
             MAIN_KB)
        return
    set_state(chat, st)


# ---------- Список ----------
def fmt_item(row):
    pid, cat, name, lat, lon, prio = row
    title = f"<a href=\"{map_url(lat, lon)}\">{html.escape(name)}</a> 📍" if lat is not None else html.escape(name)
    return f"{stars(prio)} {title}  /edit_{pid}"


def show_list(chat, cat):
    cats = list(CATS) if cat == "all" else [cat]
    blocks, total = [], 0
    for c in cats:
        rows = db.execute(
            "SELECT id,category,name,lat,lon,priority FROM places WHERE category=? ORDER BY priority DESC, id", (c,)
        ).fetchall()
        total += len(rows)
        if cat == "all" and not rows:
            continue
        head = f"<b>{CATS[c]}</b> ({len(rows)})"
        blocks.append(head + ("\n" + "\n".join(fmt_item(r) for r in rows) if rows else "\n— пока пусто"))
    if total == 0 and cat != "all":
        blocks = [f"<b>{CATS[cat]}</b>\n— пока пусто"]
    if not blocks:
        send(chat, "Список пока пуст. Нажми «➕ Добавить».", MAIN_KB)
        return
    text = "\n\n".join(blocks)
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            send(chat, chunk)
            chunk = ""
        chunk += line + "\n"
    send(chat, chunk, list_menu())


def list_menu():
    return inline([[("Все", "list:all")], [(CATS[k], f"list:{k}") for k in ("shop", "place")], [(CATS["cafe"], "list:cafe")]])


def show_item(chat, pid):
    row = db.execute("SELECT id,category,name,lat,lon,priority FROM places WHERE id=?", (pid,)).fetchone()
    if not row:
        send(chat, "Такого места нет.")
        return
    _, cat, name, lat, lon, prio = row
    if lat is not None:
        api("sendLocation", chat_id=chat, latitude=lat, longitude=lon)
    send(chat, f"<b>{html.escape(name)}</b>\n{CATS[cat]} · {stars(prio)} ({PRIO_HINT[prio]})",
         inline([[(f"{n}★", f"setprio:{pid}:{n}") for n in range(1, 6)],
                 [("🗑 Удалить", f"del:{pid}")]]))


# ---------- Обработка апдейтов ----------
def allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


def handle_message(msg):
    chat = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if text == "/id":
        send(chat, f"Твой id: <code>{uid}</code>")
        return
    if not allowed(uid):
        send(chat, f"Нет доступа. Твой id: <code>{uid}</code>")
        return

    if text in ("/start", "/help"):
        send(chat, "Привет! Это общий список мест для поездки в Японию.\n\n"
                   "➕ Добавить — категория, название, геопозиция, приоритет\n"
                   "📋 Список — по категориям, сверху то, что хочется больше всего\n\n"
                   "Быстрый способ: просто пришли геопозицию заведения (📎 → Геопозиция), "
                   "бот подхватит название и спросит остальное.\n/cancel — отменить добавление.", MAIN_KB)
        return
    if text == "/cancel":
        clear_state(chat)
        send(chat, "Отменено.", MAIN_KB)
        return
    if text in ("/add", BTN_ADD):
        prompt(chat, {"by": uid})
        return
    if text in ("/list", BTN_LIST):
        send(chat, "Что показать?", list_menu())
        return
    m = re.match(r"/edit_(\d+)", text)
    if m:
        show_item(chat, int(m.group(1)))
        return

    st = get_state(chat)

    # пришла геопозиция / заведение
    loc = msg.get("location") or (msg.get("venue") or {}).get("location")
    if loc:
        venue = msg.get("venue") or {}
        if not st:
            st = {"by": uid}
        st.update(lat=loc["latitude"], lon=loc["longitude"], geo_done=True)
        if not st.get("name") and venue.get("title"):
            st["name"] = venue["title"]
        prompt(chat, st)
        return

    if not text or text.startswith("/"):
        return

    if st and st.get("category") and st.get("name") and not st.get("geo_done"):
        c = parse_location_text(text)
        if c:
            st.update(lat=c[0], lon=c[1], geo_done=True)
            prompt(chat, st)
        else:
            send(chat, "Не смог достать координаты. Пришли геопозицию из Telegram, "
                       "полную ссылку Google Maps или нажми «Пропустить».")
        return
    if st and st.get("category") and not st.get("name"):
        st["name"] = text[:120]
        prompt(chat, st)
        return
    if not st:  # просто текст = название нового места
        prompt(chat, {"by": uid, "name": text[:120]})
        return
    send(chat, "Выбери вариант кнопкой выше или /cancel.")


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    uid = cb["from"]["id"]
    data = cb["data"]
    api("answerCallbackQuery", callback_query_id=cb["id"])
    if not allowed(uid):
        return
    st = get_state(chat)
    kind, _, rest = data.partition(":")

    if kind == "cat":
        st = st or {"by": uid}
        st["category"] = rest
        prompt(chat, st)
    elif kind == "skipgeo" and st:
        st["geo_done"] = True
        prompt(chat, st)
    elif kind == "prio" and st:
        st["priority"] = int(rest)
        prompt(chat, st)
    elif kind == "cancel":
        clear_state(chat)
        send(chat, "Отменено.", MAIN_KB)
    elif kind == "list":
        show_list(chat, rest)
    elif kind == "setprio":
        pid, n = rest.split(":")
        db.execute("UPDATE places SET priority=? WHERE id=?", (int(n), int(pid)))
        db.commit()
        send(chat, f"Приоритет обновлён: {stars(int(n))}")
    elif kind == "del":
        send(chat, "Точно удалить?", inline([[("Да, удалить", f"delok:{rest}"), ("Нет", "noop")]]))
    elif kind == "delok":
        db.execute("DELETE FROM places WHERE id=?", (int(rest),))
        db.commit()
        send(chat, "Удалено.")


def main():
    if not TOKEN:
        sys.exit("Задай BOT_TOKEN (токен от @BotFather)")
    if not ALLOWED:
        print("Внимание: ALLOWED_IDS не задан — ботом сможет пользоваться любой, кто его найдёт.")
    offset = None
    print("Бот запущен")
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        handle_message(u["message"])
                    elif "callback_query" in u:
                        handle_callback(u["callback_query"])
                except Exception as e:
                    print("handler error", repr(e), file=sys.stderr)
        except Exception as e:
            print("poll error", repr(e), file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
