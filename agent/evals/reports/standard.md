# Relatório de Avaliação E2E — Standard E2E

- **Data:** `2026-11-15 18:47:22 UTC`
- **Run ID:** `081aae6c`
- **Cenários aprovados:** **6/6** (100%)
- **Checks aprovados:** **36/36** (100%)
- **TTFT (p50 / p95):** `4669 ms` / `6457 ms`
- **Latência Total do Turno (p50 / p95):** `7491 ms` / `25182 ms`

## Resumo por Turno

| Cenário | Turno | Mensagem | Tools (Status) | TTFT | Total | Status |
| :--- | :---: | :--- | :--- | :---: | :---: | :---: |
| scenario_1 | 1 | Me acha uma camisa de linho branca até R$300 | `search_by_text(200), garment_creation(200), tryon(200), show_artifact(200)` | 2438 ms | 20744 ms | ✅ PASS |
| scenario_2 | 1 | Tenho um casamento na praia sábado, o que eu visto? | `search_by_text(200), search_by_text(200), search_by_text(200), show_artifact(200), show_artifact(200), garment_creation(200), tryon(200), show_artifact(200)` | 4096 ms | 21105 ms | ✅ PASS |
| scenario_3 | 1 | O que você acha do meu último look? | `looks(200), show_artifact(200)` | 4758 ms | 7491 ms | ✅ PASS |
| scenario_4 | 1 | Odeio amarelo, visto M e curto estilo minimalista. | `*(nenhuma)*` | 5044 ms | 6010 ms | ✅ PASS |
| scenario_4 | 2 | Me sugere algo pra um jantar | `search_by_text(200), search_by_text(200), search_by_text(200), show_artifact(200), garment_creation(200), tryon(200), show_artifact(200)` | 6457 ms | 25182 ms | ✅ PASS |
| scenario_5 | 1 | Tem jaqueta jeans? | `search_by_text(200), show_artifact(200), show_artifact(200)` | 4669 ms | 5741 ms | ✅ PASS |
| scenario_5 | 2 | Quero provar a jaqueta de couro preta | `garment_creation(200), tryon(200), show_artifact(200)` | 2720 ms | 18386 ms | ✅ PASS |
| scenario_6 | 1 | Oi | `*(nenhuma)*` | 2146 ms | 2316 ms | ✅ PASS |
| scenario_6 | 2 | Qual a capital da França? | `*(nenhuma)*` | 5050 ms | 5551 ms | ✅ PASS |

## Detalhamento de Checks por Cenário

### ✅ Cenário 1 — Ana: Camisa de linho branca até R$300
- **Turno 1** (`081aae6c_scenario_1_c1`): *"Me acha uma camisa de linho branca até R$300"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=20739ms (limit=95000ms)
  - ✅ `price_limit(<=300)` — sem violações
  - ✅ `single_garment_tryon` — ok
  - ✅ `tryon_shown` — criados=1, não mostrados=[]
  - ✅ `price_grounding` — citados=['249,90', '249,90'], reais=[59.9, 119.9, 129.9, 219.0, 239.0, 249.9, 300.0, 459.0, 520.0]
  - ✅ `has_artifact(TRYON_CARD|PRODUCT)` — shown=['TRYON_CARD']
  - **Resposta:** Encontrei a **Camisa de linho branca** (Ateliê Maré) por **R$ 249,90**! Vou gerar o seu try-on agora...  Ana, essa camisa de linho branca do Ateliê Maré ficou com um caimento leve e impecável no seu avatar! Por R$ 249,90…

### ✅ Cenário 2 — Bruno: Casamento na praia sábado
- **Turno 1** (`081aae6c_scenario_2_c2`): *"Tenho um casamento na praia sábado, o que eu visto?"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=21099ms (limit=95000ms)
  - ✅ `gender_appropriateness(MALE)` — ok
  - ✅ `single_garment_tryon` — ok
  - ✅ `tryon_shown` — criados=1, não mostrados=[]
  - ✅ `price_grounding` — citados=['249,90', '249,90', '239,00', '520,00'], reais=[129.9, 169.0, 189.0, 219.0, 229.0, 239.0, 249.9, 279.0, 459.0, 489.0, 520.0]
  - ✅ `has_artifact(TRYON_CARD|PRODUCT)` — shown=['PRODUCT', 'PRODUCT', 'TRYON_CARD']
  - **Resposta:** Separei uma proposta especial para você! A peça principal é a **Camisa de linho branca** (Ateliê Maré, **R$ 249,90**), que já vou provar no seu avatar...  Olá, Bruno! Para um casamento na praia no Rio de Janeiro, a combi…

