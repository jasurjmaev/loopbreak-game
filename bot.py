
import asyncio
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_TEXT = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_TEXT) if OWNER_ID_TEXT.isdigit() else 0

WEBAPP_URL = "https://mellow-twilight-632fd7.netlify.app/"
DB_PATH = BASE_DIR / "loopbreak.db"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())

ACHIEVEMENTS = {
    "first_game": "🎮 Birinchi qadam",
    "five_games": "🔥 Tajribali o‘yinchi",
    "xp_100": "⭐ Yulduz",
    "xp_500": "💎 Afsona",
    "three_endings": "📖 Hikoyachi",
}

DONATION_AMOUNTS = (25, 50, 100, 250, 500)
PREMIUM_PLANS = {30: 100, 90: 250, 365: 500}
LOCAL_TZ = timezone(timedelta(hours=5), "Asia/Tashkent")
STREAK_REWARDS = {3: 25, 7: 50, 14: 100, 30: 250}


class DonationStates(StatesGroup):
    waiting_for_note = State()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                xp INTEGER DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                premium INTEGER DEFAULT 0,
                premium_until TEXT,
                streak INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                last_active_date TEXT,
                created_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ending TEXT,
                score INTEGER,
                xp_earned INTEGER,
                created_at TEXT,
                result_id TEXT UNIQUE
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(game_results)"
            ).fetchall()
        }

        if "result_id" not in columns:
            conn.execute(
                "ALTER TABLE game_results ADD COLUMN result_id TEXT"
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                user_id INTEGER NOT NULL,
                achievement_key TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,
                PRIMARY KEY (user_id, achievement_key)
            )
        """)

        # To‘lov yuborilgan donatlar va ularning xabarlari.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS donations (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
        """)

        # Premium invoice va to‘lov holati alohida saqlanadi.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_payments (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT,
                telegram_charge_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS streak_rewards (
                user_id INTEGER NOT NULL,
                streak_days INTEGER NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, streak_days)
            )
        """)

        # Eski loopbreak.db faylini buzmasdan yangi ustunlar qo‘shiladi.
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for column, definition in (("premium_until", "TEXT"), ("streak", "INTEGER DEFAULT 0"), ("best_streak", "INTEGER DEFAULT 0"), ("last_active_date", "TEXT")):
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

        conn.commit()


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🎮 LOOPBREAK o‘yini",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ],
            [
                KeyboardButton(text="👤 Profilim"),
                KeyboardButton(text="🏆 Reyting"),
            ],
            [
                KeyboardButton(text="🎯 Missiyalar"),
                KeyboardButton(text="💎 Premium"),
            ],
            [
                KeyboardButton(text="🔥 Streak"),
                KeyboardButton(text="💌 Donat"),
            ],
            [KeyboardButton(text="ℹ️ Yordam")],
        ],
        resize_keyboard=True,
    )


def save_user(user):
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id, username, first_name, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            now,
        ))
        conn.commit()


def local_now():
    return datetime.now(LOCAL_TZ)


def update_streak(user_id):
    """Bugungi faollikni server bazasida qayd etadi; kuniga bir marta hisoblaydi."""
    today = local_now().date()
    now_text = datetime.now(timezone.utc).isoformat()
    rewards = []
    with db_connect() as conn:
        row = conn.execute("SELECT streak, best_streak, last_active_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return {"streak": 0, "best": 0, "reward_xp": 0, "new_day": False}
        last = row["last_active_date"]
        if last == today.isoformat():
            return {"streak": row["streak"] or 0, "best": row["best_streak"] or 0, "reward_xp": 0, "new_day": False}
        if last == (today - timedelta(days=1)).isoformat():
            streak = (row["streak"] or 0) + 1
        else:
            streak = 1
        best = max(row["best_streak"] or 0, streak)
        reward_xp = STREAK_REWARDS.get(streak, 0)
        if reward_xp:
            cur = conn.execute("INSERT OR IGNORE INTO streak_rewards (user_id, streak_days, claimed_at) VALUES (?, ?, ?)", (user_id, streak, now_text))
            if cur.rowcount == 0:
                reward_xp = 0
        conn.execute("UPDATE users SET streak = ?, best_streak = ?, last_active_date = ?, xp = xp + ? WHERE user_id = ?", (streak, best, today.isoformat(), reward_xp, user_id))
        conn.commit()
        rewards = [streak, best, reward_xp]
    return {"streak": rewards[0], "best": rewards[1], "reward_xp": rewards[2], "new_day": True}


def premium_is_active(user):
    if not user or not user["premium"]:
        return False
    until = user["premium_until"] if "premium_until" in user.keys() else None
    if not until:
        return bool(user["premium"])
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 30 kun — ⭐ 100", callback_data="premium:30")],
        [InlineKeyboardButton(text="💎 90 kun — ⭐ 250", callback_data="premium:90")],
        [InlineKeyboardButton(text="👑 365 kun — ⭐ 500", callback_data="premium:365")],
    ])


def create_premium_invoice_record(user_id, days):
    amount = PREMIUM_PLANS[days]
    payload = "premium_" + uuid.uuid4().hex
    with db_connect() as conn:
        conn.execute("INSERT INTO premium_payments (payload, user_id, days, amount, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (payload, user_id, days, amount, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return payload, amount


async def send_premium_invoice(message: Message, days: int):
    if days not in PREMIUM_PLANS:
        await message.answer("Premium rejasi topilmadi.")
        return
    payload, amount = create_premium_invoice_record(message.from_user.id, days)
    try:
        await message.bot.send_invoice(
            chat_id=message.chat.id, title=f"LOOPBREAK Premium — {days} kun",
            description=f"Premium imkoniyatlari {days} kun davomida",
            payload=payload, provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"Premium {days} kun", amount=amount)],
        )
        await message.answer("💎 Premium to‘lovi uchun Telegram Stars oynasi yuborildi. Premium faqat to‘lov Telegram tomonidan tasdiqlangandan keyin yoqiladi.")
    except Exception:
        logging.exception("Premium invoice yuborilmadi")
        await message.answer("❌ To‘lov oynasini ochib bo‘lmadi. Keyinroq urinib ko‘ring.")


def get_user(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def get_ranking():
    with db_connect() as conn:
        return conn.execute("""
            SELECT first_name, username, xp
            FROM users
            ORDER BY xp DESC, user_id ASC
            LIMIT 10
        """).fetchall()


def get_achievements(user_id):
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT achievement_key
            FROM achievements
            WHERE user_id = ?
            ORDER BY unlocked_at
        """, (user_id,)).fetchall()

    return [
        ACHIEVEMENTS[row["achievement_key"]]
        for row in rows
        if row["achievement_key"] in ACHIEVEMENTS
    ]


