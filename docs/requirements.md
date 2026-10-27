# Requisiti e tracciabilità

Ogni requisito è collegato al codice che lo implementa e al test che lo verifica.
Legenda: **O** = obbligatorio per la consegna; **P** = scelta progettuale.

## Requisiti funzionali

| ID | Requisito | Tipo | Implementazione | Verifica |
|---|---|---|---|---|
| RF-01 | Database relazionale con dati sintetici | O | `backend/schema.sql`, `backend/seed.py` | `test_backend.py::test_seed_is_deterministic` |
| RF-02 | Almeno 4 tool esposti da un server MCP | O | `mcp_server/server.py` (8 tool) | `test_mcp_server.py::test_server_exposes_eight_typed_tools` |
| RF-03 | Almeno 1 tool che modifica i dati | O | `create_request`, `assign_request`, `update_request_status` | TST-05, TST-07, TST-15 |
| RF-04 | Ciclo agentico implementato direttamente | O | `agent/loop.py` (nessun framework agentico) | `test_loop.py` |
| RF-05 | Massimo di iterazioni e condizioni di arresto esplicite | O | `Limits`, `stop_reason` in `agent/loop.py` | `test_loop.py::test_max_iterations`, TST-12 |
| RF-06 | Gestione di tool assenti, falliti e parametri non validi | O | `_handle_call` in `agent/loop.py`, `parse_error` in `agent/mcp_client.py` | TST-03, TST-10, TST-11, TST-13, `test_loop.py` |
| RF-07 | Conferma prima di ogni scrittura | O | `Policy` + `confirm` in `agent/loop.py`, `confirm_interactive` in `cli.py` | TST-05, TST-06, `test_loop.py::test_denied_confirmation_prevents_execution_and_repeat` |
| RF-08 | Log di richieste al modello, tool, parametri, risultati, token | O | `agent/logger.py` (JSONL) | `test_loop.py::test_log_contains_required_fields` |
| RF-09 | Sostituzione del modello senza toccare il resto del sistema | O | `LLMProvider`, `agent/providers/*`, `create_provider` | `test_providers.py` |
| RF-10 | Interfaccia a riga di comando | P | `cli.py` (`ask`, `chat`, `tools`, `scenarios`, `tokens`) | prova manuale, screenshot |
| RF-11 | Dominio delle richieste operative Cloud/DevOps | P | modello dati e tool | TST-01…TST-22 (18 scenari) |
| RF-12 | Test automatici in pipeline CI | P | `.github/workflows/ci.yml` (a ogni push), `scenari-modello.yml` (modello reale, a mano o settimanale) | GitHub Actions |
| RF-13 | Storico delle operazioni sulle richieste | P | tabella `request_history` | TST-05, TST-07 |
| RF-14 | Autorizzazione per ruolo sui tool | P | `Policy.build_catalog` (default deny) | TST-13, `test_policy_and_scenarios.py` |
| RF-15 | Ciclo di vita delle richieste (risoluzione, chiusura, riapertura) con transizioni controllate | P | `services.update_request_status`, `STATUS_TRANSITIONS` | TST-15, `test_backend.py::test_update_request_status_rules` |
| RF-16 | Ordinamento dell'elenco delle richieste | P | parametro `order_by` di `list_requests` | `test_backend.py::test_list_requests_order_by_id_desc` |

## Requisiti non funzionali

