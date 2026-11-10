import asyncio
import hashlib
import html
import logging
import os

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tanishuv")

# ------------------------------------------------------------------ config
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")  # Render o'zi beradi
PORT = int(os.getenv("PORT", "10000"))
SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
MIN_AGE = 18
MAX_AGE = 80

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

main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_SEARCH)], [KeyboardButton(text=B_TOPUP), KeyboardButton(text=B_PROFILE)]],
    resize_keyboard=True,
)
chat_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_NEXT), KeyboardButton(text=B_STOP)]], resize_keyboard=True)
cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CANCEL)]], resize_keyboard=True)

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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

pool: asyncpg.Pool = None  # type: ignore


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        if await c.fetchval("SELECT count(*) FROM packages") == 0:
            for coins, price in [(5, 5000), (10, 9000), (25, 20000), (50, 35000)]:
                await c.execute("INSERT INTO packages(coins, price) VALUES($1, $2)", coins, price)


async def q(sql, *a):
    async with pool.acquire() as c:
        return await c.fetch(sql, *a)


async def q1(sql, *a):
    async with pool.acquire() as c:
        return await c.fetchrow(sql, *a)


async def qv(sql, *a):
    async with pool.acquire() as c:
        return await c.fetchval(sql, *a)


async def ex(sql, *a):
    async with pool.acquire() as c:
        return await c.execute(sql, *a)


async def get_user(uid: int):
    return await q1("SELECT * FROM users WHERE id=$1", uid)


async def get_setting(key: str) -> str:
    return await qv("SELECT value FROM settings WHERE key=$1", key) or ""


async def set_setting(key: str, value: str):
    await ex(
        "INSERT INTO settings(key, value) VALUES($1, $2) ON CONFLICT (key) DO UPDATE SET value=$2", key, value
    )


def money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def esc(s) -> str:
    return html.escape(str(s))


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ------------------------------------------------------------------ bot
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
search_lock = asyncio.Lock()


class Reg(StatesGroup):
    name = State()
    age = State()
    gender = State()
    region = State()


class Pay(StatesGroup):
    screenshot = State()


class Adm(StatesGroup):
    card = State()
    give = State()
    ban = State()
    broadcast = State()
    pack = State()
    start_coins = State()


async def safe_send(uid: int, text: str, kb=None):
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


@dp.message.outer_middleware()
async def ban_guard(handler, event: Message, data):
    if event.from_user and event.chat.type == "private" and not is_admin(event.from_user.id):
        u = await get_user(event.from_user.id)
        if u and u["banned"]:
            await event.answer("🚫 Siz bloklangansiz.")
            return
    return await handler(event, data)


# ------------------------------------------------------------------ registration
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = await get_user(m.from_user.id)
    if u and u["registered"]:
        if u["partner_id"]:
            return await m.answer("Siz hozir suhbatdasiz.", reply_markup=chat_kb)
        return await m.answer("Bosh menyu 👇", reply_markup=main_kb)
    await m.answer(
        "👋 Assalomu alaykum! Tanishuv botiga xush kelibsiz.\n\nAvval ro'yxatdan o'tamiz.\n<b>Ismingiz nima?</b>",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Reg.name)


@dp.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = m.text.strip()[:30]
    if len(name) < 2:
        return await m.answer("Ism kamida 2 ta belgidan iborat bo'lsin.")
    await state.update_data(name=name)
    await state.set_state(Reg.age)
    await m.answer("<b>Yoshingiz nechada?</b> (raqam bilan)")


