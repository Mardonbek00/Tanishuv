import hashlib
import html
import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import requests
import telebot
from flask import Flask, abort, request
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from telebot import types
from telebot.apihelper import ApiTelegramException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tanishuv")

# ------------------------------------------------------------------ config
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")  # Render o'zi beradi
PORT = int(os.getenv("PORT", "10000"))
SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
MIN_AGE = 18
MAX_AGE = 80
MATCH_LOCK = 7001  # advisory lock kaliti

REGIONS = [
    "Toshkent shahri", "Toshkent viloyati", "Andijon", "Buxoro", "Farg'ona",
    "Jizzax", "Namangan", "Navoiy", "Qashqadaryo", "Qoraqalpog'iston",
    "Samarqand", "Sirdaryo", "Surxondaryo", "Xorazm",
]
GENDERS = {"m": "👨 Erkak", "f": "👩 Ayol"}

B_SEARCH = "🔍 Suhbatdosh izlash"
B_TOPUP = "💰 Tanga to'ldirish"
B_PROFILE = "👤 Profil"
B_NEXT = "⏭ Next"
B_STOP = "⛔ Stop"
B_CANCEL = "❌ Bekor qilish"
B_ADMIN = "🛠 Admin panel"

ALL_TYPES = [
    "text", "photo", "video", "audio", "voice", "document", "sticker",
    "animation", "video_note", "location", "contact", "venue", "poll", "dice",
]


def rkb(*rows):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for r in rows:
        kb.row(*[types.KeyboardButton(t) for t in r])
    return kb


def ikb(rows, adjust=None):
    """rows: [[(text, data), ...], ...]"""
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.row(*[types.InlineKeyboardButton(t, callback_data=d) for t, d in r])
    return kb


main_kb = rkb([B_SEARCH], [B_TOPUP, B_PROFILE])
admin_main_kb = rkb([B_SEARCH], [B_TOPUP, B_PROFILE], [B_ADMIN])
chat_kb = rkb([B_NEXT, B_STOP])
cancel_kb = rkb([B_CANCEL])
BACK = [("⬅️ Admin menyu", "a:menu")]

# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY,
    name TEXT, age INT, gender TEXT, region TEXT,
    coins INT NOT NULL DEFAULT 0,
    partner_id BIGINT,
    searching BOOLEAN NOT NULL DEFAULT false,
    queue_at TIMESTAMPTZ,
    banned BOOLEAN NOT NULL DEFAULT false,
    registered BOOLEAN NOT NULL DEFAULT false,
    state TEXT,
    tmp TEXT,
    photo TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tmp TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS photo TEXT;
