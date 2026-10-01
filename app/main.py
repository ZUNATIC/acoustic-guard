import asyncio
import csv
import io
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.audio_io import AudioFormatError, decode_wav
from app.audio_stream import get_engine
from app.config import BASE_DIR, get_settings, load_threat_registry
from app.forwarder import get_forwarder
from app.hardware import get_hardware_monitor
from app.keyword_engine import get_keyword_engine, reset_keyword_engine
from app.logging_setup import configure_logging
from app.patterns import reset_pattern_detector
from app.pipeline import get_pipeline
from app.schemas import (
    AnalyzeRequest, HealthResponse, PrivacyManifest, StatusResponse, StreamStartRequest, StreamStopRequest,
)
from app.security import check_bind_is_safe, require_token, websocket_authorized
from app.semantic import get_semantic_scorer
from app.storage import get_alert_store
from app.threat_engine import get_threat_engine, reset_threat_engine
from app.transcriber import LANGUAGE_NAMES, loaded_speech_models

VERSION = "2.1.0"
log = logging.getLogger("acoustic.api")


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


async def _retention_loop():
    settings = get_settings()
    while True:
        try:
            removed = get_alert_store().purge_older_than(settings.retention_days)
            if removed:
                log.info("retention purge", extra={"removed": removed, "days": settings.retention_days})
        except Exception:
            log.exception("retention purge failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    check_bind_is_safe()

    get_keyword_engine()
    threat_engine = get_threat_engine()
    threat_engine.set_semantic_scorer(get_semantic_scorer())

    loop = asyncio.get_running_loop()
    engine = get_engine()

    def _broadcast(event: dict):
        if manager.active:
            asyncio.run_coroutine_threadsafe(manager.broadcast(event), loop)

    engine.on_telemetry(_broadcast)

    hardware = get_hardware_monitor()
    hardware.on_change(lambda status: _broadcast({"type": "hardware", **status}))
    hardware.start()
    get_forwarder().start()
    retention = asyncio.create_task(_retention_loop())

    log.info("acoustic guard ready", extra={
        "version": VERSION, "microphone": hardware.available, "host": settings.host, "port": settings.port,
        "keywords": get_keyword_engine().summary(),
    })
    yield

    retention.cancel()
    if engine.is_running:
        engine.stop()
    hardware.stop()
    get_forwarder().stop()


app = FastAPI(
    title="ExfilGuard Acoustic Guard",
    version=VERSION,
    description="Local, privacy-preserving detection of spoken data disclosure (ExfilGuard module).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

auth = [Depends(require_token)]


def _models_loaded() -> dict[str, bool]:
    settings = get_settings()
    loaded = loaded_speech_models()
    return {
        "keyword_engine": True,
        "semantic": get_semantic_scorer() is not None,
        "vad": get_engine()._vad is not None,
        "whisper": settings.whisper_model in loaded,
        "kws": settings.kws_model in loaded,
        "verifier": bool(settings.verifier_model) and settings.verifier_model in loaded,
    }


# ---- status ----------------------------------------------------------------------------


@app.get("/api/v1/health", response_model=HealthResponse, tags=["status"])
def health():
    engine = get_engine()
    hw = get_hardware_monitor().status(monitoring=engine.is_running)
    device = engine.device or hw["selected_device"]
    return HealthResponse(
        status="ok",
        version=VERSION,
        monitoring_active=engine.is_running,
        module_state=hw["state"],
        microphone_available=hw["microphone_available"],
        audio_device=device["name"] if device else None,
        models_loaded=_models_loaded(),
    )


@app.get("/api/v1/status", tags=["status"])
def status():
    settings = get_settings()
    engine = get_engine()
    return {
        "version": VERSION,
        "endpoint_id": settings.endpoint_id,
        **engine.status(),
        "models": {
            "speech": settings.whisper_model,
            "keyword_spotting": settings.kws_model if settings.kws_enabled else None,
            "verifier": settings.verifier_model,
            "semantic": settings.semantic_model,
            "loaded": _models_loaded(),
        },
        "noise_reduction": {"mode": engine.noise.mode, "applied": engine.noise.applied, "skipped": engine.noise.skipped},
        "backend": get_forwarder().status(),
        "latest": engine.latest_telemetry,
        "level": engine.latest_level,
    }


@app.get("/api/v1/capabilities", tags=["status"])
def capabilities():
    settings = get_settings()
    hw = get_hardware_monitor()
    if not get_engine().is_running:
        hw.refresh()
    kw = get_keyword_engine().summary()
    return {
        "module": "acoustic_guard",
        "version": VERSION,
        "hardware": hw.status(monitoring=get_engine().is_running),
        "features": {
            "multilingual_speech": True,
            "translation_to_english": settings.translate_non_english,
            "keyword_spotting": settings.kws_enabled,
            "phonetic_matching": True,
            "semantic_intent": get_semantic_scorer() is not None,
            "sensitive_number_patterns": True,
            "second_opinion_verifier": bool(settings.verifier_model),
            "noise_reduction": settings.noise_reduction,
            "environment_analysis": settings.environment_analysis,
            "backend_forwarding": get_forwarder().enabled,
            "api_token_required": bool(settings.api_token),
            "browser_capture": True,
        },
        "keyword_registry": kw,
    }


@app.get("/api/v1/languages", tags=["status"])
def languages():
    settings = get_settings()
    return {
        "speech_recognition": "Whisper multilingual - automatic detection across ~99 languages",
        "named": LANGUAGE_NAMES,
        "keyword_variants": get_keyword_engine().summary()["languages"],
        "remap": settings.language_remap,
        "forced_language": settings.whisper_language,
        "translation_to_english": settings.translate_non_english,
    }


# ---- monitoring ------------------------------------------------------------------------


@app.post("/api/v1/stream/start", response_model=StatusResponse, tags=["monitoring"], dependencies=auth)
def stream_start(req: StreamStartRequest):
    result = get_engine().start(consent_acknowledged=req.consent_acknowledged, operator=req.operator, device=req.device)
    code = {"consent_required": 428, "disabled_no_hardware": 409, "error": 500}.get(result["status"], 200)
    return JSONResponse(status_code=code, content=StatusResponse(**result).model_dump())


@app.post("/api/v1/stream/stop", response_model=StatusResponse, tags=["monitoring"], dependencies=auth)
def stream_stop(req: StreamStopRequest | None = None):
    return StatusResponse(**get_engine().stop(operator=req.operator if req else None))


@app.get("/api/v1/stream/latest", tags=["monitoring"])
def stream_latest():
    return get_engine().latest_telemetry


@app.get("/api/v1/environment", tags=["monitoring"])
def environment(limit: int = 50):
    return {
        "live": get_engine().environment.snapshot(),
        "events": get_alert_store().list_environment_events(limit=min(limit, 500)),
    }


# ---- analysis --------------------------------------------------------------------------


@app.post("/api/v1/analyze", tags=["analysis"])
def analyze(req: AnalyzeRequest):
    result = get_threat_engine().evaluate(req.text, translation=req.translation, language=req.language)
    return result.as_dict()


@app.post("/api/v1/analyze/audio", tags=["analysis"], dependencies=auth)
async def analyze_audio(file: UploadFile = File(...), store: bool = Form(True), language: str | None = Form(None)):
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        audio = decode_wav(data, get_settings().sample_rate)
    except AudioFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    finally:
        del data

    pipeline = get_pipeline()
    started = time.monotonic()
    event, result, tr = await asyncio.to_thread(
        pipeline.process, audio, "upload", None, None, None, False, store, language or None,
    )
    if event is None:
        return {"type": "transcript", "source": "upload", "transcript": "", "severity": "NONE",
                "threat_score": 0.0, "is_violation": False, "no_speech": True, "transcription": tr.as_dict()}
    event["latency_ms"] = int((time.monotonic() - started) * 1000)
    get_engine().publish(dict(event))
    return event


# ---- alerts / audit --------------------------------------------------------------------


@app.get("/api/v1/alerts", tags=["alerts"])
def get_alerts(severity: str | None = None, language: str | None = None, limit: int = 100, offset: int = 0):
    total, alerts = get_alert_store().list_alerts(
        severity=severity, language=language, limit=max(1, min(limit, 1000)), offset=max(0, offset)
    )
    return {"total": total, "alerts": alerts}


@app.get("/api/v1/alerts/export.csv", tags=["alerts"])
def export_alerts(severity: str | None = None):
    _, alerts = get_alert_store().list_alerts(severity=severity, limit=100000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "time", "severity", "score", "language", "categories", "keywords", "sources",
                     "transcript", "translation", "review_only", "latency_ms", "endpoint"])
    for a in alerts:
        writer.writerow([
            a["id"], datetime.fromtimestamp(a["timestamp"]).isoformat(timespec="seconds"), a["severity"],
            a["threat_score"], a["language"], "|".join(a["categories"]),
            "|".join(m["phrase"] for m in a["matches"]), "|".join(a["sources"]),
            a["transcript"] or "", a["translation"] or "", a["review_only"], a["latency_ms"], a["endpoint_id"],
        ])
    name = f"acoustic_alerts_{datetime.now():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.delete("/api/v1/alerts", response_model=StatusResponse, tags=["alerts"], dependencies=auth)
def delete_alerts(operator: str | None = None):
    removed = get_alert_store().purge_all(operator=operator)
    return StatusResponse(status=f"purged_{removed}")


@app.get("/api/v1/stats", tags=["alerts"])
def get_stats():
    return {**get_alert_store().stats(), "session": get_engine().metrics}


@app.get("/api/v1/audit/verify", tags=["alerts"])
def audit_verify():
    return get_alert_store().verify_chain()


@app.get("/api/v1/consent", tags=["alerts"])
def consent_history(limit: int = 50):
    return {"records": get_alert_store().consent_history(limit=min(limit, 500))}


# ---- policy ----------------------------------------------------------------------------


@app.get("/api/v1/policy", tags=["policy"])
def get_policy():
    return load_threat_registry()


@app.get("/api/v1/policy/keywords", tags=["policy"])
def monitored_keywords():
    registry = load_threat_registry()
    out = {}
    for name, cat in registry["categories"].items():
        phrases = [p if isinstance(p, str) else p.get("text") for p in cat.get("phrases", [])]
        variants = {lang: [p if isinstance(p, str) else p.get("text") for p in items or []]
                    for lang, items in (cat.get("variants") or {}).items()}
        out[name] = {"weight": cat["weight"], "note": cat.get("note"), "en": phrases, **variants}
    return {"categories": out, "patterns": registry.get("sensitive_patterns", {}), "summary": get_keyword_engine().summary()}


def _validate_registry(registry: dict) -> None:
    cats = registry.get("categories")
    if not isinstance(cats, dict) or not cats:
        raise HTTPException(status_code=422, detail="registry needs a non-empty 'categories' mapping")
    for name, cat in cats.items():
        if not isinstance(cat, dict) or not 0 < float(cat.get("weight", 0)) <= 1:
            raise HTTPException(status_code=422, detail=f"category '{name}' needs a weight in (0, 1]")
    scoring = registry.get("scoring", {})
    if "severity_bands" not in scoring or "semantic_weight" not in scoring:
        raise HTTPException(status_code=422, detail="registry needs scoring.semantic_weight and scoring.severity_bands")
    if "semantic_reference_phrases" not in registry:
        raise HTTPException(status_code=422, detail="registry needs semantic_reference_phrases")


@app.put("/api/v1/policy", response_model=StatusResponse, tags=["policy"], dependencies=auth)
def update_policy(registry: dict):
    _validate_registry(registry)
    path = get_settings().threats_config_path
    if path.exists():
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(registry, f, sort_keys=False, allow_unicode=True)

    load_threat_registry.cache_clear()
    reset_keyword_engine()
    reset_pattern_detector()
    reset_threat_engine()
    from app.semantic import reset_semantic_scorer

    reset_semantic_scorer()
    get_threat_engine().set_semantic_scorer(get_semantic_scorer())
    return StatusResponse(status="policy_updated")


@app.get("/api/v1/privacy/manifest", response_model=PrivacyManifest, tags=["policy"])
def privacy_manifest():
    s = get_settings()
    persisted = ["timestamp", "severity", "threat_score", "matched_categories", "matched_phrases",
                 "language", "sources", "risk_factors", "endpoint_id", "transcript_hash"]
    if s.store_transcript:
        persisted += ["transcript", "english_translation"]
    sent = ["event_id", "timestamp", "endpoint_id", "severity", "threat_score", "categories", "keywords",
            "language", "sources", "risk_factors", "latency_ms"]
    include = s.store_transcript and s.backend_include_transcript
    if include:
        sent += ["transcript", "translation"]
    return PrivacyManifest(
        transcript_stored=s.store_transcript,
        transcript_sent_to_backend=include,
        retention_days=s.retention_days,
        consent_required=s.require_consent,
        fields_persisted=persisted,
        fields_sent_to_backend=sent if s.backend_url else [],
    )


# ---- live feed -------------------------------------------------------------------------


@app.websocket("/api/v1/ws")
async def websocket_endpoint(ws: WebSocket):
    if not await websocket_authorized(ws):
        # accept first so the browser receives the 4401 code and can ask for the token
        await ws.accept()
        await ws.close(code=4401, reason="API token required")
        return
    await manager.connect(ws)
    engine = get_engine()
    try:
        await ws.send_json({"type": "hello", "version": VERSION, **engine.status(), "latest": engine.latest_telemetry})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


@app.websocket("/api/v1/audio")
async def audio_ingest(ws: WebSocket):
    """Live microphone audio from a browser.

    1. client sends {"type": "start", "consent_acknowledged": true, "operator": "...", "label": "..."}
    2. server answers {"type": "started"} (or {"type": "error", ...} and closes)
    3. client streams binary frames: 16 kHz mono little-endian int16 PCM
    4. client sends {"type": "stop"} or simply disconnects
    """
    if not await websocket_authorized(ws):
        await ws.accept()
        await ws.close(code=4401, reason="API token required")
        return
    await ws.accept()
    engine = get_engine()
    started = False
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=15)
        if hello.get("type") != "start":
            await ws.send_json({"type": "error", "status": "bad_request", "reason": "first message must be start"})
            await ws.close(code=4400)
            return
        client = ws.client.host if ws.client else None
        result = engine.start_remote(
            consent_acknowledged=bool(hello.get("consent_acknowledged")),
            operator=(hello.get("operator") or None),
            label=str(hello.get("label") or "")[:120] or None,
            client=client,
        )
        if result["status"] != "monitoring_started":
            await ws.send_json({"type": "error", **result})
            await ws.close(code=4409)
            return
        started = True
        await ws.send_json({"type": "started", **result})
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                if not engine.is_running or engine.source != "browser":
                    await ws.send_json({"type": "stopped"})
                    break
                engine.feed_remote(msg["bytes"])
            elif msg.get("text"):
                try:
                    control = json.loads(msg["text"])
                except ValueError:
                    continue
                if control.get("type") == "stop":
                    break
                if control.get("type") == "device" and engine.device is not None:
                    label = str(control.get("label") or "")[:120]
                    client = engine.device.get("client")
                    engine.device["name"] = label + (f" @ {client}" if client else "")
                    engine.publish({"type": "status", "status": "listening", "device": engine.device["name"], "source": "browser"})
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if started and engine.is_running and engine.source == "browser":
            await asyncio.to_thread(engine.stop)
        # the browser has usually gone already (tab closed, page reloaded); nothing to close then
        try:
            await ws.close()
        except Exception:
            pass


static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        # always revalidate the page so a new version's asset URLs are picked up
        return FileResponse(str(static_dir / "index.html"), headers={"Cache-Control": "no-cache"})
