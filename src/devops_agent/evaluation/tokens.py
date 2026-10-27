"""Analisi dei token e stima dei costi a partire da results.json.

Con un modello locale il costo per token è zero: la stima con un listino a
pagamento serve come termine di confronto e va dichiarata con fonte e data.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

DAYS_PER_MONTH = 22  # giorni lavorativi: ipotesi da dichiarare nella relazione


# Da results.json ricaviamo la tabella dei token medi per scenario.
def token_report(
    results: Path, *, price_in: float | None, price_out: float | None, daily_requests: int = 100
) -> str:
    data = json.loads(results.read_text(encoding="utf-8"))
    records = data["records"]
    by_id: dict[str, list[dict]] = {}
    for r in records:
        by_id.setdefault(r["scenario_id"], []).append(r)

    lines = [
        f"## Token per scenario · {data['provider']} / {data['model']}",
        "",
        "| Scenario | Esecuzioni | Chiamate al modello (media) | Tool (media) | Token in (media) "
        "| Token out (media) | Durata (ms, media) |",
        "|---|---|---|---|---|---|---|",
    ]
    all_in: list[int] = []
    all_out: list[int] = []
    for sid, runs in by_id.items():
        tin = [r["tokens_in"] for r in runs if r["tokens_in"] is not None]
        tout = [r["tokens_out"] for r in runs if r["tokens_out"] is not None]
        all_in += tin
        all_out += tout
        cells = [
            sid,
            str(len(runs)),
            f"{statistics.mean(r['llm_calls'] for r in runs):.1f}",
            f"{statistics.mean(len(r['tool_calls']) for r in runs):.1f}",
            f"{statistics.mean(tin):.0f}" if tin else "n/d",
            f"{statistics.mean(tout):.0f}" if tout else "n/d",
            f"{statistics.mean(r['duration_ms'] for r in runs):.0f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")

    if not all_in:
        lines += ["", "Il provider non ha fornito il conteggio dei token: nessuna stima possibile."]
        return "\n".join(lines)

    mean_in, mean_out = statistics.mean(all_in), statistics.mean(all_out or [0])
    lines += ["", f"Media per richiesta: **{mean_in:.0f}** token di input e **{mean_out:.0f}** di output."]
    if price_in is not None and price_out is not None:
        per_request = mean_in / 1e6 * price_in + mean_out / 1e6 * price_out
        monthly = per_request * daily_requests * DAYS_PER_MONTH
        lines += [
            "",
            "### Proiezione dei costi con listino a pagamento",
            "",
            "| Voce | Valore |",
            "|---|---|",
            f"| Prezzo input (USD / 1M token) | {price_in:.4f} |",
            f"| Prezzo output (USD / 1M token) | {price_out:.4f} |",
            f"| Costo medio per richiesta (USD) | {per_request:.6f} |",
            f"| Richieste al giorno (ipotesi) | {daily_requests} |",
            f"| Giorni lavorativi al mese (ipotesi) | {DAYS_PER_MONTH} |",
            f"| Costo mensile stimato (USD) | {monthly:.2f} |",
        ]
    return "\n".join(lines)