### ✅ Cenário 3 — Carla: Opinião sobre o último look (VLM + Base64)
- **Turno 1** (`081aae6c_scenario_3_c3`): *"O que você acha do meu último look?"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=7485ms (limit=95000ms)
  - ✅ `single_garment_tryon` — ok
  - ✅ `has_artifact(TRYON_CARD)` — shown=['TRYON_CARD']
  - **Resposta:** Olá, Carla! Que prazer te ver por aqui.  Achei a sua escolha do **vestido estampado para um brunch** super alegre e cheia de personalidade! Vamos à minha análise:  * **Caimento:** A silhueta valorizou muito você. O decot…

### ✅ Cenário 4 — Ana: Memória entre conversas (Odeio amarelo / M / Minimalista -> Jantar)
- **Turno 1** (`081aae6c_scenario_4_c4_a`): *"Odeio amarelo, visto M e curto estilo minimalista."*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=6004ms (limit=95000ms)
  - ✅ `no_tools_called` — 0 tools
  - **Resposta:** Anotadíssimo, Ana!   Nada de amarelo por aqui, foco total no estilo minimalista — que eu amo pela elegância e versatilidade — e já registrei que você veste M. Como nosso catálogo não tem controle de estoque por grade de …
- **Turno 2** (`081aae6c_scenario_4_c4_b`): *"Me sugere algo pra um jantar"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=25176ms (limit=95000ms)
  - ✅ `forbidden_colors(amarelo,mostarda)` — sem cores proibidas
  - ✅ `single_garment_tryon` — ok
  - ✅ `tryon_shown` — criados=1, não mostrados=[]
  - ✅ `price_grounding` — citados=['389,00', '389,00', '349,00'], reais=[169.0, 199.0, 229.0, 259.0, 279.0, 299.0, 319.0, 349.0, 389.0, 489.0, 520.0, 899.0]
  - ✅ `has_artifact(TRYON_CARD|PRODUCT)` — shown=['PRODUCT', 'TRYON_CARD']
  - **Resposta:** Separei uma proposta especial para você! A peça principal é a **Vestido midi preto minimalista** (Linha Reta, **R$ 389,00**), que já vou provar no seu avatar...  Oi, Ana! Para um jantar especial em São Paulo, o **Vestido…

### ✅ Cenário 5 — Ana: Jaqueta jeans inexistente -> Provar jaqueta de couro preta
- **Turno 1** (`081aae6c_scenario_5_c5_same`): *"Tem jaqueta jeans?"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=5734ms (limit=95000ms)
  - ✅ `single_garment_tryon` — ok
  - ✅ `price_grounding` — citados=['899,00', '229,00'], reais=[229.0, 899.0]
  - ✅ `has_artifact(PRODUCT)` — shown=['PRODUCT', 'PRODUCT']
  - **Resposta:** Olá! No momento, não temos uma jaqueta jeans no catálogo.   Mas se você procura uma terceira peça estilosa ou quer algo em jeans, separei duas alternativas incríveis para você dar uma olhada:  - **Jaqueta de couro preta*…
- **Turno 2** (`081aae6c_scenario_5_c5_same`): *"Quero provar a jaqueta de couro preta"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=18380ms (limit=95000ms)
  - ✅ `single_garment_tryon` — ok
  - ✅ `tryon_shown` — criados=1, não mostrados=[]
  - ✅ `price_grounding` — citados=['899,00', '899,00'], reais=[229.0, 899.0]
  - ✅ `has_artifact(TRYON_CARD|PRODUCT)` — shown=['TRYON_CARD']
  - **Resposta:** Boa escolha! Vou provar a **Jaqueta de couro preta** (R$ 899,00) no seu avatar agora...  Ficou incrível em você, Ana; a modelagem unissex dessa jaqueta de couro traz uma estrutura moderna e super elegante para o visual. …

### ✅ Cenário 6 — Bruno: Smalltalk e Off-topic (Zero tools)
- **Turno 1** (`081aae6c_scenario_6_c6_a`): *"Oi"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=2311ms (limit=95000ms)
  - ✅ `no_tools_called` — 0 tools
  - **Resposta:** Oi, Bruno, que prazer falar com você! Como posso te ajudar hoje com looks, busca de peças ou nosso provador virtual?
- **Turno 2** (`081aae6c_scenario_6_c6_b`): *"Qual a capital da França?"*
  - ✅ `turn_done` — outcome=done, sse_done=True, duration_ms=5545ms (limit=95000ms)
  - ✅ `no_tools_called` — 0 tools
  - **Resposta:** É Paris, a capital oficial do romance e, claro, da alta-costura!   Falando nessa inspiração parisiense, que tal a gente montar um look elegante e cheio de estilo hoje? Me conta o que você está procurando!
