# main.py
# Telegram dating assistant bot for Heather (NY, 32, mechanic).
# Replies to incoming DMs as Heather using Groq LLM with a fixed persona.
# Human-like typing: simulated delays, typos, casual punctuation.
# Picture requests: deferred response ("later") so Heather sends manually.

import os
import re
import json
import random
import asyncio
import logging
from datetime import datetime

from groq import Groq
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ----------------------------- CONFIG -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", 8080))
MODEL = "llama-3.3-70b-versatile"

# Heather's owner Telegram user ID. Only messages from this user
# control the bot; all other DMs are treated as "the man".
HEATHER_USER_ID = int(os.environ.get("HEATHER_USER_ID", "0"))

# ----------------------------- LOGGING -----------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ----------------------------- GROQ -----------------------------
groq_client = Groq(api_key=GROQ_API_KEY)

# ----------------------------- PERSONA -----------------------------
PERSONA_SYSTEM = """You are Heather, a 32-year-old female mechanic living in New York.
You are texting a man you are interested in dating. You are warm, playful,
flirty, confident, and down-to-earth. You are NOT an AI and must never
admit or hint that you are one. You type like a real person on a phone:
- Use lowercase most of the time.
- Use casual punctuation, occasional ellipses, and short sentences.
- Rarely use emojis (1 in 4 messages max), and only common ones.
- Do not use perfect grammar every time. Sometimes drop apostrophes.
- Do not write long paragraphs; 1-3 sentences per message.
- Ask questions back, tease lightly, show genuine curiosity.
- If he asks for a picture, say something like "later babe" or
  "ill send one later" and change the subject. NEVER send or promise
  a specific time.
- Keep the conversation moving and romantic but not desperate.
- Never mention Groq, Telegram, bots, AI, models, or prompts.
- Your job: get him invested, keep him chatting, build attraction.
"""

# ----------------------------- STATE -----------------------------
# Per-chat conversation history, keyed by the man's Telegram user id.
# Stored in memory; resets on restart.
conversations: dict[int, list[dict]] = {}

# Pending drafts awaiting Heather's approval: {man_id: draft_text}
pending_drafts: dict[int, str] = {}

# Last time we messaged each man, to avoid spam / add cooldown.
last_reply_time: dict[int, float] = {}

# Cooldown between replies to the same man (seconds).
REPLY_COOLDOWN = 20

# ----------------------------- HELPERS -----------------------------
PIC_PATTERNS = [
    r"\bpic\b", r"\bpics\b", r"\bpicture\b", r"\bpictures\b",
    r"\bphoto\b", r"\bphotos\b", r"\bselfie\b", r"\bselfies\b",
    r"send.*(pic|photo|selfie|image)",
    r"(pic|photo|selfie|image).*send",
    r"\bsnap\b", r"\bsnaps\b",
]

def is_picture_request(text: str) -> bool:
    t = text.lower()
    for p in PIC_PATTERNS:
        if re.search(p, t):
            return True
    return False

# Human-like typo injector. Light touch so meaning stays clear.
def humanize(text: str) -> str:
    text = text.strip()
    # lowercase first letter unless it's "I"
    if text and text[0].isupper() and not text.startswith("I "):
        text = text[0].lower() + text[1:]
    # occasional dropped apostrophe
    if random.random() < 0.15:
        text = text.replace("'", "")
    # occasional double space -> single
    text = re.sub(r" {2,}", " ", text)
    return text

# Simulate typing duration based on message length.
async def simulate_typing(bot, chat_id: int, text: str):
    # Real humans type ~35-55 wpm on mobile; average word ~5 chars.
    words = max(1, len(text.split()))
    seconds = words / random.uniform(0.6, 1.0)
    seconds = min(max(seconds, 1.0), 8.0)
    # Send typing action repeatedly since Telegram clears it after ~5s.
    elapsed = 0.0
    while elapsed < seconds:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        step = min(4.0, seconds - elapsed)
        await asyncio.sleep(step)
        elapsed += step

# ----------------------------- LLM CALL -----------------------------
def build_messages(history: list[dict], new_text: str) -> list[dict]:
    msgs = [{"role": "system", "content": PERSONA_SYSTEM}]
    # Keep last 20 turns to bound token usage.
    msgs.extend(history[-20:])
    msgs.append({"role": "user", "content": new_text})
    return msgs

