"""Long-polling Telegram bot → POST /v1/chat (intended for EC2 next to uvicorn).

Env:
  TELEGRAM_BOT_TOKEN              — from @BotFather (required)
  TELEGRAM_ALLOWED_USER_IDS       — comma-separated Telegram user ids (required)
  TELEGRAM_NEMOWLAW_API_BASE      — default http://127.0.0.1:8000
  NEMOWLAW_CHAT_BEARER_TOKEN      — same value as in the API .env when NEMOWLAW_DASHBOARD_PASSWORD is set
  TELEGRAM_CHAT_HTTP_TIMEOUT_S    — HTTP read timeout toward /v1/chat (default 900). Routing + workers +
                                    synthesis can invoke several sequential OpenRouter calls (up to ~120s each).
  TELEGRAM_CHAT_HISTORY_MAX_MESSAGES — max messages kept per chat for vision/context (default 30). Bot-side only.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import httpx
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

# Under Telegram’s 4096 limit; UTF-8 safe for most alphabets.
_CHUNK = 3800

# One chat turn can run route + (optional repair) × workers + synthesis — several minutes worst case.
_DEFAULT_CHAT_HTTP_TIMEOUT_S = 900.0
_TYPING_REFRESH_S = 4.5

# Reply-keyboard labels (must match alias routing in ``_route_text_command``).
_LABEL_NEW = "Fresh topic"
_LABEL_SUMMARY = "Holistic summary"
_LABEL_HELP = "Help"


def _reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_LABEL_NEW], [_LABEL_SUMMARY], [_LABEL_HELP]],
        resize_keyboard=True,
    )


def _normalize_alias_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _route_text_command(normalized: str) -> str | None:
    """Map a single-line user message to ``new``, ``summary``, or ``help``; else ``None``.

    Uses exact-line aliases so coaching messages are never substring-matched.
    """
    if normalized in (
        "hc:new",
        "nemoclaw new",
        "reset",
        _normalize_alias_line(_LABEL_NEW),
    ):
        return "new"
    if normalized in (
        "hc:summary",
        "nemoclaw summary",
        "snapshot",
        _normalize_alias_line(_LABEL_SUMMARY),
    ):
        return "summary"
    if normalized in (
        "hc:help",
        "nemoclaw help",
        "help",
        _normalize_alias_line(_LABEL_HELP),
    ):
        return "help"
    return None


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
        "TELEGRAM_CHAT_HISTORY_MAX_MESSAGES",
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


def _history_maxlen() -> int:
    raw = os.environ.get("TELEGRAM_CHAT_HISTORY_MAX_MESSAGES", "").strip()
    if not raw:
        return 30
    try:
        v = int(raw)
    except ValueError:
        return 30
    return max(4, min(v, 200))


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


async def _get_chat_turn_lock(bot_data: dict[str, Any], chat_id: int) -> asyncio.Lock:
    """Per-chat asyncio lock: serializes deque updates vs /clear vs /new late assistant append."""
    reg: asyncio.Lock = bot_data["_chat_lock_registry"]
    async with reg:
        locks: dict[int, asyncio.Lock] = bot_data["chat_turn_locks"]
        if chat_id not in locks:
            locks[chat_id] = asyncio.Lock()
        return locks[chat_id]


async def _call_chat(
    message: str,
    api_base: str,
    bearer: str | None,
    timeout_s: float,
    *,
    images: list[dict[str, str]] | None = None,
    conversation_context: list[dict[str, str]] | None = None,
) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    body: dict[str, Any] = {"message": message}
    if images:
        body["images"] = images
    if conversation_context:
        body["conversation_context"] = conversation_context
    t = httpx.Timeout(timeout_s, connect=15.0, pool=60.0)
    async with httpx.AsyncClient(timeout=t) as client:
        r = await client.post(
            f"{api_base.rstrip('/')}/v1/chat",
            json=body,
            headers=headers,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "API returned 401. If the dashboard password is set, define "
                "NEMOWLAW_CHAT_BEARER_TOKEN in .env (same value for the API and this bot).",
            )
        r.raise_for_status()
        return r.json()


def _pop_pending_user(history: dict[int, deque], chat_id: int) -> None:
    dq = history[chat_id]
    if dq and dq[-1].get("role") == "user":
        dq.pop()


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
    *,
    images: list[dict[str, str]] | None = None,
    conversation_context: list[dict[str, str]] | None = None,
) -> None:
    """POST ``message`` to /v1/chat and send reply chunks."""
    api_base: str = context.bot_data["api_base"]
    bearer: str | None = context.bot_data["bearer"]
    timeout_s: float = context.bot_data["chat_timeout_s"]
    history: dict[int, deque] = context.bot_data["history"]
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    keepalive = asyncio.create_task(_typing_keepalive(context.bot, chat_id))
    try:
        data = await _call_chat(
            message,
            api_base,
            bearer,
            timeout_s,
            images=images,
            conversation_context=conversation_context,
        )
    except httpx.HTTPStatusError as e:
        logger.exception("chat API HTTP error")
        _pop_pending_user(history, chat_id)
        await update.message.reply_text(f"API error: {e.response.status_code}")
        return
    except httpx.TimeoutException:
        logger.exception("chat API timeout after %ss", timeout_s)
        _pop_pending_user(history, chat_id)
        await update.message.reply_text(
            f"The health API took longer than {int(timeout_s)}s (orchestration can chain several LLM calls). "
            "You can retry, or raise TELEGRAM_CHAT_HTTP_TIMEOUT_S in the bot env if your host is routinely slow.",
        )
        return
    except Exception as e:
        logger.exception("chat API failure")
        _pop_pending_user(history, chat_id)
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

    snapshot = reply[:8000] if isinstance(reply, str) else str(data)[:8000]
    history[chat_id].append({"role": "assistant", "content": snapshot})

    try:
        for raw_part in _chunks(reply):
            safe = raw_part.replace("\x00", "")
            await update.message.reply_text(safe)
    except Exception as e:
        logger.exception("Telegram reply send failed after successful /v1/chat response")
        try:
            await update.message.reply_text(
                "The coach reply was generated but sending it through Telegram failed. "
                f"Error: {str(e)[:500]}"
            )
        except Exception:
            logger.exception("Could not notify user after Telegram reply failure")


def _reject_if_unauthorized(update: Update, allowed: frozenset[int]) -> bool:
    """Return True when the user should receive “Unauthorized.” (handler should return)."""
    if not allowed:
        return True
    u = update.effective_user
    if not u:
        return True
    return u.id not in allowed


async def _reply_unauthorized(update: Update, allowed: frozenset[int]) -> None:
    if not update.message:
        return
    if not allowed:
        await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
    else:
        await update.message.reply_text("Unauthorized.")


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Intro text and reply keyboard. Intentionally **not** gated on allowlist so new users can read their user id."""
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        "Nemoclaw chat bridge (Popeye via /v1/chat).\n\n"
        f"Your Telegram user id: {uid}\n"
        "Add it to TELEGRAM_ALLOWED_USER_IDS on the server to allow this account.\n\n"
        "Slash commands: /new, /summary, /help\n\n"
        "If “/” commands fail in your client, use the buttons below or type one line:\n"
        f"• {_LABEL_NEW} — clear this bot’s short-term chat memory.\n"
        f"• {_LABEL_SUMMARY} — holistic coaching snapshot (same as /summary).\n"
        f"• {_LABEL_HELP} — this overview.\n"
        "Aliases (whole message, case-insensitive): hc:new, hc:summary, hc:help; "
        "nemoclaw new|summary|help; reset; snapshot; help.\n\n"
        "Send plain text or photos (with optional caption). Photos use a vision model on the server; "
        "recent turns in this chat are sent as context when you attach an image.",
        reply_markup=_reply_keyboard(),
    )