def save_game_result(user_id, ending, score, result_id):
    xp_earned = max(10, min(100, score // 2 + 10))
    now = datetime.now(timezone.utc).isoformat()
    unlocked = []

    with db_connect() as conn:
        try:
            conn.execute("""
                INSERT INTO game_results (
                    user_id, ending, score, xp_earned,
                    created_at, result_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, ending, score,
                xp_earned, now, result_id,
            ))
        except sqlite3.IntegrityError:
            return False, 0, []

        conn.execute("""
            UPDATE users
            SET xp = xp + ?,
                games_played = games_played + 1
            WHERE user_id = ?
        """, (xp_earned, user_id))

        user = conn.execute("""
            SELECT xp, games_played
            FROM users WHERE user_id = ?
        """, (user_id,)).fetchone()

        endings_count = conn.execute("""
            SELECT COUNT(DISTINCT ending)
            FROM game_results WHERE user_id = ?
        """, (user_id,)).fetchone()[0]

        checks = {
            "first_game": user["games_played"] >= 1,
            "five_games": user["games_played"] >= 5,
            "xp_100": user["xp"] >= 100,
            "xp_500": user["xp"] >= 500,
            "three_endings": endings_count >= 3,
        }

        for key, condition in checks.items():
            if condition:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO achievements (
                        user_id, achievement_key, unlocked_at
                    )
                    VALUES (?, ?, ?)
                """, (user_id, key, now))

                if cursor.rowcount > 0:
                    unlocked.append(ACHIEVEMENTS[key])

        conn.commit()

    return True, xp_earned, unlocked


# =========================
# START VA PROFIL
# =========================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user)

    streak_info = update_streak(message.from_user.id)
    streak_text = f"\n\n🔥 Streak: {streak_info['streak']} kun · Rekord: {streak_info['best']} kun"
    if streak_info["reward_xp"]:
        streak_text += f"\n🎁 Streak mukofoti: +{streak_info['reward_xp']} XP"
    await message.answer(
        "◈ LOOPBREAK ◈\n"
        "OXIRGI QAROR\n\n"
        "OBSIDIAN maxfiy tizimiga xush kelibsiz, operativ.\n\n"
        "Ishonch — qurol.\n"
        "Sir — kuch.\n"
        "Har bir qaror iz qoldiradi.\n\n"
        "Siz haqiqatni topasizmi yoki uning qurboniga aylanasizmi?\n\n"
        "✦ YARATUVCHI: JASURIY" + streak_text,
        reply_markup=main_menu(),
    )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Profilim")
async def profile(message: Message):
    save_user(message.from_user)
    user = get_user(message.from_user.id)

    if not user:
        await message.answer("Avval /start bosing.")
        return

    xp = user["xp"]
    level = xp // 100 + 1
    progress = xp % 100
    achievements = get_achievements(message.from_user.id)
    achievements_text = "\n".join(achievements) or "Hali yutuq yo‘q"

    await message.answer(
        "👤 PROFILINGIZ\n\n"
        f"⭐ XP: {xp}\n"
        f"🎚 Daraja: {level}\n"
        f"📈 Keyingi darajagacha: {100 - progress} XP\n"
        f"🎮 O‘yinlar: {user['games_played']}\n"
        f"🔥 Streak: {user['streak'] or 0} kun (rekord: {user['best_streak'] or 0})\n"
        f"💎 Premium: {'Faol' if premium_is_active(user) else 'Faol emas'}" + (f" · {user['premium_until'][:10]} gacha" if user['premium_until'] and premium_is_active(user) else "") + "\n\n"
        f"🏆 YUTUQLAR\n{achievements_text}",
        reply_markup=main_menu(),
    )


@dp.message(Command("rating"))
@dp.message(F.text == "🏆 Reyting")
async def rating(message: Message):
    rows = get_ranking()

    if not rows:
        await message.answer("Hali reyting bo‘sh.")
        return

    lines = ["🏆 TOP 10 REYTING\n"]

    for i, row in enumerate(rows, start=1):
        name = row["first_name"] or row["username"] or "O‘yinchi"
        lines.append(f"{i}. {name} — ⭐ {row['xp']} XP")

    await message.answer("\n".join(lines), reply_markup=main_menu())


@dp.message(F.text == "🎯 Missiyalar")
async def missions(message: Message):
    save_user(message.from_user)
    user = get_user(message.from_user.id)
    games = user["games_played"]
    xp = user["xp"]

    await message.answer(
        "🎯 MISSIYALAR\n\n"
        f"{'✅' if games >= 1 else '⬜'} Birinchi o‘yinni yakunlash\n"
        f"{'✅' if games >= 5 else '⬜'} 5 ta o‘yin o‘ynash\n"
        f"{'✅' if xp >= 100 else '⬜'} 100 XP to‘plash\n\n"
        f"🏆 Yutuqlar: {len(get_achievements(message.from_user.id))}/5",
        reply_markup=main_menu(),
    )


# =========================
# PREMIUM VA STREAK
# =========================

@dp.message(F.text == "💎 Premium")
@dp.message(Command("premium"))
async def premium_start(message: Message):
    save_user(message.from_user)
    user = get_user(message.from_user.id)
    status = "faol" if premium_is_active(user) else "faol emas"
    await message.answer(
        "💎 LOOPBREAK PREMIUM\n\n"
        f"Hozirgi holat: {status}\n\n"
        "Premium rejani tanlang. To‘lov Telegram Stars orqali amalga oshadi.\n"
        "⭐ 30 kun — 100 Stars\n⭐ 90 kun — 250 Stars\n⭐ 365 kun — 500 Stars",
        reply_markup=premium_keyboard(),
    )


@dp.callback_query(F.data.startswith("premium:"))
async def premium_plan_selected(callback: CallbackQuery):
    try:
        days = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Reja noto‘g‘ri.", show_alert=True)
        return
    if days not in PREMIUM_PLANS:
        await callback.answer("Bu reja mavjud emas.", show_alert=True)
        return
    await callback.answer()
    await send_premium_invoice(callback.message, days)


@dp.message(F.text == "🔥 Streak")
@dp.message(Command("streak"))
async def streak_status(message: Message):
    save_user(message.from_user)
    info = update_streak(message.from_user.id)
    text = f"🔥 Joriy seriya: {info['streak']} kun\n🏆 Eng yaxshi seriya: {info['best']} kun"
    if info["reward_xp"]:
        text += f"\n🎁 Bugungi mukofot: +{info['reward_xp']} XP"
    text += "\n\nMukofotlar: 3 kun — 25 XP, 7 kun — 50 XP, 14 kun — 100 XP, 30 kun — 250 XP."
    await message.answer(text, reply_markup=main_menu())


# =========================
# DONAT: MIQDOR VA XABAR
# =========================

def donation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"⭐ {amount} Stars",
                callback_data=f"donate:{amount}",
            )
        ]
        for amount in DONATION_AMOUNTS
    ])


