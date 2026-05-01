"""Long-polling Telegram bot → POST /v1/chat (intended for EC2 next to uvicorn).

Env:
  TELEGRAM_BOT_TOKEN           — from @BotFather (required)
  TELEGRAM_ALLOWED_USER_IDS    — comma-separated Telegram user ids (required)
  TELEGRAM_NEMOWLAW_API_BASE   — default http://127.0.0.1:8000
  NEMOWLAW_CHAT_BEARER_TOKEN   — same value as in the API .env when NEMOWLAW_DASHBOARD_PASSWORD is set
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

# Under Telegram’s 4096 limit; UTF-8 safe for most alphabets.
_CHUNK = 3800


def _repo_root() -> Path:
    """Project root (parent of ``runtime/``) — same layout as ``deploy/ec2`` systemd units."""
    return Path(__file__).resolve().parent.parent.parent


def _load_dotenv_from_repo() -> None:
    """Populate ``os.environ`` from repo ``.env`` when keys are missing or empty-string.

    ``EnvironmentFile`` can leave a key present but empty; ``load_dotenv(override=False)`` would
    not replace it — we backfill those from the file for the vars this process needs.
    """
    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return
    path = _repo_root() / ".env"
    if not path.is_file():
        return
    load_dotenv(path, override=False)
    file_vars = dotenv_values(path)
    for key in (
        "NEMOWLAW_CHAT_BEARER_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_NEMOWLAW_API_BASE",
    ):
        raw_file = file_vars.get(key)
        if raw_file is None or str(raw_file).strip() == "":
            continue
        if not (os.environ.get(key) or "").strip():
            os.environ[key] = str(raw_file).strip()


def _allowed_ids() -> frozenset[int]:
    """Parse TELEGRAM_ALLOWED_USER_IDS (comma-separated). Each entry must be a numeric user id.

    A leading ``@`` is stripped so ``@7522615345`` works. Telegram @usernames are not supported.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip().removeprefix("@")
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be numeric Telegram user ids (see @userinfobot). "
                f"Invalid entry: {part!r} — usernames like @cjs1101 are not supported.",
            )
        out.add(int(part))
    return frozenset(out)


def _chunks(text: str) -> list[str]:
    if not text:
        return ["(empty reply)"]
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)]


async def _call_chat(message: str, api_base: str, bearer: str | None) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            f"{api_base.rstrip('/')}/v1/chat",
            json={"message": message},
            headers=headers,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "API returned 401. If the dashboard password is set, define "
                "NEMOWLAW_CHAT_BEARER_TOKEN in .env (same value for the API and this bot).",
            )
        r.raise_for_status()
        return r.json()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        "Nemoclaw chat bridge.\n\n"
        f"Your Telegram user id: {uid}\n"
        "Add it to TELEGRAM_ALLOWED_USER_IDS on the server to allow this account.\n\n"
        "Send any text message to talk to /v1/chat.",
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    allowed: frozenset[int] = context.bot_data["allowed"]
    if not allowed:
        await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        return
    if uid not in allowed:
        logger.warning(
            "Rejected Telegram user_id=%s chat_id=%s",
            uid,
            update.effective_chat.id if update.effective_chat else None,
        )
        await update.message.reply_text("Unauthorized.")
        return

    api_base: str = context.bot_data["api_base"]
    bearer: str | None = context.bot_data["bearer"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )
    try:
        data = await _call_chat(update.message.text, api_base, bearer)
    except httpx.HTTPStatusError as e:
        logger.exception("chat API HTTP error")
        await update.message.reply_text(f"API error: {e.response.status_code}")
        return
    except Exception as e:
        logger.exception("chat API failure")
        await update.message.reply_text(str(e)[:3500])
        return

    reply = data.get("reply")
    if not isinstance(reply, str):
        reply = str(data)

    for raw_part in _chunks(reply):
        # Plain text: Telegram Bot API HTML mode does not support <br> / <br/>.
        safe = raw_part.replace("\x00", "")
        await update.message.reply_text(safe)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    _load_dotenv_from_repo()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Set TELEGRAM_BOT_TOKEN")
        sys.exit(2)

    try:
        allowed = _allowed_ids()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(2)

    if not allowed:
        logger.error("Set TELEGRAM_ALLOWED_USER_IDS (comma-separated integers, e.g. from @userinfobot)")
        sys.exit(2)

    api_base = os.environ.get("TELEGRAM_NEMOWLAW_API_BASE", "http://127.0.0.1:8000").strip()
    bearer = os.environ.get("NEMOWLAW_CHAT_BEARER_TOKEN", "").strip() or None
    logger.info(
        "Telegram bot config: NEMOWLAW_CHAT_BEARER_TOKEN=%s, api_base=%s",
        "set" if bearer else "unset",
        api_base,
    )

    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.bot_data["allowed"] = allowed
    app.bot_data["api_base"] = api_base
    app.bot_data["bearer"] = bearer

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Telegram bot polling (allowed user ids=%s, api_base=%s)", allowed, api_base)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