@dp.message(Reg.age, F.text)
async def reg_age(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not (MIN_AGE <= int(t) <= MAX_AGE):
        return await m.answer(f"Yoshni {MIN_AGE}–{MAX_AGE} oralig'ida raqam bilan kiriting.")
    await state.update_data(age=int(t))
    await state.set_state(Reg.gender)
    kb = InlineKeyboardBuilder()
    for k, v in GENDERS.items():
        kb.button(text=v, callback_data=f"g:{k}")
    await m.answer("<b>Jinsingiz?</b>", reply_markup=kb.as_markup())


@dp.callback_query(Reg.gender, F.data.startswith("g:"))
async def reg_gender(c: CallbackQuery, state: FSMContext):
    await state.update_data(gender=c.data[2:])
    await state.set_state(Reg.region)
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(REGIONS):
        kb.button(text=r, callback_data=f"r:{i}")
    kb.adjust(2)
    await c.message.edit_text("<b>Viloyatingizni tanlang:</b>", reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(Reg.region, F.data.startswith("r:"))
async def reg_region(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    region = REGIONS[int(c.data[2:])]
    start_coins = int(await get_setting("start_coins") or 0)
    await ex(
        """INSERT INTO users(id, name, age, gender, region, coins, registered)
           VALUES($1,$2,$3,$4,$5,$6,true)
           ON CONFLICT (id) DO UPDATE SET name=$2, age=$3, gender=$4, region=$5, registered=true""",
        c.from_user.id, d["name"], d["age"], d["gender"], region, start_coins,
    )
    await state.clear()
    await c.message.delete()
    await c.message.answer(
        f"✅ Ro'yxatdan o'tdingiz!\n🎁 Sizga <b>{start_coins}</b> ta tanga sovg'a qilindi."
        if start_coins
        else "✅ Ro'yxatdan o'tdingiz!",
        reply_markup=main_kb,
    )
    await c.answer()


# ------------------------------------------------------------------ search / chat
async def try_match(uid: int):
    async with search_lock:
        async with pool.acquire() as c:
            async with c.transaction():
                me = await c.fetchrow("SELECT * FROM users WHERE id=$1", uid)
                if not me or me["partner_id"] or not me["searching"] or me["coins"] < 1:
                    return None
                p = await c.fetchrow(
                    """SELECT * FROM users WHERE searching AND partner_id IS NULL AND id<>$1
                       AND NOT banned AND coins>=1 ORDER BY queue_at LIMIT 1""",
                    uid,
                )
                if not p:
                    return None
                await c.execute(
                    """UPDATE users SET partner_id = CASE WHEN id=$1 THEN $2 ELSE $1 END,
                       searching=false, coins=coins-1 WHERE id IN ($1,$2)""",
                    uid, p["id"],
                )
                return p


async def end_chat(uid: int):
    async with pool.acquire() as c:
        async with c.transaction():
            r = await c.fetchrow("SELECT partner_id FROM users WHERE id=$1 FOR UPDATE", uid)
            pid = r["partner_id"] if r else None
            if pid:
                await c.execute("UPDATE users SET partner_id=NULL WHERE id IN ($1,$2)", uid, pid)
            return pid


def partner_card(p) -> str:
    return (
        f"👤 {esc(p['name'])}, {p['age']} yosh\n"
        f"{GENDERS.get(p['gender'], '')} • 📍 {esc(p['region'])}"
    )


async def begin_search(m: Message):
    uid = m.from_user.id
    u = await get_user(uid)
    if not u or not u["registered"]:
        return await m.answer("Avval /start bosib ro'yxatdan o'ting.")
    if u["partner_id"]:
        return await m.answer("Siz allaqachon suhbatdasiz.", reply_markup=chat_kb)
    if u["coins"] < 1:
        return await m.answer(
            "😔 Tangangiz yetarli emas. Qidirish uchun kamida 1 ta tanga kerak.\n"
            f"Balans: <b>{u['coins']}</b>. «{B_TOPUP}» tugmasini bosing.",
            reply_markup=main_kb,
        )
    await ex("UPDATE users SET searching=true, queue_at=now() WHERE id=$1", uid)
    await m.answer("🔎 Suhbatdosh qidirilmoqda...", reply_markup=cancel_kb)
    p = await try_match(uid)
    if not p:
        return
    me = await get_user(uid)
    p = await get_user(p["id"])
    await m.answer(
        f"✅ <b>Suhbatdosh topildi!</b>\n\n{partner_card(p)}\n\n💰 Qolgan tanga: {me['coins']}",
        reply_markup=chat_kb,
    )
    await safe_send(
        p["id"],
        f"✅ <b>Suhbatdosh topildi!</b>\n\n{partner_card(me)}\n\n💰 Qolgan tanga: {p['coins']}",
        chat_kb,
    )


@dp.message(F.text == B_SEARCH)
async def search_btn(m: Message, state: FSMContext):
    await state.clear()
    await begin_search(m)


@dp.message(F.text == B_CANCEL)
async def cancel_btn(m: Message, state: FSMContext):
    await state.clear()
    await ex("UPDATE users SET searching=false WHERE id=$1", m.from_user.id)
    await m.answer("Bekor qilindi.", reply_markup=main_kb)


@dp.message(F.text == B_STOP)
async def stop_btn(m: Message, state: FSMContext):
    await state.clear()
    pid = await end_chat(m.from_user.id)
    if not pid:
        return await m.answer("Siz suhbatda emassiz.", reply_markup=main_kb)
    await m.answer("⛔ Suhbat tugatildi.", reply_markup=main_kb)
    await safe_send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)


@dp.message(F.text == B_NEXT)
async def next_btn(m: Message, state: FSMContext):
    await state.clear()
    pid = await end_chat(m.from_user.id)
    if pid:
        await safe_send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)
    await begin_search(m)


@dp.message(F.text == B_PROFILE)
async def profile_btn(m: Message, state: FSMContext):
    await state.clear()
    u = await get_user(m.from_user.id)
    if not u or not u["registered"]:
        return await m.answer("Avval /start bosing.")
    await m.answer(
        f"<b>👤 Profil</b>\n\nIsm: {esc(u['name'])}\nYosh: {u['age']}\n"
        f"Jins: {GENDERS.get(u['gender'], '-')}\nViloyat: {esc(u['region'])}\n\n💰 Tanga: <b>{u['coins']}</b>",
        reply_markup=main_kb,
    )


# ------------------------------------------------------------------ top-up
@dp.message(F.text == B_TOPUP)
async def topup_btn(m: Message, state: FSMContext):
    await state.clear()
    u = await get_user(m.from_user.id)
    if not u or not u["registered"]:
        return await m.answer("Avval /start bosing.")
    packs = await q("SELECT * FROM packages ORDER BY coins")
    if not packs:
        return await m.answer("Hozircha paketlar mavjud emas.")
    kb = InlineKeyboardBuilder()
    for p in packs:
        kb.button(text=f"💰 {p['coins']} tanga — {money(p['price'])} so'm", callback_data=f"pk:{p['id']}")
    kb.adjust(1)
    await m.answer(f"Balans: <b>{u['coins']}</b> tanga\n\nPaketni tanlang:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("pk:"))
async def pick_pack(c: CallbackQuery, state: FSMContext):
    p = await q1("SELECT * FROM packages WHERE id=$1", int(c.data[3:]))
    if not p:
        return await c.answer("Paket topilmadi", show_alert=True)
    card, owner = await get_setting("card_number"), await get_setting("card_owner")
    await state.set_state(Pay.screenshot)
    await state.update_data(pack_id=p["id"])
    await c.message.answer(
        f"💳 <b>To'lov ma'lumotlari</b>\n\nPaket: <b>{p['coins']} tanga</b>\n"
        f"Summa: <b>{money(p['price'])} so'm</b>\n\nKarta: <code>{esc(card)}</code>\nEgasi: {esc(owner)}\n\n"
        "To'lovni amalga oshirib, <b>chek skrinshotini</b> shu yerga rasm qilib yuboring.",
        reply_markup=cancel_kb,
    )
    await c.answer()


def pay_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"py:ok:{pid}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"py:no:{pid}"),
            ]
        ]
    )


