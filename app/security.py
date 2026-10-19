"""Small, dependency-free request and model-usage safeguards for the POC."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        result = default if value is None else int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer.") from exc
    if result < 1:
        raise RuntimeError(f"{name} must be a positive integer.")
    return result


class SlidingWindowLimiter:
    """Thread-safe, process-local sliding-window request limiter."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, action: str, identity: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        key = (action, identity)
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(window_seconds - (now - hits[0])))
            hits.append(now)
            if len(self._hits) > 10_000:
                cutoff = now - max(window_seconds, 3600)
                for stale_key in list(self._hits):
                    bucket = self._hits[stale_key]
                    while bucket and bucket[0] <= cutoff:
                        bucket.popleft()
                    if not bucket:
                        self._hits.pop(stale_key, None)
            return True, 0


class ModelUsageLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("The hosted-model daily allowance has been reached. Try again after the daily reset.")


class DailyModelUsageGuard:
    """In-process daily cap for Groq-backed chat actions, scoped by user and service."""

    def __init__(self) -> None:
        self._day = datetime.now(timezone.utc).date().isoformat()
        self._global_count = 0
        self._user_counts: dict[int, int] = defaultdict(int)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._day = datetime.now(timezone.utc).date().isoformat()
            self._global_count = 0
            self._user_counts.clear()

    def reserve(self, user_id: int) -> None:
        user_limit = positive_int("MODEL_ACTIONS_PER_USER_PER_DAY", 25)
        global_limit = positive_int("MODEL_ACTIONS_PER_DAY", 200)
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        next_day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86_400
        retry_after = max(1, int(next_day - now.timestamp()))
        with self._lock:
            if self._day != day:
                self._day = day
                self._global_count = 0
                self._user_counts.clear()
            if self._global_count >= global_limit or self._user_counts[user_id] >= user_limit:
                raise ModelUsageLimitExceeded(retry_after)
            self._global_count += 1
            self._user_counts[user_id] += 1


class RequestSecurityMiddleware:
    """ASGI middleware that bounds even chunked bodies before FastAPI parses them."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = positive_int("MAX_REQUEST_BODY_BYTES", 65_536)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._reject(scope, receive, send, 400, "Invalid Content-Length header.")
                return
            if declared_size < 0 or declared_size > max_bytes:
                await self._reject(scope, receive, send, 413, "Request body is too large.")
                return

        request_headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                           for key, value in scope.get("headers", [])}
        request_cookies = {}
        cookie_header = request_headers.get("cookie", "")
        for pair in cookie_header.split(";"):
            if "=" in pair:
                name, value = pair.strip().split("=", 1)
                request_cookies[name] = value
        authorization = request_headers.get("authorization", "")
        has_bearer_token = authorization.lower().startswith("bearer ") and bool(authorization[7:].strip())
        if (env_flag("SESSION_COOKIE_AUTH") and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
                and scope.get("path", "").startswith("/api/") and request_cookies.get("session")
                and not has_bearer_token):
            origin = request_headers.get("origin", "")
            host = request_headers.get("host", "").lower()
            origin_host = urlsplit(origin).netloc.lower() if origin else ""
            if not origin_host or origin_host != host:
                await self._reject(scope, receive, send, 403, "Cross-origin request rejected.")
                return

        replay = deque()
        app_receive = receive
        if content_length is None and scope.get("method") != "HEAD":
            total = 0
            while True:
                message = await receive()
                replay.append(message)
                if message["type"] == "http.disconnect":
                    return
                if message["type"] == "http.request":
                    total += len(message.get("body", b""))
                    if total > max_bytes:
                        await self._reject(scope, receive, send, 413, "Request body is too large.")
                        return
                    if not message.get("more_body", False):
                        break

            async def replay_receive():
                return replay.popleft() if replay else await receive()
            app_receive = replay_receive

        async def secured_send(message):
            if message["type"] == "http.response.start":
                response_headers = {key.lower() for key, _ in message.get("headers", [])}
                additions = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"),
                ]
                if scope.get("path", "").startswith("/api/"):
                    additions.append((b"cache-control", b"no-store"))
                if env_flag("ENABLE_HSTS"):
                    additions.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (key, value) for key, value in additions if key not in response_headers
                ]
            await send(message)

        await self.app(scope, app_receive, secured_send)

    async def _reject(self, scope, receive, send, status_code: int, detail: str) -> None:
        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"),
                ])
                message["headers"] = headers
            await send(message)
        await JSONResponse(status_code=status_code, content={"detail": detail})(scope, receive, secured_send)


request_limiter = SlidingWindowLimiter()
model_usage_guard = DailyModelUsageGuard()


def limit_request(action: str, limit_env: str, window_env: str, default_limit: int, default_window: int):
    """Build a FastAPI dependency for an IP-scoped request limit."""
    def dependency(request: Request) -> None:
        identity = request.client.host if request.client else "unknown"
        allowed, retry_after = request_limiter.allow(
            action, identity, positive_int(limit_env, default_limit), positive_int(window_env, default_window)
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Wait briefly and try again.",
                headers={"Retry-After": str(retry_after)},
            )
    return dependency
