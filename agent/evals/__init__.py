"""Suíte de avaliação E2E (caixa-preta) do Agente da Doris.

Nota de arquitetura (ADR-001): APENAS `agent/evals/*` pode chamar `/admin/log` e `/admin/reset`.
O código de runtime em `agent/doris/*` jamais acessa esses endpoints.
"""
