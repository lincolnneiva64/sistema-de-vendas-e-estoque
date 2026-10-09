# Etapa 2.8D — identidade do envio online da nova venda comum

## Problema e comportamento final

O envio convencional podia concluir a venda e perder a resposta. O catch removia
o marcador de envio, permitindo repetir estoque e financeiro sem uma identidade
consultável no servidor.

A nova venda comum agora utiliza o mesmo comando `criar_venda`, UUID, hash,
transação comercial e receipt do protocolo offline. Não há novo modelo ou migration.
Edição oficial, pedido importado e contextos explicitamente excluídos da montagem
comum continuam no fluxo anterior.

1. O rascunho já possui UUID. Sua conclusão persiste comando, sequência e marcador
   atomicamente em IndexedDB antes de qualquer POST.
2. A primeira tentativa online é imediata. O Web Lock existente impede disputa
   com recuperação de envio e sincronização em outra aba.
3. `/api/vendas/online/` valida identidade, conteúdo e hash e chama
   `process_operation`, que mantém os efeitos e o receipt na mesma transação.
4. Falha de transporte, timeout ou resposta inválida preserva a operação como
   `resultado_desconhecido`. Não libera outra criação dessa montagem.
5. O botão **Consultar resultado da venda** usa GET imediato por UUID, com
   verificação de usuário, ambiente, dispositivo, tipo e hash. Não faz POST e não
   depende da janela de estabilidade.
6. Confirmação válida entra em operations/history e libera a montagem por
   `releaseConfirmedDraft`. Falha dessa liberação preserva a confirmação oficial.
7. Ausência na consulta não prova que o primeiro envio terminou. O UUID e o comando
   permanecem intactos. Reenvio somente pela sincronização manual existente,
   com sua janela de estabilidade e suas permissões.

Os novos endpoints exigem autenticação e CSRF no POST. A primeira tentativa online
e sua consulta não adicionam a permissão `offline.registrar_observacao`. A política
existente de permissão para sincronização manual permanece.

Marcadores legados `enviando`, que não possuem comando completo/UUID consultável,
continuam protegidos após reabertura. Exigem conferência da venda oficial; esta
entrega não consegue reconstruir retroativamente a identidade desses envios.

## Arquivos principais

- `offline/views.py`: `online_sale`, `online_sale_result` e consulta compartilhada.
- `offline/urls.py`: endpoints e namespace de assets 2-8d.
- `static/offline/sales-draft-ui.js`: primeiro envio, consulta imediata e estado local.
- `static/offline/sales.js`: permite o botão de consulta durante montagem protegida.
- `estoque/templates/estoque/vendas_layout_teste.html`: integra o primeiro envio
  protegido e mantém o caminho de edição/pedido.
- `static/offline/service-worker.js`: cache `offline-pilot-shell-v26-2-8d`.
- Imports dos módulos e templates compartilhados: somente atualização para o
  namespace `/offline/assets/2-8d/`; nenhuma regra de checklist/locação alterada.

`core.js`, políticas de estabilidade, serviço comercial, modelos e migrations
permanecem sem alterações.

## Validação isolada

Testes novos em `offline/tests_online_sales.py` e
`offline/tests_online_sales_browser.py`. Os dois testes de envio online dos
rascunhos foram adaptados para verificar operações e receipts reais.

Cobertura: envio normal pelo botão; resposta descartada depois do commit real;
repetição bloqueada; F5 em resultado desconhecido e durante resposta em andamento;
reabertura do navegador; duas abas; transição offline durante envio; recuperação
oficial; ausência seguida apenas de reenvio manual estável com comando idêntico;
falha local antes do POST; falha na liberação local após confirmação; autenticação,
CSRF e identidade divergente. Prazo, à vista e consumo próprio verificam registros
reais, saldo de estoque e unicidade dos efeitos financeiros.

Comando de regressão:

```powershell
.\venv\Scripts\python.exe -B manage.py test offline --settings=offline.test_settings --noinput --verbosity=1
```

Usa SQLite temporário isolado e perfis temporários de Chrome. A execução dentro do
sandbox encerrou a conexão CDP antes da abertura da página; a validação de navegador
foi transferida para execução autorizada fora do sandbox.

Resultados:

- Direcionados finais: **16 testes em 30,197 s, todos aprovados, nenhum ignorado**.
  Incluem todos os casos novos, resposta tardia após confirmação por consulta,
  falha de liberação local, sincronização manual com 900000 ms e o checklist.
- Regressão ampla após todas as correções: **155 testes em 490,909 s;
  151 aprovados, zero falhas e quatro ignorados por exigirem PostgreSQL**.
  Inclui o cenário de resposta tardia e as verificações de assets e texto corrigidas.
