#!/usr/bin/env python3
"""
Telegram Poll Request Bot
Users send a native Telegram poll in the bot's DMs.
Admins review it with Accept / Reject buttons.
"""

import os
import uuid
import logging
import time
import asyncio
import json
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID = int(os.environ["ADMIN_GROUP_ID"])
PUBLIC_GROUP_ID = int(os.environ["PUBLIC_GROUP_ID"])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory store: poll_id → {user_id, question, options}
# ---------------------------------------------------------------------------
pending_polls: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Users who have started or used the bot. Persisted so broadcasts survive
# workflow restarts.
# ---------------------------------------------------------------------------
USERS_FILE = Path(__file__).with_name("users.json")


def load_known_users() -> set[int]:
    try:
        return {int(user_id) for user_id in json.loads(USERS_FILE.read_text())}
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("Could not read user registry: %s", e)
        return set()


def save_known_users() -> None:
    temporary_file = USERS_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(sorted(known_users)))
    temporary_file.replace(USERS_FILE)


known_users: set[int] = load_known_users()


def remember_private_user(update: Update) -> None:
    if (
        update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and update.effective_user.id not in known_users
    ):
        known_users.add(update.effective_user.id)
        save_known_users()

# ---------------------------------------------------------------------------
# Cooldown store: user_id → timestamp of last submission
# ---------------------------------------------------------------------------
COOLDOWN_SECONDS = 30
last_submission: dict[int, float] = {}
pending_broadcast_admins: set[int] = set()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_private_user(update)
    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Use /poll to learn how to submit a poll for review."
    )