def pay_caption(p, u) -> str:
    return (
        f"💳 <b>To'lov #{p['id']}</b>\n👤 {esc(u['name'] if u else '?')} (<code>{p['user_id']}</code>)\n"
        f"📦 {p['coins']} tanga — {money(p['price'])} so'm"
    )


@dp.message(Pay.screenshot, F.photo | F.document)
async def pay_screenshot(m: Message, state: FSMContext):
    d = await state.get_data()
    pack = await q1("SELECT * FROM packages WHERE id=$1", d.get("pack_id"))
    if not pack:
        await state.clear()
        return await m.answer("Paket topilmadi, qaytadan urinib ko'ring.", reply_markup=main_kb)
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    row = await q1(
        "INSERT INTO payments(user_id, coins, price, file_id) VALUES($1,$2,$3,$4) RETURNING *",
        m.from_user.id, pack["coins"], pack["price"], file_id,
    )
    await state.clear()
    await m.answer("✅ So'rovingiz adminga yuborildi. Tasdiqlangach tangalar hisobingizga tushadi.", reply_markup=main_kb)
    u = await get_user(m.from_user.id)
    for aid in ADMIN_IDS:
        try:
            if m.photo:
                await bot.send_photo(aid, file_id, caption=pay_caption(row, u), reply_markup=pay_kb(row["id"]))
            else:
                await bot.send_document(aid, file_id, caption=pay_caption(row, u), reply_markup=pay_kb(row["id"]))
        except Exception as e:  # noqa: BLE001
            log.warning("admin %s ga yuborib bo'lmadi: %s", aid, e)


