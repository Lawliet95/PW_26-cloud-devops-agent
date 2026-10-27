"""Accesso al database SQLite."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "devops.db"
DB_ENV_VAR = "DEVOPS_AGENT_DB"


# Quale database aprire? Nell'ordine: il percorso passato, poi la variabile d'ambiente, poi il default.
def resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Percorso del DB: argomento esplicito, poi variabile d'ambiente, poi default."""
    if path is not None:
        return Path(path)
    return Path(os.environ.get(DB_ENV_VAR, DEFAULT_DB_PATH))


# Apriamo la connessione SQLite attivando le chiavi esterne e con righe accessibili per nome di colonna.
def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    db_path = resolve_db_path(path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database non trovato: {db_path}. Inizializzalo con 'devops-agent init-db'.")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Le tabelle le creiamo eseguendo lo script schema.sql.
def create_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("devops_agent.backend").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


# Ricreiamo il database da zero e lo popoliamo con i dati sintetici. A parità di seed i dati escono identici:
# è proprio questo che rende ripetibili i test.
def init_db(path: str | os.PathLike[str] | None = None, *, seed: int = 42, reset: bool = True) -> Path:
    """Crea lo schema e lo popola con dati sintetici deterministici."""
    from devops_agent.backend.seed import populate  # import locale: evita cicli

    db_path = resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        populate(conn, seed=seed)
        conn.commit()
    finally:
        conn.close()
    return db_path


# Data e ora correnti in UTC, nel formato adottato nel database.
def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
