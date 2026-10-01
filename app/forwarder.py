import hashlib
import hmac
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from app.config import get_settings
from app.storage import get_alert_store
from app.threat_engine import severity_rank

log = logging.getLogger("acoustic.forwarder")

MODULE = "acoustic_guard"
MAX_BACKOFF = 300.0


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat(timespec="milliseconds")


def sign(body: bytes, secret: str, timestamp: str) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_alert_event(alert_id: int, result, transcription: dict | None, latency_ms: int | None) -> dict:
    """Structured alert for the ExfilGuard backend. Metadata only - never audio, and the
    transcript is included only when the deployment explicitly opts in."""
    s = get_settings()
    event = {
        "event_id": str(uuid.uuid4()),
        "type": "acoustic_alert",
        "module": MODULE,
        "endpoint_id": s.endpoint_id,
        "timestamp": iso(),
        "alert_id": alert_id,
        "severity": result.severity,
        "threat_score": result.threat_score,
        "categories": result.categories,
        "keywords": [m["phrase"] for m in result.matches][:10],
        "language": result.language,
        "sources": result.sources,
        "risk_factors": result.risk_factors,
        "review_only": result.review_only,
        "latency_ms": latency_ms,
    }
    if transcription:
        event["language_probability"] = transcription.get("language_probability")
    if s.store_transcript and s.backend_include_transcript:
        event["transcript"] = result.transcript
        event["translation"] = result.translation
    return event


def build_status_event(status: dict) -> dict:
    s = get_settings()
    return {
        "event_id": str(uuid.uuid4()),
        "type": "module_status",
        "module": MODULE,
        "endpoint_id": s.endpoint_id,
        "timestamp": iso(),
        **status,
    }


class BackendForwarder:
    def __init__(self):
        self.settings = get_settings()
        self.store = get_alert_store()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_success: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.backend_url)

    def enqueue(self, event: dict) -> int | None:
        if not self.enabled:
            return None
        if event.get("type") == "acoustic_alert":
            if severity_rank(event["severity"]) < severity_rank(self.settings.backend_min_severity):
                return None
        event_id = self.store.enqueue_event(event)
        self._wake.set()
        return event_id

    def _post(self, event: dict) -> tuple[bool, str]:
        s = self.settings
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"ExfilGuard-AcousticGuard/{event.get('module')}",
            "X-ExfilGuard-Module": MODULE,
            "X-ExfilGuard-Endpoint": s.endpoint_id,
            "X-ExfilGuard-Timestamp": timestamp,
            "Idempotency-Key": event["event_id"],
        }
        if s.backend_token:
            headers["Authorization"] = f"Bearer {s.backend_token}"
        if s.backend_hmac_secret:
            headers["X-ExfilGuard-Signature"] = sign(body, s.backend_hmac_secret, timestamp)

        req = urllib.request.Request(s.backend_url, data=body, headers=headers, method="POST")
        ctx = None
        if s.backend_url.startswith("https"):
            ctx = ssl.create_default_context()
            if not s.backend_verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=s.backend_timeout_seconds, context=ctx) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def flush_once(self) -> int:
        delivered = 0
        for row in self.store.due_events():
            ok, err = self._post(json.loads(row["event_json"]))
            if ok:
                self.store.mark_delivered(row["id"])
                self.last_success = time.time()
                delivered += 1
            else:
                backoff = min(MAX_BACKOFF, 5.0 * (2 ** row["attempts"]))
                self.store.mark_failed(row["id"], err, backoff)
                log.warning("backend delivery failed", extra={"event": row["id"], "error": err, "retry_in": backoff})
                break
        return delivered

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="backend-forwarder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception:
                log.exception("forwarder loop error")
            self._wake.wait(5.0)
            self._wake.clear()

    def status(self) -> dict:
        info = {"enabled": self.enabled, "url": self.settings.backend_url, "last_success": self.last_success}
        if self.enabled:
            info.update(self.store.outbox_stats())
        return info


_forwarder: BackendForwarder | None = None


def get_forwarder() -> BackendForwarder:
    global _forwarder
    if _forwarder is None:
        _forwarder = BackendForwarder()
    return _forwarder


def reset_forwarder() -> None:
    global _forwarder
    if _forwarder is not None:
        _forwarder.stop()
    _forwarder = None
