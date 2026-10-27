"""Corrispondenza deterministica tra il testo dell'utente e servizi o team.

Serve all'harness per verificare un identificativo proposto dal modello senza fidarsi
del modello: se la richiesta nomina "payments-api in produzione" e c'è un solo servizio
con quel nome in quell'ambiente, quello è l'unico id coerente con la richiesta.

Le regole sono volutamente semplici e spiegabili (sottostringhe normalizzate e parole
chiave), non un secondo modello: quando la corrispondenza non è unica non si decide nulla.
"""

from __future__ import annotations

import re
from typing import Any

ENVIRONMENT_WORDS: dict[str, tuple[str, ...]] = {
    "prod": ("prod", "produzione", "production"),
    "staging": ("staging", "collaudo", "stage", "preproduzione"),
    "dev": ("dev", "sviluppo", "development"),
}
_MIN_WORD = 5  # parole più corte (articoli, preposizioni) non contano per i team
_PREFIX = 6  # "certificati" e "certificato" condividono il prefisso "certif"


# Minuscole e niente punteggiatura: con il testo normalizzato i confronti diventano più semplici.
def normalize(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9àèéìòù]+", " ", text.lower()).strip() + " "


# Quali ambienti sono nominati nel testo? Riconosciamo prod, produzione, staging, dev e simili.
def mentioned_environments(text: str) -> set[str]:
    norm = normalize(text)
    return {env for env, words in ENVIRONMENT_WORDS.items() if any(f" {w} " in norm for w in words)}


# Restituisce i servizi il cui nome compare nel testo. Se l'utente cita anche un ambiente, teniamo solo i
# servizi di quell'ambiente.
def match_services(text: str, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Servizi il cui nome compare nel testo, filtrati per ambiente se il testo ne nomina uno."""
    norm = normalize(text)
    named = [s for s in services if s.get("name") and normalize(s["name"]) in norm]
    envs = mentioned_environments(text)
    if envs:
        named = [s for s in named if s.get("environment") in envs]
    return named


# Delle parole lunghe conserviamo solo l'inizio, così "certificati" e "certificato" diventano la stessa
# parola.
def _stems(text: str) -> set[str]:
    return {w[:_PREFIX] for w in normalize(text).split() if len(w) >= _MIN_WORD}


# Per trovare il team citato proviamo prima con il nome; se non basta, scegliamo quello con più parole in
# comune con la richiesta ("certificati", per esempio, porta a Cloud Security).
def match_teams(text: str, teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Team nominati nel testo; altrimenti il team con più parole in comune con nome e specialità."""
    norm = normalize(text)
    named = [t for t in teams if t.get("name") and normalize(t["name"]) in norm]
    if named:
        return named
    words = _stems(text)
    scored = [(len(words & _stems(f"{t.get('name', '')} {t.get('specialty', '')}")), t) for t in teams]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return []
    return [t for score, t in scored if score == best]


# In base al campo smistiamo sulla ricerca giusta: servizi per service_id, team per team_id.
def match_candidates(field: str, text: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if field == "service_id":
        return match_services(text, candidates)
    if field == "team_id":
        return match_teams(text, candidates)
    return []


# Nome leggibile di un servizio (ambiente compreso) o di un team.
def describe(field: str, item: dict[str, Any]) -> str:
    if field == "service_id":
        return f"{item.get('name')} ({item.get('environment')})"
    return str(item.get("name"))


PRIORITY_WORDS: dict[str, tuple[str, ...]] = {
    "low": ("bassa", "low"),
    "medium": ("media", "medium", "normale"),
    "high": ("alta", "high", "elevata"),
    "critical": ("critica", "critical", "massima"),
}
PRIORITY_LABELS = {"low": "bassa", "medium": "media", "high": "alta", "critical": "critica"}


# Recuperiamo la priorità detta esplicitamente dall'utente ("alta priorità"). Il livello viene restituito solo
# se è uno, altrimenti otteniamo None.
def mentioned_priority(text: str) -> str | None:
    """Priorità detta esplicitamente nel testo, solo se il livello è uno e uno solo.

    Conta solo se la parola è accanto a "priorità" ("alta priorità", "priorità alta",
    "ad alta priorità", "high priority") o a "richiesta" ("una richiesta critica"): "alta"
    da sola può avere altri significati.
    """
    norm = normalize(text)
    found = set()
    for level, words in PRIORITY_WORDS.items():
        for w in words:
            if re.search(rf" (?:{w} (?:priorit[aà]|priority)|(?:priorit[aà]|priority|richiesta) {w}) ", norm):
                found.add(level)
    return found.pop() if len(found) == 1 else None


_LEVEL_STEMS: dict[str, tuple[str, ...]] = {
    "critical": ("critic", "critical", "massim", "urgent"),
    "high": ("alta", "alte", "alto", "high", "elevat"),
    "medium": ("medi", "medium", "normal"),
    "low": ("bass", "low"),
}


# Variante più larga della precedente: prende tutti i livelli citati, anche senza la parola "priorità"
# ("critiche", "basse"). Ci torna utile per capire se un filtro era voluto.
def mentioned_priority_levels(text: str) -> set[str]:
    """Livelli di priorità nominati nel testo, in senso largo ("critiche", "priority high", "basse").

    Serve a decidere se un filtro di priorità in una lettura è stato chiesto dall'utente: qui un falso
    positivo è innocuo (il filtro resta), un falso negativo toglie un filtro voluto, per questo le
    radici sono ampie.
    """
    words = normalize(text).split()
    return {
        level for level, stems in _LEVEL_STEMS.items() if any(w.startswith(s) for w in words for s in stems)
    }
