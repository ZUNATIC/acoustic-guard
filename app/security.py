import hmac
import ipaddress

from fastapi import HTTPException, Request, WebSocket, status

from app.config import get_settings

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind_is_safe() -> None:
    s = get_settings()
    if not is_loopback(s.host) and not s.api_token:
        raise RuntimeError(
            f"refusing to listen on {s.host} without ACOUSTIC_API_TOKEN set; "
            "bind to 127.0.0.1 or configure a token"
        )


def _token_ok(presented: str | None) -> bool:
    expected = get_settings().api_token
    if not expected:
        return True
    return bool(presented) and hmac.compare_digest(presented, expected)


def _extract(headers, query) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-token") or query.get("token")


def require_token(request: Request) -> None:
    if not _token_ok(_extract(request.headers, request.query_params)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API token")


async def websocket_authorized(ws: WebSocket) -> bool:
    return _token_ok(_extract(ws.headers, ws.query_params))
