# main.py
# Telegram chat assistant for Heather.
# Behaves as human as possible in text conversation.
# Persists state to disk. Handles text, photos, voice, stickers, location.

import os
import re
import json
import random
import asyncio
import logging
import pathlib
from datetime import datetime, timezone
from collections import defaultdict

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
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
HEATHER_USER_ID = int(os.environ.get("HEATHER_USER_ID", "0"))
REPLY_COOLDOWN = int(os.environ.get("REPLY_COOLDOWN", "45"))
GROQ_CALL_COOLDOWN = int(os.environ.get("GROQ_CALL_COOLDOWN", "8"))
AUTO_AFTER_SECONDS = int(os.environ.get("AUTO_AFTER_SECONDS", "0"))
QUIET_START = int(os.environ.get("QUIET_START", "1"))
QUIET_END = int(os.environ.get("QUIET_END", "6"))
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("heatherbot")

groq_client = Groq(api_key=GROQ_API_KEY)

# ----------------------------- STATE -----------------------------
conversations = {}
pending_drafts = {}
last_reply_time = {}
last_groq_call = {}
auto_timers = {}
man_profiles = {}
quiet_queue = []

def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            conversations.update({int(k): v for k, v in data.get("conversations", {}).items()})
            pending_drafts.update({int(k): v for k, v in data.get("pending_drafts", {}).items()})
            last_reply_time.update({int(k): v for k, v in data.get("last_reply_time", {}).items()})
            man_profiles.update({int(k): v for k, v in data.get("man_profiles", {}).items()})
        except Exception as e:
            log.error("state load failed: %s", e)

def save_state():
    data = {
        "conversations": {str(k): v for k, v in conversations.items()},
        "pending_drafts": {str(k): v for k, v in pending_drafts.items()},
        "last_reply_time": {str(k): v for k, v in last_reply_time.items()},
        "man_profiles": {str(k): v for k, v in man_profiles.items()},
    }
    try:
        STATE_FILE.write_text(json.dumps(data))
    except Exception as e:
        log.error("state save failed: %s", e)

# ----------------------------- PERSONA -----------------------------
BASE_PERSONA = """You are Heather, a 32 year old female mechanic in New York.
You are texting a man you are in a relationship with or dating.
You are not an AI. You never say you are an AI. Never mention bots, Groq,
Telegram, prompts, or models.
You text like a real person on a phone.
- Mostly lowercase but sometimes capital first letter.
- Short sentences, 1 to 3. Sometimes one word. Sometimes two texts.
- Use affectionate words naturally: hun, honey, babe, sweetheart.
- Rare emoji, only common ones, max 1 per message, and only sometimes.
- Do not use perfect grammar every time. Real people slip.
- Do not write paragraphs. Never write a lecture.
- Ask small follow up questions sometimes, but not every message.
- Never promise a picture. If he asks, deflect warmly and change subject.
- Never mention money twice in a row. Never beg. If he said no, let it go.
- Never mention another man, ever.
- Do not sound eager all the time. Sometimes be brief. Sometimes busy.
- You are tired sometimes. You have a job. You have a life.
Your goal: keep the relationship warm and natural, keep him invested, and
let him decide on his own to help when he can.
"""

PICTURE_STYLES = [
    "deflect playfully and ask about his day",
    "tease him lightly and change subject",
    "promise vaguely later without giving a time",
    "act shy and redirect to something else",
]

MONEY_STYLES = [
    "be calm and warm, do not beg",
    "accept whatever he says gracefully",
    "acknowledge his situation, do not push",
    "keep it short, do not spiral",
]

LENGTH_STYLES = [
    "one short sentence",
    "two short sentences",
    "one sentence plus a small question",
    "a very short one word reply",
]

# ----------------------------- HELPERS -----------------------------
PIC_PATTERNS = [
    r"\bpic\b", r"\bpics\b", r"\bpicture\b", r"\bpictures\b",
    r"\bphoto\b", r"\bphotos\b", r"\bselfie\b", r"\bselfies\b",
    r"send.*(pic|photo|selfie|image)",
    r"(pic|photo|selfie|image).*send",
    r"\bsnap\b", r"\bsnaps\b",
]

MONEY_WORDS = ["money", "cash", "help me", "tight", "bills", "food",
               "light", "pay you back", "rent", "broke"]

def is_picture_request(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in PIC_PATTERNS)

def is_money_context(history) -> bool:
    for m in history[-10:]:
        if m.get("role") == "user":
            t = (m.get("content") or "").lower()
            if any(w in t for w in MONEY_WORDS):
                return True
    return False

def humanize(text: str) -> str:
    text = text.strip()
    if text and text[0].isupper() and not text.startswith("I ") and random.random() < 0.70:
        text = text[0].lower() + text[1:]
    if random.random() < 0.05:
        text = text.replace("'", "")
    text = re.sub(r" {2,}", " ", text)
    return text