CREATE TABLE IF NOT EXISTS packages(
    id SERIAL PRIMARY KEY, coins INT NOT NULL, price INT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL, coins INT NOT NULL, price INT NOT NULL,
    file_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
INSERT INTO settings(key, value) VALUES
    ('card_number', '0000 0000 0000 0000'),
    ('card_owner', 'Ism Familiya'),
    ('start_coins', '3')
ON CONFLICT (key) DO NOTHING;
"""

pool = ThreadedConnectionPool(1, 8, DATABASE_URL, sslmode=os.getenv("DB_SSLMODE", "require"))


@contextmanager
def db():
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def q(sql, *a):
    with db() as cur:
        cur.execute(sql, a)
        return cur.fetchall()


def q1(sql, *a):
    with db() as cur:
        cur.execute(sql, a)
        return cur.fetchone()


def qv(sql, *a):
    r = q1(sql, *a)
    return next(iter(r.values())) if r else None


def ex(sql, *a):
    with db() as cur:
        cur.execute(sql, a)
        return cur.rowcount


def init_db():
    with db() as cur:
        cur.execute(SCHEMA)
        cur.execute("SELECT count(*) AS n FROM packages")
        if cur.fetchone()["n"] == 0:
            for coins, price in [(5, 5000), (10, 9000), (25, 20000), (50, 35000)]:
                cur.execute("INSERT INTO packages(coins, price) VALUES(%s, %s)", (coins, price))


def get_user(uid):
    return q1("SELECT * FROM users WHERE id=%s", uid)


def get_setting(key):
    return qv("SELECT value FROM settings WHERE key=%s", key) or ""


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, value)


def set_state(uid, state, tmp=None):
    if tmp is None:
        ex("UPDATE users SET state=%s WHERE id=%s", state, uid)
    else:
        ex("UPDATE users SET state=%s, tmp=%s WHERE id=%s", state, json.dumps(tmp), uid)


def money(n):
    return f"{int(n):,}".replace(",", " ")


def esc(s):
    return html.escape(str(s))


def is_admin(uid):
    return uid in ADMIN_IDS


# ------------------------------------------------------------------ bot
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=False)


def send(uid, text, kb=None):
    if kb is main_kb and is_admin(uid):
        kb = admin_main_kb  # adminlarga menyuda «Admin panel» tugmasi ham chiqadi
    try:
        bot.send_message(uid, text, reply_markup=kb)
        return True
    except ApiTelegramException as e:
        log.warning("send %s: %s", uid, e)
        return False


# ------------------------------------------------------------------ registration
@bot.message_handler(commands=["start"], chat_types=["private"])
def cmd_start(m):
    uid = m.from_user.id
    ex("INSERT INTO users(id) VALUES(%s) ON CONFLICT (id) DO NOTHING", uid)
    u = get_user(uid)
    if u["banned"] and not is_admin(uid):
        return send(uid, "🚫 Siz bloklangansiz.")
    if u["registered"]:
        set_state(uid, None)
        if u["partner_id"]:
            return send(uid, "Siz hozir suhbatdasiz.", chat_kb)
        return send(uid, "Bosh menyu 👇", main_kb)
    set_state(uid, "name")
    bot.send_message(
        uid,
        "👋 Assalomu alaykum! Tanishuv botiga xush kelibsiz.\n\nAvval ro'yxatdan o'tamiz.\n<b>Ismingiz nima?</b>",
        reply_markup=types.ReplyKeyboardRemove(),
    )


def state_name(u, text):
    name = text[:30]
    if len(name) < 2:
        return send(u["id"], "Ism kamida 2 ta belgidan iborat bo'lsin.")
    set_state(u["id"], "age", {"name": name})
    send(u["id"], "<b>Yoshingiz nechada?</b> (raqam bilan)")


def state_age(u, text):
    if not text.isdigit() or not (MIN_AGE <= int(text) <= MAX_AGE):
        return send(u["id"], f"Yoshni {MIN_AGE}–{MAX_AGE} oralig'ida raqam bilan kiriting.")
    tmp = json.loads(u["tmp"] or "{}")
    tmp["age"] = int(text)
    set_state(u["id"], "gender", tmp)
    send(u["id"], "<b>Jinsingiz?</b>", ikb([[(v, f"g:{k}") for k, v in GENDERS.items()]]))


def region_kb(prefix="r"):
    rows, row = [], []
    for i, r in enumerate(REGIONS):
        row.append((r, f"{prefix}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ikb(rows)


# ------------------------------------------------------------------ search / chat
def try_match(uid):
    with db() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MATCH_LOCK,))
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        me = cur.fetchone()
        if not me or me["partner_id"] or not me["searching"] or me["coins"] < 1:
            return None
        cur.execute(
            """SELECT id FROM users WHERE searching AND partner_id IS NULL AND id<>%s
               AND NOT banned AND coins>=1 ORDER BY queue_at LIMIT 1""",
            (uid,),
        )
        p = cur.fetchone()
        if not p:
            return None
        cur.execute(
            """UPDATE users SET partner_id = CASE WHEN id=%s THEN %s ELSE %s END,
               searching=false, coins=coins-1 WHERE id IN (%s,%s)""",
            (uid, p["id"], uid, uid, p["id"]),
        )
        return p["id"]


def end_chat(uid):
    with db() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MATCH_LOCK,))
        cur.execute("SELECT partner_id FROM users WHERE id=%s", (uid,))
        r = cur.fetchone()
        pid = r["partner_id"] if r else None
        if pid:
            cur.execute("UPDATE users SET partner_id=NULL WHERE id IN (%s,%s)", (uid, pid))
        return pid


def partner_card(p):
    return f"👤 {esc(p['name'])}, {p['age']} yosh\n{GENDERS.get(p['gender'], '')} • 📍 {esc(p['region'])}"


def send_card(uid, person, text, kb=None):
    """Rasmi bo'lsa rasm bilan, bo'lmasa oddiy matn. Yetkazilmasa False."""
    if person["photo"]:
        try:
            bot.send_photo(uid, person["photo"], caption=text, reply_markup=kb)
            return True
        except ApiTelegramException as e:
            if e.error_code == 403:
                return False
            log.warning("send_card photo %s: %s", uid, e)
    return send(uid, text, kb)


def announce(uid, pid):
    me, p = get_user(uid), get_user(pid)
    if not send_card(pid, me, f"✅ <b>Suhbatdosh topildi!</b>\n\n{partner_card(me)}\n\n💰 Qolgan tanga: {p['coins']}", chat_kb):
        # sherik botni bloklagan: suhbatni bekor qilamiz, tangani qaytaramiz
        end_chat(uid)
        ex("UPDATE users SET coins=coins+1 WHERE id=%s", uid)
        return send(uid, "⚠️ Suhbatdosh bilan bog'lanib bo'lmadi. Tangangiz qaytarildi, qayta urinib ko'ring.", main_kb)
    send_card(uid, p, f"✅ <b>Suhbatdosh topildi!</b>\n\n{partner_card(p)}\n\n💰 Qolgan tanga: {me['coins']}", chat_kb)


def do_search(u):
    uid = u["id"]
    if u["partner_id"]:
        return send(uid, "Siz allaqachon suhbatdasiz.", chat_kb)
    if u["coins"] < 1:
        return send(
            uid,
            "😔 Tangangiz yetarli emas. Qidirish uchun kamida 1 ta tanga kerak.\n"
            f"Balans: <b>{u['coins']}</b>. «{B_TOPUP}» tugmasini bosing.",
            main_kb,
        )
    ex("UPDATE users SET searching=true, queue_at=now() WHERE id=%s", uid)
    send(uid, "🔎 Suhbatdosh qidirilmoqda...", cancel_kb)
    pid = try_match(uid)
    if pid:
        announce(uid, pid)


def do_cancel(u):
    ex("UPDATE users SET searching=false WHERE id=%s", u["id"])
    send(u["id"], "Bekor qilindi.", main_kb)


def do_stop(u):
    pid = end_chat(u["id"])
    if not pid:
        return send(u["id"], "Siz suhbatda emassiz.", main_kb)
    send(u["id"], "⛔ Suhbat tugatildi.", main_kb)
    send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)


def do_next(u):
    pid = end_chat(u["id"])
    if pid:
        send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)
    do_search(get_user(u["id"]))


def do_profile(u):
    rows = [
        [("✏️ Ism", "e:name"), ("✏️ Yosh", "e:age")],
        [("✏️ Jins", "e:gender"), ("✏️ Viloyat", "e:region")],
        [("📸 Rasm yuklash" if not u["photo"] else "📸 Rasmni almashtirish", "e:photo")],
    ]
    if u["photo"]:
        rows.append([("🗑 Rasmni o'chirish", "e:delphoto")])
    text = (
        f"<b>👤 Profil</b>\n\nIsm: {esc(u['name'])}\nYosh: {u['age']}\n"
        f"Jins: {GENDERS.get(u['gender'], '-')}\nViloyat: {esc(u['region'])}\n\n💰 Tanga: <b>{u['coins']}</b>"
    )
    send_card(u["id"], u, text, ikb(rows)) or send(u["id"], text, ikb(rows))


def state_edit(m, u):
    """Profilni tahrirlash va ro'yxatdan keyingi ixtiyoriy rasm bosqichi."""
    uid, st = u["id"], u["state"]
    text = (m.text or "").strip() if m.content_type == "text" else ""
    if st == "edit:name":
        if len(text) < 2:
            return send(uid, "Ism kamida 2 ta belgidan iborat bo'lsin.")
        ex("UPDATE users SET name=%s, state=NULL WHERE id=%s", text[:30], uid)
    elif st == "edit:age":
        if not text.isdigit() or not (MIN_AGE <= int(text) <= MAX_AGE):
            return send(uid, f"Yoshni {MIN_AGE}–{MAX_AGE} oralig'ida raqam bilan kiriting.")
        ex("UPDATE users SET age=%s, state=NULL WHERE id=%s", int(text), uid)
    else:  # "photo" yoki "edit:photo"
        if m.content_type != "photo":
            return send(uid, "Iltimos, oddiy <b>rasm</b> yuboring (fayl emas).")
        ex("UPDATE users SET photo=%s, state=NULL WHERE id=%s", m.photo[-1].file_id, uid)
        if st == "photo":
            return send(uid, "✅ Rasm saqlandi! Bosh menyu 👇", main_kb)
    send(uid, "✅ Yangilandi.")
    do_profile(get_user(uid))


def relay(m, u):
    try:
        bot.copy_message(u["partner_id"], m.chat.id, m.message_id)
    except ApiTelegramException as e:
        if e.error_code == 403:
            end_chat(u["id"])
            send(u["id"], "⚠️ Suhbatdosh botni tark etgan. Suhbat tugatildi.", main_kb)
        else:
            send(u["id"], "Bu xabarni yuborib bo'lmadi.")


# ------------------------------------------------------------------ top-up
def do_topup(u):
    packs = q("SELECT * FROM packages ORDER BY coins")
    if not packs:
        return send(u["id"], "Hozircha paketlar mavjud emas.", main_kb)
    kb = ikb([[(f"💰 {p['coins']} tanga — {money(p['price'])} so'm", f"pk:{p['id']}")] for p in packs])
    send(u["id"], f"Balans: <b>{u['coins']}</b> tanga\n\nPaketni tanlang:", kb)


def pay_kb(pid):
    return ikb([[("✅ Tasdiqlash", f"py:ok:{pid}"), ("❌ Rad etish", f"py:no:{pid}")]])


def pay_caption(p, u):
    return (
        f"💳 <b>To'lov #{p['id']}</b>\n👤 {esc(u['name'] if u and u['name'] else '?')} (<code>{p['user_id']}</code>)\n"
        f"📦 {p['coins']} tanga — {money(p['price'])} so'm"
    )


def send_payment_to(aid, p, u):
    try:
        bot.send_photo(aid, p["file_id"], caption=pay_caption(p, u), reply_markup=pay_kb(p["id"]))
    except ApiTelegramException:
        bot.send_document(aid, p["file_id"], caption=pay_caption(p, u), reply_markup=pay_kb(p["id"]))


def state_pay(m, u):
    uid = u["id"]
    if m.content_type not in ("photo", "document"):
        return send(uid, "Iltimos, chek skrinshotini <b>rasm</b> qilib yuboring yoki «❌ Bekor qilish» ni bosing.")
    pack = q1("SELECT * FROM packages WHERE id=%s", int(u["state"].split(":")[1]))
    if not pack:
        set_state(uid, None)
        return send(uid, "Paket topilmadi, qaytadan urinib ko'ring.", main_kb)
    file_id = m.photo[-1].file_id if m.content_type == "photo" else m.document.file_id
    row = q1(
        "INSERT INTO payments(user_id, coins, price, file_id) VALUES(%s,%s,%s,%s) RETURNING *",
        uid, pack["coins"], pack["price"], file_id,
    )
    set_state(uid, None)
    send(uid, "✅ So'rovingiz adminga yuborildi. Tasdiqlangach tangalar hisobingizga tushadi.", main_kb)
    for aid in ADMIN_IDS:
        try:
            send_payment_to(aid, row, u)
        except Exception as e:  # noqa: BLE001
            log.warning("admin %s ga yuborib bo'lmadi: %s", aid, e)


# ------------------------------------------------------------------ admin panel
def admin_menu_kb():
    return ikb([
        [("📊 Statistika", "a:stats")],
        [("💳 Kutilayotgan to'lovlar", "a:pays")],
        [("📦 Tanga paketlari", "a:packs")],
        [("💳 Karta raqami", "a:card")],
        [("🎁 Tanga berish/ayirish", "a:give"), ("🚫 Ban / Unban", "a:ban")],
        [("📢 Xabar yuborish", "a:bc")],
        [("🎁 Boshlang'ich tanga", "a:start")],
    ])


def open_admin(uid):
    ex("INSERT INTO users(id) VALUES(%s) ON CONFLICT (id) DO NOTHING", uid)
    set_state(uid, None)
    send(uid, "🛠 <b>Admin panel</b>", admin_menu_kb())


@bot.message_handler(commands=["admin"], chat_types=["private"])
def cmd_admin(m):
    uid = m.from_user.id
    if not is_admin(uid):
        return send(
            uid,
            f"⛔ Siz admin emassiz.\nSizning Telegram ID: <code>{uid}</code>\n\n"
            "Admin bo'lish uchun shu raqamni Render → Environment → <code>ADMIN_IDS</code> ga yozing.",
        )
    open_admin(uid)


@bot.message_handler(commands=["id"], chat_types=["private"])
def cmd_id(m):
    send(m.from_user.id, f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


def state_admin(m, u):
    uid, st = u["id"], u["state"]
    if not is_admin(uid):
        return set_state(uid, None)
    text = (m.text or "").strip()

    if st == "adm:bc":
        set_state(uid, None)
        threading.Thread(target=broadcast, args=(m.chat.id, m.message_id, uid), daemon=True).start()
        return send(uid, "📢 Yuborish boshlandi, tugagach xabar beraman.")

    if not text:
        return send(uid, "Matn yuboring.")

    if st == "adm:pack":
        try:
            coins, price = (int(x) for x in text.split())
            assert coins > 0 and price > 0
        except Exception:  # noqa: BLE001
            return send(uid, "Format xato. Masalan: <code>10 9000</code>")
        ex("INSERT INTO packages(coins, price) VALUES(%s,%s)", coins, price)
        set_state(uid, None)
        return send(uid, f"✅ Qo'shildi: {coins} tanga — {money(price)} so'm", ikb([BACK]))

    if st == "adm:card":
        parts = text.split(maxsplit=1)
        set_setting("card_number", parts[0])
        set_setting("card_owner", parts[1] if len(parts) > 1 else "")
        set_state(uid, None)
        return send(uid, f"✅ Karta yangilandi:\n<code>{esc(parts[0])}</code> {esc(parts[1] if len(parts) > 1 else '')}", ikb([BACK]))

    if st == "adm:give":
        try:
            tid, amount = (int(x) for x in text.split())
        except Exception:  # noqa: BLE001
            return send(uid, "Format xato. Masalan: <code>123456789 10</code>")
        t = get_user(tid)
        if not t:
            return send(uid, "Bunday foydalanuvchi topilmadi.")
        ex("UPDATE users SET coins=GREATEST(coins+%s,0) WHERE id=%s", amount, tid)
        set_state(uid, None)
        if amount > 0:
            send(tid, f"🎁 Hisobingizga <b>{amount}</b> tanga qo'shildi.")
        return send(uid, f"✅ Bajarildi. {esc(t['name'])} balansi: {qv('SELECT coins FROM users WHERE id=%s', tid)}", ikb([BACK]))

    if st == "adm:ban":
        if not text.isdigit():
            return send(uid, "Faqat raqamli user_id yuboring.")
        tid = int(text)
        t = get_user(tid)
        if not t:
            return send(uid, "Bunday foydalanuvchi topilmadi.")
        banned = not t["banned"]
        ex("UPDATE users SET banned=%s, searching=false WHERE id=%s", banned, tid)
        if banned:
            pid = end_chat(tid)
            if pid:
                send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)
        set_state(uid, None)
        return send(uid, ("🚫 Banlandi" if banned else "✅ Ban olib tashlandi") + f": {esc(t['name'])} ({tid})", ikb([BACK]))

    if st == "adm:start":
        if not text.isdigit():
            return send(uid, "Raqam yuboring.")
        set_setting("start_coins", text)
        set_state(uid, None)
        return send(uid, f"✅ Boshlang'ich tanga: {text}", ikb([BACK]))


def broadcast(chat_id, message_id, admin_id):
    ids = [r["id"] for r in q("SELECT id FROM users WHERE registered AND NOT banned")]
    ok = 0
    for tid in ids:
        try:
            bot.copy_message(tid, chat_id, message_id)
            ok += 1
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)
    send(admin_id, f"✅ Yuborildi: {ok}/{len(ids)}", ikb([BACK]))


