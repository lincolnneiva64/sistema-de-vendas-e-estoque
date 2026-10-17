# Manifesto de publicação de vendas locais

Worktree: .worktrees/vendas-local-controlada-c149564. Base: c149564.

Nenhum arquivo está autorizado para commit, push, deploy ou migration no Neon nesta etapa.

## Arquivos propostos

29 arquivos: 18 de implementação, 8 de testes, 1 patch de compatibilidade e 2 documentos.

- `compat/receipt-reader-v1.patch`
- `docs/manifesto-publicacao-vendas-locais.md`
- `docs/vendas-locais-controladas.md`
- `estoque/templates/estoque/base.html`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `estoque/views.py`
- `manage.py`
- `offline/local_sales.py`
- `offline/migrations/0004_local_sale_principal.py`
- `offline/migrations/0005_validate_local_sale_principal.py`
- `offline/models.py`
- `offline/services.py`
- `offline/tests.py`
- `offline/tests_customer_payment_browser.py`
- `offline/tests_local_sales.py`
- `offline/tests_local_sales_browser.py`
- `offline/tests_local_sales_postgresql.py`
- `offline/tests_mixed_versions_postgresql.py`
- `offline/tests_sale_sync_browser.py`
- `offline/tests_sales_browser.py`
- `offline/urls.py`
- `offline/views.py`
- `sistema/settings.py`
- `static/offline/app.js`
- `static/offline/core.js`
- `static/offline/local-sales.js`
- `static/offline/sales-draft-ui.js`
- `static/offline/sales.js`
- `static/offline/service-worker.js`

## Exclusões preservadas

Não incluir .validation-pg-run.py, .operational-local-launch.py, .operational-open-chrome.ps1, docs/validacao-operacional-temporaria.md ou docs/validacao-postgresql-vendas-locais.md. São artefatos locais preservados, sem autorização de publicação. Não usar git add .

## Implantação e riscos

A sequência obrigatória é: ponte de leitura em todas as versões antigas; expansão 0004; validação histórica 0005; distribuição de código/assets; ativação explícita apenas local. Ver docs/vendas-locais-controladas.md para os detalhes e rollback.

Os padrões permanentes permitem INSERTs de versões antigas. O isolamento de recibos depende da ponte instalada em todos os leitores antigos antes da ativação. Locks DDL são breves e têm espera limitada a dois segundos; instalação pode precisar ser reagendada. Não liberar versões antigas sem o patch após existirem operações locais.

A modalidade local não é habilitada apenas por DEBUG=True e permanece bloqueada no Render. O funcionário declarado não deve ser confundido com usuário Django autenticado. Uma estação local autorizada deve ser confiável.

## Testes e estado

Validação final concluída: 194 testes aprovados (140 PostgreSQL e 54 Chrome). Foram preservados 164 resultados já concluídos e retomados somente os 30 casos interrompidos; estes passaram em 246,168 segundos. Cluster temporário encerrado, sem falhas.

## SHA-256 dos arquivos propostos

| Arquivo | SHA-256 |
| --- | --- |
| `compat/receipt-reader-v1.patch` | `7e717bc44ce309b677b28c3ebf46adb4d0ee01ec89954b72eb35042011bf3144` |
| `docs/vendas-locais-controladas.md` | `5cd073904d0e1ad938fc0c1dc819a2c320987fb21bd1b422d3f7257b65f99232` |
| `estoque/templates/estoque/base.html` | `59905859e4f3da6b4d05885fff9887ce3fc37e8208843d33ab9c8e884e66b6d9` |
| `estoque/templates/estoque/vendas_layout_teste.html` | `bb176da583240ecf841806da9fa269f1ec59f57e7c2e4d2484d671d21f00a6e0` |
| `estoque/views.py` | `0deed461a741dd6cf1d2e5256d3743777bc224de2c4b8585df9af6796bbf64ce` |
| `manage.py` | `407382b1b9609b1307d35363a2fc715a010c548f1242d9f322a72d5256986197` |
| `offline/local_sales.py` | `5c6d39fd904c44468f312e6052d4ab47a55ed7535fb4db5ed905bd2f814aefb0` |
| `offline/migrations/0004_local_sale_principal.py` | `00360cc31dd98b74a804258cc55d84c1df32e01304a3c1c3961287192e41644c` |
| `offline/migrations/0005_validate_local_sale_principal.py` | `bae7ec606d3158468b5a26048af034f375f05dbe58b4d76488c94221274f0093` |
| `offline/models.py` | `99f7ff873c81f5d9020b5ca663efab387962ec81d14a053c4bc15686ec714001` |
| `offline/services.py` | `18ac88ff719339bb6fdcc72b34bf1ae4f72c827682a4159a53da6d4bac7e1967` |
| `offline/tests.py` | `34579e981e3a84ec9cbc018344d1e6cf1b1fccb0b18eae47eb28111d6af88dc3` |
| `offline/tests_customer_payment_browser.py` | `8cf80ad1b51a519715c6a706df238fee08490bd3337379c772c545c8b0b3efcb` |
| `offline/tests_local_sales.py` | `c05a59b5817973e63c97f9289a45ee1d180a7f6b9643f574c97d4027d58b35a0` |
| `offline/tests_local_sales_browser.py` | `9e95f39e52b9a37a9dfe2db053da7059f392127cfa1f3baae6d4fb753cfedfd5` |
| `offline/tests_local_sales_postgresql.py` | `e1ccf04f450f371219e1e82dfe62685a70164d36d5db3368af51d41d4584bd3c` |
| `offline/tests_mixed_versions_postgresql.py` | `6ef0e1faf992ed281075baab6c25f1b3325abb29f3c23a1ad270ba18206a04ce` |
| `offline/tests_sale_sync_browser.py` | `64c413658273400063bd493836240820ab1d0a995090bf48c593a3044dcc34ae` |
| `offline/tests_sales_browser.py` | `c4c1964ee61ac9ff3938a38d674ef868f6b03bf7e4e36f6944aa1ba43684bc8c` |
| `offline/urls.py` | `920ee1337873d29fc9eedd1392f3c0f932e567909196692948f03c520cba12d8` |
| `offline/views.py` | `37b1b20e45254728bf7b03a88660e6f62df464611090fdf5bd053bfb8c03fd1a` |
| `sistema/settings.py` | `2b2249a2672c10ce46552871d791a1ba847da78f45f874c1a42bea2de89c8663` |
| `static/offline/app.js` | `a3933de0f222d794022bfbbc0f17521d3771db2819cacf11c19558413f8f0c4b` |
| `static/offline/core.js` | `aae251d2ef0411b1bfd54856f0e703d10cf0df68494b77e1e3110ca2f5399313` |
| `static/offline/local-sales.js` | `6df4ed0dfb355868c46061ae9d753e8980fbfdeee0293de4447b7026b07b6131` |
| `static/offline/sales-draft-ui.js` | `f9bae32ec85995efeb47d2eb98392e20f9ffe2ee431a7a3bf0756a3e514fcd84` |
| `static/offline/sales.js` | `ad38320c9bcfd0ac4d6f84f7dd80d3b69072f851637d2bb0e4cdc120ccd832e1` |
| `static/offline/service-worker.js` | `85c33068064c088acb5a2ec2e34471af7a3296c7e74c537143def375809f7866` |

O manifesto não inclui seu próprio hash.
