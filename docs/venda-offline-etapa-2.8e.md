# Etapa 2.8E — liberação da montagem após revisão confirmada

Base auditada: `6431345`, etapa 2.8D. Sem commit, push, deploy ou acesso ao banco
de produção nesta intervenção.

## Causa comprovada

`refreshCompletion` reconhecia a confirmação da substituta por
`revisionPresentation`, mas condicionava a liberação ao receipt confirmado da
própria original. `releaseConfirmedDraft` repetia essa exigência. A original deve
permanecer em conflito, portanto a cadeia resolvida podia continuar bloqueada.

## Correção

- `sales-revisions.js`: resolução compartilhada que percorre A → B → C, verifica
  vínculo, identidade, tipo, hashes, recibos e número oficial positivo. Rejeições
  intermediárias precisam ser conflitos comerciais coerentes. Cadeias ambíguas ou
  cíclicas e recibos incompatíveis não autorizam liberação.
- Observações obtidas pela consulta existente ao servidor são validadas pela cadeia
  inteira. Quando uma substituta também existe localmente, seu comando e estado
  terminal precisam ser coerentes com a observação; estado desconhecido não é
  convertido implicitamente em confirmação. Operações locais usadas na resolução
  têm o hash recalculado antes da autorização.
- `sales-drafts.js`: antes de liberar, prepara a evidência em leitura consistente
  de metadata/operations. Reconfere o mesmo snapshot dentro da transação de escrita
  e recusa alterações concorrentes. Remove somente o marcador da montagem e avança
  sua revisão. Preserva operations, history, UUIDs, payloads, hashes e vínculos.
  O registro `last-confirmed` referencia a original e a operação oficial resolvida.
- `sales-draft-ui.js`: libera pelo número oficial validado da original ou da cadeia,
  mostra o link da venda e permite nova montagem. O erro histórico da original fica
  no diagnóstico, sem contradizer a mensagem principal de resolução. A recuperação
  do modo online considera conflitos resolvidos, sem alterar as operações originais.

Original sem revisão e substitutas pendentes, enviando, em resultado desconhecido,
erro ou novo conflito permanecem bloqueadas. Existir uma revisão não basta.

## Versionamento

Namespace `/offline/assets/2-8e/` e cache `offline-pilot-shell-v27-2-8e`.
Os treze módulos e os templates compartilhados apontam para a mesma versão.
Rotas anteriores continuam disponíveis. Nos outros módulos houve somente troca
de referências de versão; não houve mudança de regras de locações ou checklists.

## Testes isolados

`offline/tests_sale_release_browser.py` usa Chrome com perfil temporário e SQLite
isolado. As rejeições, substitutas e confirmações são geradas pelo servidor real
dos testes, com verificações de Venda, ItemVenda, ContaReceber, estoque e vínculos.
Não se fabrica um receipt de sucesso para comprovar liberação.

Cobertura específica:

- Original em conflito sem revisão e todos os estados não confirmados da substituta.
- Tentativas diretas de liberação recusadas, sem alterações em operações/históricos.
- Substituta confirmada, duas abas, F5 e início de uma nova montagem.
- A → B → C, novo conflito, resposta perdida após commit, F5 antes da confirmação
  local, reconciliação sem segundo POST e fechamento/reabertura do navegador.
- Recibo, número oficial, hash, payload, identidade ou vínculo adulterados recusados.
- Comparação integral da original, de seu histórico e do registro no servidor;
  comparação de operations/history antes e depois da liberação.

Os testes avançam somente o relógio de teste para observar a janela existente de
900000 ms. A linha do tempo é mantida entre abas. A política da aplicação não muda.

Comando de regressão:

```powershell
.\venv\Scripts\python.exe -B manage.py test offline --settings=offline.test_settings --noinput --verbosity=2
```

Resultados finais:

- Cenários específicos de liberação: **quatro aprovados**, em 24,273 s na rodada
  dirigida; incluem as verificações negativas de receipt/hash/identidade/vínculo.
- Revisão, liberação, sincronização e proteção online: **38 testes aprovados**,
  sem falhas ou ignorados, em 141,965 s na rodada dirigida.
- Regressão completa da versão final: **159 testes em 517,891 s; 155 aprovados,
  zero falhas e quatro ignorados por exigirem PostgreSQL isolado**. Inclui os
  quatro cenários novos, o endurecimento final da validação de estados/recibos
  e a mensagem de resolução sem o erro histórico na mensagem principal.
- Os quatro testes PostgreSQL de concorrência já haviam sido aprovados em instância
  local isolada na 2.8D. Não foram reexecutados nesta intervenção; o backend que
  exercitam permanece sem alterações.
- `git diff --check`: aprovado. Aviso preexistente de namespace `estoque` duplicado.

Logs no TEMP: `offline-28e-release-3205da2a-e56f-40e8-9e98-95e9a0be82c5.log`,
`offline-28e-final-directed-eb754948-c72c-42ae-850a-23e01b760612.log` e
`offline-28e-regression-24b6bc47-2fe0-480e-8172-3c511f8f1407.log`.

## Limites preservados

Não implementa vendas consecutivas enquanto uma conclusão ainda estiver pendente,
atualização automática de snapshots, sincronização automática ou etapa 2.8F.
Serviço comercial, financeiro, estoque, modelos, migrations, core e janela de
estabilidade permanecem sem alterações. Uma liberação sem gravação local bem-sucedida
permanece bloqueada; os dados não são apagados para contornar a falha.

`separacao_vendas_fila.html` e os arquivos locais preexistentes não integram a etapa.

## Manifesto para staging seletivo

- `docs/venda-offline-etapa-2.8e.md`
- `estoque/templates/estoque/base.html`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `locacoes/templates/locacoes/includes/offline_checklist.html`
- `offline/templates/offline/sale_revision.html`
- `offline/tests.py`
- `offline/tests_browser.py`
- `offline/tests_commercial.py`
- `offline/tests_commercial_browser.py`
- `offline/tests_operation_lookup.py`
- `offline/tests_sale_release_browser.py`
- `offline/tests_sale_revisions_browser.py`
- `offline/tests_sale_sync_browser.py`
- `offline/tests_sales_browser.py`
- `offline/tests_sales_connectivity_browser.py`
- `offline/tests_sales_drafts_browser.py`
- `offline/urls.py`
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
