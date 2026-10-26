# Fornecedores nos cards de produtos

## Escopo e regras

Apenas leitura e apresentação. Nenhum model, migration, vínculo, cadastro,
compra ou regra de estoque/preços foi alterado. O CSS de contraste aprovado
foi mantido; acrescentados somente estilos da lista expansível.

`preparar_fornecedores_cards` recebe os produtos já filtrados da consulta e
prepara atributos transitórios para o template. Duas consultas adicionais em
lote: vínculos com nome/estado do fornecedor; pares distintos produto/fornecedor
no histórico, sem carregar preços, quantidades ou todas as instâncias dos itens.
Não executa gravações. Lista vazia não faz consulta adicional.

Todos os fornecedores estruturados são deduplicados pelo ID, com prioridade
de apresentação: vínculo ativo, vínculo inativo, somente histórico. Vínculo
inativo prevalece sobre histórico do mesmo fornecedor, evitando apresentá-lo
como atual. Cadastro de fornecedor desativado é explicitamente identificado.
Nomes iguais de IDs diferentes não são fundidos; nenhum principal é escolhido.

Histórico segue o critério já utilizado em
`_ultimas_compras_produtos_para_lista_fornecedor`: compra não cancelada,
excluindo status cancelada, rascunho e finalização iniciada. Compras abertas
legadas continuam elegíveis como histórico registrado, sem afirmar finalização.
Não há limite de três compras ou de três fornecedores.

Um fornecedor mostra nome e, quando pertinente, Histórico/Vínculo inativo.
Vários mostram contagem e lista nativa details/summary, com nome e origem.
Vinculados aparecem primeiro. Texto legado não entra na contagem nem cria
relações: sozinho aparece como Cadastro; junto a dados estruturados permanece
no detalhe, exceto quando repete exatamente um nome, ignorando caixa e espaços
externos. Essa supressão textual não funde IDs nem presume vínculo. Ausência
de todas as fontes mostra “Fornecedor não informado”.

Nenhum filtro, ordenação de produtos ou comportamento de paginação foi
alterado. A consulta home atual já carregava os produtos para os totais; a
resolução reaproveita os mesmos produtos. Abertura da lista não faz requisições.

## Arquivos da implementação

- `estoque/services/fornecedores_cards.py`: resolução em lote.
- `estoque/views.py`: chamada na consulta existente.
- `estoque/templates/estoque/home.html`: inclui apresentação resolvida.
- `estoque/templates/estoque/includes/produto_card_fornecedores.html`: lista.
- `static/estoque/produtos-cards-v2.css`: estilos da expansão e foco.
- `estoque/tests_fornecedores_cards.py`: oito testes novos.
- `estoque/tests_produtos_cards_v2.py`: atualiza expectativa do texto ausente.

## Validação

13 testes aprovados em 2,710 s: oito novos e cinco existentes dos cards.
Cobrem ausência, texto legado, um/vários vínculos ativos, histórico completo
com mais de três fornecedores e compras repetidas, cancelamentos/rascunhos,
vínculo inativo, cadastro inativo, duplicidade vínculo/histórico, IDs distintos
com nome igual, prioridade, escape HTML, nomes longos, unidades, busca,
preços/conversão e botões/CSRF. Snapshots dos cinco models envolvidos
permaneceram idênticos, sem INSERT/UPDATE/DELETE na requisição verificada.

O resolvedor faz exatamente duas consultas com 1 ou 26 produtos contendo
vínculos e histórico; o total de consultas da página também permaneceu constante.
Com zero produtos, o resolvedor faz zero consultas.

Execução: `venv/Scripts/python.exe -B amostras/testar_fornecedores_cards.py`.
Runner local derivado do anterior, configuração isolada sem .env e SQLite
em memória. Schema criado diretamente a partir dos models; executor de
migrations bloqueado. Nenhuma migration executada.

Edge/Chromium headless com perfil temporário: 1440, 1280, 1100, 390, 360 e
320 px. Mouse no desktop, toque em mobile, Enter/Espaço para abrir/fechar e
Tab para continuar a navegação, busca e retorno às mesmas alturas ao fechar.
Sem overflow dos cards/página, sem listas cortadas e sem erros JS capturados
durante as interações. Cards desktop fechados: 108 px na amostra fictícia.
Inspeção visual das capturas desktop e mobile. Teste de toque usa coordenadas
da área visual móvel; foco e rolagem são estabilizados na automação.
Não houve teste em dispositivo físico nem medição de tempo em dados reais.

Prévia: `amostras/previa-fornecedores-cards/index.html`, com listas fechadas e
abertas, doze imagens e métricas em `layout.json`. Os oito produtos e todos os
fornecedores são fictícios. Página real servida apenas em loopback por
`amostras/preview_fornecedores_cards.py`, com SQLite temporário privado.
O navegador é exercitado por `amostras/validar_fornecedores_cards.py`.
Os artefatos novos não substituem prévias anteriores.

`git diff --check` aprovado. Trabalho local preexistente preservado. Validação
sem acesso à produção, migrations ou deploy.

## Publicação aprovada

Implementação e prévia aprovadas pelo responsável. Revisão final confirma as
regras acima e os 13 testes aprovados; não houve mudança no código desde a
rodada final, portanto não foi necessária nova execução. Commit e push
autorizados somente na branch `backup/wip-pedidos-base-2026-06-04`, com
`[skip render]`. Prévia, runners locais, imagens, logs e bancos ficam fora do
commit. Nenhum deploy autorizado.