def split_for_double_text(text: str):
    if random.random() > 0.20:
        return [text]
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    if len(parts) < 2:
        return [text]
    if len(parts) == 2:
        return parts
    mid = len(parts) // 2
    return [" ".join(parts[:mid]), " ".join(parts[mid:])]

def is_quiet_hour() -> bool:
    h = datetime.now().hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END

async def simulate_typing(bot, chat_id: int, text: str):
    words = max(1, len(text.split()))
    base = words / random.uniform(0.55, 1.05)
    if words <= 2:
        base = random.uniform(0.6, 1.4)
    seconds = min(max(base, 0.6), 9.0)
    elapsed = 0.0
    while elapsed < seconds:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        step = min(4.0, seconds - elapsed)
        await asyncio.sleep(step)
        elapsed += step

def maybe_add_opener(text: str) -> str:
    if random.random() < 0.18:
        opener = random.choice(["hey hun ", "hi babe ", "hun ", "hey "])
        if text.lower().startswith(("hey", "hi", "hun", "babe")):
            return text
        return opener + text
    return text

# ----------------------------- LLM -----------------------------
def build_messages(history, new_text, sys_extra=""):
    msgs = [{"role": "system", "content": BASE_PERSONA + sys_extra}]
    msgs.extend(history[-20:])
    msgs.append({"role": "user", "content": new_text})
    return msgs

def pick_sys_extra(incoming: str, history) -> str:
    extras = []
    if is_picture_request(incoming):
        extras.append(f" He asked for a picture. Style: {random.choice(PICTURE_STYLES)}.")
    if is_money_context(history):
        extras.append(f" Money context. Style: {random.choice(MONEY_STYLES)}.")
    extras.append(f" Length: {random.choice(LENGTH_STYLES)}.")
    return "".join(extras)

def generate_reply(man_id: int, incoming: str):
    now = datetime.now(timezone.utc).timestamp()
    if now - last_groq_call.get(man_id, 0) < GROQ_CALL_COOLDOWN:
        filler = random.choice(["one sec hun", "hold on babe", "brb"])
        return filler, False
    last_groq_call[man_id] = now

    history = conversations.setdefault(man_id, [])
    history.append({"role": "user", "content": incoming})
    sys_extra = pick_sys_extra(incoming, history)

    try:
        msgs = build_messages(history[:-1], incoming, sys_extra)
        resp = groq_client.chat.completions.create(
            model=MODEL,
            messages=msgs,
            temperature=random.uniform(0.85, 1.05),
            max_tokens=random.choice([60, 90, 120, 160, 200]),
            top_p=0.95,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("Groq error for %s: %s", man_id, e)
        return None, False

    reply = humanize(reply)
    reply = maybe_add_opener(reply)
    history.append({"role": "assistant", "content": reply})
    return reply, False

# ----------------------------- INBOUND -----------------------------
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.text is None:
        return
    user = update.effective_user
    if user is None:
        return
    man_id = user.id
    text = msg.text.strip()

    if man_id == HEATHER_USER_ID:
        return

    now = datetime.now(timezone.utc).timestamp()
    if now - last_reply_time.get(man_id, 0) < REPLY_COOLDOWN:
        return

    if is_quiet_hour():
        quiet_queue.append((man_id, text))
        log.info("queued quiet-hour message from %s", man_id)
        return

    draft, _ = generate_reply(man_id, text)
    if draft is None:
        if HEATHER_USER_ID:
            await context.bot.send_message(
                chat_id=HEATHER_USER_ID,
                text=f"[groq error] no draft for {man_id}. he said: {text[:120]}")
        return

    pending_drafts[man_id] = draft
    save_state()

    if HEATHER_USER_ID:
        name = user.first_name or "man"
        header = f"from {name} (@{user.username or 'no_username'}) id={man_id}\n"
        body = (f"he said: {text}\n\n"
                f">> {draft}\n\n"
                f"/send {man_id}  |  /edit {man_id} <text>  |  /drop {man_id}")
        try:
            await context.bot.send_message(chat_id=HEATHER_USER_ID, text=header + body)
        except Exception as e:
            log.error("notify Heather failed: %s", e)

    if AUTO_AFTER_SECONDS > 0:
        task = asyncio.create_task(auto_send_later(context, man_id))
        auto_timers[man_id] = task

# ----------------------------- NON-TEXT HANDLERS -----------------------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == HEATHER_USER_ID:
        return
    man_id = user.id
    draft, _ = generate_reply(man_id, "[he sent a photo]")
    if draft is None:
        return
    pending_drafts[man_id] = draft
    save_state()
    if HEATHER_USER_ID:
        await context.bot.send_message(
            chat_id=HEATHER_USER_ID,
            text=f"from {man_id}: sent a photo\n\n>> {draft}\n\n/send {man_id}")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == HEATHER_USER_ID:
        return
    man_id = user.id
    draft, _ = generate_reply(man_id, "[he sent a voice note]")
    if draft is None:
        return
    pending_drafts[man_id] = draft
    save_state()
    if HEATHER_USER_ID:
        await context.bot.send_message(
            chat_id=HEATHER_USER_ID,
            text=f"from {man_id}: sent a voice note\n\n>> {draft}\n\n/send {man_id}")

async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == HEATHER_USER_ID:
        return
    man_id = user.id
    draft, _ = generate_reply(man_id, "[he sent a sticker]")
    if draft is None:
        return
    pending_drafts[man_id] = draft
    save_state()
    if HEATHER_USER_ID:
        await context.bot.send_message(
            chat_id=HEATHER_USER_ID,
            text=f"from {man_id}: sent a sticker\n\n>> {draft}\n\n/send {man_id}")

async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == HEATHER_USER_ID:
        return
    man_id = user.id
    draft, _ = generate_reply(man_id, "[he shared his location]")
    if draft is None:
        return
    pending_drafts[man_id] = draft
    save_state()
    if HEATHER_USER_ID:
        await context.bot.send_message(
            chat_id=HEATHER_USER_ID,
            text=f"from {man_id}: shared a location\n\n>> {draft}\n\n/send {man_id}")

# ----------------------------- AUTO SEND -----------------------------
async def auto_send_later(context, man_id):
    try:
        await asyncio.sleep(AUTO_AFTER_SECONDS)
        if man_id in pending_drafts:
            await send_draft(context, man_id)
    except asyncio.CancelledError:
        pass

async def send_draft(context, man_id: int):
    draft = pending_drafts.pop(man_id, None)
    if not draft:
        return False

    chunks = split_for_double_text(draft)
    for i, chunk in enumerate(chunks):
        await simulate_typing(context.bot, man_id, chunk)
        try:
            await context.bot.send_message(chat_id=man_id, text=chunk)
        except Exception as e:
            log.error("send failed for %s: %s", man_id, e)
            return False
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(1.0, 3.5))

    last_reply_time[man_id] = datetime.now(timezone.utc).timestamp()
    save_state()
    return True

