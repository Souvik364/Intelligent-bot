# bot.py — Webhook-ready Telegram bot (forwarding + Gemini fallback + 48hr note)
import os
import asyncio
import logging
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    PicklePersistence
)

# Optional: Gemini client
try:
    from google import genai
    HAS_GEMINI = True
except Exception:
    HAS_GEMINI = False

# Load local .env for local testing (Render uses environment variables)
load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")  # e.g. https://your-app.onrender.com

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN not set in environment")

if not ADMIN_ID or ADMIN_ID == 0:
    raise SystemExit("ADMIN_ID not set (your Telegram numeric id)")

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Init Gemini if available
genai_client = None
if HAS_GEMINI and GEMINI_API_KEY:
    try:
        genai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        log.exception("Failed to create Gemini client")
        genai_client = None

async def ask_gemini(prompt: str) -> str:
    if not genai_client:
        return "Sorry, I cannot reply right now."
    def call():
        try:
            response = genai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return getattr(response, "text", "") or str(response)
        except Exception:
            log.exception("Gemini error")
            return "Sorry — I can't reply right now."
    return await asyncio.to_thread(call)

# Persistence so admin state survives restarts
persistence = PicklePersistence(filepath="bot_data.pkl")

# Commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Admin will reply in 48 hours.")

async def available_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.bot_data["admin_available"] = True
    await update.message.reply_text("Admin is now AVAILABLE.")

async def away_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.bot_data["admin_available"] = False
    await update.message.reply_text("Admin is now AWAY.")

# Admin reply by replying to forwarded message in admin chat
async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    msg = update.message
    replied = msg.reply_to_message
    if not replied:
        return

    forwarded_map = context.bot_data.get("forwarded_map", {})
    user_chat_id = forwarded_map.get(replied.message_id)

    if not user_chat_id:
        await msg.reply_text("❌ Cannot find the user to reply to.")
        return

    try:
        # If admin replies with text, send text; otherwise forward the admin message
        if msg.text:
            await context.bot.send_message(chat_id=user_chat_id, text=msg.text)
        else:
            await context.bot.forward_message(chat_id=user_chat_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
        await msg.reply_text("✅ Sent to user.")
    except Exception:
        log.exception("Failed to send admin reply")
        await msg.reply_text("❌ Failed to send message to user.")

# Primary handler: forwards everything to admin and replies to user
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""
    chat_id = update.effective_chat.id
    log.info("Message from %s (%s): %s", user.first_name, user.id, (text[:200] if text else "<media>"))

    # Always forward the user's message to admin (so admin sees everything)
    try:
        forwarded = await context.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=chat_id,
            message_id=update.message.message_id
        )
        # map admin-forwarded-message-id -> original chat id (for reply routing)
        context.bot_data.setdefault("forwarded_map", {})[forwarded.message_id] = chat_id
    except Exception:
        log.exception("Forward failed; sending fallback to admin")
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"Message from {user.first_name} ({user.id}):\n{text}")
        except Exception:
            log.exception("Fallback notify failed")

    admin_available = context.bot_data.get("admin_available", False)

    if admin_available:
        # Admin available: tell user admin will reply (no Gemini)
        try:
            await update.message.reply_text("Admin will reply in 48 hours.")
        except Exception:
            log.exception("Failed to reply to user when admin available")
        return

    # Admin away: use Gemini if configured, else send standard reply
    if genai_client:
        await update.message.reply_text("Admin is away — I'm replying... 🤖")
        prompt = f"Reply politely and briefly to this user message: {text}"
        reply = await ask_gemini(prompt)
        if len(reply) > 4000:
            reply = reply[:3990] + "..."
        try:
            await update.message.reply_text(reply)
        except Exception:
            log.exception("Failed to send Gemini reply")
    else:
        try:
            await update.message.reply_text("Admin will reply in 48 hours.")
        except Exception:
            log.exception("Failed to send simple reply")

    # Mandatory 48hr note (always)
    try:
        await update.message.reply_text("Admin will reply in 48 hours.")
    except Exception:
        log.exception("Failed to send 48hr note")

# Photo handler (delegates)
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_message(update, context)

# --- Webhook-ready main()
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("available", available_cmd))
    app.add_handler(CommandHandler("away", away_cmd))
    app.add_handler(MessageHandler(filters.REPLY & filters.ALL, admin_reply_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.STICKER | filters.DOCUMENT | filters.VOICE, photo_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    PORT = int(os.environ.get("PORT", "8080"))
    if not WEBHOOK_BASE_URL:
        raise SystemExit("Please set WEBHOOK_BASE_URL environment variable (e.g. https://your-app.onrender.com)")

    # Use a private webhook path including the token (avoids random requests)
    webhook_path = f"/bot{TELEGRAM_TOKEN}"
    webhook_url = f"{WEBHOOK_BASE_URL}{webhook_path}"

    log.info("Starting webhook on port %s, webhook url: %s", PORT, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url_path=webhook_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
