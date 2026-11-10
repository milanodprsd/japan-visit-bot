#!/usr/bin/env python3
"""Телеграм-бот для планирования поездки: общий список мест с геопозицией и приоритетом.

Интерфейс — одно сообщение-«панель», которое перерисовывается при переходах (чат не разрастается).
Без внешних зависимостей. Запуск:
    export BOT_TOKEN="токен от @BotFather"
    export ALLOWED_IDS="111111,222222"   # необязательно: id тебя и девушки (узнать: команда /id)
    python3 japan-bot.py
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
PAGE = 8
HELP = ("Общий список мест для Японии.\n"
        "Вкладки сверху — категории, цифра — открыть место, ➕ — добавить.\n"
        "Быстрый способ добавить: пришли геопозицию заведения (📎 → Геопозиция).")

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
db.execute("CREATE TABLE IF NOT EXISTS panel (chat_id INTEGER PRIMARY KEY, message_id INTEGER)")
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
        body = e.read().decode("utf-8", "ignore")
        try:
            res = json.loads(body)
        except ValueError:
            res = {"ok": False, "description": body[:200]}
        if "not modified" not in res.get("description", ""):
            print("API error", method, res.get("description"), file=sys.stderr)
        return res


def inline(rows):
    """rows: список рядов из пар (текст, callback_data или https-ссылка)."""
    return {"inline_keyboard": [
        [({"text": t, "url": d} if d.startswith("http") else {"text": t, "callback_data": d}) for t, d in row]
        for row in rows
    ]}


# ---------- Панель: одно сообщение, которое редактируется ----------
def get_panel(chat):
    row = db.execute("SELECT message_id FROM panel WHERE chat_id=?", (chat,)).fetchone()
    return row[0] if row else None


def set_panel(chat, mid):
    if mid is None:
        db.execute("DELETE FROM panel WHERE chat_id=?", (chat,))
    else:
        db.execute("INSERT OR REPLACE INTO panel(chat_id,message_id) VALUES(?,?)", (chat, mid))
    db.commit()


def render(chat, text, kb=None, mid=None):
    mid = mid or get_panel(chat)
    if mid:
        r = api("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=kb)
        if r.get("ok") or "not modified" in r.get("description", ""):
            return
    r = api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=kb)
    if r.get("ok"):
        if mid:  # старая панель устарела/не редактируется — убираем её
            api("deleteMessage", chat_id=chat, message_id=mid)
        set_panel(chat, r["result"]["message_id"])


# ---------- Состояние мастера добавления ----------
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


def stars(p):
    return "★" * p + "☆" * (5 - p)


# ---------- Экран: список с вкладками ----------
def show_list(chat, cat="all", page=0, mid=None, note=""):
    cnt = dict(db.execute("SELECT category, COUNT(*) FROM places GROUP BY category").fetchall())
    total = sum(cnt.values())
    if cat == "all":
        rows = db.execute("SELECT id,category,name,lat,lon,priority FROM places ORDER BY priority DESC, id").fetchall()
    else:
        rows = db.execute("SELECT id,category,name,lat,lon,priority FROM places WHERE category=? "
                          "ORDER BY priority DESC, id", (cat,)).fetchall()
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * PAGE:(page + 1) * PAGE]

    lines = [note, ""] if note else []
    head = f"<b>{'Все места' if cat == 'all' else CATS[cat]}</b> — {len(rows)}"
    if pages > 1:
        head += f" · стр. {page + 1}/{pages}"
    lines += [head, ""]
    if not chunk:
        lines.append("Пока пусто. Нажми «➕ Добавить».")
    for i, (pid, c, name, lat, lon, prio) in enumerate(chunk, page * PAGE + 1):
        label = html.escape(name)
        if lat is not None:
            label = f'<a href="{map_url(lat, lon)}">{label}</a> 📍'
        icon = CATS[c].split()[0] + " " if cat == "all" else ""
        lines.append(f"{i}. {icon}{stars(prio)} {label}")
    if chunk:
        lines += ["", "Нажми номер, чтобы открыть место."]

    tabs = []
    for key, label in [("all", "Все")] + [(k, CATS[k].split()[0]) for k in CATS]:
        n = total if key == "all" else cnt.get(key, 0)
        t = f"{label} {n}"
        tabs.append((f"• {t}" if key == cat else t, f"list:{key}:0"))
    kb = [tabs]
    nums = [(str(i), f"item:{r[0]}:{cat}:{page}") for i, r in enumerate(chunk, page * PAGE + 1)]
    for i in range(0, len(nums), 4):
        kb.append(nums[i:i + 4])
    if pages > 1:
        kb.append([("◀", f"list:{cat}:{max(page - 1, 0)}"), (f"{page + 1}/{pages}", "noop"),
                   ("▶", f"list:{cat}:{min(page + 1, pages - 1)}")])
    kb.append([("➕ Добавить", f"add:{cat}")])
    render(chat, "\n".join(lines), inline(kb), mid)


# ---------- Экран: карточка места ----------
def show_item(chat, pid, cat, page, mid=None, note=""):
    row = db.execute("SELECT id,category,name,lat,lon,priority FROM places WHERE id=?", (pid,)).fetchone()
    if not row:
        show_list(chat, cat, page, mid, "Такого места уже нет.")
        return
    _, c, name, lat, lon, prio = row
    lines = [note, ""] if note else []
    lines += [f"<b>{html.escape(name)}</b>", CATS[c],
              f"{stars(prio)} — {PRIO_HINT[prio]}",
              "📍 Геопозиция есть" if lat is not None else "Геопозиции нет"]
    kb = [[(f"{n}★", f"setprio:{pid}:{n}:{cat}:{page}") for n in range(1, 6)]]
    if lat is not None:
        kb.append([("📍 Открыть на карте", map_url(lat, lon))])
    kb.append([("🗑 Удалить", f"del:{pid}:{cat}:{page}"), ("⬅ Назад", f"list:{cat}:{page}")])
    render(chat, "\n".join(lines), inline(kb), mid)


# ---------- Экран: мастер добавления ----------
def draft_text(st):
    lines = ["<b>➕ Новое место</b>"]
    if st.get("category"):
        lines.append(f"Категория: {CATS[st['category']]}")
    if st.get("name"):
        lines.append(f"Название: {html.escape(st['name'])}")
    if st.get("geo_done"):
        lines.append("Геопозиция: 📍 есть" if st.get("lat") is not None else "Геопозиция: —")
    return "\n".join(lines)


def prompt(chat, st, mid=None, note=""):
    """Показывает следующий недостающий шаг или сохраняет место."""
    if st.get("category") and st.get("name") and st.get("geo_done") and st.get("priority"):
        db.execute(
            "INSERT INTO places(category,name,lat,lon,priority,added_by,created) VALUES(?,?,?,?,?,?,?)",
            (st["category"], st["name"], st.get("lat"), st.get("lon"), st["priority"], st.get("by"), int(time.time())),
        )
        db.commit()
        clear_state(chat)
        show_list(chat, st["category"], 0, mid, f"✅ Добавлено: <b>{html.escape(st['name'])}</b>")
        return
    set_state(chat, st)
    cancel = [("Отмена", "cancel")]
    if not st.get("category"):
        q, kb = "Что это?", [[(CATS[k], f"cat:{k}")] for k in CATS] + [cancel]
    elif not st.get("name"):
        q, kb = "Как называется? Напиши название сообщением.", [cancel]
    elif not st.get("geo_done"):
        q = ("Пришли геопозицию: 📎 → Геопозиция (можно выбрать заведение из списка), "
             "или ссылку из Google Maps, или координаты.")
        kb = [[("Пропустить", "skipgeo")] + cancel]
    else:
        q = "Насколько хочется?\n1★ — если будет время, 5★ — обязательно!"
        kb = [[(f"{n}★", f"prio:{n}") for n in range(1, 6)], cancel]
    render(chat, (note + "\n\n" if note else "") + draft_text(st) + "\n\n" + q, inline(kb), mid)


# ---------- Обработка апдейтов ----------
def allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


def handle_message(msg):
    chat = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if text == "/id":
        api("sendMessage", chat_id=chat, text=f"Твой id: {uid}")
        return
    if not allowed(uid):
        api("sendMessage", chat_id=chat, text=f"Нет доступа. Твой id: {uid}")
        return
    try:
        process_message(chat, uid, msg, text)
    finally:  # сообщения пользователя убираем, чтобы чат не разрастался
        if text or msg.get("location") or msg.get("venue"):
            api("deleteMessage", chat_id=chat, message_id=msg["message_id"])


def process_message(chat, uid, msg, text):
    if text in ("/start", "/help"):
        r = api("sendMessage", chat_id=chat, text="⏳", reply_markup={"remove_keyboard": True})
        if r.get("ok"):
            api("deleteMessage", chat_id=chat, message_id=r["result"]["message_id"])
        old = get_panel(chat)
        if old:
            api("deleteMessage", chat_id=chat, message_id=old)
        set_panel(chat, None)
        clear_state(chat)
        show_list(chat, "all", 0, None, HELP)
        return
    if text in ("/cancel", "/list", "📋 Список"):
        clear_state(chat)
        show_list(chat, "all", 0)
        return
    if text in ("/add", "➕ Добавить"):
        prompt(chat, {"by": uid})
        return

    st = get_state(chat)

    loc = msg.get("location") or (msg.get("venue") or {}).get("location")
    if loc:
        venue = msg.get("venue") or {}
        st = st or {"by": uid}
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
            prompt(chat, st, note="⚠️ Не смог достать координаты. Пришли геопозицию из Telegram, "
                                  "полную ссылку Google Maps или нажми «Пропустить».")
        return
    if st and st.get("category") and not st.get("name"):
        st["name"] = text[:120]
        prompt(chat, st)
        return
    if not st:  # просто текст = название нового места
        prompt(chat, {"by": uid, "name": text[:120]})
        return
    prompt(chat, st, note="Выбери вариант кнопкой.")


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    uid = cb["from"]["id"]
    mid = cb["message"]["message_id"]
    api("answerCallbackQuery", callback_query_id=cb["id"])
    if not allowed(uid):
        return
    set_panel(chat, mid)
    st = get_state(chat)
    p = cb["data"].split(":")
    k = p[0]

    if k == "list":
        show_list(chat, p[1], int(p[2]), mid)
    elif k == "item":
        show_item(chat, int(p[1]), p[2], int(p[3]), mid)
    elif k == "setprio":
        db.execute("UPDATE places SET priority=? WHERE id=?", (int(p[2]), int(p[1])))
        db.commit()
        show_item(chat, int(p[1]), p[3], int(p[4]), mid, "Приоритет обновлён.")
    elif k == "del":
        row = db.execute("SELECT name FROM places WHERE id=?", (int(p[1]),)).fetchone()
        name = html.escape(row[0]) if row else "место"
        render(chat, f"Удалить <b>{name}</b>?",
               inline([[("Да, удалить", f"delok:{p[1]}:{p[2]}:{p[3]}"), ("Нет", f"item:{p[1]}:{p[2]}:{p[3]}")]]), mid)
    elif k == "delok":
        db.execute("DELETE FROM places WHERE id=?", (int(p[1]),))
        db.commit()
        show_list(chat, p[2], int(p[3]), mid, "🗑 Удалено.")
    elif k == "add":
        st = {"by": uid}
        if p[1] in CATS:
            st["category"] = p[1]
        prompt(chat, st, mid)
    elif k == "cat":
        st = st or {"by": uid}
        st["category"] = p[1]
        prompt(chat, st, mid)
    elif k == "skipgeo" and st:
        st["geo_done"] = True
        prompt(chat, st, mid)
    elif k == "prio" and st:
        st["priority"] = int(p[1])
        prompt(chat, st, mid)
    elif k == "cancel":
        clear_state(chat)
        show_list(chat, "all", 0, mid)
    elif k in ("skipgeo", "prio"):  # устаревшая кнопка без активного добавления
        show_list(chat, "all", 0, mid)


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
