# feat(agent): Agente da Doris com Orquestração Determinística LangGraph, Memória SQLite em 2 Camadas e Suíte E2E de Robustez

## Resumo
Implementa o agente conversacional da Doris em `agent/` compatível com o contrato OpenAI Chat Completions em streaming (`POST /v1/chat/completions`, `stream=true`) e extensões `doris.feedback`.

### Principais Entregas
- **Orquestração Determinística (LangGraph — ADR-001):**
  - Router estruturado (`openai/gpt-6-luna`) classificando intenções em `search_and_tryon`, `tryon_known_item`, `outfit_for_occasion`, `opinion_on_look`, `preference_update`, `smalltalk` e `off_topic`.
  - Tools de alto nível (`find_products`, `run_tryon_pipeline`, `show_products`, `get_latest_look`) com validação estrita por `IdAllowlist` da conversa.
  - Filtros determinísticos em código (`apply_filters`) para preço (`max_price`/`min_price`), sinônimos de cor (`amarelo` $\leftrightarrow$ `mostarda`, `dourado`, `ocre`; `branco` $\leftrightarrow$ `off-white`, `cru`) e adequação de gênero.
  - Sanitização e isolamento de `<catalog_data>` contra *prompt injection* (neutralizando ataques embutidos como no item `p10`).
- **Visão Multimodal & Roteamento de Modelos (ADR-002):**
  - Download interno de imagens do proxy (`/seed-images/...`) e conversão para `data:image/jpeg;base64,...` antes do envio ao `google/gemini-3.8-flash` (fallback automático para `openai/gpt-6-luna`).
- **Memória em Duas Camadas no SQLite (ADR-003):**
  - Curto prazo por `conversation_id` (substituindo base64 de imagens do usuário por `"[imagem anexada]"`).
  - Longo prazo por `user` completo (`u_ana__a` isolado de `u_ana__b`), com extração assíncrona em background sincronizada por `get_user_lock(user)`.
- **Resiliência, Prazo e Streaming (ADR-004):**
  - `Deadline(80s)` interno, emissão do 1º token de texto antes do início do try-on, retry exponencial (`0.5s`, `1.0s`) restrito a `retryable=true`, fallback gracioso para card `PRODUCT` e garantia de encerramento com `finish_reason="stop"` + `data: [DONE]`.

## Validação e Métricas
- **Testes Unitários/Integração (`pytest`):** `58 passed` em `0.40s` (100% sem rede via `httpx.MockTransport`).
- **Evals E2E Standard (`agent/evals/reports/standard.md`):** **6/6 cenários (100%)**, **36/36 checks (100%)**, TTFT p50 `4.67s`, Total p50 `7.49s`.
- **Evals E2E Robustness (`agent/evals/reports/robustness.md` — `faults.json` + `overrides.json`):** **6/6 cenários (100%)**, **36/36 checks (100%)**, TTFT p50 `4.50s`, validando recuperação de 503 em `tryon`, degradação para `PRODUCT` em erro 500 não-retentável e resistência a adulteração de preço/prompt injection.