@dp.message(F.text == "💌 Donat")
async def donation_start(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "💌 LOOPBREAK’NI QO‘LLAB-QUVVATLANG\n\n"
        "Loyihani rivojlantirishga yordam bering!\n"
        "Avval donat miqdorini tanlang. Keyin xabaringizni "
        "yozasiz va Telegram Stars orqali to‘laysiz.\n\n"
        "⭐ Miqdorni tanlang:",
        reply_markup=donation_keyboard(),
    )


@dp.callback_query(F.data.startswith("donate:"))
async def donation_amount_selected(
    callback: CallbackQuery,
    state: FSMContext,
):
    try:
        amount = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Miqdor noto‘g‘ri.", show_alert=True)
        return

    if amount not in DONATION_AMOUNTS:
        await callback.answer("Bu miqdor mavjud emas.", show_alert=True)
        return

    await state.update_data(donation_amount=amount)
    await state.set_state(DonationStates.waiting_for_note)

    await callback.answer()
    await callback.message.answer(
        f"⭐ Tanlangan donat: {amount} Stars\n\n"
        "Endi donatingiz bilan yuboriladigan xabarni yozing.\n"
        "Masalan: “LOOPBREAK rivojiga omad!”\n\n"
        "Bekor qilish uchun /cancel yozing."
    )


