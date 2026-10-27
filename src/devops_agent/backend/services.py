"""Servizi applicativi: regole di business sopra il database.

Il server MCP espone solo queste funzioni; nessun tool esegue SQL arbitrario.
Ogni violazione prevista solleva DomainError con un codice stabile.
"""

from __future__ import annotations

import sqlite3

from devops_agent.backend.database import utcnow
from devops_agent.backend.models import (
    ASSIGNABLE_STATUSES,
    DESCRIPTION_MAX,
    LIST_LIMIT_MAX,
    NOTE_MAX,
    NOTE_MIN,
    PRIORITY_RANK,
    STATUS_TRANSITIONS,
    TITLE_MAX,
    TITLE_MIN,
    DomainError,
    HistoryEntry,
    PipelineRunOut,
    PipelineStatus,
    RequestDetail,
    RequestList,
    RequestSummary,
    ServiceList,
    ServiceOut,
    TeamList,
    TeamOut,
)

_SUMMARY_SELECT = """
    SELECT r.id, r.title, r.status, r.priority, r.service_id, s.name AS service_name,
           s.environment, r.team_id, t.name AS team_name, r.created_at,
           r.description, r.updated_at
    FROM operational_request r
    JOIN service s ON s.id = r.service_id
    LEFT JOIN team t ON t.id = r.team_id
"""

_PRIORITY_ORDER_SQL = (
    "CASE r.priority " + " ".join(f"WHEN '{p}' THEN {rank}" for p, rank in PRIORITY_RANK.items()) + " END"
)

# Ordinamenti ammessi per list_requests: valori fissi, mai testo del modello nella query.
_ORDER_SQL: dict[str, str] = {
    "priority": f"{_PRIORITY_ORDER_SQL} DESC, r.created_at, r.id",
    "newest": "r.created_at DESC, r.id DESC",
    "oldest": "r.created_at, r.id",
    "id_desc": "r.id DESC",
    "id_asc": "r.id",
}


# --------------------------------------------------------------------------- #
# Letture
# --------------------------------------------------------------------------- #


# Elenco dei servizi, filtrabile per ambiente e per una parte del nome.
def list_services(
    conn: sqlite3.Connection, environment: str | None = None, name_contains: str | None = None
) -> ServiceList:
    sql = "SELECT id, name, environment, description FROM service WHERE 1=1"
    params: list[object] = []
    if environment:
        sql += " AND environment = ?"
        params.append(environment)
    if name_contains:
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(name_contains.strip())}%")
    sql += " ORDER BY name, environment"
    items = [ServiceOut(**dict(row)) for row in conn.execute(sql, params)]
    return ServiceList(count=len(items), items=items)


# Tutti i team, ognuno con la sua specialità.
def list_teams(conn: sqlite3.Connection) -> TeamList:
    items = [TeamOut(**dict(row)) for row in conn.execute("SELECT id, name, specialty FROM team ORDER BY id")]
    return TeamList(count=len(items), items=items)