@dp.message(Pay.screenshot)
async def pay_wrong(m: Message):
    await m.answer("Iltimos, chek skrinshotini <b>rasm</b> qilib yuboring yoki bekor qiling.")


@dp.callback_query(F.data.startswith("py:"))
async def pay_decide(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    _, act, pid = c.data.split(":")
    ok = act == "ok"
    row = await q1(
        "UPDATE payments SET status=$2 WHERE id=$1 AND status='pending' RETURNING *",
        int(pid), "approved" if ok else "rejected",
    )
    if not row:
        return await c.answer("Bu to'lov allaqachon ko'rib chiqilgan", show_alert=True)
    if ok:
        await ex("UPDATE users SET coins=coins+$2 WHERE id=$1", row["user_id"], row["coins"])
        await safe_send(row["user_id"], f"✅ To'lovingiz tasdiqlandi! Hisobingizga <b>{row['coins']}</b> tanga qo'shildi.")
    else:
        await safe_send(row["user_id"], "❌ To'lovingiz rad etildi. Muammo bo'lsa admin bilan bog'laning.")
    try:
        await c.message.edit_caption(
            caption=(c.message.caption or "") + ("\n\n✅ TASDIQLANDI" if ok else "\n\n❌ RAD ETILDI"),
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass
    await c.answer("Bajarildi")


# ------------------------------------------------------------------ admin panel
def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in [
        ("📊 Statistika", "a:stats"),
        ("💳 Kutilayotgan to'lovlar", "a:pays"),
        ("📦 Tanga paketlari", "a:packs"),
        ("💳 Karta raqami", "a:card"),
        ("🎁 Tanga berish/ayirish", "a:give"),
        ("🚫 Ban / Unban", "a:ban"),
        ("📢 Xabar yuborish", "a:bc"),
        ("🎁 Boshlang'ich tanga", "a:start"),
    ]:
        kb.button(text=text, callback_data=data)
    kb.adjust(1, 1, 1, 1, 2, 1, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Admin menyu", callback_data="a:menu")]])


@dp.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


@dp.callback_query(F.data.startswith("a:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    act = c.data[2:]
    await c.answer()
    await state.clear()

    if act == "menu":
        return await c.message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())

    if act == "stats":
        r = await q1(
            """SELECT count(*) total, count(*) FILTER (WHERE registered) reg,
               count(*) FILTER (WHERE searching) srch, count(*) FILTER (WHERE partner_id IS NOT NULL) chat,
               count(*) FILTER (WHERE banned) banned, coalesce(sum(coins),0) coins FROM users"""
        )
        pend = await qv("SELECT count(*) FROM payments WHERE status='pending'")
        s = await q1("SELECT count(*) n, coalesce(sum(price),0) s FROM payments WHERE status='approved'")
        return await c.message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {r['total']} (ro'yxatdan o'tgan: {r['reg']})\n"
            f"🔎 Qidiruvda: {r['srch']}\n💬 Suhbatda: {r['chat']} kishi\n🚫 Banlangan: {r['banned']}\n"
            f"🪙 Jami tangalar (foydalanuvchilarda): {r['coins']}\n\n"
            f"⏳ Kutilayotgan to'lovlar: {pend}\n✅ Tasdiqlangan: {s['n']} ta, {money(s['s'])} so'm",
            reply_markup=back_kb(),
        )

    if act == "pays":
        rows = await q("SELECT * FROM payments WHERE status='pending' ORDER BY id LIMIT 10")
        if not rows:
            return await c.message.answer("Kutilayotgan to'lovlar yo'q.", reply_markup=back_kb())
        for p in rows:
            u = await get_user(p["user_id"])
            try:
                await bot.send_photo(c.from_user.id, p["file_id"], caption=pay_caption(p, u), reply_markup=pay_kb(p["id"]))
            except TelegramBadRequest:
                await bot.send_document(c.from_user.id, p["file_id"], caption=pay_caption(p, u), reply_markup=pay_kb(p["id"]))
        return await c.message.answer("⬆️ Oxirgi 10 ta kutilayotgan to'lov", reply_markup=back_kb())

    if act == "packs":
        packs = await q("SELECT * FROM packages ORDER BY coins")
        kb = InlineKeyboardBuilder()
        for p in packs:
            kb.button(text=f"🗑 {p['coins']} tanga — {money(p['price'])} so'm", callback_data=f"pd:{p['id']}")
        kb.button(text="➕ Paket qo'shish", callback_data="a:addpack")
        kb.button(text="⬅️ Admin menyu", callback_data="a:menu")
        kb.adjust(1)
        return await c.message.answer("📦 <b>Paketlar</b> (o'chirish uchun bosing):", reply_markup=kb.as_markup())

    if act == "addpack":
        await state.set_state(Adm.pack)
        return await c.message.answer("Yangi paket: <code>tanga narx</code> ko'rinishida yuboring.\nMasalan: <code>10 9000</code>")

    if act == "card":
        await state.set_state(Adm.card)
        return await c.message.answer(
            "Yangi karta: <code>karta_raqami Ism Familiya</code>\nMasalan: <code>8600123412341234 Ali Valiyev</code>"
        )

    if act == "give":
        await state.set_state(Adm.give)
        return await c.message.answer(
            "<code>user_id tanga</code> yuboring. Ayirish uchun minus.\nMasalan: <code>123456789 10</code> yoki <code>123456789 -5</code>"
        )

    if act == "ban":
        await state.set_state(Adm.ban)
        return await c.message.answer("Ban/Unban qilinadigan <code>user_id</code> ni yuboring (holati almashadi).")

    if act == "bc":
        await state.set_state(Adm.broadcast)
        return await c.message.answer("Hammaga yuboriladigan xabarni yuboring (matn, rasm, video — istalgani).")

    if act == "start":
        cur = await get_setting("start_coins")
        await state.set_state(Adm.start_coins)
        return await c.message.answer(f"Yangi foydalanuvchiga beriladigan tanga soni (hozir: {cur}). Raqam yuboring.")


@dp.callback_query(F.data.startswith("pd:"))
async def pack_delete(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await ex("DELETE FROM packages WHERE id=$1", int(c.data[3:]))
    await c.answer("O'chirildi")
    await c.message.edit_text("🗑 Paket o'chirildi.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Paketlar", callback_data="a:packs")],
        [InlineKeyboardButton(text="⬅️ Admin menyu", callback_data="a:menu")],
    ]))


