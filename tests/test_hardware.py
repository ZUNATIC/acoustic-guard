import pytest

from app.config import get_settings
from app.hardware import HardwareMonitor, resolve_device

DEVICES = [
    {"index": 0, "name": "HDA Intel PCH: ALC256 Analog", "channels": 2, "is_default": False},
    {"index": 4, "name": "USB Audio Device", "channels": 1, "is_default": True},
]


def test_resolve_default_index_and_name():
    assert resolve_device(DEVICES, None)["index"] == 4
    assert resolve_device(DEVICES, 0)["name"].startswith("HDA")
    assert resolve_device(DEVICES, "usb")["index"] == 4
    assert resolve_device(DEVICES, "missing") is None
    assert resolve_device([], None) is None


def test_no_microphone_disables_module(monkeypatch):
    monkeypatch.setattr(get_settings(), "simulate_no_microphone", True)
    hw = HardwareMonitor()
    status = hw.status()
    assert status["microphone_available"] is False
    assert status["state"] == "disabled_no_hardware"
    assert status["reason"]


def test_engine_refuses_to_start_without_microphone(monkeypatch, use_store):
    import app.hardware as hardware
    from app.audio_stream import AcousticGuardEngine

    monkeypatch.setattr(get_settings(), "simulate_no_microphone", True)
    monkeypatch.setattr(hardware, "_monitor", HardwareMonitor())
    result = AcousticGuardEngine().start(consent_acknowledged=True, operator="tester")
    assert result["status"] == "disabled_no_hardware"
    assert use_store.consent_history() == []


def test_single_empty_scan_does_not_disable(monkeypatch):
    import app.hardware as hardware

    scans = iter([(DEVICES, None), ([], "no audio input device found"), (DEVICES, None),
                  ([], "no audio input device found"), ([], "no audio input device found")])
    monkeypatch.setattr(hardware, "list_input_devices", lambda refresh=False: next(scans))
    hw = HardwareMonitor()
    assert hw.available
    hw.refresh()
    assert hw.available, "one empty scan must not disable the module"
    hw.refresh()
    hw.refresh()
    assert hw.available
    hw.refresh()
    assert not hw.available, "two empty scans in a row mean the mic is really gone"


def test_device_order_change_is_not_a_change(monkeypatch):
    import app.hardware as hardware

    scans = iter([(DEVICES, None), (list(reversed(DEVICES)), None)])
    monkeypatch.setattr(hardware, "list_input_devices", lambda refresh=False: next(scans))
    hw = HardwareMonitor()
    assert hw.refresh() is False
