# Evidenze per la relazione

Copia qui, dalla cartella `logs/scenarios/<data>-<provider>/`, le evidenze dell'esecuzione
con il modello reale che userai nella relazione:

- `report.md` e `results.json` dell'esecuzione;
- per ogni scenario, il file JSONL (o l'estratto rilevante) indicato nel report;
- gli screenshot della CLI, con nome `TST-xx.png`.

Per gli screenshot conviene eseguire il singolo scenario in modo interattivo, così la CLI
mostra passi, conferme ed esito:

```bash
devops-agent ask "Apri una richiesta ad alta priorità per payments-api in produzione: la build è lenta"
```

I log degli scenari con il modello reale sono versionati in `logs/scenarios/` (tranne i database
temporanei, `logs/**/db/`); qui vanno l'analisi e gli estratti citati nella relazione.

| Documento | Contenuto |
|---|---|
| [2026-10-25_qwen2.5-7b_analisi.md](2026-10-25_qwen2.5-7b_analisi.md) | Sessioni interattive: errori A–E e contromisure |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-1.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-1.md) | Prima esecuzione degli scenari, 31/51 |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-2.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-2.md) | Seconda esecuzione, 37/54, confronto e contromisure della PR #6 |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-3.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-3.md) | Terza esecuzione, 51/57, andamento delle tre esecuzioni e contromisure della PR #7 |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-4.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-4.md) | Quarta esecuzione, 54/54, revisione di tutte le risposte contro i dati e contromisure della PR #8 |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-5.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-5.md) | Quinta esecuzione, 45/54 con controlli più severi, contromisure della PR #9 |
| [2026-10-25_qwen2.5-7b_sessione-5-chat.md](2026-10-25_qwen2.5-7b_sessione-5-chat.md) | Prova in chat dopo la settima esecuzione (54/54): filtro inventato e dati ricopiati, contromisure della PR #12 |
| [2026-10-25_qwen2.5-7b_scenari-esecuzione-6.md](2026-10-25_qwen2.5-7b_scenari-esecuzione-6.md) | Sesta esecuzione, 51/54, allucinazione numerica in TST-11 e contromisure della PR #10 |
