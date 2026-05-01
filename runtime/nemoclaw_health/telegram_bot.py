"""Long-polling Telegram bot → POST /v1/chat (intended for EC2 next to uvicorn).

Env:
  TELEGRAM_BOT_TOKEN              — from @BotFather (required)
  TELEGRAM_ALLOWED_USER_IDS       — comma-separated Telegram user ids (required)
  TELEGRAM_NEMOWLAW_API_BASE      — default http://127.0.0.1:8000
  NEMOWLAW_CHAT_BEARER_TOKEN      — same value as in the API .env when NEMOWLAW_DASHBOARD_PASSWORD is set
  TELEGRAM_CHAT_HTTP_TIMEOUT_S    — HTTP read timeout toward /v1/chat (default 900). Routing + workers +
                                    synthesis can invoke several sequential OpenRouter calls (up to ~120s each).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

# Under Telegram’s 4096 limit; UTF-8 safe for most alphabets.
_CHUNK = 3800

# One chat turn can run route + (optional repair) × workers + synthesis — several minutes worst case.
_DEFAULT_CHAT_HTTP_TIMEOUT_S = 900.0
_TYPING_REFRESH_S = 4.5

_SUMMARY_PROMPT = (
    "[Telegram command /summary] As Popeye, give a cohesive holistic health coaching snapshot. "
    "Use delegation to Stan (nutrition/eating patterns), Dick (training load and progression), "
    "and Joy (recovery signals / wearable cautions when relevant) via the normal orchestration path. "
    "Ground specifics in bounded insight/WHOOP/ingested context when present; do not invent data. "
    "Structure: (1) Top priorities this week (2) Nutrition (3) Training (4) Recovery / cautions "
    "(5) One concrete next step. Non-diagnostic; include Joy disclaimer markers where policy requires."
)


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
        "TELEGRAM_CHAT_HTTP_TIMEOUT_S",
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


def _chat_http_timeout_seconds() -> float:
    raw = os.environ.get("TELEGRAM_CHAT_HTTP_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_CHAT_HTTP_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        logger.warning(
            "Invalid TELEGRAM_CHAT_HTTP_TIMEOUT_S=%r, using default %s",
            raw,
            _DEFAULT_CHAT_HTTP_TIMEOUT_S,
        )
        return _DEFAULT_CHAT_HTTP_TIMEOUT_S
    return max(60.0, min(v, 3600.0))


def _chunks(text: str) -> list[str]:
    if not text:
        return ["(empty reply)"]
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)]


async def _call_chat(message: str, api_base: str, bearer: str | None, timeout_s: float) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    t = httpx.Timeout(timeout_s, connect=15.0, pool=60.0)
    async with httpx.AsyncClient(timeout=t) as client:
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


async def _typing_keepalive(bot, chat_id: int) -> None:
    """Telegram clears “typing…” after ~5s; renew so long /v1/chat turns feel alive."""
    try:
        while True:
            await asyncio.sleep(_TYPING_REFRESH_S)
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except asyncio.CancelledError:
        return


async def _deliver_chat_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
) -> None:
    """POST ``message`` to /v1/chat and send reply chunks."""
    api_base: str = context.bot_data["api_base"]
    bearer: str | None = context.bot_data["bearer"]
    timeout_s: float = context.bot_data["chat_timeout_s"]
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    keepalive = asyncio.create_task(_typing_keepalive(context.bot, chat_id))
    try:
        data = await _call_chat(message, api_base, bearer, timeout_s)
    except httpx.HTTPStatusError as e:
        logger.exception("chat API HTTP error")
        await update.message.reply_text(f"API error: {e.response.status_code}")
        return
    except httpx.TimeoutException:
        logger.exception("chat API timeout after %ss", timeout_s)
        await update.message.reply_text(
            f"The health API took longer than {int(timeout_s)}s (orchestration can chain several LLM calls). "
            "You can retry, or raise TELEGRAM_CHAT_HTTP_TIMEOUT_S in the bot env if your host is routinely slow.",
        )
        return
    except Exception as e:
        logger.exception("chat API failure")
        await update.message.reply_text(str(e)[:3500])
        return
    finally:
        keepalive.cancel()
        try:
            await keepalive
        except asyncio.CancelledError:
            pass

    reply = data.get("reply")
    if not isinstance(reply, str):
        reply = str(data)

    for raw_part in _chunks(reply):
        safe = raw_part.replace("\x00", "")
        await update.message.reply_text(safe)


def _reject_if_unauthorized(update: Update, allowed: frozenset[int]) -> bool:
    """Return True when the user should receive “Unauthorized.” (handler should return)."""
    if not allowed:
        return True
    u = update.effective_user
    if not u:
        return True
    return u.id not in allowed


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        "Nemoclaw chat bridge (Popeye via /v1/chat).\n\n"
        f"Your Telegram user id: {uid}\n"
        "Add it to TELEGRAM_ALLOWED_USER_IDS on the server to allow this account.\n\n"
        "Commands:\n"
        "/new — pause and start a fresh topic (psychological reset; each message is its own API turn).\n"
        "/summary — ask Popeye for a holistic health coaching snapshot.\n"
        "/help — repeat this overview.\n\n"
        "Or send plain text — food logs, workouts, anything — and it forwards to Popeye.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        if not allowed:
            await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        else:
            await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text(
        "Fresh topic — Popeye won’t see earlier Telegram exchanges in the payload; "
        "each turn is handled independently. Reply with whatever you want to focus on next "
        "(e.g. a food log, workout, or question). Long answers can take several minutes "
        "when the backend chains multiple model calls—you’ll see “typing…” while it works.",
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        if not allowed:
            await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        else:
            await update.message.reply_text("Unauthorized.")
        return

    await _deliver_chat_response(update, context, _SUMMARY_PROMPT)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        if not allowed:
            await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        else:
            logger.warning(
                "Rejected Telegram user_id=%s chat_id=%s",
                update.effective_user.id,
                update.effective_chat.id if update.effective_chat else None,
            )
            await update.message.reply_text("Unauthorized.")
        return

    await _deliver_chat_response(update, context, update.message.text)


async def post_init(application: Application) -> None:
    """Override stale BotFather menus so “/” only lists commands this process implements."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Intro, your user id, and command list"),
            BotCommand("help", "Same as /start"),
            BotCommand("new", "Start a fresh topic with Popeye"),
            BotCommand("summary", "Holistic health coaching snapshot"),
        ]
    )


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
    chat_timeout_s = _chat_http_timeout_seconds()
    logger.info(
        "Telegram bot config: NEMOWLAW_CHAT_BEARER_TOKEN=%s, api_base=%s, chat_http_timeout_s=%s",
        "set" if bearer else "unset",
        api_base,
        chat_timeout_s,
    )

    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["allowed"] = allowed
    app.bot_data["api_base"] = api_base
    app.bot_data["bearer"] = bearer
    app.bot_data["chat_timeout_s"] = chat_timeout_s

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Telegram bot polling (allowed user ids=%s, api_base=%s)", allowed, api_base)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