@dp.message(Command("cancel"))
async def cancel_donation(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Donat jarayoni bekor qilindi.",
        reply_markup=main_menu(),
    )


@dp.message(DonationStates.waiting_for_note, F.text)
async def donation_note_received(message: Message, state: FSMContext):
    note = message.text.strip()

    if not note:
        await message.answer("Iltimos, xabar yozing.")
        return

    if len(note) > 500:
        await message.answer(
            "Xabar juda uzun. Iltimos, 500 ta belgidan oshirmang."
        )
        return

    data = await state.get_data()
    amount = data.get("donation_amount")

    if amount not in DONATION_AMOUNTS:
        await state.clear()
        await message.answer("Donat jarayonini qaytadan boshlang.")
        return

    if not BOT_TOKEN:
        await state.clear()
        await message.answer("Bot sozlamalarida xatolik yuz berdi.")
        return

    payload = "donate_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO donations (
                payload, user_id, amount, note, status, created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (
            payload, message.from_user.id, amount, note, now,
        ))
        conn.commit()

    await state.clear()

    bot = message.bot

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="LOOPBREAK donati",
            description=f"Loyihani qo‘llab-quvvatlash — {amount} Stars",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f"LOOPBREAK donati ({amount} Stars)",
                    amount=amount,
                )
            ],
        )
        await message.answer(
            "💌 Xabaringiz saqlandi.\n"
            "To‘lovni yuqoridagi Telegram invoice orqali yakunlang.\n\n"
            "To‘lov tasdiqlanmaguncha donat muvaffaqiyatli "
            "deb hisoblanmaydi."
        )
    except Exception:
        logging.exception("Donat invoice yuborilmadi")
        await message.answer(
            "❌ To‘lov oynasini ochib bo‘lmadi. "
            "Iltimos, keyinroq qayta urinib ko‘ring."
        )