def admin_action(c, act):
    uid = c.from_user.id
    if act == "menu":
        return send(uid, "🛠 <b>Admin panel</b>", admin_menu_kb())

    if act == "stats":
        r = q1(
            """SELECT count(*) AS total, count(*) FILTER (WHERE registered) AS reg,
               count(*) FILTER (WHERE searching) AS srch, count(*) FILTER (WHERE partner_id IS NOT NULL) AS chat,
               count(*) FILTER (WHERE banned) AS banned, coalesce(sum(coins),0) AS coins FROM users"""
        )
        pend = qv("SELECT count(*) FROM payments WHERE status='pending'")
        s = q1("SELECT count(*) AS n, coalesce(sum(price),0) AS s FROM payments WHERE status='approved'")
        return send(
            uid,
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {r['total']} (ro'yxatdan o'tgan: {r['reg']})\n"
            f"🔎 Qidiruvda: {r['srch']}\n💬 Suhbatda: {r['chat']} kishi\n🚫 Banlangan: {r['banned']}\n"
            f"🪙 Jami tangalar (foydalanuvchilarda): {r['coins']}\n\n"
            f"⏳ Kutilayotgan to'lovlar: {pend}\n✅ Tasdiqlangan: {s['n']} ta, {money(s['s'])} so'm",
            ikb([BACK]),
        )

    if act == "pays":
        rows = q("SELECT * FROM payments WHERE status='pending' ORDER BY id LIMIT 10")
        if not rows:
            return send(uid, "Kutilayotgan to'lovlar yo'q.", ikb([BACK]))
        for p in rows:
            send_payment_to(uid, p, get_user(p["user_id"]))
        return send(uid, "⬆️ Oxirgi 10 ta kutilayotgan to'lov", ikb([BACK]))

    if act == "packs":
        packs = q("SELECT * FROM packages ORDER BY coins")
        rows = [[(f"🗑 {p['coins']} tanga — {money(p['price'])} so'm", f"pd:{p['id']}")] for p in packs]
        rows += [[("➕ Paket qo'shish", "a:addpack")], BACK]
        return send(uid, "📦 <b>Paketlar</b> (o'chirish uchun bosing):", ikb(rows))

    prompts = {
        "addpack": ("adm:pack", "Yangi paket: <code>tanga narx</code> ko'rinishida yuboring.\nMasalan: <code>10 9000</code>"),
        "card": ("adm:card", "Yangi karta: <code>karta_raqami Ism Familiya</code>\nMasalan: <code>8600123412341234 Ali Valiyev</code>"),
        "give": ("adm:give", "<code>user_id tanga</code> yuboring. Ayirish uchun minus.\nMasalan: <code>123456789 10</code> yoki <code>123456789 -5</code>"),
        "ban": ("adm:ban", "Ban/Unban qilinadigan <code>user_id</code> ni yuboring (holati almashadi)."),
        "bc": ("adm:bc", "Hammaga yuboriladigan xabarni yuboring (matn, rasm, video — istalgani)."),
        "start": ("adm:start", f"Yangi foydalanuvchiga beriladigan tanga soni (hozir: {get_setting('start_coins')}). Raqam yuboring."),
    }
    if act in prompts:
        st, text = prompts[act]
        set_state(uid, st)
        return send(uid, text)


