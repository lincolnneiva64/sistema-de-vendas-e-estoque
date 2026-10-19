# Conferência de estoque: revisão para mobile e desktop

Base: `06627cfcf1bb28c64ff0ee142a0771aeab5fc861`.
Worktree: `.worktrees/inventario-usabilidade-20261019`.
Alterações locais para revisão, sem commit, push ou deploy.

## Comportamento preparado

- Produtos PCT/KG recebem um quadro **Total convertido em PCT**, somente visual, abaixo das duas entradas. Ele permanece após guardar, confirmar OK, enviar e recarregar a página.
- A cada edição, uma consulta somente de leitura chama `inventario.conversao.converter`, usando o cadastro congelado da própria conferência. Não há fórmula ou arredondamento paralelos em JavaScript. Exemplos: `2 + 1,899 / 3 = 2,633 PCT`; `1 + 1,560 / 2,5 = 1,624 PCT`.
- A tela mostra “Calculando...” imediatamente. Respostas antigas são descartadas. Campos incompletos/invalidos ou conexão indisponível não exibem um total antigo como se fosse atual. A prévia exige conexão; ela não confirma produtos nem modifica estoque.
- Guardar contagem, OK, Atualizar OK, envio final, sinalização e verificação de acesso recebem texto de processamento, além da reação visual ao toque. Mantidas as regras existentes de confirmação e verificação após resposta incerta.
- Formulários administrativos de conferência e ajustes recebem processamento visível, bloqueio temporário de repetição, erro na própria tela preservando os campos e confirmação após sucesso. A resposta do servidor e os controles de versão/previa assinada continuam determinando o resultado. Não há reenvio automático.
- Geração/renovação de links mantém seu tratamento existente, inclusive a apresentação do link uma única vez. Um aviso de sucesso administrativo pode atravessar a navegação usando `sessionStorage`; contém apenas texto de apresentação e destino, sem contagens, credenciais ou autorização.
- Administração: caminho atual, início do estoque, todas as conferências e retorno à conferência nas telas subordinadas. Contagem individual: identificação da conferência e âncoras para os produtos e o início da própria contagem, sem links administrativos.

## Arquivos de implementação

- `inventario/views.py`: total inicial e nas respostas existentes; prévia GET autorizada por atribuição; mensagem administrativa de sucesso; identificação da conferência na contagem.
- `inventario/urls.py`: rota da prévia somente de leitura.
- `inventario/middleware.py`: mesma classificação de recusa de acesso para a nova prévia.
- `inventario/ajustes_views.py`: mensagem de sucesso após a operação existente.
- `inventario/templates/inventario/base.html`: feedback compartilhado, mensagens e reação visual ao toque.
- `inventario/templates/inventario/navegacao.html`: navegação administrativa contextual.
- `inventario/templates/inventario/lista.html`.
- `inventario/templates/inventario/detalhe.html`.
- `inventario/templates/inventario/ajustes.html`.
- `inventario/templates/inventario/historico.html`.
- `inventario/templates/inventario/movimento_manual.html`.
- `inventario/templates/inventario/mobile.html`: terceiro quadro e navegação limitada à contagem.
- `static/inventario/contagem.css`: apresentação do total.
- `static/inventario/contagem.js`: consulta automática, descarte de respostas antigas e textos de processamento.
- `static/inventario/feedback.js`: feedback dos POSTs administrativos, recuperação após falha e retorno ao GET existente.
- `inventario/tests_usabilidade.py`: testes complementares fictícios.
- Este relatório.

Nenhuma alteração em modelos, migrations, serviços de gravação, conversor oficial, permissões ou autenticação. Nenhuma alteração em vendas, locações ou recolhimentos. As alterações preexistentes da worktree principal foram preservadas.

## Validação

Executor: `python -m inventario.validar_confiabilidade_postgresql <módulos>` com a venv existente. Cada execução cria outro cluster PostgreSQL local descartável e verifica host, porta, usuário, banco e diretório físico antes de permitir conexões. As tabelas e os observadores são preparados diretamente para os testes; nenhuma migration é executada. Todos os clusters iniciados foram encerrados.

Chrome real headless com emulação mobile de 390 px e desktop de 1366 px; testes existentes de ajustes também usam 320 px. Capturas de contagem enviada inspecionadas visualmente, em `artifacts/usabilidade/contagem-enviada-390.png` e `contagem-enviada-1366.png`.

Regressão ampliada: 104 testes executados, 103 aprovados. Cobertura: conversão, contagem, confirmações, CSRF, acessos separados, atribuição/renovação, ajustes individuais e em lote, concorrência, rollback, auditoria imutável e saldos históricos aplicados. Os módulos foram `tests_usabilidade`, `tests_browser`, `tests_links_browser`, `tests_mobile_protecao`, `tests_ajustes`, `tests_confirmacao_unica`, `tests_sessoes`, `tests`, `tests_saldo_aplicado` e `tests_navegacao_resumo`, todos de `inventario`.

**Falha preexistente comprovada:** `inventario.tests_navegacao_resumo.NavegacaoResumoBrowserTests.test_produtos_administracao_e_atalhos_abrem_mesma_pagina`, linha 158. O teste espera que o atalho de conferências não exista em vendas para anônimos; a base já apresenta esse atalho público para encaminhar ao login gerencial. O mesmo teste falhou na worktree intacta do commit-base, com a mesma asserção. Os testes de acesso continuam negando efetivamente as páginas administrativas. Nenhum código ou teste de vendas foi alterado para mascarar essa divergência.

Resultado final dos complementos de `inventario.tests_usabilidade`: **10 testes aprovados**, considerando o último resultado de cada caso. O ensaio de dez casos aprovou oito; os dois casos mobile/desktop foram corrigidos quanto às aspas do seletor JavaScript do próprio teste e reexecutados isoladamente, ambos aprovados. Eles cobrem também os empates do arredondamento oficial, navegação interna da contagem, respostas de prévia fora de ordem e falha de conexão sem exibir total desatualizado.

Logs e metadados em `artifacts/usabilidade/`: `regressao-ampliada.log/.json`, `base-intacta.log/.json`, `complementos.log/.json` e `mobile-desktop-final.log/.json`. Os JSONs confirmam `migrations_executed: false` e `state: stopped`. `git diff --check` aprovado. As advertências sobre namespace `estoque` duplicado e diretório `staticfiles` ausente no teste já pertencem à base.

Sem acesso a banco de produção ou uso de conferências comerciais reais.
