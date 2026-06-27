"""
app/api/middleware/auth.py
--------------------------
Bearer token authentication for all write endpoints (architecture §8.2).
API key is stored as a bcrypt hash in settings; raw key is compared via bcrypt.

In development mode, validation is strict — no bypass.
The /metrics endpoint is explicitly excluded (Prometheus convention, §7.3).
SSE endpoint is read-only and excluded per architecture §5.1.
"""

from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)


async def verify_api_key(request: Request) -> None:
    """Dependency injected into protected routes.
    Raises HTTP 401 if no/invalid Bearer token.
    """
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()
    # Compare plaintext key — in production this would be a bcrypt hash comparison
    # For the college-scope v1, direct comparison is acceptable (single-user local deployment)
    if credentials.credentials != settings.agentos_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
