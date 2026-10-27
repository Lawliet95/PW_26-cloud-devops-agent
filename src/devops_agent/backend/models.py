"""Tipi di dominio e modelli di uscita.

I modelli Pydantic sono usati sia dai servizi applicativi sia dal server MCP:
lo schema di uscita di ogni tool viene generato da qui, quindi documentazione,
validazione e codice restano allineati.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Environment = Literal["dev", "staging", "prod"]
Priority = Literal["low", "medium", "high", "critical"]
RequestStatus = Literal["open", "in_progress", "resolved", "closed"]
RunStatus = Literal["success", "failed", "running", "canceled"]
HistoryEvent = Literal["created", "assigned", "status_changed"]
RequestOrder = Literal["priority", "newest", "oldest", "id_desc", "id_asc"]

PRIORITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}
ASSIGNABLE_STATUSES: frozenset[str] = frozenset({"open", "in_progress"})

# Transizioni di stato ammesse. "closed" è terminale; "resolved" può essere riaperta.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_progress", "resolved", "closed"}),
    "in_progress": frozenset({"open", "resolved", "closed"}),
    "resolved": frozenset({"in_progress", "closed"}),
    "closed": frozenset(),
}
NOTE_MIN, NOTE_MAX = 5, 500

TITLE_MIN, TITLE_MAX = 5, 120
DESCRIPTION_MAX = 2000
LIST_LIMIT_MAX = 50


# Errore previsto dal dominio (per esempio una richiesta che non esiste), con un codice stabile come NOT_FOUND
# o CONFLICT che arriva fino al modello.
class DomainError(Exception):
    """Errore applicativo previsto, con un codice stabile leggibile dal modello.

    Codici usati:
      NOT_FOUND         l'entità richiesta non esiste
      INVALID_ARGUMENT  valore non accettabile per le regole di business
      CONFLICT          operazione non ammessa nello stato attuale
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# Da qui in poi le classi descrivono cosa restituiscono gli strumenti: è da loro che si genera in automatico
# lo schema di uscita dei tool MCP. Cominciamo dal singolo servizio.
class ServiceOut(BaseModel):
    id: int
    name: str
    environment: Environment
    description: str


# La lista dei servizi, con quanti sono.
class ServiceList(BaseModel):
    count: int
    items: list[ServiceOut]


# Un team e la sua specialità.
class TeamOut(BaseModel):
    id: int
    name: str
    specialty: str


# E qui l'elenco dei team.
class TeamList(BaseModel):
    count: int
    items: list[TeamOut]


# Forma breve di una richiesta, quella che compare negli elenchi.
class RequestSummary(BaseModel):
    id: int
    title: str
    status: RequestStatus
    priority: Priority
    service_id: int
    service_name: str
    environment: Environment
    team_id: int | None = Field(description="Team assegnato; null se la richiesta non è assegnata")
    team_name: str | None
    created_at: str


# Un elenco di richieste: quante ne restituiamo, quante ce ne sono in totale e se il limite le ha troncate.
class RequestList(BaseModel):
    count: int = Field(description="Numero di richieste restituite")
    total_matching: int = Field(description="Numero totale di richieste che soddisfano i filtri")
    truncated: bool = Field(description="True se esistono altri risultati oltre il limite")
    items: list[RequestSummary]


# Voce dello storico: chi ha fatto cosa e quando.
class HistoryEntry(BaseModel):
    event: HistoryEvent
    old_value: str | None
    new_value: str | None
    note: str | None = None
    actor: str
    at: str


# Richiesta completa, con descrizione e storico.
class RequestDetail(RequestSummary):
    description: str
    updated_at: str
    history: list[HistoryEntry]


# L'ultima esecuzione di una pipeline: esito, durata e messaggio.
class PipelineRunOut(BaseModel):
    pipeline_id: int
    pipeline_name: str
    status: RunStatus | None = Field(description="Esito dell'ultima esecuzione; null se mai eseguita")
    executed_at: str | None
    duration_seconds: int | None
    summary: str | None


# Stato delle pipeline di un servizio.
class PipelineStatus(BaseModel):
    service_id: int
    service_name: str
    environment: Environment
    pipelines: list[PipelineRunOut]
