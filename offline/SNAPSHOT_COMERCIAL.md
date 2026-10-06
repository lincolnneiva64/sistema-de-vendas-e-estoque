# Etapa 2.1 — catálogo comercial de referência

## Auditoria do fluxo existente

`estoque.views.vendas` atende `/vendas/` e renderiza `vendas_layout_teste.html`
(o arquivo `vendas.html` não é a tela ativa). As fontes foram verificadas antes da implementação:

| Dado | Fonte atual | Decisão para o snapshot |
| --- | --- | --- |
| Produtos | `Produto.objects.filter(excluido=False, ativo=True)`; opções do select `produto` | Mesma seleção, catálogo completo, sem truncar resultados |
| Identificação | ID e nome usados nas opções; `Produto.codigo` disponível | ID, nome e código para identificação futura |
| Preço | `data-preco` = `preco_venda`; `data-preco2` = `preco_vista_fracionado` | Strings decimais, sem recalcular preço |
| Unidades/conversão | `unidade_venda_1`, `unidade_venda_2`, `vende_fracionado`, `fator_conversao`; `_quantidade_estoque_para_unidade_base` revalida no servidor | Unidades cadastradas e fator; nenhuma nova regra de baixa local |
| Custo | `data-custo`/`data-custo2`; `atualizarCustoMargemVenda` alimenta `custoVenda` readonly | Só os dois custos de referência necessários à UX atual |
| Estoque | `quantidade`, `estoque_conferido` no HTML; `/produtos/<id>/conferencia-estoque/` atualiza a referência online | Referência e flags; sem disponibilidade definitiva ou movimentos |
| Clientes | `/clientes/autocomplete/` (`clientes_autocomplete`), apenas ativos; busca por nome, apelido e WhatsApp | ID, nome, apelido, telefone usado na tela e prazo padrão |
| Prazo | `prazo_padrao_dias` preenche vencimento e sugere À vista/A prazo | Incluído como condição comercial de referência |
| Financeiro do cliente | `_resumo_cliente_venda`, crédito, status, contas, cobranças | Excluído; não necessário para montar os itens e exige revalidação online |
| Documento/rota/endereço | CPF não é condição da busca padrão de venda; rota é usada em outros contextos | Excluídos CPF/CNPJ, rota/bairro, endereço, e-mail e chave Pix |
| Operador | `Funcionario.objects.filter(ativo=True, pode_operar_sistema=True)` | Só IDs e nomes; usuário autenticado separado do operador escolhido |
| Formas | `tipoVenda`: À vista, A prazo, consumo_proprio | Mesmos valores; não confundir com modalidades de recebimento do caixa |
| Histórico | `/vendas/cliente-produto-historico/` | Excluído (compras anteriores não são necessárias ao catálogo) |
| Pedido/edição | `pedido_id`/`editar` em `/vendas/`; itens e estados do pedido/venda | Excluídos nesta etapa; seleção/importação de pedidos ainda exige servidor |
| Auxiliares | Painéis de locações, cobrança, resumo diário, alertas de fornecedores, separação | Excluídos; não copiar a tela inteira para o navegador |

Estoque mínimo, fornecedor, autoria de preços, notas internas e preços alternativos
não utilizados pelas opções da venda atual também ficam fora. A classificação especial
de clientes de consumo próprio existente no JS não foi duplicada em um novo motor.
Esse fluxo e suas autorizações devem ser auditados/revalidados na próxima etapa.

## Contrato e atualização

`GET /api/offline/snapshot/comercial/?device_id=<UUID>` é autenticado, somente leitura,
sem cache HTTP (`private, no-store`). Exige `offline.registrar_observacao`,
`estoque.view_cliente` e `estoque.view_produto`. Não cria device no servidor.
Sem `device_id`, a API retorna `null`; o cliente de atualização sempre envia o device existente.

O documento possui `tipo=comercial_vendas`, `schema_version=1`, `snapshot_id` (UUID de
cada geração), `gerado_em`, `origem`, `environment_id`, `actor`, `device_id`, `contagens`,
`somente_referencia` e `revalidar_no_servidor`. IDs são strings; valores decimais também,
para não converter moeda/quantidade em ponto flutuante durante transporte.

