import os
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.environ["BOT_TOKEN"]
MONGODB_URL  = os.environ["MONGODB_URL"]          # mongodb+srv://...
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")
PORT         = int(os.environ.get("PORT", 8080))

CHANNEL_1_USERNAME = "@ucplanet"
CHANNEL_2_ID       = -1003934812939
CHANNEL_2_LINK     = "https://t.me/+FZ4aRhgmrvQ1ZmI6"
CHANNEL_3_ID       = -1003999645745
CHANNEL_3_LINK     = "https://t.me/+e6xEfcq-pkk0NWVi"
PRIZE_CHANNEL_ID   = -1003822385223
BOT_USERNAME       = "ucfoydabot"
ADMIN_ID           = 5523761749
REQUIRED_INVITES   = 2

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── MongoDB ──────────────────────────────────────────────────────────────────

_client: MongoClient = None
_users: Collection   = None
_requests: Collection = None


def init_db():
    global _client, _users, _requests
    _client   = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=10000)
    db        = _client.get_default_database(default="botdb")
    _users    = db["users"]
    _requests = db["join_requests"]

    # indexes
    _users.create_index("telegram_id", unique=True)
    _requests.create_index(
        [("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True
    )
    logger.info("MongoDB connected")


# ─── DB helpers ───────────────────────────────────────────────────────────────

def upsert_user(telegram_id: int, username: Optional[str],
                first_name: str, invited_by: Optional[int] = None):
    existing = _users.find_one({"telegram_id": telegram_id})
    if existing:
        update = {"$set": {"username": username, "first_name": first_name}}
        # Only set invited_by if it was never set
        if existing.get("invited_by") is None and invited_by is not None:
            update["$set"]["invited_by"] = invited_by
        _users.update_one({"telegram_id": telegram_id}, update)
    else:
        _users.insert_one({
            "telegram_id":    telegram_id,
            "username":       username,
            "first_name":     first_name,
            "started_at":     datetime.now(timezone.utc),
            "is_verified":    False,
            "invited_by":     invited_by,
            "referral_count": 0,
            "join_link_sent": False,
        })


def get_user(telegram_id: int) -> Optional[dict]:
    return _users.find_one({"telegram_id": telegram_id})


def set_verified(telegram_id: int):
    _users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"is_verified": True}}
    )


def set_join_link_sent(telegram_id: int):
    _users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"join_link_sent": True}}
    )


def increment_referral_count(telegram_id: int) -> int:
    result = _users.find_one_and_update(
        {"telegram_id": telegram_id},
        {"$inc": {"referral_count": 1}},
        return_document=True,
    )
    return result["referral_count"] if result else 0


def record_join_request(user_id: int, chat_id: int):
    try:
        _requests.insert_one({
            "user_id":      user_id,
            "chat_id":      chat_id,
            "requested_at": datetime.now(timezone.utc),
        })
    except Exception:
        pass  # duplicate — unique index, ignore


def has_join_request(user_id: int, chat_id: int) -> bool:
    return _requests.find_one({"user_id": user_id, "chat_id": chat_id}) is not None


def get_total_user_count() -> int:
    return _users.count_documents({})


def get_all_user_ids() -> list:
    return [u["telegram_id"] for u in _users.find({}, {"telegram_id": 1})]


def clear_all_referrals() -> int:
    total = _users.count_documents({})
    _requests.delete_many({})
    _users.update_many({}, {"$set": {
        "is_verified":    False,
        "referral_count": 0,
        "join_link_sent": False,
        "invited_by":     None,
    }})
    return total


# ─── Bot helpers ──────────────────────────────────────────────────────────────

def ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 1-Kanal | @ucplanet",
                              url="https://t.me/ucplanet")],
        [InlineKeyboardButton("🔐 2-Kanal | Qo'shilish so'rovi yuboring",
                              url=CHANNEL_2_LINK)],
        [InlineKeyboardButton("🔐 3-Kanal | Qo'shilish so'rovi yuboring",
                              url=CHANNEL_3_LINK)],
        [InlineKeyboardButton("✅ Tekshirish", callback_data="check_subs")],
    ])


async def is_channel1_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_1_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def send_prize_link(bot, user_id: int):
    try:
        link = await bot.create_chat_invite_link(
            PRIZE_CHANNEL_ID,
            member_limit=1,
            name=f"winner_{user_id}",
        )
        set_join_link_sent(user_id)
        await bot.send_message(
            user_id,
            f"🏆 <b>TABRIKLAYMIZ!</b> Siz {REQUIRED_INVITES} ta do'stingizni "
            f"taklif qildingiz!\n\n"
            f"🎁 <b>Maxsus kanalga kirish uchun sizning 1 martalik havolangiz:</b>\n\n"
            f"🔗 {link.invite_link}\n\n"
            f"⚠️ <i>Bu havola faqat 1 marta ishlaydi — hech kim bilan ulashmang!</i>\n"
            f"🎮 <b>Yaxshi o'yin!</b> 💎",
            parse_mode="HTML",
        )
        logger.info(f"Prize link sent → user {user_id}")
    except Exception as e:
        logger.error(f"send_prize_link failed for {user_id}: {e}")


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    invited_by = None

    if context.args and context.args[0].startswith("ref_"):
        try:
            ref_id = int(context.args[0][4:])
            if ref_id != user.id:
                invited_by = ref_id
        except ValueError:
            pass

    upsert_user(user.id, user.username, user.first_name, invited_by)

    await update.message.reply_html(
        "🎮 <b>PUBG UC Konkursiga xush kelibsiz!</b> 🏆\n\n"
        "💎 <b>100 UC</b> yutib olish imkoniyatini qo'ldan boy bermang!\n\n"
        "📋 <b>Ishtirok etish uchun:</b>\n"
        "✅ 1-Kanalga obuna bo'ing\n"
        "✅ 2 va 3-kanalga qo'shilish so'rovi yuboring\n\n"
        "⬇️ <i>Quyidagi kanallarga o'ting, so'ng \"Tekshirish\" tugmasini bosing:</i>",
        reply_markup=subscription_keyboard(),
    )