- Validação complementar em **PostgreSQL 17.11 local isolado: cinco testes em
  4,462 s; cinco aprovados, zero falhas e zero ignorados**. Os quatro casos antes
  ignorados foram executados, além da disputa pelo último saldo entre os endpoints
  de venda online e sincronização offline. A suíte completa não foi executada em
  PostgreSQL; os resultados das duas rodadas são apresentados separadamente.
- Testes PostgreSQL existentes executados:
  - `offline.tests.PostgreSQLConcurrencyTests.test_concurrent_same_uuid_creates_one_event`
  - `offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_cria_uma_venda_e_um_financeiro`
  - `offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_conteudo_diferente_preserva_vencedor`
  - `offline.tests_sale_revisions.SaleRevisionPostgreSQLTests.test_two_devices_create_only_one_replacement_sale`
- Teste complementar temporário:
  `pg28d_extra_tests.LastStockConcurrencyTests.test_online_and_offline_compete_for_last_stock_and_replay`.
  Confirmou uma venda, um item, uma movimentação financeira de R$ 10, estoque final
  zero e duas operações (uma confirmada e outra em conflito). Reenvios dos dois
  comandos preservaram os registros, hashes e resultados, sem novos efeitos.
- A instância temporária escutava exclusivamente em `127.0.0.1:63266`. Diretório,
  identidade do cluster e identificador da instância foram conferidos antes dos
  testes. O executor recusava conexões fora dessa instância. Não houve acesso ao
  Neon, alteração do `.env`, instalação de serviço ou uso de token administrativo.
  O servidor foi encerrado; os arquivos temporários foram preservados.
- `git diff --check`: aprovado. Aviso preexistente de namespace `estoque` duplicado.

Log da regressão final no TEMP:
`offline-28d-precommit-ceeb4fe1-fc96-4bb2-999d-a858e584c042.log`.
Logs anteriores: `offline-28d-regression.log`, `offline-28d-final-directed.log`
e `offline-28d-checklist.log`.

O ambiente PostgreSQL permanece em
`C:/Users/Camila Neiva/AppData/Local/Temp/codex-pg28d-xk__fa0f`.
Contém `tests.log`, `guarded_tests.py`, `pg28d_extra_tests.py`, os binários e os
dados temporários. O teste adicional e o executor pertencem somente à validação
isolada: não foram adicionados ao projeto e não integram este commit. O diretório
pode ser removido posteriormente, após preservar as evidências. Seu arquivo de
estado contém credenciais exclusivamente locais de teste e não deve ser publicado.

## Limites preservados

- Não implementa vendas consecutivas offline ou liberação após revisão confirmada.
- Não transforma edição oficial/pedidos em operações idempotentes nesta etapa.
- Não reserva estoque global enquanto a operação está local.
- Depende de IndexedDB e Web Locks; sem gravação local não realiza o primeiro POST.
- Usa os limites existentes do protocolo: até 200 itens e comando limitado a 20 KB.
- Telas já abertas com código antigo precisam ser reabertas após publicação. O
  endpoint convencional permanece para os contextos fora desta intervenção.
- Não realiza deploy, commit ou push e não testa em produção.

## Lista de arquivos desta intervenção

- `docs/venda-offline-etapa-2.8d.md`
- `estoque/templates/estoque/base.html`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `locacoes/templates/locacoes/includes/offline_checklist.html`
- `offline/templates/offline/sale_revision.html`
- `offline/tests.py`
- `offline/tests_browser.py`
- `offline/tests_commercial.py`
- `offline/tests_commercial_browser.py`
- `offline/tests_online_sales.py`
- `offline/tests_online_sales_browser.py`
- `offline/tests_operation_lookup.py`
- `offline/tests_sale_revisions_browser.py`
- `offline/tests_sale_sync_browser.py`
- `offline/tests_sales_browser.py`
- `offline/tests_sales_connectivity_browser.py`
- `offline/tests_sales_drafts_browser.py`
- `offline/urls.py`
- `offline/views.py`
- `static/offline/app.js`
- `static/offline/checklist-restore.js`
- `static/offline/checklist.html`
- `static/offline/checklist.js`
- `static/offline/commercial-ui.js`
- `static/offline/operation-details.js`
- `static/offline/pilot.html`
- `static/offline/sales-draft-ui.js`
- `static/offline/sales-drafts.js`
- `static/offline/sales-revision-ui.js`
- `static/offline/sales-revisions.js`
- `static/offline/sales.js`
- `static/offline/service-worker.js`

A alteração preexistente em `separacao_vendas_fila.html` não integra esta etapa.