@dp.message(Adm.pack, F.text)
async def adm_pack(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        coins, price = (int(x) for x in m.text.split())
        assert coins > 0 and price > 0
    except Exception:  # noqa: BLE001
        return await m.answer("Format xato. Masalan: <code>10 9000</code>")
    await ex("INSERT INTO packages(coins, price) VALUES($1,$2)", coins, price)
    await state.clear()
    await m.answer(f"✅ Qo'shildi: {coins} tanga — {money(price)} so'm", reply_markup=back_kb())


@dp.message(Adm.card, F.text)
async def adm_card(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    parts = m.text.strip().split(maxsplit=1)
    number = parts[0]
    owner = parts[1] if len(parts) > 1 else ""
    await set_setting("card_number", number)
    await set_setting("card_owner", owner)
    await state.clear()
    await m.answer(f"✅ Karta yangilandi:\n<code>{esc(number)}</code> {esc(owner)}", reply_markup=back_kb())


@dp.message(Adm.give, F.text)
async def adm_give(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        uid, amount = (int(x) for x in m.text.split())
    except Exception:  # noqa: BLE001
        return await m.answer("Format xato. Masalan: <code>123456789 10</code>")
    u = await get_user(uid)
    if not u:
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    await ex("UPDATE users SET coins=GREATEST(coins+$2,0) WHERE id=$1", uid, amount)
    await state.clear()
    new = await qv("SELECT coins FROM users WHERE id=$1", uid)
    if amount > 0:
        await safe_send(uid, f"🎁 Hisobingizga <b>{amount}</b> tanga qo'shildi.")
    await m.answer(f"✅ Bajarildi. {esc(u['name'])} balansi: {new}", reply_markup=back_kb())


@dp.message(Adm.ban, F.text)
async def adm_ban(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if not m.text.strip().lstrip("-").isdigit():
        return await m.answer("Faqat raqamli user_id yuboring.")
    uid = int(m.text.strip())
    u = await get_user(uid)
    if not u:
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    banned = not u["banned"]
    await ex("UPDATE users SET banned=$2, searching=false WHERE id=$1", uid, banned)
    if banned:
        pid = await end_chat(uid)
        if pid:
            await safe_send(pid, "⚠️ Suhbatdosh suhbatni tugatdi.", main_kb)
    await state.clear()
    await m.answer(("🚫 Banlandi" if banned else "✅ Ban olib tashlandi") + f": {esc(u['name'])} ({uid})", reply_markup=back_kb())


@dp.message(Adm.start_coins, F.text)
async def adm_start_coins(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if not m.text.strip().isdigit():
        return await m.answer("Raqam yuboring.")
    await set_setting("start_coins", m.text.strip())
    await state.clear()
    await m.answer(f"✅ Boshlang'ich tanga: {m.text.strip()}", reply_markup=back_kb())


@dp.message(Adm.broadcast)
async def adm_broadcast(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    ids = [r["id"] for r in await q("SELECT id FROM users WHERE registered AND NOT banned")]
    await m.answer(f"📢 {len(ids)} ta foydalanuvchiga yuborilmoqda...")
    ok = 0
    for uid in ids:
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}/{len(ids)}", reply_markup=back_kb())


# ------------------------------------------------------------------ relay (eng oxirida)
@dp.message(F.chat.type == "private")
async def relay(m: Message, state: FSMContext):
    if await state.get_state():
        return
    u = await get_user(m.from_user.id)
    if not u or not u["registered"]:
        return await m.answer("Boshlash uchun /start bosing.")
    if not u["partner_id"]:
        return await m.answer("Bosh menyu 👇", reply_markup=main_kb)
    try:
        await m.copy_to(u["partner_id"])
    except TelegramForbiddenError:
        await end_chat(m.from_user.id)
        await m.answer("⚠️ Suhbatdosh botni tark etgan. Suhbat tugatildi.", reply_markup=main_kb)
    except TelegramBadRequest:
        await m.answer("Bu xabarni yuborib bo'lmadi.")


# ------------------------------------------------------------------ run
KEEP_ALIVE_TASK = None


async def keep_alive():
    """Render bepul rejasida uyquga ketmaslik uchun o'z URL'ini har 9 daqiqada chaqiradi."""
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(f"{BASE_URL}/", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    log.info("keep-alive: %s", r.status)
            except Exception as e:  # noqa: BLE001
                log.warning("keep-alive xato: %s", e)
            await asyncio.sleep(540)


async def on_startup(bot: Bot):
    global KEEP_ALIVE_TASK
    await init_db()
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=SECRET, drop_pending_updates=True)
        log.info("Webhook: %s/webhook", BASE_URL)
        KEEP_ALIVE_TASK = asyncio.create_task(keep_alive())


async def on_shutdown(bot: Bot):
    if KEEP_ALIVE_TASK:
        KEEP_ALIVE_TASK.cancel()
    if pool:
        await pool.close()


async def health(_):
    return web.Response(text="ok")


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    if BASE_URL:  # Render: webhook rejimi
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        app = web.Application()
        app.router.add_get("/", health)
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:  # lokal: polling
        async def run():
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)

        asyncio.run(run())


if __name__ == "__main__":
    main()
