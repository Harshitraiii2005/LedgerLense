"""JWT (HS256) authentication and role-based access.

Roles: "analyst" can read; "admin" can read and view the audit log.
The signing secret comes from the JWT_SECRET env var (a Kubernetes Secret in-cluster).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, get_args

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.errors import ApiError

Role = Literal["analyst", "admin"]
ROLES: tuple[str, ...] = get_args(Role)


@dataclass(frozen=True)
class Principal:
    sub: str
    role: Role


def mint_token(secret: str, sub: str, role: Role, ttl: timedelta = timedelta(hours=8)) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"sub": sub, "role": role, "iat": now, "exp": now + ttl}, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> Principal:
    try:
        # algorithms is pinned: never let the token header choose (e.g. "none").
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "token_expired", "token has expired") from None
    except jwt.InvalidTokenError:
        raise ApiError(401, "unauthorized", "invalid token") from None
    if claims.get("role") not in ROLES:
        raise ApiError(403, "forbidden", "unknown role")
    return Principal(sub=str(claims["sub"]), role=claims["role"])


_bearer = HTTPBearer(auto_error=False)


def current_principal(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    if creds is None:
        raise ApiError(401, "unauthorized", "missing bearer token")
    return decode_token(request.app.state.settings.jwt_secret, creds.credentials)


def require_admin(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if p.role != "admin":
        raise ApiError(403, "forbidden", "admin role required")
    return p


AnyUser = Annotated[Principal, Depends(current_principal)]
Admin = Annotated[Principal, Depends(require_admin)]