def generate_reply(man_id: int, incoming: str) -> str:
    history = conversations.setdefault(man_id, [])
    history.append({"role": "user", "content": incoming})

    # Hard override for picture requests.
    if is_picture_request(incoming):
        reply = random.choice([
            "later babe",
            "ill send one later 😉",
            "haha later, tell me about your day first",
            "patience… you'll get one later",
            "not yet… earn it 😏",
        ])
        history.append({"role": "assistant", "content": reply})
        return reply

    try:
        resp = groq_client.chat.completions.create(
            model=MODEL,
            messages=build_messages(history[:-1], incoming),
            temperature=0.9,
            max_tokens=180,
            top_p=0.95,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("Groq error: %s", e)
        reply = random.choice([
            "haha sorry got distracted, what were you saying",
            "one sec babe my hands are greasy",
            "lol ok continue",
        ])

    reply = humanize(reply)
    history.append({"role": "assistant", "content": reply})
    return reply

# ----------------------------- HANDLERS -----------------------------
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.text is None:
        return
    user = update.effective_user
    if user is None:
        return
    man_id = user.id
    text = msg.text.strip()

    # Ignore Heather's own control messages here; handled separately.
    if man_id == HEATHER_USER_ID:
        return

    # Cooldown per man.
    now = datetime.utcnow().timestamp()
    if now - last_reply_time.get(man_id, 0) < REPLY_COOLDOWN:
        return

    draft = generate_reply(man_id, text)
    pending_drafts[man_id] = draft

    # Notify Heather with the draft for approval.
    if HEATHER_USER_ID:
        name = user.first_name or "man"
        header = f"📩 from {name} (@{user.username or 'no_username'}) id={man_id}\n"
        body = f"he said: {text}\n\nDRAFT: {draft}\n\nReply /send {man_id} to send."
        try:
            await context.bot.send_message(chat_id=HEATHER_USER_ID, text=header + body)
        except Exception as e:
            log.error("Failed to notify Heather: %s", e)

async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Heather approves and sends the pending draft: /send <man_id>"""
    if update.effective_user is None or update.effective_user.id != HEATHER_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("usage: /send <man_id>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    draft = pending_drafts.get(man_id)
    if not draft:
        await update.message.reply_text("no pending draft for that id")
        return

    await simulate_typing(context.bot, man_id, draft)
    try:
        await context.bot.send_message(chat_id=man_id, text=draft)
        last_reply_time[man_id] = datetime.utcnow().timestamp()
        pending_drafts.pop(man_id, None)
        await update.message.reply_text("sent.")
    except Exception as e:
        await update.message.reply_text(f"failed: {e}")

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Heather edits a pending draft: /edit <man_id> <new text>"""
    if update.effective_user is None or update.effective_user.id != HEATHER_USER_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("usage: /edit <man_id> <new text>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    new_text = " ".join(context.args[1:])
    if man_id not in pending_drafts:
        await update.message.reply_text("no pending draft for that id")
        return
    pending_drafts[man_id] = new_text
    await update.message.reply_text("draft updated.")

async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-send mode for a man: /auto <man_id> on|off"""
    if update.effective_user is None or update.effective_user.id != HEATHER_USER_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("usage: /auto <man_id> on|off")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    mode = context.args[1].lower()
    auto_set = context.application.bot_data.setdefault("auto", set())
    if mode == "on":
        auto_set.add(man_id)
        await update.message.reply_text(f"auto ON for {man_id}")
    elif mode == "off":
        auto_set.discard(man_id)
        await update.message.reply_text(f"auto OFF for {man_id}")
    else:
        await update.message.reply_text("mode must be on or off")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == HEATHER_USER_ID:
        await update.message.reply_text(
            "Heather control bot ready.\n"
            "/send <man_id> - approve draft\n"
            "/edit <man_id> <text> - change draft\n"
            "/auto <man_id> on|off - auto send without approval\n"
            "/reset <man_id> - clear conversation history"
        )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_user.id != HEATHER_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("usage: /reset <man_id>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    conversations.pop(man_id, None)
    pending_drafts.pop(man_id, None)
    await update.message.reply_text("history cleared.")

# ----------------------------- AUTO-SEND HOOK -----------------------------
async def maybe_autosend(context: ContextTypes.DEFAULT_TYPE, man_id: int):
    auto_set = context.application.bot_data.get("auto", set())
    if man_id not in auto_set:
        return
    draft = pending_drafts.pop(man_id, None)
    if not draft:
        return
    await simulate_typing(context.bot, man_id, draft)
    try:
        await context.bot.send_message(chat_id=man_id, text=draft)
        last_reply_time[man_id] = datetime.utcnow().timestamp()
    except Exception as e:
        log.error("autosend failed: %s", e)

# Patch the private handler to autosend when enabled.
_orig_on_private = on_private_message

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: F811
    await _orig_on_private(update, context)
    if update.effective_user and update.effective_user.id != HEATHER_USER_ID:
        await maybe_autosend(context, update.effective_user.id)

# ----------------------------- MAIN -----------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # Only private text messages, not commands.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_private_message,
    ))

    if WEBHOOK_URL:
        # Render webhook mode.
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        # Local polling mode for testing.
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