async def run_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        await _reply_unauthorized(update, allowed)
        return

    chat_id = update.effective_chat.id
    lock = await _get_chat_turn_lock(context.bot_data, chat_id)
    async with lock:
        dq = context.bot_data["history"][chat_id]
        dq.clear()
    await update.message.reply_text(
        "Fresh topic — bot-side chat memory for this thread was cleared. "
        "Reply with text or a photo (caption optional). Long answers can take several minutes "
        "when the backend chains multiple model calls—you’ll see “typing…” while it works.",
        reply_markup=_reply_keyboard(),
    )


async def run_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        await _reply_unauthorized(update, allowed)
        return

    chat_id = update.effective_chat.id
    hist = context.bot_data["history"]
    lock = await _get_chat_turn_lock(context.bot_data, chat_id)
    async with lock:
        prior = [dict(x) for x in hist[chat_id]]
        hist[chat_id].append({"role": "user", "content": "[Telegram /summary]"})
        await _deliver_chat_response(
            update,
            context,
            _SUMMARY_PROMPT,
            conversation_context=prior if prior else None,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_welcome(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_welcome(update, context)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_new(update, context)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_summary(update, context)


def _mime_from_tg_path(file_path: str | None) -> str:
    p = (file_path or "").lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    if p.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.photo:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        if not allowed:
            await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        else:
            await update.message.reply_text("Unauthorized.")
        return

    chat_id = update.effective_chat.id
    hist = context.bot_data["history"]
    caption = (update.message.caption or "").strip()

    photo = update.message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        raw = buf.getvalue()
    except Exception:
        logger.exception("telegram download photo")
        await update.message.reply_text("Could not download the photo; try again.")
        return

    mt = _mime_from_tg_path(tg_file.file_path)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    images = [{"mime_type": mt, "data_base64": b64}]

    lock = await _get_chat_turn_lock(context.bot_data, chat_id)
    async with lock:
        prior = [dict(x) for x in hist[chat_id]]
        label = caption if caption else "[photo]"
        hist[chat_id].append({"role": "user", "content": label})
        await _deliver_chat_response(
            update,
            context,
            caption,
            images=images,
            conversation_context=prior if prior else None,
        )


async def on_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.document:
        return
    allowed: frozenset[int] = context.bot_data["allowed"]
    if _reject_if_unauthorized(update, allowed):
        if not allowed:
            await update.message.reply_text("Bot misconfigured: TELEGRAM_ALLOWED_USER_IDS is empty.")
        else:
            await update.message.reply_text("Unauthorized.")
        return

    doc = update.message.document
    chat_id = update.effective_chat.id
    hist = context.bot_data["history"]
    caption = (update.message.caption or "").strip()

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        raw = buf.getvalue()
    except Exception:
        logger.exception("telegram download document")
        await update.message.reply_text("Could not download the file; try again.")
        return

    mt = (doc.mime_type or _mime_from_tg_path(tg_file.file_path)).split(";")[0].strip().lower()
    if mt not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        await update.message.reply_text(
            "Unsupported image type for the health API (use JPEG, PNG, GIF, or WebP)."
        )
        return

    b64 = base64.standard_b64encode(raw).decode("ascii")
    images = [{"mime_type": mt, "data_base64": b64}]

    lock = await _get_chat_turn_lock(context.bot_data, chat_id)
    async with lock:
        prior = [dict(x) for x in hist[chat_id]]
        label = caption if caption else "[image file]"
        hist[chat_id].append({"role": "user", "content": label})
        await _deliver_chat_response(
            update,
            context,
            caption,
            images=images,
            conversation_context=prior if prior else None,
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    routed = _route_text_command(_normalize_alias_line(text))
    # Same as /help: always available so anyone can see their numeric user id from aliases too.
    if routed == "help":
        await send_welcome(update, context)
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

    if routed == "new":
        await run_new(update, context)
        return
    if routed == "summary":
        await run_summary(update, context)
        return

    chat_id = update.effective_chat.id
    hist = context.bot_data["history"]
    lock = await _get_chat_turn_lock(context.bot_data, chat_id)
    async with lock:
        prior = [dict(x) for x in hist[chat_id]]
        hist[chat_id].append({"role": "user", "content": text})
        await _deliver_chat_response(
            update,
            context,
            text,
            conversation_context=prior if prior else None,
        )


async def post_init(application: Application) -> None:
    """Override stale BotFather menus so “/” only lists commands this process implements."""
    application.bot_data.setdefault("_chat_lock_registry", asyncio.Lock())
    application.bot_data.setdefault("chat_turn_locks", {})
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
    app.bot_data["history"] = defaultdict(lambda: deque(maxlen=_history_maxlen()))
    app.bot_data["_chat_lock_registry"] = asyncio.Lock()
    app.bot_data["chat_turn_locks"] = {}

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Telegram bot polling (allowed user ids=%s, api_base=%s)", allowed, api_base)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