# ---------------------------------------------------------------------------
# /poll — instructions
# ---------------------------------------------------------------------------
async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_private_user(update)
    await update.message.reply_text(
        "📊 *How to submit a poll:*\n\n"
        "1️⃣ Tap the 📎 *attachment* icon in this chat\n"
        "2️⃣ Select *Poll*\n"
        "3️⃣ Fill in your question and options\n"
        "4️⃣ Send it here\n\n"
        "Your poll will be sent to the admins for review. "
        "You'll get a DM when it's approved or rejected.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /chatid — debug helper (run inside a group to get its ID)
# ---------------------------------------------------------------------------
async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_private_user(update)
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"🆔 This chat's ID is:\n`{cid}`\n\n"
        f"Configured ADMIN\\_GROUP\\_ID: `{ADMIN_GROUP_ID}`\n"
        f"Configured PUBLIC\\_GROUP\\_ID: `{PUBLIC_GROUP_ID}`",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# User sends a poll in the DM → forward to admin group
# ---------------------------------------------------------------------------
async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Only accept polls sent in private chat (DMs)
    if update.effective_chat.type != "private":
        return

    remember_private_user(update)

    poll = update.message.poll
    if poll is None:
        return

    # Cooldown check
    now = time.time()
    last = last_submission.get(user.id, 0)
    remaining = COOLDOWN_SECONDS - (now - last)
    if remaining > 0:
        await update.message.reply_text(
            f"⏳ You're on cooldown! Please wait *{int(remaining) + 1} seconds* "
            f"before submitting another poll.",
            parse_mode="Markdown",
        )
        return

    poll_id = uuid.uuid4().hex[:10]
    options_text = "\n".join(
        f"  {i + 1}. {opt.text}" for i, opt in enumerate(poll.options)
    )

    pending_polls[poll_id] = {
        "user_id": user.id,
        "user_handle": f"@{user.username}" if user.username else user.full_name,
        "question": poll.question,
        "options": [opt.text for opt in poll.options],
        "original_message_id": update.message.message_id,
    }

    admin_msg = (
        f"📬 *New Poll Submission*\n\n"
        f"👤 *From:* {pending_polls[poll_id]['user_handle']} (`{user.id}`)\n"
        f"❓ *Question:* {poll.question}\n"
        f"🗳 *Options:*\n{options_text}\n\n"
        f"_Poll ID: `{poll_id}`_"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Accept", callback_data=f"accept:{poll_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{poll_id}"),
        ]
    ]

    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        last_submission[user.id] = time.time()
        await update.message.reply_text(
            "✅ Your poll has been submitted for review!\n"
            "You'll receive a DM once an admin makes a decision."
        )
        logger.info("Poll %s submitted by user %s", poll_id, user.id)
    except Exception as e:
        logger.error("Failed to send poll to admin group: %s", e)
        await update.message.reply_text(
            "⚠️ Something went wrong sending your poll to the admins. Please try again later."
        )


# ---------------------------------------------------------------------------
# Admin: Accept / Reject
# ---------------------------------------------------------------------------
async def handle_admin_decision(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    action, poll_id = query.data.split(":", 1)
    poll = pending_polls.get(poll_id)

    if poll is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ This submission has already been processed.")
        return

    admin_name = query.from_user.full_name

    if action == "accept":
        try:
            await context.bot.send_poll(
                chat_id=PUBLIC_GROUP_ID,
                question=poll["question"],
                options=poll["options"],
                is_anonymous=True,
            )
        except Exception as e:
            logger.error("Failed to post poll to public group: %s", e)
            await query.message.reply_text(
                f"⚠️ Couldn't post to the public group: {e}"
            )
            return

        try:
            await context.bot.send_message(
                chat_id=poll["user_id"],
                text=(
                    f"✅ Your poll *{poll['question']}* has been *approved* "
                    f"and posted to the group!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Could not DM user %s: %s", poll["user_id"], e)

        await query.edit_message_text(
            text=query.message.text + f"\n\n✅ *Accepted* by {admin_name}",
            parse_mode="Markdown",
        )

    elif action == "reject":
        try:
            await context.bot.send_message(
                chat_id=poll["user_id"],
                text=f"❌ Your poll \"{poll['question']}\" was rejected by an admin.",
            )
        except Exception as e:
            logger.warning("Could not DM user %s: %s", poll["user_id"], e)

        await query.edit_message_text(
            text=query.message.text + f"\n\n❌ *Rejected* by {admin_name}",
            parse_mode="Markdown",
        )

    del pending_polls[poll_id]


# ---------------------------------------------------------------------------
# Admin: broadcast a custom message to all users who have used the bot
# ---------------------------------------------------------------------------
async def is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False

    try:
        member = await context.bot.get_chat_member(ADMIN_GROUP_ID, user.id)
        return member.status in {"administrator", "creator"}
    except Exception as e:
        logger.warning("Could not verify admin user %s: %s", user.id, e)
        return False


async def broadcast_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    recipients = list(known_users)
    if not recipients:
        await update.message.reply_text("There are no registered users to message yet.")
        return

    sent = 0
    failed = 0
    blocked_users: set[int] = set()

    for user_id in recipients:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.send_message(chat_id=user_id, text=text)
                sent += 1
            except Exception as retry_error:
                failed += 1
                logger.warning("Could not broadcast to user %s: %s", user_id, retry_error)
        except Forbidden:
            failed += 1
            blocked_users.add(user_id)
        except Exception as e:
            failed += 1
            logger.warning("Could not broadcast to user %s: %s", user_id, e)

        # Stay comfortably below Telegram's broadcast rate limit.
        await asyncio.sleep(0.05)

    if blocked_users:
        known_users.difference_update(blocked_users)
        save_known_users()

    await update.message.reply_text(
        f"📣 Broadcast complete.\n✅ Delivered: {sent}\n⚠️ Failed: {failed}"
    )
    logger.info("Broadcast sent to %s users; %s failed", sent, failed)


async def send_everyone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_private_user(update)

    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /send_everyone in the bot's private chat.")
        return

    if not await is_admin_user(update, context):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    text = " ".join(context.args).strip()
    if not text:
        pending_broadcast_admins.add(update.effective_user.id)
        await update.message.reply_text(
            "Send the message you want to broadcast in your next message.\n"
            "Only administrators can use this feature."
        )
        return

    await broadcast_message(update, context, text)


async def receive_broadcast_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    remember_private_user(update)
    user = update.effective_user
    if (
        user is None
        or update.effective_chat.type != "private"
        or user.id not in pending_broadcast_admins
    ):
        return

    pending_broadcast_admins.discard(user.id)
    if not await is_admin_user(update, context):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    await broadcast_message(update, context, update.message.text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("poll", poll_command))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("send_everyone", send_everyone))
    app.add_handler(MessageHandler(filters.POLL, receive_poll))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_text)
    )
    app.add_handler(
        CallbackQueryHandler(handle_admin_decision, pattern=r"^(accept|reject):")
    )

    logger.info("Bot is running — polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
