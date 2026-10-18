# Publicação da Etapa 1 do painel gerencial

Base direta: `77f88ee71a8a5e8a31b26fe8c29aa25606caf9d6`.
Implementação preparada na worktree isolada `inventario-painel-77f88ee`.
Destino autorizado: `backup/wip-pedidos-base-2026-06-04`.
Deploy exclusivamente manual no Render; nenhuma mudança no Auto-Deploy.

## Escopo

- Indicadores de produtos conferidos, faltas, sobras, corretos e pendentes,
  considerando apenas a rodada atual de cada item.
- Impacto das divergências ao custo, percentuais com denominadores explícitos
  e indicação de cobertura parcial quando não existe custo válido.
- Estoque atual do sistema separado da referência histórica capturada no OK.
  O saldo na criação não é armazenado e não é inventado retroativamente.
- Valores centralizados, cores por resultado, tabela no desktop e cartões
  no mobile. Formatação pt-BR restrita ao template gerencial.
- Auditoria, justificativas, revisão física, sinalizações e recontagem em
  seções recolhidas, sem remoção de formulários ou permissões.

Os únicos arquivos de execução alterados são `inventario/painel.py`,
`inventario/views.py`, `inventario/templates/inventario/detalhe.html` e
`static/inventario/revisao.css`. Os demais arquivos desta publicação são os
testes do painel, duas esperas de navegador tornadas tolerantes ao DOM ainda
carregando e esta documentação.

Não inclui implementação da linha do tempo de compras/vendas ou da correção
de renovação de links. Os planos dessas entregas e as capturas de demonstração
permanecem como artefatos locais fora do commit.

## Cálculos

Diferença por produto: `contado_base − esperado_no_OK`.
Percentual quantitativo: `100 × diferença / abs(esperado_no_OK)`.
Diferença e base exibidas usam a mesma unidade, inclusive em produtos fracionados.
Esperado zero ou confirmação pendente não produz percentual.

Estimativa por item: diferença na unidade base × custo congelado na seleção,
arredondada a centavos. Impacto total: soma dos valores absolutos das estimativas,
sem compensar falta por sobra. Saldo líquido fica disponível nos critérios.

Percentual financeiro: impacto total dividido pela soma de
`abs(esperado_no_OK) × custo_base`, por item arredondada a centavos, multiplicado
por 100. Os dois lados usam o mesmo conjunto de itens confirmados com custo
não negativo. Custos ausentes ou negativos são excluídos de ambos, com cobertura
explicitada. Base zero não gera percentual; ausência total de custos válidos
não é apresentada como estimativa zero. Unidades de produtos distintos não
são somadas.

## Proteções e validação

A projeção do painel é chamada somente no detalhe gerencial, depois das
verificações existentes de autenticação/permissão. Os endpoints, payloads,
templates e JavaScript da contagem cega permanecem inalterados. O teste HTTP
confirma que o funcionário recebe 403 no painel e não recebe saldo, custo,
referência ou percentual na contagem, tanto em HTML como em JSON.

`INVENTARIO_AJUSTES_HABILITADOS=False` e o bloqueio correspondente no serviço
permanecem intactos. Não há migrations novas, alteração de configuração,
dependências novas ou mudança nos modelos e serviços de estoque.
Não foi necessário consultar ou alterar produção para esta entrega.

**99 testes direcionados aprovados, sem falhas ou ignorados**, em PostgreSQL
17.11 descartável e Chrome, incluindo os três testes de concorrência, isolamento
comercial, CSRF, recuperação, perda de resposta, recontagem e preservação de
históricos. O cluster foi encerrado; não foram executadas migrations.

O painel foi testado em 1366, 768, 390 e 320 pixels, sem rolagem horizontal,
com números centralizados e acesso aos formulários preservado. Uma consulta
GET foi verificada contra escritas SQL e snapshots dos modelos: nenhum dado
foi alterado.

Comando reproduzível a partir da worktree, com o venv do workspace:

```powershell
& '..\..\venv\Scripts\python.exe' -B -m inventario.validar_confiabilidade_postgresql inventario.tests_painel inventario.tests_aperfeicoamentos inventario.tests_links_atribuicao inventario.tests_links_browser inventario.tests_navegacao_resumo.NavegacaoResumoTests inventario.tests_atalho_vendas inventario.tests_browser inventario.tests_confiabilidade inventario.tests_sessoes inventario.tests_mobile_protecao inventario.tests_ciclo_vida inventario.tests_sessoes_browser
```

Evidências locais: `%TEMP%/inventario-confiabilidade-pg-blm7_mgh/tests.log` e
`validation.json`: 99 testes em 69,979 s, falhas 0, `state=stopped` e
`migrations_executed=false`.

Um teste legado fora da seleção espera que o atalho de inventário seja oculto
para anônimos. Essa falha também foi reproduzida no código original de `77f88ee`;
a navegação comercial não foi alterada para satisfazer essa expectativa antiga.
