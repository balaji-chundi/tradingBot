"""HTTP Basic Auth dependency for the dashboard.

When `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are both set in .env, every
dashboard route requires those credentials. When either is empty, the gate
is bypassed (dev mode / unit tests).

Caveat: this is HTTP, not HTTPS, by default. The Basic Auth header travels
in cleartext (just base64 encoded). Use a strong unique password and assume
it's sniffable on hostile networks. Layer HTTPS before Phase 8 live trading.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_security = HTTPBasic(auto_error=False)


def require_auth(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)],
) -> str:
    settings = get_settings()
    expected_user = settings.dashboard_user
    expected_pass = settings.dashboard_password

    # Bypass when not configured — covers `make test` and the bootstrap window
    # before the user sets credentials.
    if not expected_user or not expected_pass:
        return "unauth"

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="trading-app"'},
        )

    # Constant-time compare to avoid timing attacks
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": 'Basic realm="trading-app"'},
        )
    return credentials.username