# Telegram to‘lovdan oldin payload, foydalanuvchi va summani tekshiradi.
@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    with db_connect() as conn:
        donation = conn.execute("SELECT user_id, amount, status FROM donations WHERE payload = ?", (query.invoice_payload,)).fetchone()
        premium = conn.execute("SELECT user_id, amount, status FROM premium_payments WHERE payload = ?", (query.invoice_payload,)).fetchone()

    record = donation if donation is not None else premium
    valid = (record is not None and record["user_id"] == query.from_user.id
             and record["amount"] == query.total_amount and query.currency == "XTR"
             and record["status"] == "pending")
    if not valid:
        await query.answer(ok=False, error_message="To‘lov ma’lumotlari tekshiruvdan o‘tmadi.")
        return
    await query.answer(ok=True)


# Faqat Telegram tasdiqlagan to‘lovdan keyin donat yuboriladi.
@dp.message(F.successful_payment)
async def payment_success(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        premium = conn.execute("SELECT * FROM premium_payments WHERE payload = ?", (payload,)).fetchone()
        donation = conn.execute("SELECT * FROM donations WHERE payload = ?", (payload,)).fetchone()

        if premium is not None:
            if (premium["user_id"] != message.from_user.id or premium["amount"] != payment.total_amount
                    or payment.currency != "XTR"):
                logging.error("Premium to‘lovi ma’lumotlari mos kelmadi.")
                await message.answer("To‘lovni tekshirishda muammo yuz berdi. Yordam uchun /help bosing.")
                return
            if premium["status"] == "paid":
                await message.answer("✅ Bu Premium to‘lovi avval qayd etilgan.")
                return
            user_row = conn.execute("SELECT premium_until FROM users WHERE user_id = ?", (message.from_user.id,)).fetchone()
            base = datetime.now(timezone.utc)
            if user_row and user_row["premium_until"]:
                try:
                    old_until = datetime.fromisoformat(user_row["premium_until"])
                    if old_until > base:
                        base = old_until
                except ValueError:
                    pass
            days = premium["days"]
            until = (base + timedelta(days=days)).isoformat()
            conn.execute("UPDATE premium_payments SET status='paid', paid_at=?, telegram_charge_id=? WHERE payload=? AND status='pending'", (now, payment.telegram_payment_charge_id, payload))
            conn.execute("UPDATE users SET premium=1, premium_until=? WHERE user_id=?", (until, message.from_user.id))
            conn.commit()
            await message.answer(f"🎉 Premium faollashtirildi!\n💎 Muddat: {days} kun\n📅 Tugash sanasi: {until[:10]}", reply_markup=main_menu())
            return

        if donation is None or donation["user_id"] != message.from_user.id or donation["amount"] != payment.total_amount or payment.currency != "XTR":
            logging.error("To‘lov payload topilmadi yoki ma’lumotlari mos kelmadi.")
            await message.answer("To‘lov qabul qilindi, lekin ma’lumotlarni tekshirishda muammo yuz berdi. /help bosing.")
            return
        if donation["status"] == "paid":
            await message.answer("✅ Bu donat to‘lovi avval qayd etilgan.")
            return
        conn.execute("UPDATE donations SET status='paid', paid_at=? WHERE payload=? AND status='pending'", (now, payload))
        conn.commit()

    await message.answer(f"🎉 Rahmat! Donatingiz muvaffaqiyatli amalga oshdi.\n\n⭐ Miqdor: {donation['amount']} Stars\n💌 Xabaringiz yaratuvchiga yuboriladi.", reply_markup=main_menu())
    if OWNER_ID:
        user = message.from_user
        username = f"@{user.username}" if user.username else "username yo‘q"
        owner_text = ("💌 YANGI DONAT — LOOPBREAK\n\n" f"⭐ Miqdor: {donation['amount']} Stars\n" f"👤 Ism: {user.full_name}\n" f"🔹 Username: {username}\n" f"🆔 Telegram ID: {user.id}\n\n" f"💬 Xabar:\n{donation['note']}")
        try:
            await message.bot.send_message(OWNER_ID, owner_text)
        except Exception:
            logging.exception("Yaratuvchiga donat xabari yuborilmadi.")
    else:
        logging.warning("OWNER_ID .env faylida sozlanmagan; donat xabari yaratuvchiga yuborilmadi.")

# =========================
# YORDAM VA O‘YIN NATIJALARI
# =========================

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def help_message(message: Message):
    await message.answer(
        "ℹ️ YORDAM\n\n"
        "🎮 O‘yin — Mini App’ni ochish\n"
        "👤 Profilim — XP va yutuqlar\n"
        "🏆 Reyting — TOP 10\n"
        "🎯 Missiyalar — vazifalar\n"
        "💎 Premium — Stars orqali Premium olish\n"
        "🔥 Streak — ketma-ket kunlar va XP mukofotlari\n"
        "💌 Donat — loyihani Stars orqali qo‘llab-quvvatlash\n"
        "/start — asosiy menyu",
        reply_markup=main_menu(),
    )


@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    save_user(message.from_user)

    try:
        data = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        await message.answer("❌ Natijani o‘qib bo‘lmadi.")
        return

    if not isinstance(data, dict):
        await message.answer("❌ Noto‘g‘ri so‘rov formati.")
        return

    request_type = data.get("type")
    if request_type == "premium_request":
        await message.answer("💎 Premium muddatini tanlang:", reply_markup=premium_keyboard())
        return
    if request_type == "streak_checkin":
        info = update_streak(message.from_user.id)
        text = f"🔥 Joriy Streak: {info['streak']} kun\n🏆 Rekord: {info['best']} kun"
        if info["reward_xp"]:
            text += f"\n🎁 Mukofot: +{info['reward_xp']} XP"
        text += "\n\nHar kuni botga kirib seriyangizni saqlang."
        await message.answer(text, reply_markup=main_menu())
        return
    if request_type != "game_result":
        await message.answer("❌ Noma’lum so‘rov turi.")
        return

    result_id = data.get("result_id")

    if not isinstance(result_id, str) or not result_id.strip():
        await message.answer("⚠️ Natija identifikatori topilmadi.")
        return

    try:
        score = int(data.get("score", 0))
    except (ValueError, TypeError, OverflowError):
        await message.answer("❌ Ball noto‘g‘ri.")
        return

    score = max(0, min(score, 200))
    ending = str(data.get("ending", "Noma'lum"))[:100]
    result_id = result_id[:100]

    streak_info = update_streak(message.from_user.id)
    saved, xp_earned, unlocked = save_game_result(
        message.from_user.id,
        ending,
        score,
        result_id,
    )

    if not saved:
        await message.answer("ℹ️ Bu natija oldin saqlangan.")
        return

    user = get_user(message.from_user.id)
    level = user["xp"] // 100 + 1 if user else 1

    text = (
        "✅ NATIJA SAQLANDI!\n\n"
        f"🏁 Yakun: {ending}\n"
        f"🎯 Ball: {score}\n"
        f"⭐ Qo‘shilgan XP: +{xp_earned}\n"
        f"🎚 Daraja: {level}\n"
        f"🔥 Streak: {streak_info['streak']} kun"
        + (f"\n🎁 Streak mukofoti: +{streak_info['reward_xp']} XP" if streak_info['reward_xp'] else "")
    )

    if unlocked:
        text += "\n\n🏆 YANGI YUTUQLAR!\n" + "\n".join(unlocked)

    await message.answer(text, reply_markup=main_menu())


@dp.message()
async def other_messages(message: Message):
    await message.answer(
        "Menyudan kerakli bo‘limni tanlang 👇",
        reply_markup=main_menu(),
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            ".env faylida BOT_TOKEN topilmadi. Tokenni tekshiring."
        )

    init_db()
    bot = Bot(token=BOT_TOKEN)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("LOOPBREAK bot ishga tushdi!")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
