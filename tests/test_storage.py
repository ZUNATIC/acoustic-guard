import sqlite3

import pytest

from app.storage import AlertStore


@pytest.fixture
def store(tmp_path):
    return AlertStore(db_path=tmp_path / "test.db")


def _alert(store, text="root password shared", severity="HIGH", **kw):
    return store.insert_alert(text, 0.9, severity, 0.95, 0.2,
                              [{"category": "credentials", "phrase": "root password", "weight": 0.95}], **kw)


def test_insert_and_list(store):
    _alert(store, language="ur", translation="the root password", sources=["keyword"], categories=["credentials"])
    total, alerts = store.list_alerts()
    assert total == 1
    a = alerts[0]
    assert a["severity"] == "HIGH" and a["transcript"] == "root password shared"
    assert a["language"] == "ur" and a["translation"] == "the root password"
    assert a["categories"] == ["credentials"] and a["sources"] == ["keyword"]


def test_filter_by_severity_and_language(store):
    _alert(store, "a", "HIGH", language="en")
    _alert(store, "b", "CRITICAL", language="ur")
    total, alerts = store.list_alerts(severity="critical")
    assert total == 1 and alerts[0]["transcript"] == "b"
    total, _ = store.list_alerts(language="en")
    assert total == 1


def test_data_minimisation_omits_transcript(store):
    _alert(store, "sensitive words here", translation="also sensitive", store_transcript=False)
    a = store.list_alerts()[1][0]
    assert a["transcript"] is None and a["translation"] is None
    assert a["transcript_hash"] is not None


def test_stats(store):
    _alert(store, "a", "HIGH", language="en", categories=["credentials"])
    _alert(store, "b", "HIGH", language="ur", categories=["credentials", "pii"])
    _alert(store, "c", "CRITICAL", language="ur", latency_ms=1200)
    stats = store.stats()
    assert stats["total_alerts"] == 3
    assert stats["by_severity"] == {"HIGH": 2, "CRITICAL": 1}
    assert stats["by_language"]["ur"] == 2
    assert stats["by_category"]["credentials"] == 2
    assert stats["avg_alert_latency_ms"] == 1200


def test_audit_chain_valid(store):
    store.record_consent("stream_start", "Umae Habiba")
    _alert(store)
    _alert(store, "second")
    report = store.verify_chain()
    assert report["valid"] and report["entries"] == 3


def test_audit_chain_detects_edited_alert(store):
    alert_id = _alert(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE alerts SET severity='LOW' WHERE id=?", (alert_id,))
    report = store.verify_chain()
    assert not report["valid"]
    assert any("altered" in p for p in report["problems"])


def test_audit_chain_detects_silent_deletion(store):
    alert_id = _alert(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    assert not store.verify_chain()["valid"]


def test_audit_chain_detects_rewritten_history(store):
    _alert(store)
    _alert(store, "two")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE audit_chain SET payload_hash='00' WHERE seq=1")
    assert not store.verify_chain()["valid"]


def test_erasure_is_recorded_and_chain_stays_valid(store):
    _alert(store)
    _alert(store)
    removed = store.purge_all(operator="admin")
    assert removed == 2 and store.list_alerts()[0] == 0
    report = store.verify_chain()
    assert report["valid"], report


def test_consent_history(store):
    store.record_consent("stream_start", "Operator A")
    store.record_consent("stream_stop", "Operator A")
    rows = store.consent_history()
    assert [r["action"] for r in rows] == ["stream_stop", "stream_start"]
    assert rows[0]["operator"] == "Operator A"


def test_environment_events(store):
    store.insert_environment_event("sudden_sound", -20.0, -45.0)
    events = store.list_environment_events()
    assert events[0]["kind"] == "sudden_sound" and events[0]["delta_db"] == 25.0
    assert store.stats()["environment_events_last_24h"] == 1


def test_outbox_lifecycle(store):
    event_id = store.enqueue_event({"type": "acoustic_alert", "severity": "HIGH"})
    assert [e["id"] for e in store.due_events()] == [event_id]
    store.mark_failed(event_id, "HTTP 503", retry_in=60)
    assert store.due_events() == []
    assert store.outbox_stats()["last_error"] == "HTTP 503"
    store.mark_delivered(event_id)
    assert store.outbox_stats() == {"pending": 0, "delivered": 1, "last_error": None}


def test_legacy_schema_is_moved_aside(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts REAL, transcript TEXT)")
        conn.execute("INSERT INTO alerts (ts, transcript) VALUES (1, 'old')")
    store = AlertStore(db_path=db)
    _alert(store)
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "alerts_v1" in tables
    assert store.list_alerts()[0] == 1
