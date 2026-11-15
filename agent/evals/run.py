"""Runner da suíte de avaliação E2E (caixa-preta) do Agente da Doris."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Garante que `doris` e `evals` possam ser importados mesmo rodando `python agent/evals/run.py` da raiz
AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from evals.checks import CheckResult, run_configured_checks  # noqa: E402
from evals.client import EvalClient, EvalTurnResult  # noqa: E402


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil((p / 100.0) * len(s)) - 1))
    return s[k]


async def run_suite(
    proxy_url: str,
    scenarios_path: Path,
    report_path: Path,
    suite_name: str = "Standard E2E",
) -> bool:
    spec = yaml.safe_load(scenarios_path.read_text(encoding="utf-8"))
    scenarios = spec.get("scenarios") or []
    run_id = uuid.uuid4().hex[:8]

    turn_records: list[dict[str, Any]] = []
    scenario_summaries: list[dict[str, Any]] = []

    async with EvalClient(proxy_url=proxy_url) as client:
        await client.reset()
        print(f"=== Iniciando Evals [{suite_name}] (run_id={run_id}, proxy={proxy_url}) ===")

        for sc in scenarios:
            sc_id = sc["id"]
            sc_name = sc["name"]
            persona = sc["persona"]
            user_id = f"{persona}__{run_id}_{sc_id}"
            conv_map: dict[str, str] = {}
            sc_passed = True
            sc_turns: list[dict[str, Any]] = []

            print(f"\n▶ {sc_name} (user={user_id})")
            for idx, t_spec in enumerate(sc.get("turns") or [], start=1):
                c_key = t_spec["conversation_key"]
                if c_key not in conv_map:
                    conv_map[c_key] = f"{run_id}_{sc_id}_{c_key}"
                cid = conv_map[c_key]
                msg = t_spec["message"]

                res: EvalTurnResult = await client.send_turn(
                    user_id=user_id,
                    conversation_id=cid,
                    message=msg,
                    images=t_spec.get("images") or [],
                )
                checks: list[CheckResult] = run_configured_checks(res, t_spec.get("checks") or [])
                turn_ok = all(c.passed for c in checks)
                if not turn_ok:
                    sc_passed = False

                status_icon = "✅" if turn_ok else "❌"
                ttft_str = f"{res.ttft_ms:.0f}ms" if res.ttft_ms is not None else "N/A"
                print(f"  {status_icon} Turno {idx} [{c_key}]: \"{msg}\" (TTFT={ttft_str}, Total={res.total_ms:.0f}ms)")
                for chk in checks:
                    c_icon = "✓" if chk.passed else "✗"
                    print(f"      {c_icon} {chk.name}: {chk.detail}")

                rec = {
                    "scenario_id": sc_id,
                    "scenario_name": sc_name,
                    "turn_index": idx,
                    "conversation_id": cid,
                    "user_id": user_id,
                    "message": msg,
                    "passed": turn_ok,
                    "ttft_ms": res.ttft_ms,
                    "total_ms": res.total_ms,
                    "checks": checks,
                    "full_text": res.full_text,
                    "tools_called": [
                        f"{c.get('tool')}({c.get('status')})"
                        for c in (res.turn_log.get("calls") or [])
                    ],
                }
                turn_records.append(rec)
                sc_turns.append(rec)

            scenario_summaries.append({
                "id": sc_id,
                "name": sc_name,
                "passed": sc_passed,
                "turns": sc_turns,
            })

    # Estatísticas agregadas
    total_scenarios = len(scenario_summaries)
    passed_scenarios = sum(1 for s in scenario_summaries if s["passed"])
    all_checks = [c for r in turn_records for c in r["checks"]]
    passed_checks = sum(1 for c in all_checks if c.passed)

    ttfts = [float(r["ttft_ms"]) for r in turn_records if r["ttft_ms"] is not None]
    totals = [float(r["total_ms"]) for r in turn_records]

    ttft_p50 = _percentile(ttfts, 50)
    ttft_p95 = _percentile(ttfts, 95)
    total_p50 = _percentile(totals, 50)
    total_p95 = _percentile(totals, 95)

    # Geração do relatório Markdown
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# Relatório de Avaliação E2E — {suite_name}",
        "",
        f"- **Data:** `{now_iso}`",
        f"- **Run ID:** `{run_id}`",
        f"- **Cenários aprovados:** **{passed_scenarios}/{total_scenarios}** ({(100.0 * passed_scenarios / max(1, total_scenarios)):.0f}%)",
        f"- **Checks aprovados:** **{passed_checks}/{len(all_checks)}** ({(100.0 * passed_checks / max(1, len(all_checks))):.0f}%)",
        f"- **TTFT (p50 / p95):** `{ttft_p50:.0f} ms` / `{ttft_p95:.0f} ms`",
        f"- **Latência Total do Turno (p50 / p95):** `{total_p50:.0f} ms` / `{total_p95:.0f} ms`",
        "",
        "## Resumo por Turno",
        "",
        "| Cenário | Turno | Mensagem | Tools (Status) | TTFT | Total | Status |",
        "| :--- | :---: | :--- | :--- | :---: | :---: | :---: |",
    ]
    for r in turn_records:
        tools_str = ", ".join(r["tools_called"]) if r["tools_called"] else "*(nenhuma)*"
        ttft_s = f"{r['ttft_ms']:.0f} ms" if r["ttft_ms"] is not None else "N/A"
        st_icon = "✅ PASS" if r["passed"] else "❌ FAIL"
        lines.append(
            f"| {r['scenario_id']} | {r['turn_index']} | {r['message']} | `{tools_str}` | {ttft_s} | {r['total_ms']:.0f} ms | {st_icon} |"
        )

    lines.extend(["", "## Detalhamento de Checks por Cenário", ""])
    for sc in scenario_summaries:
        sc_icon = "✅" if sc["passed"] else "❌"
        lines.append(f"### {sc_icon} {sc['name']}")
        for r in sc["turns"]:
            lines.append(f"- **Turno {r['turn_index']}** (`{r['conversation_id']}`): *\"{r['message']}\"*")
            for chk in r["checks"]:
                ci = "✅" if chk.passed else "❌"
                lines.append(f"  - {ci} `{chk.name}` — {chk.detail}")
            preview = r["full_text"].replace("\n", " ").strip()
            if len(preview) > 220:
                preview = preview[:220] + "…"
            lines.append(f"  - **Resposta:** {preview}")
        lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_content = "\n".join(lines)
    report_path.write_text(report_content, encoding="utf-8")
    print(f"\n📄 Relatório salvo em: {report_path}")
    print(f"🏁 Resultado final [{suite_name}]: {passed_scenarios}/{total_scenarios} cenários, {passed_checks}/{len(all_checks)} checks.")
    return passed_scenarios == total_scenarios and passed_checks == len(all_checks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Runner E2E de avaliação da Doris")
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get("EVAL_PROXY_URL", "http://localhost:3000"),
        help="URL base do proxy (ex.: http://localhost:3000 ou http://proxy:3000)",
    )
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).resolve().parent / "scenarios.yaml"),
        help="Caminho para o arquivo scenarios.yaml",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent / "reports" / "latest.md"),
        help="Caminho de saída do relatório Markdown",
    )
    parser.add_argument(
        "--suite-name",
        default="Standard E2E",
        help="Nome identificador da bateria no relatório",
    )
    args = parser.parse_args()

    ok = asyncio.run(
        run_suite(
            proxy_url=args.proxy_url,
            scenarios_path=Path(args.scenarios),
            report_path=Path(args.output),
            suite_name=args.suite_name,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
