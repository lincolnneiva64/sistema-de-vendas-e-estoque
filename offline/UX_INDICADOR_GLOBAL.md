# Auditoria do indicador global offline

## Arquitetura e alcance

O componente compartilhado é `estoque/templates/estoque/includes/offline_global.html`.
Ele recebe estado e detalhes de `static/offline/app.js`, usando o renderizador visual
`static/offline/presentation.js` e os estilos de `static/offline/indicator.css`.
Os IDs de estado, botão manual, identidade e ambiente foram preservados.

Pontos de inclusão encontrados:

- `estoque/templates/estoque/base.html`: 71 templates herdam essa base, incluindo home,
  Caixa/Banco, vendas/consulta, clientes, compras, fornecedores, contas a pagar/receber,
  cobranças, PIX, despesas, painéis financeiros, separação e entregas/checklists.
- `estoque/templates/estoque/vendas_layout_teste.html`: página independente usada
  pela rota `/vendas/`; inclui o componente diretamente.
- `locacoes/templates/locacoes/includes/offline_checklist.html`: inclui o componente
  quando `offline_indicator_present` não é verdadeiro. Usado por checklist operacional,
  conferência de entrega e conferência de recolhimento de locações.
  O checklist de estoque também usa esse include, com a flag verdadeira, evitando duplicação.

Não foi adicionada cobertura a outras páginas. A substituição do include alcança
automaticamente todos os consumidores existentes, inclusive separação, sem editar
`separacao_vendas_fila.html`.

## Páginas que exigiriam inclusão adicional

Estes templates independentes não recebem o indicador global atualmente:

- Estoque: `cartoes`, `cadastrar_produto`, `produto_detalhe`, `venda_detalhe`,
  `pedido_criar`, `pedido_detalhe`, `pedidos_lista`, `meios_pagamento`, `lixeira`,
  `receber_cliente_recebimentos_rota`, `receber_cliente_recebimentos_dia`,
  `receber_cliente_corrigir_recebimento`, `conferencia_recebimentos_rota`,
  `compras_lista_fornecedor_conferencia_externa`, `compras_lista_fornecedor_conferencia_erro`,
  `compras_sugestao_fornecedor_whatsapp` e `core/templates/core/home`.
- Locações: `lista`, `nova`, `detalhe`, `configuracoes`, `devolucao`, `pagamento`,
  `termo`, `recibo`, `recibos_pendentes`, `checklist_entrega_cliente` e
  `checklist_recolhimento_cliente`.
- Login offline e os shells estáticos `pilot.html` e `checklist.html` têm apresentação
  própria; não são consumidores do indicador global. `busca_funcionando.html`, na raiz,
  também não inclui o componente.

Os nomes acima correspondem a arquivos `.html`, não a confirmação de que todas essas
rotas estão em uso em produção. Nenhum deles ganhou a nova aba.

## Apresentação

Aba fixa esquerda de 44 × 80 px (44 × 74 px no celular), acima da aba Pendências
existente (96 px no desktop, 82 px no mobile). Sem altura reservada no fluxo da página.
Verde para online/sucesso; amarelo para espera/autenticação; vermelho para offline,
erro/conflito; azul forte para operações prontas. Badge usa a contagem atual do estado.

Um `details/summary` nativo abre e recolhe o painel por mouse, toque ou teclado.
O painel mantém estado, contador, pendências, último reset, botão manual e link offline,
com largura limitada ao viewport e rolagem interna em telas baixas. Consultar o status
não abre modal; a confirmação já existente de sincronização manual permanece.

O renderizador atualiza a aba mesmo com o painel fechado. Nenhuma mudança em core,
health-check, contador, fila, UUID, idempotência, backend ou fluxo de sincronização.
Apenas a versão do cache de assets do service worker foi incrementada para distribuir
a apresentação nova; sua estratégia de cache não foi modificada.

## Validação

Comando: `venv\Scripts\python.exe manage.py test offline --settings=offline.test_settings --verbosity=1`.
Banco isolado SQLite em memória; sem uso do banco operacional.
Testes visuais ajustados para aba fixa, cores, badge, abrir/recolher, painel sem modal,
viewports 1366/390/320 e avanço de estabilidade com painel fechado seguido de envio manual.
A suíte existente verifica também múltiplas abas, suspensão/retomada, refresh,
fila preservada, sincronização e idempotência.

Resultado da suíte completa: 35 testes, OK, 1 ignorado por exigir PostgreSQL isolado.
O Django emite o aviso preexistente `urls.W005` (namespace `estoque` duplicado).
`git diff --check` passou.

## Arquivos alterados nesta tarefa

- `estoque/templates/estoque/includes/offline_global.html`: aba e painel.
- `static/offline/indicator.css`: apresentação lateral e responsividade.
- `static/offline/presentation.js`: texto acessível, rótulo curto e badge.
- `estoque/templates/estoque/base.html` e `estoque/templates/estoque/vendas_layout_teste.html`:
  versão da URL do CSS.
- `static/offline/service-worker.js`: somente versão do cache visual.
- `offline/tests_browser.py`: expectativas visuais e verificações de interação.
- `offline/UX_INDICADOR_GLOBAL.md`: este relatório.

Arquivos antigos, backups e a alteração preexistente em `separacao_vendas_fila.html`
foram preservados. Nenhum commit, push ou staging foi realizado.

Limitações: testes automatizados em Chrome headless, sem validação em dispositivo físico.
Como qualquer aba fixa, ocupa uma faixa estreita sobre o conteúdo na borda esquerda;
o painel aberto é uma sobreposição temporária e pode ser recolhido pelo mesmo controle.