# Leggiamo le richieste applicando i filtri chiesti (stato, priorità, team, servizio) e l'ordinamento scelto.
# Restituiamo anche il totale, utile per capire se il limite ha tagliato l'elenco.
def list_requests(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    priority: str | None = None,
    team_id: int | None = None,
    service_id: int | None = None,
    unassigned_only: bool = False,
    limit: int = 20,
    order_by: str = "priority",
) -> RequestList:
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise DomainError("INVALID_ARGUMENT", f"limit deve essere tra 1 e {LIST_LIMIT_MAX}")
    if order_by not in _ORDER_SQL:
        raise DomainError("INVALID_ARGUMENT", f"order_by ammessi: {', '.join(_ORDER_SQL)}")
    if team_id is not None and unassigned_only:
        raise DomainError("INVALID_ARGUMENT", "team_id e unassigned_only sono alternativi")
    if team_id is not None:
        _require(conn, "team", team_id)
    if service_id is not None:
        _require(conn, "service", service_id)

    where, params = ["1=1"], []
    for column, value in (
        ("r.status", status),
        ("r.priority", priority),
        ("r.team_id", team_id),
        ("r.service_id", service_id),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if unassigned_only:
        where.append("r.team_id IS NULL")
    clause = " WHERE " + " AND ".join(where)

    total = conn.execute("SELECT COUNT(*) FROM operational_request r" + clause, params).fetchone()[0]
    rows = conn.execute(
        _SUMMARY_SELECT + clause + f" ORDER BY {_ORDER_SQL[order_by]} LIMIT ?",
        [*params, limit],
    ).fetchall()
    items = [_summary(row) for row in rows]
    return RequestList(count=len(items), total_matching=total, truncated=total > len(items), items=items)


# Dettaglio completo di una richiesta, storico compreso; se non c'è, NOT_FOUND.
def get_request(conn: sqlite3.Connection, request_id: int) -> RequestDetail:
    row = conn.execute(_SUMMARY_SELECT + " WHERE r.id = ?", (request_id,)).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", f"la richiesta {request_id} non esiste")
    history = [
        HistoryEntry(**dict(h))
        for h in conn.execute(
            "SELECT event, old_value, new_value, note, actor, at FROM request_history "
            "WHERE request_id = ? ORDER BY at, id",
            (request_id,),
        )
    ]
    return RequestDetail(
        **_summary(row).model_dump(),
        description=row["description"],
        updated_at=row["updated_at"],
        history=history,
    )


# Per ogni pipeline di un servizio (o per quella indicata) recuperiamo l'ultima esecuzione. Con un nome che
# non esiste, l'errore elenca le pipeline disponibili.
def get_pipeline_status(
    conn: sqlite3.Connection, service_id: int, pipeline_name: str | None = None
) -> PipelineStatus:
    service = conn.execute("SELECT id, name, environment FROM service WHERE id = ?", (service_id,)).fetchone()
    if service is None:
        raise DomainError("NOT_FOUND", f"il servizio {service_id} non esiste")

    sql = "SELECT id, name FROM pipeline WHERE service_id = ?"
    params: list[object] = [service_id]
    if pipeline_name:
        sql += " AND name = ?"
        params.append(pipeline_name.strip().lower())
    pipelines = conn.execute(sql + " ORDER BY name", params).fetchall()
    if not pipelines:
        available = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM pipeline WHERE service_id = ? ORDER BY name", (service_id,)
            )
        ]
        raise DomainError(
            "NOT_FOUND",
            f"nessuna pipeline '{pipeline_name}' per il servizio {service_id}; "
            f"disponibili: {', '.join(available) or 'nessuna'}",
        )

    runs: list[PipelineRunOut] = []
    for p in pipelines:
        last = conn.execute(
            "SELECT status, executed_at, duration_seconds, summary FROM pipeline_run "
            "WHERE pipeline_id = ? ORDER BY executed_at DESC, id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        runs.append(
            PipelineRunOut(
                pipeline_id=p["id"],
                pipeline_name=p["name"],
                status=last["status"] if last else None,
                executed_at=last["executed_at"] if last else None,
                duration_seconds=last["duration_seconds"] if last else None,
                summary=last["summary"] if last else None,
            )
        )
    return PipelineStatus(
        service_id=service["id"],
        service_name=service["name"],
        environment=service["environment"],
        pipelines=runs,
    )


# --------------------------------------------------------------------------- #
# Scritture (transazionali, sempre con voce in request_history)
# --------------------------------------------------------------------------- #


# Creazione di una richiesta, che nasce open e senza team. Validiamo titolo, descrizione, priorità e servizio,
# poi salviamo richiesta e storico in un'unica transazione.
def create_request(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    priority: str,
    service_id: int,
    actor: str,
) -> RequestDetail:
    title, description = title.strip(), description.strip()
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        raise DomainError("INVALID_ARGUMENT", f"il titolo deve avere tra {TITLE_MIN} e {TITLE_MAX} caratteri")
    if len(description) > DESCRIPTION_MAX:
        raise DomainError("INVALID_ARGUMENT", f"la descrizione supera {DESCRIPTION_MAX} caratteri")
    if priority not in PRIORITY_RANK:
        raise DomainError("INVALID_ARGUMENT", f"priorità non ammessa: {priority}")
    _require(conn, "service", service_id)

    now = utcnow()
    with conn:
        cur = conn.execute(
            "INSERT INTO operational_request "
            "(service_id, team_id, title, description, status, priority, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 'open', ?, ?, ?)",
            (service_id, title, description, priority, now, now),
        )
        request_id = int(cur.lastrowid)
        _history(conn, request_id, "created", None, "open", actor, now)
    return get_request(conn, request_id)


