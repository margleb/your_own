"""Same-process Mini App API. Telegram signatures identify the authenticated owner.

No access logger, request strings, query parameters, payloads or exception text
are recorded. Mini App replies use the existing dispatcher and shared budget.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from aiohttp import web

from pastoral_bot.policy import ERROR_MESSAGES
from pastoral_bot.types import Mode

logger = logging.getLogger("pastoral.web")
BODY_LIMIT = 32768
USER_ID = web.RequestKey("user_id", int) if hasattr(web, "RequestKey") else "user_id"


class AuthenticationError(ValueError):
    """Fixed explanation; never includes the signed payload."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def validate_init_data(raw: str, token: str, *, now: float | None = None, max_age: int = 7200) -> int:
    """Official bot-token HMAC, independent of untrusted initDataUnsafe.

    The Ed25519-only signature field remains part of this HMAC check string,
    as required by Telegram's bot-token verification algorithm.
    """
    try:
        if not token or not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 16384 or re.search(r"%(?![0-9A-Fa-f]{2})", raw):
            raise ValueError("malformed")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict", max_num_fields=32)
        data = _unique_object(pairs)
        if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", key) for key in data):
            raise ValueError("malformed")
        given_hash = data.pop("hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", given_hash):
            raise ValueError("malformed")
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, given_hash.lower()):
            raise ValueError("signature")
        if not re.fullmatch(r"[0-9]{1,12}", data["auth_date"]):
            raise ValueError("auth_date")
        moment = time.time() if now is None else now
        auth_date = int(data["auth_date"])
        if auth_date > moment + 60 or moment - auth_date > max_age:
            raise ValueError("expired")
        user = json.loads(data["user"], object_pairs_hook=_unique_object)
        user_id = user.get("id") if isinstance(user, dict) else None
        if type(user_id) is not int or not 0 < user_id < (1 << 63) or user.get("is_bot") is True:
            raise ValueError("user")
        if "chat_type" in data and data["chat_type"] not in ("private", "sender"):
            raise ValueError("chat_type")
        if "chat" in data:
            chat = json.loads(data["chat"], object_pairs_hook=_unique_object)
            if not isinstance(chat, dict) or chat.get("type") != "private":
                raise ValueError("chat_type")
        return user_id
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise AuthenticationError("invalid_telegram_auth") from None


def _error(code: str, status: int = 400) -> web.Response:
    messages = {
        "unauthorized": "Откройте помощника через кнопку в Telegram.",
        "stale_epoch": "Режим или беседа изменились. Обновите экран.",
        "temporary_expired": "Временная беседа завершилась. Начните её заново.",
        "request_expired": "Этот запрос уже завершён или устарел. Создайте новое сообщение.",
        "queue_full": "Дождитесь ответа или остановите текущую беседу.",
        "bad_request": "Не удалось принять запрос. Обновите экран и попробуйте снова.",
        "origin_rejected": "Откройте помощника через Telegram.",
        "unavailable": "Помощник временно недоступен. Попробуйте позже.",
    }
    return web.json_response({"error": {"code": code, "message": messages.get(code, messages["bad_request"])}}, status=status)


def _request_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("request_id")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("request_id")
    return str(parsed)


async def _body(request: web.Request, allowed: set[str]) -> dict:
    if request.content_type != "application/json":
        raise ValueError("content_type")
    raw = await request.text()
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError("schema")
    if type(data.get("expected_epoch")) is not int or not 0 <= data["expected_epoch"] < (1 << 31):
        raise ValueError("epoch")
    return data


def _public_result(result: dict) -> dict:
    result = dict(result)
    code = result.pop("error_code", None)
    if result["status"] == "error":
        code = code or "provider_unavailable"
        result["error"] = {"code": code, "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["provider_unavailable"])}
    return result


def create_web_app(botapp, settings, static_dir: str | Path | None = None) -> web.Application:
    """Caller owns AppRunner(access_log=None), TCPSite and runner.cleanup()."""
    directory = (Path(static_dir) if static_dir else Path(__file__).parent / "miniapp" / "dist").resolve()
    public = urlsplit(settings.web_public_url)
    origin = f"{public.scheme}://{public.netloc}"

    @web.middleware
    async def secure(request, handler):
        is_api = request.path == "/api" or request.path.startswith("/api/")
        try:
            if is_api:
                sent_origin = request.headers.get("Origin")
                if (sent_origin is not None and sent_origin != origin) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                    response = _error("origin_rejected", 403)
                else:
                    authorization = request.headers.get("Authorization", "")
                    if not authorization.startswith("tma "):
                        raise AuthenticationError("invalid_telegram_auth")
                    request[USER_ID] = validate_init_data(authorization[4:], settings.telegram_token.get_secret_value(), max_age=settings.web_auth_max_age_seconds)
                    response = await handler(request)
            else:
                response = await handler(request)
        except AuthenticationError:
            response = _error("unauthorized", 401)
        except (ValueError, UnicodeError):
            response = _error("bad_request")
        except web.HTTPException as exc:
            response = _error("bad_request", exc.status) if is_api else web.Response(status=exc.status, text="Page unavailable")
        except Exception as exc:
            logger.warning("web_error kind=%s", type(exc).__name__)
            response = _error("unavailable", 503)
        response.headers["Cache-Control"] = "no-store" if is_api else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "font-src 'self' data:; connect-src 'self'; base-uri 'self'; "
            "form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        return response

    app = web.Application(client_max_size=BODY_LIMIT, middlewares=[secure])

    async def state(request):
        async with botapp.user_lock(request[USER_ID]):
            return web.json_response(await botapp.web_state(request[USER_ID]))

    async def control(request):
        action = request.match_info["action"]
        allowed = {"expected_epoch", "mode"} if action == "mode" else {"expected_epoch", "confirm"} if action == "delete-history" else {"expected_epoch"}
        data = await _body(request, allowed)
        mode = Mode(data["mode"]) if action == "mode" else None
        if action == "delete-history" and data["confirm"] is not True:
            raise ValueError("confirmation")
        result = await botapp.web_control(request[USER_ID], data["expected_epoch"], "delete" if action == "delete-history" else action, mode)
        return web.json_response(result) if result is not None else _error("stale_epoch", 409)

    async def submit(request):
        data = await _body(request, {"request_id", "text", "expected_epoch"})
        request_id = _request_id(data["request_id"])
        text = data["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > settings.max_message_chars:
            raise ValueError("text")
        result, status = await botapp.web_submit(request[USER_ID], request_id, text.strip(), data["expected_epoch"])
        return _error(result["code"], status) if "code" in result else web.json_response(_public_result(result), status=status)

    async def result(request):
        request_id = _request_id(request.match_info["request_id"])
        async with botapp.user_lock(request[USER_ID]):
            return web.json_response(_public_result(await botapp.web_result(request[USER_ID], request_id)))

    async def health(request):
        return web.json_response({"status": "ok"})

    async def static(request):
        relative = request.match_info.get("path", "")
        path = (directory / (relative or "index.html")).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise web.HTTPNotFound()
        if path.suffix not in {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".ico"}:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.router.add_get("/healthz", health)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/{action:mode|new|stop|delete-history}", control)
    app.router.add_post("/api/messages", submit)
    app.router.add_get("/api/messages/{request_id}", result)
    app.router.add_get("/{path:.*}", static)
    return app