| ID | Requisito | Come è soddisfatto |
|---|---|---|
| RNF-01 | Riproducibilità | Seed fisso dei dati, DB nuovo per ogni scenario, versione dell'SDK MCP bloccata |
| RNF-02 | Sicurezza dei dati | Solo dati sintetici; nessun SQL libero; chiavi API solo da variabili d'ambiente, mai nei log |
| RNF-03 | Difesa da prompt injection | Contenuti dei dati trattati come dati (prompt), scritture solo con conferma umana, tool filtrati per ruolo (TST-14) |
| RNF-04 | Osservabilità | Un file JSONL per esecuzione, con durata di ogni chiamata e token quando disponibili; stringhe fino a 20.000 caratteri, così i risultati dei tool sono registrati per intero |
| RNF-05 | Costo | Modello locale come default; misura dei token per scenario e stima con listino (`devops-agent tokens`) |
| RNF-06 | Portabilità | Python ≥ 3.11 su Windows, macOS e Linux; SQLite; nessun servizio esterno obbligatorio |
| RNF-07 | Onestà delle metriche | Token non forniti dal provider registrati come `null` e riportati come "n/d", mai stimati |
| RNF-08 | Identificativi verificati | Una scrittura è bloccata se usa un id che non compare nella richiesta dell'utente né nei risultati dei tool (`_ungrounded_ids` in `agent/loop.py`, TST-05); la conferma mostra il nome leggibile di servizio, team e richiesta |
| RNF-09 | Scritture sequenziali | Al massimo una scrittura per risposta del modello (`UNA_SCRITTURA_PER_TURNO`), TST-15 |
| RNF-10 | Parametri chiusi | Parametri non dichiarati nello schema rifiutati dall'harness prima del server (`_reject_unknown_parameters`) |
| RNF-11 | Contesto controllato | Cronologia della chat limitata agli ultimi `history_turns` turni; `num_ctx` esplicito per Ollama |
| RNF-12 | Letture tolleranti | Parametri numerici fuori limite ricondotti al limite solo per i tool di lettura, con evento `tool_args_adjusted` e nota nel risultato (TST-11) |
| RNF-13 | Ricerca assistita degli id | Su `ID_NON_VERIFICATO` per servizi e team l'harness legge i valori ammessi e li restituisce come `candidati` (TST-17) |
| RNF-14 | Verifica degli id per corrispondenza | Id accettato se coincide con l'unico servizio/team nominato nella richiesta (`agent/matching.py`); letture con id inventato corrette; correzione riproposta una volta al modello (`harness_nudge`), TST-17, TST-07 |
| RNF-15 | Prompt coerente con il ruolo | Il prompt dichiara la sola lettura quando il ruolo non ha strumenti di scrittura (TST-13) |
| RNF-16 | Chiamate scritte come testo | Una risposta che è *interamente* una chiamata JSON a un tool del catalogo è trattata come tool call e passa da tutti i controlli e dalla conferma (`_text_tool_calls`, evento `text_tool_call_parsed`, TST-19) |
| RNF-17 | Esito delle scritture dichiarato dall'harness | Se il modello ha proposto scritture e nessuna è riuscita, alla risposta si aggiunge una nota deterministica (`answer_annotated`, TST-20): la risposta del modello non è la fonte dello stato del sistema |
| RNF-18 | Priorità coerente con la richiesta | Mappatura bassa/media/alta/critica → low/medium/high/critical nella descrizione di `priority` e nel prompt; se l'utente indica un solo livello in modo esplicito, una scrittura con priorità diversa è bloccata (`PARAMETRO_INCOERENTE`, evento `tool_arg_mismatch`, `matching.mentioned_priority`), TST-17 |
| RNF-19 | Risposte fedeli ai dati | Per elenchi oltre 10 elementi l'harness genera dai dati l'elenco e il riepilogo stato × priorità e li allega alla risposta; il modello scrive solo una frase senza cifre (`list_attached`); gli scenari verificano anche il contenuto della risposta (`answer_all`, `answer_none`), TST-10, TST-11 |
| RNF-20 | Riprovo delle letture | Una lettura fallita per `TOOL_FALLITO` o `TIMEOUT` è ritentata una volta dall'harness (`tool_retry`); le scritture mai, perché un timeout non dice se l'operazione è avvenuta (TST-10) |
| RNF-21 | Scenari sull'esito | Con il modello reale gli scenari verificano esito, database e conferme; gli eventi del percorso di correzione solo con il provider scripted (`events_present_scripted`), TST-17, TST-07 |
| RNF-22 | Filtri chiesti dall'utente | Nelle letture un filtro di priorità non nominato nel messaggio dell'utente viene tolto e segnalato (`_drop_unrequested_filters`), TST-21 |
| RNF-23 | Dati letti, non ricordati | In chat i risultati dei turni precedenti non restano nella cronologia (`history_for_next_turn`); una risposta con dati senza lettura riceve una richiesta di rilettura (`stale_data_nudge`) o una nota, TST-22 |