async def check_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🔍 Tekshirilmoqda...")
    user = update.effective_user

    db_user = get_user(user.id)
    if not db_user:
        await query.message.reply_text("❗ Iltimos, /start buyrug'ini yuboring.")
        return

    if db_user["is_verified"]:
        count = db_user["referral_count"]
        await query.message.reply_html(
            f"✅ <b>Siz allaqachon ro'yxatdan o'tgansiz!</b>\n\n"
            f"🔗 Sizning shaxsiy havolangiz:\n"
            f"{ref_link(user.id)}\n\n"
            f"👥 Taklif qilganlar: <b>{count}</b> / {REQUIRED_INVITES}"
        )
        return

    ch1ok = await is_channel1_member(context.bot, user.id)
    ch2ok = has_join_request(user.id, CHANNEL_2_ID)
    ch3ok = has_join_request(user.id, CHANNEL_3_ID)

    if not (ch1ok and ch2ok and ch3ok):
        lines = ["❌ <b>Barcha shartlar bajarilmagan!</b>\n"]
        lines.append(
            ("✅" if ch1ok else "❌") + " 1-Kanal (@ucplanet) — " +
            ("Obuna bo'lgansiz" if ch1ok else "Obuna bo'lmadingiz")
        )
        lines.append(
            ("✅" if ch2ok else "❌") + " 2-Kanal — " +
            ("So'rov yuborgansiz" if ch2ok else "So'rov yubormagansiz")
        )
        lines.append(
            ("✅" if ch3ok else "❌") + " 3-Kanal — " +
            ("So'rov yuborgansiz" if ch3ok else "So'rov yubormagansiz")
        )
        lines.append("\n📌 <i>Barcha amallarni bajaring va qayta tekshiring.</i>")
        await query.message.reply_html(
            "\n".join(lines), reply_markup=subscription_keyboard()
        )
        return

    set_verified(user.id)

    # Credit inviter now that this user has fully verified
    inviter_id = db_user.get("invited_by")
    if inviter_id:
        inviter = get_user(inviter_id)
        if inviter and not inviter.get("join_link_sent"):
            new_count = increment_referral_count(inviter_id)
            try:
                await context.bot.send_message(
                    inviter_id,
                    f"👥 Do'stingiz tasdiqdan o'tdi! "
                    f"Taklif: <b>{new_count}</b> / {REQUIRED_INVITES}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            if new_count >= REQUIRED_INVITES:
                await send_prize_link(context.bot, inviter_id)

    await query.message.reply_html(
        f"🎉 <b>BARAKALLA! Barcha shartlarni bajardingiz!</b>\n\n"
        f"🤝 Konkursda <b>g'olib</b> bo'lish uchun "
        f"<b>{REQUIRED_INVITES} ta do'stingizni</b> taklif qiling!\n\n"
        f"🔗 <b>Sizning shaxsiy havolangiz:</b>\n"
        f"{ref_link(user.id)}\n\n"
        f"💡 <i>Bu havolani do'stlaringizga yuboring. Ular botni ishga tushirib, "
        f"kanallarni tasdiqlashlari bilanoq siz mukofot olasiz!</i>"
    )


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req     = update.chat_join_request
    user_id = req.from_user.id
    chat_id = req.chat.id
    if chat_id in (CHANNEL_2_ID, CHANNEL_3_ID):
        record_join_request(user_id, chat_id)
        logger.info(f"Join request: user={user_id} chat={chat_id}")


async def odam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    count = get_total_user_count()
    await update.message.reply_html(
        f"👥 <b>Bot foydalanuvchilari statistikasi</b>\n\n"
        f"📊 Jami botni boshlagan: <b>{count}</b> ta foydalanuvchi"
    )


async def xabar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text("❗ Xabar matni kiriting: /xabar <matn>")
        return

    user_ids = get_all_user_ids()
    await update.message.reply_text(
        f"📤 Xabar yuborilmoqda... {len(user_ids)} ta foydalanuvchiga"
    )
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(
                uid, f"📣 <b>E'lon</b>\n\n{text}", parse_mode="HTML"
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.035)

    await update.message.reply_html(
        f"✅ <b>Xabar yuborildi!</b>\n\n"
        f"📨 Muvaffaqiyatli: <b>{sent}</b>\n"
        f"❌ Yuborilmadi: <b>{failed}</b>"
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Tozalanmoqda...")
    total = clear_all_referrals()
    await update.message.reply_html(
        f"✅ <b>Tozalash tugadi!</b>\n\n"
        f"🗑 Barcha referrallar, tasdiqlashlar nolga qaytarildi.\n"
        f"👤 Ta'sirlangan foydalanuvchilar: <b>{total}</b>"
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Python 3.10+ no longer auto-creates an event loop — set one explicitly
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_subs, pattern="^check_subs$"))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CommandHandler("odam", odam_command))
    app.add_handler(CommandHandler("xabar", xabar_command))
    app.add_handler(CommandHandler("clear", clear_command))

    if WEBHOOK_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        full_url     = f"{WEBHOOK_URL}{webhook_path}"
        logger.info(f"Webhook mode → {full_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=full_url,
            url_path=webhook_path,
            drop_pending_updates=True,
        )
    else:
        logger.info("Polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