`static/offline/commercial.js` exporta `atualizarSnapshotComercial`,
`salvarSnapshotComercial`, `carregarSnapshotComercial`, `obterProdutosSnapshot` e
`obterClientesSnapshot`. Recebem o `Repository` existente e o escopo
`{actor_id, environment_id}`. A chave é `comercial_vendas:[ambiente,usuario]`, dentro
do store `snapshots` do mesmo `vendas-offline-pilot` (versão 1).
Não há banco/store novo, migration, fila ou history comercial.

O device reutiliza `metadata/device` por uma transação do `Repository`, sem avançar a
sequência de operações. O `core.js` não muda: a preparação também funciona com a versão
anterior desse módulo ainda carregada pelo Service Worker.
A atualização é manual, somente online, via Status → Preparar dados de vendas em `/vendas/`.
Não há download automático, timer comercial ou sincronização de vendas.
Falha de rede/autenticação, JSON inválido, quota e abort de transação preservam o snapshot
anterior. Campos são projetados novamente antes da gravação, e a substituição é um único
commit IndexedDB. Uma resposta mais antiga não substitui a mais recente.
`prepared_at` registra o sucesso local; `gerado_em` e `snapshot_id` permitem avaliar
idade e atualização posteriormente. Nenhum prazo de expiração foi inventado nesta etapa.

As leituras não acessam a rede, validam schema/usuário/ambiente/device e não usam dados
de outro escopo. Isso não criptografa o IndexedDB: acesso ao perfil do navegador continua
permitindo inspecionar dados comerciais. Dados antigos de outros usuários não são mostrados
pela API de leitura, nem apagados automaticamente.

O Service Worker só passa a armazenar os dois módulos comerciais. O endpoint e a
navegação `/vendas/` permanecem fora de seu cache. O snapshot não concede autorização,
crédito, saldo oficial, disponibilidade, preço definitivo ou ID de uma venda nova.

## Limites para a Etapa 2.2

Ainda não existe carrinho persistente, criação/conclusão/sincronização de venda, comando
`criar_venda`, reserva/baixa de estoque, ContaReceber ou MovimentoFinanceiro offline.
Qualquer venda futura deverá revalidar cliente/produto ativo, preços, conversões,
estoque, crédito, regras comerciais, forma, operador e pedido de origem no servidor.
Conflitos e permissões de criação não fazem parte desta entrega.

Antes de 2.2: definir UX de snapshot antigo, limites de tamanho para catálogos grandes,
política de retenção em dispositivos compartilhados e classificação de consumo próprio;
auditar o fluxo de pedido e garantir identidade/sessão atual antes de criar comandos.

## Validação desta entrega

Rodada final em SQLite isolado:

```powershell
.\venv\Scripts\python.exe manage.py test offline.tests offline.test_checklist_history offline.tests_commercial offline.tests_commercial_browser --settings=offline.test_settings --noinput
```

30 testes: 29 aprovados, 1 ignorado por exigir PostgreSQL isolado. A cobertura comercial
inclui API/permissões, projeção sem dados financeiros, zero mutações, catálogo ativo,
metadados, IndexedDB existente, abort de transação, respostas fora do escopo, respostas
antigas, preservação após falhas, desktop/mobile e import/leitura em documento novo sem rede.

Também foi executada a suite ampla de 51 testes, incluindo `offline.tests_browser`.
Dois testes visuais antigos do indicador não passaram:
`test_global_indicator_on_normal_page_and_mobile` espera texto verde, embora o CSS
preexistente use branco no indicador online; `test_shared_state_visuals_on_pilot_and_sales`
espera fundo online claro no botão lateral, embora o CSS preexistente use verde sólido.
A expectativa de cache desse segundo teste foi atualizada para v17 e validada em
reexecução, antes de alcançar suas asserções visuais antigas. O CSS e o motor de
conexão não foram alterados para resolver essas pendências fora da etapa comercial.