# Assegniamo la richiesta a un team; se era open passa a in_progress, e tutto finisce nello storico. Una
# richiesta chiusa, o già di quel team, non si può assegnare (CONFLICT).
def assign_request(conn: sqlite3.Connection, *, request_id: int, team_id: int, actor: str) -> RequestDetail:
    current = conn.execute(
        "SELECT status, team_id FROM operational_request WHERE id = ?", (request_id,)
    ).fetchone()
    if current is None:
        raise DomainError("NOT_FOUND", f"la richiesta {request_id} non esiste")
    _require(conn, "team", team_id)
    if current["status"] not in ASSIGNABLE_STATUSES:
        raise DomainError(
            "CONFLICT",
            f"la richiesta {request_id} è in stato '{current['status']}' e non può essere assegnata",
        )
    if current["team_id"] == team_id:
        raise DomainError("CONFLICT", f"la richiesta {request_id} è già assegnata al team {team_id}")

    now = utcnow()
    new_status = "in_progress" if current["status"] == "open" else current["status"]
    with conn:
        conn.execute(
            "UPDATE operational_request SET team_id = ?, status = ?, updated_at = ? WHERE id = ?",
            (team_id, new_status, now, request_id),
        )
        _history(
            conn,
            request_id,
            "assigned",
            None if current["team_id"] is None else str(current["team_id"]),
            str(team_id),
            actor,
            now,
        )
        if new_status != current["status"]:
            _history(conn, request_id, "status_changed", current["status"], new_status, actor, now)
    return get_request(conn, request_id)


# Cambio di stato consentito solo per le transizioni ammesse (closed, per dire, è definitivo); la nota la
# registriamo nello storico.
def update_request_status(
    conn: sqlite3.Connection, *, request_id: int, status: str, note: str, actor: str
) -> RequestDetail:
    """Cambia lo stato di una richiesta secondo le transizioni ammesse, con una nota obbligatoria."""
    note = note.strip()
    if not NOTE_MIN <= len(note) <= NOTE_MAX:
        raise DomainError("INVALID_ARGUMENT", f"la nota deve avere tra {NOTE_MIN} e {NOTE_MAX} caratteri")
    if status not in STATUS_TRANSITIONS:
        raise DomainError("INVALID_ARGUMENT", f"stato non ammesso: {status}")
    current = conn.execute("SELECT status FROM operational_request WHERE id = ?", (request_id,)).fetchone()
    if current is None:
        raise DomainError("NOT_FOUND", f"la richiesta {request_id} non esiste")
    old = current["status"]
    if old == status:
        raise DomainError("CONFLICT", f"la richiesta {request_id} è già in stato '{status}'")
    if status not in STATUS_TRANSITIONS[old]:
        allowed = ", ".join(sorted(STATUS_TRANSITIONS[old])) or "nessuna (stato finale)"
        raise DomainError(
            "CONFLICT", f"transizione '{old}' → '{status}' non ammessa; da '{old}' sono ammesse: {allowed}"
        )

    now = utcnow()
    with conn:
        conn.execute(
            "UPDATE operational_request SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, request_id),
        )
        _history(conn, request_id, "status_changed", old, status, actor, now, note=note)
    return get_request(conn, request_id)


# --------------------------------------------------------------------------- #
# Utilità
# --------------------------------------------------------------------------- #

_TABLE_LABEL = {"service": "il servizio", "team": "il team"}


# Verifichiamo che il servizio o il team esista; in caso contrario lanciamo NOT_FOUND.
def _require(conn: sqlite3.Connection, table: str, entity_id: int) -> None:
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)).fetchone() is None:
        raise DomainError("NOT_FOUND", f"{_TABLE_LABEL[table]} {entity_id} non esiste")


# Aggiunge una riga allo storico della richiesta.
def _history(
    conn: sqlite3.Connection,
    request_id: int,
    event: str,
    old: str | None,
    new: str | None,
    actor: str,
    at: str,
    *,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO request_history (request_id, event, old_value, new_value, note, actor, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, event, old, new, note, actor, at),
    )


# Da riga del database a forma breve della richiesta.
def _summary(row: sqlite3.Row) -> RequestSummary:
    return RequestSummary(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        priority=row["priority"],
        service_id=row["service_id"],
        service_name=row["service_name"],
        environment=row["environment"],
        team_id=row["team_id"],
        team_name=row["team_name"],
        created_at=row["created_at"],
    )


# Nella ricerca per nome i caratteri speciali di LIKE (% e _) vanno protetti, ed è quello che facciamo qui.
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
