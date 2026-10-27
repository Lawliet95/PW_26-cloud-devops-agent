-- Schema del sistema informativo dimostrativo (dati esclusivamente sintetici).
-- Tutti i timestamp sono stringhe ISO 8601 in UTC.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS service (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    environment  TEXT    NOT NULL CHECK (environment IN ('dev', 'staging', 'prod')),
    description  TEXT    NOT NULL DEFAULT '',
    UNIQUE (name, environment)
);

CREATE TABLE IF NOT EXISTS team (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    specialty  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id  INTEGER NOT NULL REFERENCES service(id),
    name        TEXT    NOT NULL,
    UNIQUE (service_id, name)
);

CREATE TABLE IF NOT EXISTS pipeline_run (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id       INTEGER NOT NULL REFERENCES pipeline(id),
    status            TEXT    NOT NULL CHECK (status IN ('success', 'failed', 'running', 'canceled')),
    duration_seconds  INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    executed_at       TEXT    NOT NULL,
    summary           TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operational_request (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id   INTEGER NOT NULL REFERENCES service(id),
    team_id      INTEGER REFERENCES team(id),          -- NULL finché non assegnata
    title        TEXT    NOT NULL CHECK (length(title) BETWEEN 5 AND 120),
    description  TEXT    NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    status       TEXT    NOT NULL CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    priority     TEXT    NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

-- Storico delle operazioni sulle richieste: tracciabilità e verifica dei test di scrittura.
CREATE TABLE IF NOT EXISTS request_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES operational_request(id),
    event       TEXT    NOT NULL CHECK (event IN ('created', 'assigned', 'status_changed')),
    old_value   TEXT,
    new_value   TEXT,
    note        TEXT    CHECK (note IS NULL OR length(note) <= 500),
    actor       TEXT    NOT NULL,
    at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_request_status   ON operational_request(status, priority);
CREATE INDEX IF NOT EXISTS ix_request_team     ON operational_request(team_id);
CREATE INDEX IF NOT EXISTS ix_run_pipeline     ON pipeline_run(pipeline_id, executed_at);
CREATE INDEX IF NOT EXISTS ix_history_request  ON request_history(request_id);
