# Encerramento administrativo global de conflito offline

Base: branch `backup/wip-pedidos-base-2026-06-04`, commit `0ca20c7`.

## Causa comprovada

Antes desta intervenção não existia decisão administrativa global separada para abandonar um conflito sem gerar venda. O recibo comercial original precisa continuar preservado. Apagar operações ou alterar o recibo perderia evidência e não bloquearia revisões em outros dispositivos.

## Implementação

`EncerramentoVendaOffline` registra a decisão separadamente: UUID próprio, UUID/hash da original, operador, ambiente, dispositivo solicitante, motivo, data/hora e evidência do recibo original. `close_conflict` verifica comando/payload, identidade, conflito definitivo sem identificação oficial e ausência de revisão/comando substituto.

Encerramento e criação de revisão usam `select_for_update` na mesma linha original. Se a revisão ganhar, o encerramento é recusado. Se o encerramento ganhar, uma revisão posterior recebe conflito técnico sem venda ou vínculo substituto. Repetir a mesma solicitação administrativa devolve o mesmo registro. Não há chamada ao serviço comercial no encerramento.

- POST `/api/offline/operations/<uuid>/close/`: motivo obrigatório, `confirm: true`, sessão, permissão offline e CSRF.
- GET `/api/offline/operations/<uuid>/closure/`: somente leitura, verifica operador/ambiente/hash/dispositivo original.
- A consulta existente de operação também informa o encerramento separado.

`sales-closures.js` persiste a intenção administrativa completa antes do POST. Resposta perdida ou falha de persistência local não confirma encerramento. Ausência em GET não prova término do POST. Consulta posterior recupera a decisão; reenvio administrativo preserva a identidade original da solicitação. Não existe envio automático ao abrir a tela.

Após confirmação oficial, uma transação IndexedDB espelha o recibo administrativo e libera apenas o marcador correspondente à original, dispositivo e revisão da montagem. Montagem posterior permanece intacta. Operação, payload, hash e histórico comercial não são reescritos. O encerramento aparece na fila/histórico e exportação JSON de metadata. A original sai da sincronização, revisão e consumo provisório de estoque sem modificar o snapshot.

## Limites e riscos restantes

- Encerramento exige conexão e confirmação oficial. Falha/ausência mantém o estado anterior protegido.
- Operações com revisão vinculada, inclusive substitutas em conflito, são recusadas. Não há encerramento em cascata.
- Rascunho de revisão local ativo ou resultado comercial desconhecido bloqueia a solicitação. Rascunho de revisão descartado não impede encerramento, mantendo sua evidência.
- Outros dispositivos devem consultar o servidor para espelhar a decisão; o bloqueio de novas revisões no servidor independe desse espelhamento.
- Marcador incompatível, montagem posterior ou substituta local ainda não resolvida não é apagada para liberar a tela.
- Web Locks/IndexedDB continuam necessários. Sincronização comercial permanece manual e com estabilidade de 15 minutos; a consulta administrativa é imediata.

## Versionamento e publicação futura

Módulos alterados usam `/offline/assets/2-8g-close/`; core e módulos comerciais sem alteração conservam suas referências. Entradas de Vendas/base/piloto/revisão foram atualizadas. Service Worker: `offline-pilot-shell-v30-admin-close`, com novos módulos no cache. IndexedDB não muda de schema.

A migration `offline.0003_encerramentovendaoffline` é necessária para publicação futura. Só foi aplicada pelos runners nos bancos de teste. Coordenar schema, backend e assets em eventual deploy autorizado; carregar o novo Service Worker online antes de testar offline. Não limpar dados do site.

## Resultados finais

122 testes distintos aprovados, nenhuma falha restante:

| Ambiente | Testes distintos aprovados |
| --- | ---: |
| SQLite isolado: encerramento, criação/revisão, envio online e consulta | 57 |
| PostgreSQL isolado: concorrência encerramento/revisão, encerramentos e idempotência comercial | 6 |
| Chrome: encerramento, duas abas, F5/reabertura, rede, persistência, revisão/sincronização, envio online, vendas consecutivas, estoque/data e cache | 59 |
| Total | 122 |

