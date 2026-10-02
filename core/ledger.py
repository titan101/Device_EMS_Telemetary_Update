"""Fleet ledger: one row per device, its latest run and state.

The per-run status.json is the authority for a run; the ledger is the
cross-run answer to "which of the 500 boxes are done". Rows are upserted,
never dropped. Schema changes go through _migrate (ALTER only).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device TEXT PRIMARY KEY,
    platform TEXT,
    model TEXT,
    version TEXT,
    state TEXT NOT NULL,
    detail TEXT,
    run_id TEXT,
    credential TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    cm_number TEXT,
    note TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_device(conn: sqlite3.Connection, device: str, state: str, detail: str, run_id: str,
                  now: str, platform: str = "", model: str = "", version: str = "",
                  credential: str = "") -> None:
    conn.execute(
        """INSERT INTO devices (device, platform, model, version, state, detail, run_id, credential,
                                created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device) DO UPDATE SET
             platform = COALESCE(NULLIF(excluded.platform, ''), devices.platform),
             model = COALESCE(NULLIF(excluded.model, ''), devices.model),
             version = COALESCE(NULLIF(excluded.version, ''), devices.version),
             credential = COALESCE(NULLIF(excluded.credential, ''), devices.credential),
             state = excluded.state, detail = excluded.detail, run_id = excluded.run_id,
             updated_at = excluded.updated_at""",
        (device, platform, model, version, state, detail, run_id, credential, now, now),
    )
    conn.commit()


def record_run(conn: sqlite3.Connection, run_id: str, now: str, cm_number: str = "", note: str = "") -> None:
    conn.execute(
        """INSERT INTO runs (run_id, created_at, cm_number, note) VALUES (?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
             cm_number = COALESCE(NULLIF(excluded.cm_number, ''), runs.cm_number),
             note = COALESCE(NULLIF(excluded.note, ''), runs.note)""",
        (run_id, now, cm_number, note),
    )
    conn.commit()


def list_devices(conn: sqlite3.Connection, state: str = "", run_id: str = "") -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM devices", []
    clauses = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return list(conn.execute(sql + " ORDER BY updated_at DESC", params))


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) AS n FROM devices GROUP BY state")
    return {row["state"]: row["n"] for row in rows}


def get_device(conn: sqlite3.Connection, device: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM devices WHERE device = ?", (device,)).fetchone()
