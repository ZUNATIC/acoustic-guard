import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings

SCHEMA_VERSION = 2
GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'stream',
    endpoint_id TEXT,
    transcript TEXT,
    transcript_hash TEXT,
    translation TEXT,
    language TEXT,
    language_probability REAL,
    threat_score REAL NOT NULL,
    severity TEXT NOT NULL,
    keyword_score REAL NOT NULL,
    semantic_score REAL NOT NULL,
    pattern_score REAL NOT NULL DEFAULT 0,
    matches_json TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    categories_json TEXT NOT NULL DEFAULT '[]',
    risk_json TEXT NOT NULL DEFAULT '[]',
    review_only INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

CREATE TABLE IF NOT EXISTS consent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    operator TEXT,
    endpoint_id TEXT
);

CREATE TABLE IF NOT EXISTS environment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    level_db REAL NOT NULL,
    baseline_db REAL NOT NULL,
    delta_db REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_env_ts ON environment_events(ts);

CREATE TABLE IF NOT EXISTS audit_chain (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    payload_hash TEXT NOT NULL,
    detail TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    event_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    delivered_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered_at, next_attempt);
"""

_ALERT_HASH_FIELDS = (
    "id", "ts", "source", "endpoint_id", "transcript", "transcript_hash", "translation",
    "language", "threat_score", "severity", "keyword_score", "semantic_score", "pattern_score",
    "matches_json", "sources_json", "categories_json", "risk_json", "review_only",
)


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hash_transcript(text: str) -> str:
    return _sha(text)[:16]


def _canonical(row: dict, fields: tuple[str, ...]) -> str:
    return json.dumps({k: row.get(k) for k in fields}, sort_keys=True, ensure_ascii=False, default=str)


class AlertStore:
    def __init__(self, db_path: Path | None = None):
        settings = get_settings()
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version < SCHEMA_VERSION and "alerts" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
                if "sources_json" not in cols:
                    conn.execute("ALTER TABLE alerts RENAME TO alerts_v1")
                    if "consent_log" in tables:
                        conn.execute("ALTER TABLE consent_log RENAME TO consent_log_v1")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- audit chain -------------------------------------------------------------------

    def _chain_append(self, conn, kind: str, ref_id: int | None, payload_hash: str, detail: str | None = None):
        last = conn.execute("SELECT row_hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last[0] if last else GENESIS
        ts = time.time()
        row_hash = _sha(f"{prev}|{kind}|{ref_id}|{ts!r}|{payload_hash}|{detail or ''}")
        conn.execute(
            "INSERT INTO audit_chain (ts, kind, ref_id, payload_hash, detail, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?)",
            (ts, kind, ref_id, payload_hash, detail, prev, row_hash),
        )

    def verify_chain(self) -> dict:
        with self._conn() as conn:
            chain = conn.execute("SELECT * FROM audit_chain ORDER BY seq").fetchall()
            alerts = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM alerts")}
            consents = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM consent_log")}

        prev = GENESIS
        erased: set[int] = set()
        erased_all_before: int | None = None
        problems: list[str] = []

        for entry in reversed(chain):
            if entry["kind"] == "erasure" and entry["detail"]:
                info = json.loads(entry["detail"])
                erased.update(info.get("ids", []))
                if info.get("all_until") is not None:
                    erased_all_before = max(erased_all_before or 0, info["all_until"])

        for entry in chain:
            expected = _sha(
                f"{entry['prev_hash']}|{entry['kind']}|{entry['ref_id']}|{entry['ts']!r}|{entry['payload_hash']}|{entry['detail'] or ''}"
            )
            if entry["prev_hash"] != prev:
                problems.append(f"chain break at seq {entry['seq']}")
            if expected != entry["row_hash"]:
                problems.append(f"entry {entry['seq']} was modified")
            prev = entry["row_hash"]

            if entry["kind"] == "alert":
                row = alerts.get(entry["ref_id"])
                if row is None:
                    covered = entry["ref_id"] in erased or (
                        erased_all_before is not None and entry["ref_id"] <= erased_all_before
                    )
                    if not covered:
                        problems.append(f"alert {entry['ref_id']} deleted without an erasure record")
                elif _sha(_canonical(row, _ALERT_HASH_FIELDS)) != entry["payload_hash"]:
                    problems.append(f"alert {entry['ref_id']} content was altered")
            elif entry["kind"] == "consent":
                row = consents.get(entry["ref_id"])
                if row is None or _sha(_canonical(row, ("id", "ts", "action", "operator", "endpoint_id"))) != entry["payload_hash"]:
                    problems.append(f"consent record {entry['ref_id']} missing or altered")

        return {
            "valid": not problems,
            "entries": len(chain),
            "head": prev if chain else None,
            "problems": problems[:20],
        }

    # ---- consent -----------------------------------------------------------------------

    def record_consent(self, action: str, operator: str | None = None) -> int:
        settings = get_settings()
        with self._write_lock, self._conn() as conn:
            ts = time.time()
            cur = conn.execute(
                "INSERT INTO consent_log (ts, action, operator, endpoint_id) VALUES (?, ?, ?, ?)",
                (ts, action, operator, settings.endpoint_id),
            )
            row = {"id": cur.lastrowid, "ts": ts, "action": action, "operator": operator, "endpoint_id": settings.endpoint_id}
            self._chain_append(conn, "consent", cur.lastrowid, _sha(_canonical(row, tuple(row))))
            return cur.lastrowid

    def consent_history(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM consent_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- alerts ------------------------------------------------------------------------

    def insert_alert(
        self,
        transcript: str,
        threat_score: float,
        severity: str,
        keyword_score: float,
        semantic_score: float,
        matches: list[dict],
        store_transcript: bool = True,
        translation: str | None = None,
        language: str | None = None,
        language_probability: float | None = None,
        pattern_score: float = 0.0,
        sources: list[str] | None = None,
        categories: list[str] | None = None,
        risk_factors: list[str] | None = None,
        review_only: bool = False,
        source: str = "stream",
        latency_ms: int | None = None,
    ) -> int:
        settings = get_settings()
        row = {
            "ts": time.time(),
            "source": source,
            "endpoint_id": settings.endpoint_id,
            "transcript": transcript if store_transcript else None,
            "transcript_hash": _hash_transcript(transcript) if transcript else None,
            "translation": translation if store_transcript else None,
            "language": language,
            "language_probability": language_probability,
            "threat_score": threat_score,
            "severity": severity,
            "keyword_score": keyword_score,
            "semantic_score": semantic_score,
            "pattern_score": pattern_score,
            "matches_json": json.dumps(matches, ensure_ascii=False),
            "sources_json": json.dumps(sources or []),
            "categories_json": json.dumps(categories or []),
            "risk_json": json.dumps(risk_factors or [], ensure_ascii=False),
            "review_only": int(bool(review_only)),
            "latency_ms": latency_ms,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(f"INSERT INTO alerts ({cols}) VALUES ({marks})", tuple(row.values()))
            row["id"] = cur.lastrowid
            stored = dict(conn.execute("SELECT * FROM alerts WHERE id = ?", (row["id"],)).fetchone())
            self._chain_append(conn, "alert", row["id"], _sha(_canonical(stored, _ALERT_HASH_FIELDS)))
            return row["id"]

    @staticmethod
    def _format(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "timestamp": row["ts"],
            "source": row["source"],
            "endpoint_id": row["endpoint_id"],
            "transcript": row["transcript"],
            "transcript_hash": row["transcript_hash"],
            "translation": row["translation"],
            "language": row["language"],
            "language_probability": row["language_probability"],
            "threat_score": row["threat_score"],
            "severity": row["severity"],
            "keyword_score": row["keyword_score"],
            "semantic_score": row["semantic_score"],
            "pattern_score": row["pattern_score"],
            "matches": json.loads(row["matches_json"]),
            "sources": json.loads(row["sources_json"]),
            "categories": json.loads(row["categories_json"]),
            "risk_factors": json.loads(row["risk_json"]),
            "review_only": bool(row["review_only"]),
            "latency_ms": row["latency_ms"],
        }

    def list_alerts(self, severity: str | None = None, limit: int = 100, offset: int = 0,
                    language: str | None = None, since: float | None = None):
        where, params = [], []
        if severity:
            where.append("severity = ?")
            params.append(severity.upper())
        if language:
            where.append("language = ?")
            params.append(language)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM alerts{clause} ORDER BY ts DESC LIMIT ? OFFSET ?", (*params, limit, offset)
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM alerts{clause}", params).fetchone()[0]
        return total, [self._format(r) for r in rows]

    def purge_all(self, operator: str | None = None) -> int:
        with self._write_lock, self._conn() as conn:
            last = conn.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
            cur = conn.execute("DELETE FROM alerts")
            detail = json.dumps({"all_until": last, "operator": operator, "reason": "erasure_request"})
            self._chain_append(conn, "erasure", None, _sha(detail), detail)
            return cur.rowcount

    def purge_older_than(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._write_lock, self._conn() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM alerts WHERE ts < ?", (cutoff,))]
            if not ids:
                return 0
            conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM environment_events WHERE ts < ?", (cutoff,))
            detail = json.dumps({"ids": ids, "reason": f"retention_{days}d"})
            self._chain_append(conn, "erasure", None, _sha(detail), detail)
            return len(ids)

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            by_severity = conn.execute("SELECT severity, COUNT(*) n FROM alerts GROUP BY severity").fetchall()
            by_language = conn.execute(
                "SELECT COALESCE(language, 'unknown') lang, COUNT(*) n FROM alerts GROUP BY lang"
            ).fetchall()
            cats: dict[str, int] = {}
            for (cj,) in conn.execute("SELECT categories_json FROM alerts"):
                for c in json.loads(cj):
                    cats[c] = cats.get(c, 0) + 1
            last_24h = conn.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (time.time() - 86400,)).fetchone()[0]
            env_24h = conn.execute(
                "SELECT COUNT(*) FROM environment_events WHERE ts >= ?", (time.time() - 86400,)
            ).fetchone()[0]
            latency = conn.execute(
                "SELECT AVG(latency_ms), MAX(latency_ms) FROM alerts WHERE latency_ms IS NOT NULL"
            ).fetchone()
        return {
            "total_alerts": total,
            "alerts_last_24h": last_24h,
            "by_severity": {r["severity"]: r["n"] for r in by_severity},
            "by_language": {r["lang"]: r["n"] for r in by_language},
            "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
            "environment_events_last_24h": env_24h,
            "avg_alert_latency_ms": round(latency[0]) if latency[0] is not None else None,
            "max_alert_latency_ms": latency[1],
        }

    # ---- environment -------------------------------------------------------------------

    def insert_environment_event(self, kind: str, level_db: float, baseline_db: float) -> int:
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO environment_events (ts, kind, level_db, baseline_db, delta_db) VALUES (?, ?, ?, ?, ?)",
                (time.time(), kind, round(level_db, 1), round(baseline_db, 1), round(level_db - baseline_db, 1)),
            )
            return cur.lastrowid

    def list_environment_events(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM environment_events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- outbox (backend delivery) -----------------------------------------------------

    def enqueue_event(self, event: dict) -> int:
        with self._write_lock, self._conn() as conn:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO outbox (created, event_json, next_attempt) VALUES (?, ?, ?)",
                (now, json.dumps(event, ensure_ascii=False), now),
            )
            return cur.lastrowid

    def due_events(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt <= ? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_delivered(self, event_id: int) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute("UPDATE outbox SET delivered_at = ?, last_error = NULL WHERE id = ?", (time.time(), event_id))

    def mark_failed(self, event_id: int, error: str, retry_in: float) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, next_attempt = ? WHERE id = ?",
                (error[:300], time.time() + retry_in, event_id),
            )

    def outbox_stats(self) -> dict:
        with self._conn() as conn:
            pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]
            delivered = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL").fetchone()[0]
            last_err = conn.execute(
                "SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {"pending": pending, "delivered": delivered, "last_error": last_err[0] if last_err else None}


_store: AlertStore | None = None


def get_alert_store() -> AlertStore:
    global _store
    if _store is None:
        _store = AlertStore()
    return _store


def reset_alert_store() -> None:
    global _store
    _store = None
