"""Small trusted native Telegram boundary. Never takes identity from LLM text."""
from __future__ import annotations
import asyncio
import json
import logging
import socket
import time
from pathlib import Path
from .settings import root, load, private_json
from .compat import require_hermes_apis

logger = logging.getLogger(__name__)


def _private_callback_authorized(adapter, user_id: str, chat_id: str) -> bool:
    check = getattr(adapter, "_is_callback_user_authorized", None)
    if not callable(check):
        logger.warning(
            "Telegram adapter lacks its callback authorization method; refusing Fleet command"
        )
        return False
    return check(user_id, chat_id=chat_id, chat_type="dm") is True
from .control import control, socket_path  # noqa: F401 (re-exported for service/__main__)

def owner_event(update, owners: list[int]) -> bool:
    m = getattr(update, "message", None)
    u = getattr(update, "effective_user", None)
    c = getattr(update, "effective_chat", None)
    if m is None or u is None or c is None or getattr(update, "internal", False):
        return False
    if getattr(update, "effective_message", None) is not m:
        return False
    if type(getattr(update, "update_id", None)) is not int or update.update_id < 0:
        return False
    if (type(u.id) is not int or u.id not in owners or u.is_bot or c.type != "private"
            or c.id != u.id or getattr(m, "from_user", None) is not u
            or getattr(m, "chat", None) is not c):
        return False
    return not any(getattr(m, key, None) for key in (
        "forward_origin", "forward_date", "forward_from", "forward_from_chat",
        "forward_sender_name", "via_bot", "is_automatic_forward", "sender_chat"))

def register(ctx) -> None:
    import logging
    from hermes_constants import get_hermes_home
    from hermes_cli.plugin_isolation import isolation_mode
    log = logging.getLogger(__name__)
    in_process = isolation_mode() != "host"
    require_hermes_apis(ctx, native=in_process)
    expected_home = Path(get_hermes_home()).resolve()
    # Platform-neutral flow first: it must not depend on Telegram or on in-process isolation.
    from .commands import register_chat_command
    from .cli import register_cli
    register_chat_command(ctx, lambda: expected_home)
    register_cli(ctx)
    if not in_process:
        log.warning("Fleet: plugins.isolation is 'host'; skipping legacy Telegram handlers and the "
                    "dashboard auth provider (both need in-process isolation). /fleet still works.")
    else:
        from .backend_provider import BackendLeaseProvider
        ctx.register_dashboard_auth_provider(BackendLeaseProvider(expected_home))
        register_legacy_telegram(ctx, expected_home)

def register_legacy_telegram(ctx, expected_home: Path) -> None:
    from hermes_constants import get_hermes_home
    def factory(native, adapter):
        from telegram.ext import CommandHandler
        async def handle(update, context):
            try:
                home = Path(get_hermes_home()).resolve()
                if home != expected_home:
                    return
                command = update.message.text.split()[0].split("@")[0].lower()
                if command == "/fleetsetup":
                    if not owner_event(update, [getattr(update.effective_user, "id", None)]):
                        return
                    if (not _private_callback_authorized(
                            adapter, str(update.effective_user.id), str(update.effective_chat.id))
                            or len(update.message.text.split()) != 1):
                        return
                    if (root(home) / "settings.json").exists():
                        await update.message.reply_text("Fleet is configured. Use /fleetstatus; existing owner binding is preserved.")
                        return
                    request = root(home) / "setup-request.json"
                    if request.exists():
                        previous = json.loads(request.read_text())
                        if previous["owner"] != update.effective_user.id or previous["bot_id"] != context.bot.id:
                            await update.message.reply_text("A different setup request is pending local owner review.")
                            return
                    private_json(request, {"owner": update.effective_user.id, "bot_id": context.bot.id,
                        "update_id": update.update_id, "home": str(home), "expires_at": time.time()+600})
                    await update.message.reply_text("Verified Telegram setup request recorded for ten minutes. The host owner must review the request, approve owner binding and activate the private connector. No invitation was issued. Follow the connector README, then /fleetstatus and /fleetqr.")
                    return
                cfg = load(home)
                if not owner_event(update, cfg.get("owners") or []):
                    return
                # Established runner policy, including allowlists and gateway pairing.
                if (not _private_callback_authorized(
                        adapter, str(update.effective_user.id), str(update.effective_chat.id))
                        or not cfg.get("bot_id") or context.bot.id != cfg["bot_id"]):
                    return
                actions = {"/fleetstatus": "status", "/fleetqr": "issue", "/fleetdevices": "devices"}
                action = actions.get(command)
                if action is None or len(update.message.text.split()) != 1:
                    await update.message.reply_text("Use /fleetstatus, /fleetqr or /fleetdevices without arguments.")
                    return
                result = await asyncio.to_thread(control, home, {
                    "action": action, "owner": update.effective_user.id,
                    "bot_id": context.bot.id, "update_id": update.update_id,
                    "explicit_issue": command == "/fleetqr"})
                # Only safe classifications and non-secret device metadata cross Telegram.
                await update.message.reply_text(result.get("message", "Fleet request refused."))
            except Exception:
                await update.message.reply_text("Fleet connector unavailable. No QR was confirmed. Use /fleetstatus for the safe status before retrying.")
        native.add_handler(CommandHandler(["fleetsetup", "fleetstatus", "fleetqr", "fleetdevices"], handle))
    ctx.register_platform_handler("telegram", factory)
