import logging
import threading
import time
from typing import Callable

from app.config import get_settings

log = logging.getLogger("acoustic.hardware")

try:
    import sounddevice as sd

    _IMPORT_ERROR: str | None = None
except Exception as exc:  # PortAudio library missing or no audio server reachable
    sd = None
    _IMPORT_ERROR = f"audio subsystem unavailable ({exc})"

STATE_ACTIVE = "active"
STATE_READY = "ready"
STATE_DISABLED = "disabled_no_hardware"


def list_input_devices(refresh: bool = False) -> tuple[list[dict], str | None]:
    """Enumerate capture devices. Returns (devices, error)."""
    settings = get_settings()
    if settings.simulate_no_microphone:
        return [], "no microphone (simulated)"
    if sd is None:
        return [], _IMPORT_ERROR
    try:
        if refresh:
            sd._terminate()
            sd._initialize()
        devices = []
        default_in = sd.default.device[0] if sd.default.device else None
        for idx, dev in enumerate(sd.query_devices()):
            # ".monitor" sources record the speakers' output, not a microphone
            if dev.get("max_input_channels", 0) > 0 and not dev["name"].endswith(".monitor"):
                devices.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "default_samplerate": dev.get("default_samplerate"),
                    "is_default": idx == default_in,
                })
        if not devices:
            return [], "no audio input device found"
        return devices, None
    except Exception as exc:
        return [], f"audio device query failed: {exc}"


def resolve_device(devices: list[dict], wanted) -> dict | None:
    if not devices:
        return None
    if wanted is None or wanted == "":
        return next((d for d in devices if d["is_default"]), devices[0])
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
        idx = int(wanted)
        return next((d for d in devices if d["index"] == idx), None)
    wanted = str(wanted).lower()
    return next((d for d in devices if wanted in d["name"].lower()), None)


class HardwareMonitor:
    """Hardware capability detection (proposal section 7.7). Checks for a microphone at start-up
    and keeps polling, so the module switches itself on or off when a mic is plugged or removed,
    without restarting the service."""

    def __init__(self):
        self.settings = get_settings()
        self.devices: list[dict] = []
        self.error: str | None = None
        self.checked_at: float | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.streaming = False
        self._empty_scans = 0
        self.refresh(initial=True)

    @property
    def available(self) -> bool:
        return bool(self.devices)

    def on_change(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def refresh(self, initial: bool = False) -> bool:
        before = {d["name"] for d in self.devices}
        devices, error = list_input_devices(refresh=not initial and not self.streaming)
        self.checked_at = time.time()
        if self.streaming and not devices and error and "query failed" in error:
            return False
        # a single empty scan is usually another audio client re-initialising at the same moment;
        # only report "no microphone" when two scans in a row agree
        if not devices and self.devices and not initial:
            self._empty_scans += 1
            if self._empty_scans < 2:
                return False
        else:
            self._empty_scans = 0
        self.devices, self.error = devices, error
        changed = before != {d["name"] for d in devices}
        if changed and not initial:
            log.info("audio input devices changed", extra={"devices": [d["name"] for d in devices]})
            for cb in self._listeners:
                try:
                    cb(self.status())
                except Exception:
                    log.exception("hardware listener failed")
        return changed

    def status(self, monitoring: bool = False) -> dict:
        state = STATE_ACTIVE if monitoring else (STATE_READY if self.available else STATE_DISABLED)
        selected = resolve_device(self.devices, self.settings.audio_device)
        return {
            "microphone_available": self.available,
            "state": state,
            "reason": None if self.available else self.error,
            "devices": self.devices,
            "selected_device": selected,
            "checked_at": self.checked_at,
        }

    def start(self) -> None:
        if self._thread is not None or self.settings.hardware_poll_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name="hardware-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.hardware_poll_seconds):
            if self.streaming:
                continue
            try:
                self.refresh()
            except Exception:
                log.exception("hardware poll failed")


_monitor: HardwareMonitor | None = None


def get_hardware_monitor() -> HardwareMonitor:
    global _monitor
    if _monitor is None:
        _monitor = HardwareMonitor()
    return _monitor


def reset_hardware_monitor() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.stop()
    _monitor = None