Os seis testes PostgreSQL inicialmente ignorados na execução SQLite foram executados no PostgreSQL local. Reexecuções direcionadas não estão duplicadas no total acima. Houve uma falha intermediária de asserção no teste novo, causada por seletor inexistente em `/vendas/`; a asserção foi corrigida para `offline-modal-text` e a execução final passou.

O teste de fechamento mantém duas vendas oficiais isoladas existentes e compara seus registros e os efeitos de estoque, despesas, contas e movimentos antes/depois. O teste concorrente comprova uma única decisão e no máximo uma venda quando a revisão vence. Testes de navegador com servidor real comprovam recibo recuperado, original/histórico preservados, montagem posterior preservada e ausência de reenvio comercial.

Verificações: `git diff --check` sem problemas; `makemigrations offline --check --dry-run --settings=offline.test_settings` sem mudanças pendentes. Aviso preexistente de namespace Django `estoque` duplicado continua presente.

## Isolamento e preservação

PostgreSQL exclusivo em `127.0.0.1:64324`, base `closure_isolated`, banco descartável `test_closure_isolated`. Antes dos testes, foram conferidos endereço, porta, usuário, database e `data_directory`, além da configuração Django com somente o alias default apontando para esse destino.

Cluster novo: `C:/Users/Camila Neiva/AppData/Local/Temp/codex-closure-pg-sda78_5d`. Binários portáteis existentes foram reutilizados sem instalação ou privilégios administrativos, sem tocar no cluster anterior. O cluster novo está parado; arquivos preservados no TEMP para inspeção e remoção posterior. Não foi instalado serviço nem alterado `.env`/conexão permanente.

Nenhum encerramento ou reenvio real foi executado. Produção, Neon e IndexedDB real não foram acessados. Vendas #744/#745 e UUID real `97ff91bd-2144-4f67-a917-4e0a04181f32` permanecem intocados. Nenhum staging, commit, push ou deploy foi executado.

## Manifesto seletivo

26 arquivos desta intervenção: 20 rastreados modificados e 6 novos, incluindo este relatório. Os testes antigos alterados apenas atualizam as referências de versão/cache e verificam o cache novo. Não incluir `separacao_vendas_fila.html`, `.worktrees/`, `offline/diagnostics/`, backups ou arquivos temporários preexistentes.

```text
docs/venda-offline-encerramento-administrativo.md
estoque/templates/estoque/base.html
estoque/templates/estoque/vendas_layout_teste.html
offline/closures.py
offline/migrations/0003_encerramentovendaoffline.py
offline/models.py
offline/services.py
offline/templates/offline/sale_revision.html
offline/tests_browser.py
offline/tests_sale_closures.py
offline/tests_sale_closures_browser.py
offline/tests_sale_revisions_browser.py
offline/tests_sale_sync_browser.py
offline/tests_sales_browser.py
offline/urls.py
offline/views.py
static/offline/app.js
static/offline/operation-details.js
static/offline/pilot.html
static/offline/sales-closures.js
static/offline/sales-draft-ui.js
static/offline/sales-drafts.js
static/offline/sales-revision-ui.js
static/offline/sales-revisions.js
static/offline/sales-stock.js
static/offline/service-worker.js
```

Comando recomendado, não executado:

```powershell
git add -- "docs/venda-offline-encerramento-administrativo.md" `
  "estoque/templates/estoque/base.html" `
  "estoque/templates/estoque/vendas_layout_teste.html" `
  "offline/closures.py" `
  "offline/migrations/0003_encerramentovendaoffline.py" `
  "offline/models.py" `
  "offline/services.py" `
  "offline/templates/offline/sale_revision.html" `
  "offline/tests_browser.py" `
  "offline/tests_sale_closures.py" `
  "offline/tests_sale_closures_browser.py" `
  "offline/tests_sale_revisions_browser.py" `
  "offline/tests_sale_sync_browser.py" `
  "offline/tests_sales_browser.py" `
  "offline/urls.py" `
  "offline/views.py" `
  "static/offline/app.js" `
  "static/offline/operation-details.js" `
  "static/offline/pilot.html" `
  "static/offline/sales-closures.js" `
  "static/offline/sales-draft-ui.js" `
  "static/offline/sales-drafts.js" `
  "static/offline/sales-revision-ui.js" `
  "static/offline/sales-revisions.js" `
  "static/offline/sales-stock.js" `
  "static/offline/service-worker.js"
```