# ------------------------------------------------------------------ callbacks
@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    uid = c.from_user.id
    data = c.data or ""
    answer = None
    try:
        if is_admin(uid):
            ex("INSERT INTO users(id) VALUES(%s) ON CONFLICT (id) DO NOTHING", uid)
        u = get_user(uid)
        if not u or (u["banned"] and not is_admin(uid)):
            answer = ("Ruxsat yo'q", True)

        elif data.startswith("g:") and u["state"] == "gender":
            tmp = json.loads(u["tmp"] or "{}")
            tmp["gender"] = data[2:]
            set_state(uid, "region", tmp)
            bot.edit_message_text("<b>Viloyatingizni tanlang:</b>", uid, c.message.message_id, reply_markup=region_kb())

        elif data.startswith("r:") and u["state"] == "region":
            tmp = json.loads(u["tmp"] or "{}")
            start_coins = int(get_setting("start_coins") or 0)
            ex(
                """UPDATE users SET name=%s, age=%s, gender=%s, region=%s, coins=coins+%s,
                   registered=true, state=NULL, tmp=NULL WHERE id=%s AND NOT registered""",
                tmp.get("name"), tmp.get("age"), tmp.get("gender"), REGIONS[int(data[2:])], start_coins, uid,
            )
            try:
                bot.delete_message(uid, c.message.message_id)
            except ApiTelegramException:
                pass
            send(
                uid,
                f"✅ Ro'yxatdan o'tdingiz!\n🎁 Sizga <b>{start_coins}</b> ta tanga sovg'a qilindi." if start_coins else "✅ Ro'yxatdan o'tdingiz!",
                main_kb,
            )

        elif data.startswith("pk:") and u["registered"]:
            p = q1("SELECT * FROM packages WHERE id=%s", int(data[3:]))
            if not p:
                answer = ("Paket topilmadi", True)
            else:
                set_state(uid, f"pay:{p['id']}")
                send(
                    uid,
                    f"💳 <b>To'lov ma'lumotlari</b>\n\nPaket: <b>{p['coins']} tanga</b>\n"
                    f"Summa: <b>{money(p['price'])} so'm</b>\n\nKarta: <code>{esc(get_setting('card_number'))}</code>\n"
                    f"Egasi: {esc(get_setting('card_owner'))}\n\n"
                    "To'lovni amalga oshirib, <b>chek skrinshotini</b> shu yerga rasm qilib yuboring.",
                    cancel_kb,
                )

        elif data.startswith("py:"):
            if not is_admin(uid):
                answer = ("Ruxsat yo'q", True)
            else:
                _, act, pid = data.split(":")
                ok = act == "ok"
                with db() as cur:
                    cur.execute(
                        "UPDATE payments SET status=%s WHERE id=%s AND status='pending' RETURNING *",
                        ("approved" if ok else "rejected", int(pid)),
                    )
                    row = cur.fetchone()
                    if row and ok:
                        cur.execute("UPDATE users SET coins=coins+%s WHERE id=%s", (row["coins"], row["user_id"]))
                if not row:
                    answer = ("Bu to'lov allaqachon ko'rib chiqilgan", True)
                else:
                    if ok:
                        send(row["user_id"], f"✅ To'lovingiz tasdiqlandi! Hisobingizga <b>{row['coins']}</b> tanga qo'shildi.")
                    else:
                        send(row["user_id"], "❌ To'lovingiz rad etildi. Muammo bo'lsa admin bilan bog'laning.")
                    try:
                        bot.edit_message_caption(
                            (getattr(c.message, "html_caption", None) or "") + ("\n\n✅ TASDIQLANDI" if ok else "\n\n❌ RAD ETILDI"),
                            uid, c.message.message_id, reply_markup=None,
                        )
                    except ApiTelegramException:
                        pass
                    answer = ("Bajarildi", False)

        elif data.startswith("pd:"):
            if not is_admin(uid):
                answer = ("Ruxsat yo'q", True)
            else:
                ex("DELETE FROM packages WHERE id=%s", int(data[3:]))
                bot.edit_message_text(
                    "🗑 Paket o'chirildi.", uid, c.message.message_id,
                    reply_markup=ikb([[("📦 Paketlar", "a:packs")], BACK]),
                )

        elif data.startswith("a:"):
            if not is_admin(uid):
                answer = ("Ruxsat yo'q", True)
            else:
                set_state(uid, None)
                admin_action(c, data[2:])
    except Exception:  # noqa: BLE001
        log.exception("callback xatosi: %s", data)
    try:
        bot.answer_callback_query(c.id, text=answer[0] if answer else None, show_alert=bool(answer and answer[1]))
    except ApiTelegramException:
        pass


