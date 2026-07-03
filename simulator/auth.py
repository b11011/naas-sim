"""OAuth2 client-credentials flow, mirroring POST /oauth/v2/token on Lumen Connect.

Accepts the client id/secret either as HTTP Basic auth or as form fields.
"""
import base64
import secrets
import time

from fastapi import APIRouter, Header, HTTPException, Request

from . import config
from .state import store

router = APIRouter(tags=["OAuth"])


@router.post("/oauth/v2/token")
async def issue_token(request: Request):
    form = await request.form()
    if form.get("grant_type") != "client_credentials":
        raise HTTPException(400, "unsupported grant_type; use client_credentials")

    client_id, client_secret = form.get("client_id"), form.get("client_secret")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            client_id, client_secret = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)
        except Exception:
            raise HTTPException(401, "malformed Basic authorization header")

    if (client_id, client_secret) != (config.CLIENT_ID, config.CLIENT_SECRET):
        raise HTTPException(401, "invalid client credentials")

    token = secrets.token_urlsafe(32)
    store.tokens[token] = time.time() + config.TOKEN_TTL_SECONDS
    return {"access_token": token, "token_type": "Bearer", "expires_in": config.TOKEN_TTL_SECONDS}


async def require_token(authorization: str = Header(default="")):
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    expiry = store.tokens.get(authorization.split(" ", 1)[1])
    if not expiry or expiry < time.time():
        raise HTTPException(401, "invalid or expired token")
