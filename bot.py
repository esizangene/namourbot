"""
ربات دوست‌یابی تلگرام (aiogram 3)
- دعوت اختصاصی و تاییدشده برای هر کاربر
- ثبت‌نام مرحله‌ای + احراز هویت با عکس و ویدیوی متن تصادفی (تایید توسط ادمین)
- کشف، لایک، مچ، چت ناشناس از طریق ربات
- دستیار هوش مصنوعی (هر API سازگار با OpenAI)
- پنل ادمین، گزارش و بلاک
"""
import asyncio
import html
import logging
import os
import random
import sqlite3
import time

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
REQUIRED_INVITES = int(os.getenv("REQUIRED_INVITES", "2"))
AI_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1/chat/completions")
AI_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_DAILY = int(os.getenv("AI_DAILY_LIMIT", "25"))

e = html.escape
GENDERS = ["مرد", "زن", "ترنس", "گی", "لز"]
MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
COLORS = ["primary", "success", "danger"]  # رنگ دکمه‌های شیشه‌ای تلگرام

bot: Bot
dp = Dispatcher(storage=MemoryStorage())
r = Router()
dp.include_router(r)
BOT_USERNAME = ""

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT, invites INTEGER DEFAULT 0,
 gender TEXT, seeking TEXT, name TEXT, age INTEGER, height INTEGER, bmonth TEXT,
 province TEXT, city TEXT, hood TEXT, expect TEXT, dates TEXT, bio TEXT,
 photo TEXT, video TEXT, vphrase TEXT, verified INTEGER DEFAULT 0,
 banned INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
 ai_day TEXT, ai_cnt INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS invites(invitee INTEGER PRIMARY KEY, inviter INTEGER, confirmed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS seen(src INTEGER, dst INTEGER, action TEXT, PRIMARY KEY(src,dst));
CREATE TABLE IF NOT EXISTS blocks(src INTEGER, dst INTEGER, PRIMARY KEY(src,dst));
CREATE TABLE IF NOT EXISTS reports(src INTEGER, dst INTEGER, ts INTEGER);
""")
db.commit()


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    db.commit()
    rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def get(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def setf(uid, **kw):
    cols = ",".join(f"{k}=?" for k in kw)
    q(f"UPDATE users SET {cols} WHERE id=?", (*kw.values(), uid))


# ───────────────────────── ابزارهای UI ─────────────────────────
def B(text, data, style=None, url=None):
    extra = {"style": style} if style else {}
    if url:
        return InlineKeyboardButton(text=text, url=url, **extra)
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(x) for x in rows])


def home_kb():
    return kb([B("🏠 منوی اصلی", "m:home", "primary")])


def ctx(uid) -> FSMContext:
    return FSMContext(storage=dp.storage,
                      key=StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid))


class Reg(StatesGroup):
    input = State()


class Chat(StatesGroup):
    ai = State()
    relay = State()


ORDER = ["gender", "seeking", "name", "age", "height", "bmonth", "province",
         "city", "hood", "expect", "dates", "bio", "photo", "video"]
TEXT_PROMPTS = {
    "name": "👤 اسمت چیه؟ (اسم مستعار هم اوکیه)",
    "age": "🎂 چند سالته؟ (فقط عدد، حداقل ۱۸)",
    "height": "📏 قدت چنده؟ (به سانتی‌متر، مثلاً ۱۷۵)",
    "province": "🗺 ساکن کدوم استانی؟",
    "city": "🏙 ساکن کدوم شهری؟",
    "hood": "📍 محله‌ات؟ (فقط برای پیشنهاد بهتر استفاده می‌شه و هرگز به بقیه نشون داده نمی‌شه. نمی‌خوای؟ بنویس «-»)",
    "expect": "💞 انتظاراتت از طرف مقابل چیه؟",
    "bio": "📝 یه معرفی کوتاه از خودت بنویس (علایق، سبک زندگی و ...)",
}


def missing(u):
    for f in ORDER:
        if not u[f]:
            return f
    return None


def card_text(c, own=False):
    t = (f"<b>{e(c['name'])}</b>، {c['age']} ساله {'✅' if c['verified'] == 2 else ''}\n"
         f"🧬 {e(c['gender'])}  |  📏 {c['height']} cm  |  🌙 {e(c['bmonth'])}\n"
         f"📍 {e(c['city'])}، {e(c['province'])}\n"
         f"💞 انتظار: {e(c['expect'])}\n"
         f"🗓 اهل قرار: {e(c['dates'])}\n"
         f"📝 {e(c['bio'])}")
    if own:
        t += f"\n\n🔎 دنبال: {e(c['seeking'])}"
    return t


# ───────────────────────── ثبت‌نام مرحله‌ای ─────────────────────────
async def ask_next(uid):
    u = get(uid)
    f = missing(u)
    st = ctx(uid)
    if f is None:
        await st.clear()
        if u["verified"] == 1:
            await bot.send_message(uid, "⏳ پروفایلت ثبت شد و در انتظار تایید هویت توسط ادمینه. نتیجه همین‌جا بهت اعلام می‌شه.")
        else:
            await send_menu(uid)
        return
    await st.set_state(Reg.input)
    if f == "gender":
        rows = [[B(g, f"set:gender:{g}", COLORS[i % 3]) for g in GENDERS[:3]],
                [B(g, f"set:gender:{g}", COLORS[(i + 3) % 3]) for i, g in enumerate(GENDERS[3:])]]
        await bot.send_message(uid, "👋 جنسیتت رو انتخاب کن:", reply_markup=kb(*rows))
    elif f == "seeking":
        opts = GENDERS + ["همه"]
        rows = [[B(g, f"set:seeking:{g}", COLORS[i % 3]) for i, g in enumerate(opts[:3])],
                [B(g, f"set:seeking:{g}", COLORS[i % 3]) for i, g in enumerate(opts[3:])]]
        await bot.send_message(uid, "🔎 دنبال چه کسی هستی؟", reply_markup=kb(*rows))
    elif f == "bmonth":
        rows = [[B(m, f"set:bmonth:{m}", COLORS[(i + j) % 3]) for j, m in enumerate(MONTHS[i:i + 3])]
                for i in range(0, 12, 3)]
        await bot.send_message(uid, "🌙 متولد چه ماهی هستی؟", reply_markup=kb(*rows))
    elif f == "dates":
        await bot.send_message(uid, "🗓 اهل قرار هستی؟", reply_markup=kb(
            [B("✅ بله", "set:dates:بله", "success"), B("❌ نه", "set:dates:نه", "danger")],
            [B("🤔 بستگی داره", "set:dates:بستگی داره", "primary")]))
    elif f == "photo":
        await bot.send_message(uid, "📸 یه عکس <b>واقعی و واضح از صورت خودت</b> بفرست (برای احراز هویت لازمه).")
    elif f == "video":
        if not u["vphrase"]:
            ph = " ".join(random.sample(["آفتاب", "دریا", "کتاب", "ماه", "باران", "گل", "قهوه", "کوه", "ستاره", "چای"], 3)) \
                 + f" {random.randint(100, 999)}"
            setf(uid, vphrase=ph)
            u = get(uid)
        await bot.send_message(
            uid, "🎥 مرحله آخر: یه ویدیوی چند ثانیه‌ای از خودت بفرست که "
                 f"این متن رو با صدای بلند می‌گی:\n\n<b>«{e(u['vphrase'])}»</b>\n\n"
                 "صورتت باید کامل توی ویدیو دیده بشه. (فقط ادمین می‌بینه و عکس و ویدیو بعد از بررسی هرگز برای بقیه نمایش داده نمی‌شه، جز عکس پروفایل.)")
    else:
        await bot.send_message(uid, TEXT_PROMPTS[f])


@r.callback_query(F.data.startswith("set:"), Reg.input)
async def set_choice(c: CallbackQuery):
    _, field, val = c.data.split(":", 2)
    u = get(c.from_user.id)
    if missing(u) != field:
        return await c.answer()
    setf(c.from_user.id, **{field: val})
    await c.answer("✔️")
    try:
        await c.message.edit_text(f"{c.message.text}\n\n➡️ <b>{e(val)}</b>")
    except Exception:
        pass
    await ask_next(c.from_user.id)


@r.message(Reg.input, F.text)
async def reg_text(m: Message):
    uid = m.from_user.id
    f = missing(get(uid))
    t = m.text.strip()
    if f not in TEXT_PROMPTS:
        return await m.answer("لطفاً از دکمه‌ها یا همون نوع پیامی که خواسته شده استفاده کن 🙂")
    if f == "age":
        if not t.isdigit() or not 18 <= int(t) <= 80:
            return await m.answer("⚠️ سن باید عدد بین ۱۸ تا ۸۰ باشه. این ربات فقط برای افراد ۱۸ سال به بالاست.")
        t = int(t)
    elif f == "height":
        if not t.isdigit() or not 120 <= int(t) <= 230:
            return await m.answer("⚠️ قد رو به سانتی‌متر و بین ۱۲۰ تا ۲۳۰ بنویس.")
        t = int(t)
    elif not 1 <= len(t) <= 300:
        return await m.answer("⚠️ متن خیلی کوتاه یا خیلی بلنده.")
    setf(uid, **{f: t})
    await ask_next(uid)


@r.message(Reg.input, F.photo)
async def reg_photo(m: Message):
    uid = m.from_user.id
    if missing(get(uid)) != "photo":
        return
    setf(uid, photo=m.photo[-1].file_id)
    await ask_next(uid)


@r.message(Reg.input, F.video | F.video_note)
async def reg_video(m: Message):
    uid = m.from_user.id
    u = get(uid)
    if missing(u) != "video":
        return
    vid = (m.video or m.video_note).file_id
    setf(uid, video=vid, verified=1)
    await ask_next(uid)
    await notify_admins_review(uid)


async def notify_admins_review(uid):
    u = get(uid)
    cap = (f"🆕 درخواست احراز هویت\nID: <code>{uid}</code>\n"
           f"{card_text(u)}\n\nمتن ویدیو: «{e(u['vphrase'])}»")
    markup = kb([B("✅ تایید", f"vf:ok:{uid}", "success"), B("❌ رد", f"vf:no:{uid}", "danger")],
                [B("⛔️ بن", f"ban:{uid}", "danger")])
    for a in ADMINS:
        try:
            await bot.send_photo(a, u["photo"], caption=cap)
            try:
                await bot.send_video(a, u["video"], reply_markup=markup)
            except Exception:
                await bot.send_video_note(a, u["video"], reply_markup=markup)
        except Exception as ex:
            logging.warning("admin notify failed: %s", ex)


@r.callback_query(F.data.startswith("vf:"))
async def verify_cb(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if act == "ok":
        setf(uid, verified=2)
        await c.answer("تایید شد")
        await bot.send_message(uid, "🎉 هویتت تایید شد و پروفایلت فعال شد!")
        await send_menu(uid)
    else:
        setf(uid, verified=0, video=None, vphrase=None)
        await c.answer("رد شد")
        await bot.send_message(uid, "❌ ویدیوت تایید نشد (صورت نامشخص، متن اشتباه یا عدم تطابق با عکس). دوباره تلاش کن:")
        await ask_next(uid)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ───────────────────────── شروع، دعوت و مسیریابی ─────────────────────────
WELCOME = ("🌹 <b>به ربات دوست‌یابی خوش اومدی!</b>\n\n"
           "اینجا آدم‌های واقعی و احراز هویت‌شده پیدا می‌کنی؛ بدون پروفایل فیک.\n"
           f"برای ورود باید <b>{REQUIRED_INVITES} نفر</b> رو با لینک اختصاصی خودت دعوت کنی.")


@r.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    u = get(uid)
    new = u is None
    if new:
        q("INSERT INTO users(id,username,created) VALUES(?,?,?)", (uid, m.from_user.username, int(time.time())))
    else:
        setf(uid, username=m.from_user.username)
    await m.answer(WELCOME)
    arg = command.args or ""
    if new and arg.startswith("ref_") and arg[4:].isdigit():
        inviter = int(arg[4:])
        if inviter != uid and get(inviter):
            q("INSERT OR IGNORE INTO invites(invitee,inviter,confirmed) VALUES(?,?,0)", (uid, inviter))
            return await m.answer("🔗 با لینک دعوت یکی از کاربرها وارد شدی. برای ثبت دعوت دکمه زیر رو بزن:",
                                  reply_markup=kb([B("✅ تایید ورود با دعوت", "inv:confirm", "success")]))
    await route(uid)


@r.callback_query(F.data == "inv:confirm")
async def inv_confirm(c: CallbackQuery):
    uid = c.from_user.id
    row = q("SELECT * FROM invites WHERE invitee=?", (uid,), one=True)
    if row and not row["confirmed"]:
        q("UPDATE invites SET confirmed=1 WHERE invitee=?", (uid,))
        q("UPDATE users SET invites=invites+1 WHERE id=?", (row["inviter"],))
        inv = get(row["inviter"])
        await c.answer("دعوت ثبت شد ✅")
        try:
            await bot.send_message(row["inviter"], f"🎊 یک نفر با لینک تو وارد شد! دعوت‌های تاییدشده: {inv['invites']}/{REQUIRED_INVITES}")
        except Exception:
            pass
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await c.answer()
    await route(uid)


def invite_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"


async def route(uid):
    u = get(uid)
    if u["banned"]:
        return await bot.send_message(uid, "⛔️ حساب شما مسدود شده است.")
    if u["invites"] < REQUIRED_INVITES:
        return await show_invite(uid)
    await ask_next(uid)


async def show_invite(uid):
    u = get(uid)
    link = invite_link(uid)
    await bot.send_message(
        uid, f"🔒 برای استفاده از ربات باید {REQUIRED_INVITES} نفر رو دعوت کنی.\n"
             f"✅ دعوت‌های تاییدشده: <b>{u['invites']}/{REQUIRED_INVITES}</b>\n\n"
             f"🔗 لینک اختصاصی تو:\n<code>{link}</code>\n\n"
             "هر دعوت فقط وقتی حساب می‌شه که یک کاربر <b>جدید و واقعی</b> با لینک تو وارد بشه و تایید بزنه.",
        reply_markup=kb([B("📤 اشتراک‌گذاری لینک", "", "success",
                           url=f"https://t.me/share/url?url={link}&text=بیا به ربات دوست‌یابی")],
                        [B("🔄 بررسی دعوت‌ها", "m:home", "primary")]))


# ───────────────────────── منوی اصلی ─────────────────────────
def allowed(u):
    return u and not u["banned"] and u["invites"] >= REQUIRED_INVITES and u["verified"] == 2 and missing(u) is None


async def send_menu(uid):
    u = get(uid)
    likes = q("SELECT COUNT(*) n FROM seen WHERE dst=? AND action='like' AND src NOT IN (SELECT dst FROM seen WHERE src=?)",
              (uid, uid), one=True)["n"]
    await bot.send_message(
        uid, f"🏠 <b>منوی اصلی</b>\n💌 لایک‌های جدید: {likes}",
        reply_markup=kb(
            [B("🔍 کشف و آشنایی", "m:browse", "success"), B(f"💌 لایک‌ها ({likes})", "m:likes", "danger")],
            [B("💬 مچ‌ها و چت", "m:chats", "primary"), B("🤖 دستیار هوش مصنوعی", "m:ai", "success")],
            [B("👤 پروفایل من", "m:me", "primary"), B("✏️ ویرایش پروفایل", "m:edit", "primary")],
            [B("🔗 دعوت دوستان", "m:invite", "success"), B("🛡 امنیت و راهنما", "m:safety", "danger")],
            [B("⚙️ تنظیمات", "m:settings", "primary")]))


@r.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext):
    await state.clear()
    u = get(m.from_user.id)
    if not u:
        return await m.answer("اول /start رو بزن.")
    if allowed(u):
        await send_menu(m.from_user.id)
    else:
        await route(m.from_user.id)


SAFETY = ("🛡 <b>نکات امنیتی</b>\n\n"
          "• اطلاعات شخصی (آدرس، شماره، محل کار) رو زود نده.\n"
          "• قرار اول رو در مکان عمومی و شلوغ بذار و به یه دوست خبر بده.\n"
          "• درخواست پول یا کارت = کلاهبرداری. گزارش بده.\n"
          "• هر رفتار مشکوک یا آزاردهنده رو با 🚩 گزارش کن.\n"
          "• رسانه‌های احراز هویت فقط برای ادمین‌ها قابل مشاهده است.")


@r.callback_query(F.data.startswith("m:"))
async def menu_cb(c: CallbackQuery, state: FSMContext):
    key = c.data[2:]
    uid = c.from_user.id
    await c.answer()
    u = get(uid)
    if not u or u["banned"]:
        return
    if key == "home":
        await state.clear()
        return await (send_menu(uid) if allowed(u) else route(uid))
    if key == "invite":
        return await show_invite(uid)
    if key == "safety":
        return await bot.send_message(uid, SAFETY, reply_markup=home_kb())
    if not allowed(u):
        return await route(uid)
    await state.clear()
    if key == "browse":
        await send_card(uid, "b")
    elif key == "likes":
        await send_card(uid, "l")
    elif key == "chats":
        rows = q("""SELECT u.id,u.name FROM seen a JOIN seen b ON a.src=b.dst AND a.dst=b.src
                    JOIN users u ON u.id=a.dst WHERE a.src=? AND a.action='like' AND b.action='like'""", (uid,))
        if not rows:
            return await bot.send_message(uid, "هنوز مچی نداری. کمی دیگه کشف کن 💚", reply_markup=home_kb())
        await bot.send_message(uid, "💬 مچ‌های تو:", reply_markup=kb(
            *[[B(f"💬 {x['name']}", f"chat:{x['id']}", COLORS[i % 3])] for i, x in enumerate(rows)],
            [B("🏠 منو", "m:home", "primary")]))
    elif key == "ai":
        await state.set_state(Chat.ai)
        await state.update_data(hist=[])
        await bot.send_message(uid, "🤖 دستیار هوش مصنوعی آماده‌ست! هرچی درباره رابطه، پیام اول، ساخت بیو یا آمادگی قرار بپرسی جواب می‌ده.\nبرای خروج /menu رو بزن.",
                               reply_markup=home_kb())
    elif key == "me":
        await bot.send_photo(uid, u["photo"], caption=card_text(u, own=True) +
                             f"\n\n👁 وضعیت نمایش: {'فعال' if u['active'] else 'مخفی'}", reply_markup=home_kb())
    elif key == "edit":
        fields = [("name", "اسم"), ("age", "سن"), ("height", "قد"), ("city", "شهر"), ("province", "استان"),
                  ("hood", "محله"), ("expect", "انتظارات"), ("dates", "قرار"), ("bio", "معرفی"),
                  ("seeking", "دنبال چه کسی"), ("photo", "عکس (نیاز به احراز مجدد)")]
        rows = [[B(t, f"edit:{f}", COLORS[i % 3])] for i, (f, t) in enumerate(fields)]
        await bot.send_message(uid, "✏️ کدوم بخش رو می‌خوای تغییر بدی؟", reply_markup=kb(*rows, [B("🏠 منو", "m:home", "primary")]))
    elif key == "settings":
        await bot.send_message(uid, "⚙️ تنظیمات", reply_markup=kb(
            [B("👁 مخفی/نمایش پروفایل", "st:toggle", "primary")],
            [B("🗑 حذف کامل حساب و اطلاعات", "st:del", "danger")],
            [B("🏠 منو", "m:home", "primary")]))


@r.callback_query(F.data.startswith("edit:"))
async def edit_cb(c: CallbackQuery):
    uid = c.from_user.id
    if not allowed(get(uid)):
        return await c.answer()
    f = c.data[5:]
    if f == "photo":
        setf(uid, photo=None, video=None, vphrase=None, verified=0)
    else:
        setf(uid, **{f: None})
    await c.answer()
    await ask_next(uid)


@r.callback_query(F.data.startswith("st:"))
async def settings_cb(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    act = c.data[3:]
    await c.answer()
    if act == "toggle":
        u = get(uid)
        setf(uid, active=0 if u["active"] else 1)
        await bot.send_message(uid, "👁 پروفایل شما " + ("مخفی شد." if u["active"] else "دوباره نمایش داده می‌شه."), reply_markup=home_kb())
    elif act == "del":
        await bot.send_message(uid, "⚠️ مطمئنی؟ همه اطلاعات برای همیشه پاک می‌شه.", reply_markup=kb(
            [B("🗑 بله، حذف کن", "st:delok", "danger"), B("↩️ انصراف", "m:home", "success")]))
    elif act == "delok":
        for t, col in (("users", "id"), ("seen", "src"), ("seen", "dst"), ("blocks", "src"), ("blocks", "dst")):
            q(f"DELETE FROM {t} WHERE {col}=?", (uid,))
        await state.clear()
        await bot.send_message(uid, "✅ حساب و اطلاعات شما حذف شد. هر وقت خواستی با /start برگرد.")


# ───────────────────────── کشف و لایک ─────────────────────────
def find_candidate(me, mode):
    liker = "AND c.id IN (SELECT src FROM seen WHERE dst=:me AND action='like')" if mode == "l" else ""
    return q(f"""SELECT * FROM users c WHERE c.id!=:me AND c.verified=2 AND c.banned=0 AND c.active=1
      AND (:seek='همه' OR c.gender=:seek) AND c.seeking IN (:g,'همه')
      AND c.id NOT IN (SELECT dst FROM seen WHERE src=:me)
      AND c.id NOT IN (SELECT dst FROM blocks WHERE src=:me)
      AND c.id NOT IN (SELECT src FROM blocks WHERE dst=:me) {liker}
      ORDER BY (c.city=:city) DESC, RANDOM() LIMIT 1""",
             {"me": me["id"], "seek": me["seeking"], "g": me["gender"], "city": me["city"]}, one=True)


async def send_card(uid, mode):
    cand = find_candidate(get(uid), mode)
    if not cand:
        return await bot.send_message(uid, "فعلاً کسی نمونده 🌱 کمی بعد دوباره سر بزن.", reply_markup=home_kb())
    i = cand["id"]
    await bot.send_photo(uid, cand["photo"], caption=card_text(cand), reply_markup=kb(
        [B("💚 لایک", f"act:like:{i}:{mode}", "success"), B("⏭ بعدی", f"act:skip:{i}:{mode}", "primary")],
        [B("🚩 گزارش", f"act:rep:{i}:{mode}", "danger"), B("🚫 بلاک", f"act:blk:{i}:{mode}", "danger")],
        [B("🏠 منو", "m:home", "primary")]))


@r.callback_query(F.data.startswith("act:"))
async def act_cb(c: CallbackQuery):
    _, act, dst, mode = c.data.split(":")
    uid, dst = c.from_user.id, int(dst)
    if not allowed(get(uid)):
        return await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if act == "like":
        q("INSERT OR REPLACE INTO seen VALUES(?,?,'like')", (uid, dst))
        await c.answer("💚")
        if q("SELECT 1 FROM seen WHERE src=? AND dst=? AND action='like'", (dst, uid), one=True):
            for a, b in ((uid, dst), (dst, uid)):
                other = get(b)
                try:
                    await bot.send_message(a, f"🎉 مچ شدید با <b>{e(other['name'])}</b>!",
                                           reply_markup=kb([B("💬 شروع چت", f"chat:{b}", "success")]))
                except Exception:
                    pass
        else:
            try:
                await bot.send_message(dst, "💌 یک نفر پروفایلت رو لایک کرد! از منو بخش لایک‌ها رو ببین.")
            except Exception:
                pass
    elif act == "skip":
        q("INSERT OR REPLACE INTO seen VALUES(?,?,'skip')", (uid, dst))
        await c.answer()
    elif act == "blk":
        q("INSERT OR IGNORE INTO blocks VALUES(?,?)", (uid, dst))
        q("INSERT OR REPLACE INTO seen VALUES(?,?,'skip')", (uid, dst))
        await c.answer("بلاک شد 🚫")
    elif act == "rep":
        q("INSERT INTO reports VALUES(?,?,?)", (uid, dst, int(time.time())))
        q("INSERT OR REPLACE INTO seen VALUES(?,?,'skip')", (uid, dst))
        await c.answer("گزارش ثبت شد 🚩")
        for a in ADMINS:
            try:
                await bot.send_message(a, f"🚩 گزارش علیه <code>{dst}</code> از <code>{uid}</code>",
                                       reply_markup=kb([B("⛔️ بن", f"ban:{dst}", "danger")]))
            except Exception:
                pass
    await send_card(uid, mode)


# ───────────────────────── چت ناشناس بین مچ‌ها ─────────────────────────
def matched(a, b):
    return bool(q("""SELECT 1 FROM seen x JOIN seen y ON x.src=y.dst AND x.dst=y.src
                     WHERE x.src=? AND x.dst=? AND x.action='like' AND y.action='like'""", (a, b), one=True))


@r.callback_query(F.data.startswith("chat:"))
async def chat_cb(c: CallbackQuery, state: FSMContext):
    uid, other = c.from_user.id, int(c.data[5:])
    await c.answer()
    if not allowed(get(uid)) or not matched(uid, other):
        return
    await state.set_state(Chat.relay)
    await state.update_data(partner=other)
    o = get(other)
    await bot.send_message(uid, f"💬 در حال چت با <b>{e(o['name'])}</b>. هر پیامی بنویسی براش ارسال می‌شه.\nخروج: /menu")


@r.message(Chat.relay, F.text)
async def relay(m: Message, state: FSMContext):
    uid = m.from_user.id
    partner = (await state.get_data()).get("partner")
    if not partner or not matched(uid, partner) or q("SELECT 1 FROM blocks WHERE src=? AND dst=?", (partner, uid), one=True):
        await state.clear()
        return await m.answer("امکان ارسال نیست.", reply_markup=home_kb())
    me = get(uid)
    try:
        await bot.send_message(partner, f"💬 <b>{e(me['name'])}</b>:\n{e(m.text)}",
                               reply_markup=kb([B("↩️ پاسخ", f"chat:{uid}", "success")]))
        await m.answer("✔️ ارسال شد")
    except Exception:
        await m.answer("❌ ارسال نشد.")


# ───────────────────────── دستیار هوش مصنوعی ─────────────────────────
AI_SYS = ("تو یک دستیار مهربان و محترم فارسی‌زبان در یک ربات دوست‌یابی هستی. به کاربر در نوشتن بیو، "
          "شروع گفتگو، آداب آشنایی، امنیت قرار و مسائل رابطه کمک کن. محتوای جنسی صریح، توهین یا "
          "تشویق به کار غیرقانونی نده. کوتاه و کاربردی جواب بده.")


@r.message(Chat.ai, F.text)
async def ai_chat(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = get(uid)
    today = time.strftime("%Y-%m-%d")
    cnt = u["ai_cnt"] if u["ai_day"] == today else 0
    if cnt >= AI_DAILY:
        return await m.answer("سهمیه امروز دستیار تموم شده؛ فردا برگرد 🙏")
    if not AI_KEY:
        return await m.answer("دستیار هنوز توسط ادمین پیکربندی نشده.")
    setf(uid, ai_day=today, ai_cnt=cnt + 1)
    hist = (await state.get_data()).get("hist", [])[-6:]
    hist.append({"role": "user", "content": m.text[:1500]})
    await bot.send_chat_action(uid, "typing")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(AI_URL, headers={"Authorization": f"Bearer {AI_KEY}"},
                              json={"model": AI_MODEL, "max_tokens": 600,
                                    "messages": [{"role": "system", "content": AI_SYS}] + hist},
                              timeout=aiohttp.ClientTimeout(total=45)) as resp:
                data = await resp.json()
        ans = data["choices"][0]["message"]["content"]
    except Exception as ex:
        logging.warning("AI error: %s", ex)
        return await m.answer("⚠️ الان نمی‌تونم جواب بدم، کمی بعد دوباره امتحان کن.")
    hist.append({"role": "assistant", "content": ans})
    await state.update_data(hist=hist)
    await m.answer(e(ans))


# ───────────────────────── پنل ادمین ─────────────────────────
@r.callback_query(F.data.startswith("ban:"))
async def ban_cb(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    setf(int(c.data[4:]), banned=1)
    await c.answer("بن شد")


MATCH_SQL = ("SELECT COUNT(*)/2 FROM seen a JOIN seen b ON a.src=b.dst AND a.dst=b.src "
             "WHERE a.action='like' AND b.action='like'")


@r.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id not in ADMINS:
        return
    s = lambda sql: q(sql, one=True)[0]
    await m.answer(
        "🛠 <b>پنل ادمین</b>\n"
        f"👥 کل کاربران: {s('SELECT COUNT(*) FROM users')}\n"
        f"✅ تاییدشده: {s('SELECT COUNT(*) FROM users WHERE verified=2')}\n"
        f"⏳ منتظر تایید: {s('SELECT COUNT(*) FROM users WHERE verified=1')}\n"
        f"💞 مچ‌ها: {s(MATCH_SQL)}\n"
        f"🚩 گزارش‌ها: {s('SELECT COUNT(*) FROM reports')}\n\n"
        "/pending ، /broadcast متن ، /ban آیدی ، /unban آیدی")


@r.message(Command("pending"))
async def pending_cmd(m: Message):
    if m.from_user.id not in ADMINS:
        return
    for row in q("SELECT id FROM users WHERE verified=1 AND video IS NOT NULL"):
        await notify_admins_review(row["id"])


@r.message(Command("broadcast"))
async def broadcast_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS or not command.args:
        return
    n = 0
    for row in q("SELECT id FROM users WHERE banned=0"):
        try:
            await bot.send_message(row["id"], command.args)
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"ارسال شد برای {n} نفر")


@r.message(Command("ban", "unban"))
async def ban_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS or not (command.args or "").isdigit():
        return
    setf(int(command.args), banned=1 if command.command == "ban" else 0)
    await m.answer("انجام شد")


@r.message()
async def fallback(m: Message):
    u = get(m.from_user.id)
    if not u:
        return await m.answer("برای شروع /start رو بزن.")
    await m.answer("برای دیدن منو /menu رو بزن 🙂")


async def main():
    global bot, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    BOT_USERNAME = (await bot.get_me()).username
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