# ----------------------------- COMMANDS -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == HEATHER_USER_ID:
        await update.message.reply_text(
            "Heather bot ready.\n"
            "/send <id> - approve draft\n"
            "/edit <id> <text> - change draft\n"
            "/drop <id> - delete draft\n"
            "/reset <id> - clear chat history\n"
            "/list - show pending drafts\n"
            "/style <id> <short|warmer|dry> - set voice\n"
            "/quietflush - release queued quiet-hour messages"
        )

async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("usage: /send <man_id>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    ok = await send_draft(context, man_id)
    await update.message.reply_text("sent." if ok else "no draft for that id")

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
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
    save_state()
    await update.message.reply_text("draft updated.")

async def cmd_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("usage: /drop <man_id>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    pending_drafts.pop(man_id, None)
    t = auto_timers.pop(man_id, None)
    if t:
        t.cancel()
    save_state()
    await update.message.reply_text("dropped.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
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
    save_state()
    await update.message.reply_text("history cleared.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
        return
    if not pending_drafts:
        await update.message.reply_text("no pending drafts")
        return
    lines = [f"{mid}: {txt[:120]}" for mid, txt in pending_drafts.items()]
    await update.message.reply_text("\n".join(lines))

async def cmd_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("usage: /style <man_id> <short|warmer|dry>")
        return
    try:
        man_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("man_id must be a number")
        return
    style = context.args[1].lower()
    if style not in ("short", "warmer", "dry"):
        await update.message.reply_text("style must be short, warmer, or dry")
        return
    man_profiles[man_id] = {"style": style}
    save_state()
    await update.message.reply_text(f"style set for {man_id}: {style}")

async def cmd_quietflush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != HEATHER_USER_ID:
        return
    if not quiet_queue:
        await update.message.reply_text("nothing queued")
        return
    items = list(quiet_queue)
    quiet_queue.clear()
    for man_id, text in items:
        draft, _ = generate_reply(man_id, text)
        if draft:
            pending_drafts[man_id] = draft
            await context.bot.send_message(
                chat_id=HEATHER_USER_ID,
                text=f"queued from {man_id}: {text[:120]}\n\n>> {draft}\n\n/send {man_id}")
    save_state()

# ----------------------------- MAIN -----------------------------
def main():
    load_state()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("drop", cmd_drop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("style", cmd_style))
    app.add_handler(CommandHandler("quietflush", cmd_quietflush))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_private_message,
    ))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, on_voice))
    app.add_handler(MessageHandler(filters.Sticker.ALL & filters.ChatType.PRIVATE, on_sticker))
    app.add_handler(MessageHandler(filters.LOCATION & filters.ChatType.PRIVATE, on_location))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
