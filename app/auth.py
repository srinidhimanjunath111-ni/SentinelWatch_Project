"""Optional deployment login, active only when SENTINELWATCH_ADMIN_PASSWORD is set.

Reads stay public so a shared demo URL works; mutating API routes need a signed, httponly
cookie. The token is an expiry plus an HMAC keyed from the password, so changing the
password invalidates every outstanding session and no session table is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings

COOKIE_NAME = "sentinelwatch_session"
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_OPEN_PATHS = {"/api/auth/login", "/api/auth/logout"}

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(max_length=512)


def _signature(expires: str) -> str:
    key = hashlib.sha256(f"sentinelwatch-session:{settings.admin_password}".encode()).digest()
    return hmac.new(key, expires.encode(), hashlib.sha256).hexdigest()


def issue_token(now: float | None = None) -> str:
    expires = str(int((now if now is not None else time.time()) + settings.auth_session_hours * 3600))
    return f"{expires}.{_signature(expires)}"


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _signature(expires)):
        return False
    try:
        return int(expires) > (now if now is not None else time.time())
    except ValueError:
        return False


class LoginRequiredMiddleware:
    """A no-op unless a deployment password is configured.

    Pure ASGI rather than BaseHTTPMiddleware, which would buffer the SSE stream's
    disconnect signal away from the route.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if settings.auth_required and scope["type"] == "http":
            request = Request(scope)
            if (
                request.method in _MUTATING
                and request.url.path.startswith("/api/")
                and request.url.path not in _OPEN_PATHS
                and not token_valid(request.cookies.get(COOKIE_NAME))
            ):
                response = JSONResponse(
                    {"detail": "Login required."}, status_code=status.HTTP_401_UNAUTHORIZED
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    if not settings.auth_required:
        return {"authenticated": True, "auth_required": False}
    if not hmac.compare_digest(payload.password.encode(), settings.admin_password.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")
    response.set_cookie(
        COOKIE_NAME,
        issue_token(),
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"authenticated": True, "auth_required": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False, "auth_required": settings.auth_required}


@router.get("/me")
def me(request: Request) -> dict:
    return {
        "authenticated": not settings.auth_required or token_valid(request.cookies.get(COOKIE_NAME)),
        "auth_required": settings.auth_required,
    }