# ------------------------------------------------------------------ asosiy router
@bot.message_handler(func=lambda m: True, content_types=ALL_TYPES, chat_types=["private"])
def route(m):
    uid = m.from_user.id
    u = get_user(uid)
    if not u:
        return send(uid, "Boshlash uchun /start bosing.")
    if u["banned"] and not is_admin(uid):
        return send(uid, "🚫 Siz bloklangansiz.")
    text = (m.text or "").strip()
    st = u["state"]

    if u["registered"]:
        buttons = {
            B_CANCEL: do_cancel, B_STOP: do_stop, B_NEXT: do_next,
            B_SEARCH: do_search, B_TOPUP: do_topup, B_PROFILE: do_profile,
        }
        if text in buttons:
            if st:
                set_state(uid, None)
            return buttons[text](u)

    if st and st.startswith("adm:") and is_admin(uid):
        return state_admin(m, u)

    if st == "name" and not u["registered"]:
        return state_name(u, text)
    if st == "age" and not u["registered"]:
        return state_age(u, text)
    if st in ("gender", "region") and not u["registered"]:
        return send(uid, "Iltimos, yuqoridagi tugmalardan birini tanlang.")
    if not u["registered"]:
        return send(uid, "Boshlash uchun /start bosing.")

    if st and st.startswith("pay:"):
        return state_pay(m, u)
    if st and st.startswith("adm:"):
        return state_admin(m, u)

    if u["partner_id"]:
        return relay(m, u)
    if u["searching"]:
        return send(uid, "🔎 Suhbatdosh qidirilmoqda... Kuting yoki bekor qiling.", cancel_kb)
    send(uid, "Bosh menyu 👇", main_kb)


# ------------------------------------------------------------------ web server / ishga tushirish
app = Flask(__name__)


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception:  # noqa: BLE001
        log.exception("update ishlov berishda xato")
    return "", 200


def keep_alive():
    """Render bepul rejasida uyquga ketmaslik uchun o'z URL'ini har 9 daqiqada chaqiradi."""
    time.sleep(60)
    while True:
        try:
            r = requests.get(f"{BASE_URL}/", timeout=20)
            log.info("keep-alive: %s", r.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("keep-alive xato: %s", e)
        time.sleep(540)


init_db()
if BASE_URL:
    bot.remove_webhook()
    bot.set_webhook(url=f"{BASE_URL}/webhook", secret_token=SECRET, drop_pending_updates=True)
    log.info("Webhook: %s/webhook", BASE_URL)
    threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == "__main__":
    if BASE_URL:  # `python main.py` bilan Render'da ishga tushirilsa ham ishlaydi
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    else:  # lokal: polling
        bot.remove_webhook()
        bot.infinity_polling(skip_pending=True)
