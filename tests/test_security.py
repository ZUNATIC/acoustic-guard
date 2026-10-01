import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.security import check_bind_is_safe, is_loopback


def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.10")


def test_refuses_public_bind_without_token(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "host", "0.0.0.0")
    monkeypatch.setattr(s, "api_token", None)
    with pytest.raises(RuntimeError):
        check_bind_is_safe()
    monkeypatch.setattr(s, "api_token", "secret")
    check_bind_is_safe()


@pytest.fixture
def secured(monkeypatch, use_store):
    monkeypatch.setattr(get_settings(), "api_token", "s3cret")
    with TestClient(app) as c:
        yield c


def test_mutating_routes_need_token(secured):
    assert secured.delete("/api/v1/alerts").status_code == 401
    assert secured.post("/api/v1/stream/stop").status_code == 401
    assert secured.delete("/api/v1/alerts", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert secured.delete("/api/v1/alerts", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert secured.delete("/api/v1/alerts", headers={"X-API-Token": "s3cret"}).status_code == 200


def test_read_routes_stay_open(secured):
    assert secured.get("/api/v1/health").status_code == 200
    assert secured.post("/api/v1/analyze", json={"text": "hello"}).status_code == 200


def test_websocket_needs_token(secured):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with secured.websocket_connect("/api/v1/ws") as ws:
            ws.receive_json()
    with secured.websocket_connect("/api/v1/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_websocket_rejection_carries_4401(secured):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with secured.websocket_connect("/api/v1/ws") as ws:
            ws.receive_json()
    assert exc.value.code == 4401
