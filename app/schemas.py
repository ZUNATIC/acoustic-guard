from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    translation: str | None = Field(None, max_length=4000)
    language: str | None = Field(None, max_length=8)


class StreamStartRequest(BaseModel):
    consent_acknowledged: bool = False
    operator: str | None = Field(None, max_length=120)
    device: int | str | None = None


class StreamStopRequest(BaseModel):
    operator: str | None = Field(None, max_length=120)


class StatusResponse(BaseModel):
    status: str
    reason: str | None = None
    device: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    monitoring_active: bool
    module_state: str
    microphone_available: bool
    audio_device: str | None
    models_loaded: dict[str, bool]


class PrivacyManifest(BaseModel):
    raw_audio_ever_stored: bool = False
    raw_audio_ever_transmitted: bool = False
    processing_location: str = "local-only"
    transcript_stored: bool
    transcript_sent_to_backend: bool
    retention_days: int
    consent_required: bool
    audit_trail: str = "sha256 hash chain over alerts, consent and erasure records"
    fields_persisted: list[str]
    fields_sent_to_backend: list[str]
