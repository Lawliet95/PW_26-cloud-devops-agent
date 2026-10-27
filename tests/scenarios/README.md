# Scenari di test

Ogni file `TST-xx_*.toml` descrive uno scenario: richiesta in linguaggio naturale, esito atteso e
aspettative verificabili in modo automatico. Gli scenari di questa cartella si eseguono con qualsiasi
provider, anche con il modello reale:

```bash
devops-agent scenarios --repeat 3            # con il modello configurato
devops-agent --provider scripted scenarios   # con le risposte registrate (come in CI)
```

La cartella `regressione/` contiene due scenari (TST-19, TST-20) che riproducono risposte sbagliate
registrate in una prova precedente e si eseguono solo con il provider scripted, dentro `pytest`.

La numerazione non è continua: TST-16, TST-18 e TST-23 sono stati accorpati in TST-15, TST-07 e TST-21,
e i numeri sono stati mantenuti per restare allineati ai log e alle evidenze delle esecuzioni precedenti
in `docs/evidence/`.
