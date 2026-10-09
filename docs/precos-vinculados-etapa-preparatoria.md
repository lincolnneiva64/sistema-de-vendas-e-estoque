# Preços vinculados: etapa preparatória

Registro histórico desta etapa. O estado final das interfaces e a validação
consolidada estão em [Etapa 3](precos-vinculados-etapa-3.md).

Base: `6add971`, branch `backup/wip-pedidos-base-2026-06-04`.

## Limite desta entrega

O pedido permite concluir uma etapa pequena e consistente quando a implementação
integral exigir mudanças grandes ou houver incompatibilidade com compras/revisão.
Essa condição se aplica: `aplicar_precos_compra` guarda snapshots apenas do produto
comprado em `ItemCompra.alteracoes_precos`; `restaurar_precos_compra` restaura apenas
esse produto. Na exclusão, a compra já bloqueia os produtos antes da restauração.
Adicionar propagação nesses pontos sem redesenhar evidência e ordem de bloqueios
poderia restaurar um único integrante, perder alterações posteriores ou inverter
bloqueios com o serviço de grupos.

Foi concluída somente a base de diagnóstico/prévia. Não é a implementação completa
de preços vinculados e não está pronta para publicação como tal. A continuação
depende de autorização do usuário conforme a cláusula de etapas do pedido.

## Implementado

- Reutilização integral de `GrupoProdutoVinculado` e `MembroGrupoProduto`.
- Serviço somente leitura `diagnosticar_grupo`, com quatro campos explícitos;
  grupos não fracionados consideram somente vista/prazo principais.
- Comparação de unidades, fator e fracionamento, incluindo membros inativos.
- Identificação de divergências, derivados incoerentes, exclusão lógica e cadastro
  incompleto de conversão.
- Seleção explícita de referência para prévia, sem escolher o primeiro integrante.
- Comparação dos valores anteriores/novos por integrante; custos não são copiados.
- Reutilização de `Produto.clean()` para indicar impedimentos comerciais por membro,
  sem criar margem mínima ou salvar o candidato.
- Impressão digital `versao_observada` para identificar mudanças no conjunto lido.
  É uma observação diagnóstica, não uma versão persistida nem um bloqueio concorrente.
- Diagnóstico e prévia no modal existente de gerenciamento, desktop e mobile.
- GET do grupo com `referencia=<id>` somente para simular; referência externa recusada.
- `propagacao_disponivel=false` explícito e aviso de integração ainda pendente.

Não há botão de aplicação, ativação de grupo ou escritor de preços nesta etapa.
Criação/entrada/saída mantêm o comportamento comercial anterior. Peso, volume e
apresentação física precisam de conferência explícita; igualdade de unidade/fator
não comprova equivalência física.

## Próxima etapa proposta, ainda não autorizada

1. Definir estado/versionamento dos grupos existentes e auditoria durável de preços
   por produto/campo, sem migration de normalização de preços.
2. Serviço central transacional com bloqueio de grupos por ID e produtos por ID;
   harmonizar também associação/remoção e os bloqueios já adquiridos por compras.
3. Atualização seletiva dos quatro campos e do derivado `preco_venda`, com rejeição
   de formulário desatualizado, validação de todos os membros e rollback integral.
4. Evidência de compras abrangendo todos os integrantes afetados, versão do grupo,
   valores/tokens anteriores e novos. Restauração coletiva apenas com evidência
   suficiente; ambiguidade exige revisão e não pode restaurar isoladamente.
5. Integrar cadastro/edição, conferência antiga, compras/revisões, admin e escritas
   programáticas. Mudança de custo individual não altera automaticamente preço comum.
6. Habilitar regularização com prévia e confirmação, criação e entrada de membros.
   Grupos divergentes/incompatíveis não recebem referência automática.
7. Executar os testes obrigatórios de propagação, compras/restauração, idempotência,
   rollback e concorrência PostgreSQL em cluster exclusivamente isolado.

Nenhum mecanismo de propagação foi colocado apenas em signals ou em um fluxo
isolado, pois isso deixaria outras gravações escaparem da proteção.

## Validação e preservação

Dez testes novos passaram: diagnóstico/prévia e preservação dos registros, quatro
campos, qualquer referência, inativos, unidades/fatores/fracionamento, prejuízo,
não fracionados, referência inválida, mudanças na impressão digital e navegador
desktop/mobile. Regressão dos grupos: 20 aprovados e duas falhas preexistentes.
As falhas `test_contagem_apos_remover_e_renomear_preserva_vinculos` e
`test_listagem_com_e_sem_vinculo` foram reproduzidas com o template original do HEAD;
esperam textos contíguos antigos que a listagem atual não apresenta.

`manage.py check --settings=offline.test_settings`: sem erros, aviso preexistente
`urls.W005` por namespace `estoque` duplicado. `git diff --check`: sem erros.

Não foi criada migration: esta etapa utiliza o schema existente e só lê dados.
Nenhuma consulta ou escrita em produção/Neon foi executada. Os 30 grupos e 82
produtos foram informados pelo usuário, não conferidos no banco real nesta tarefa.
Nenhum preço real foi alterado. Não houve mudanças em vendas, compras, financeiro,
offline, `.env`, backups, `.worktrees/` ou `separacao_vendas_fila.html`.
Sem staging, commit, push ou deploy.

## Manifesto seletivo da etapa preparatória

```text
docs/precos-vinculados-etapa-preparatoria.md
estoque/services/precos_vinculados.py
estoque/templates/estoque/grupos_produtos.html
estoque/tests_precos_vinculados.py
estoque/views_grupos_produtos.py
```

Comando recomendado, não executado:

```powershell
git add -- docs/precos-vinculados-etapa-preparatoria.md estoque/services/precos_vinculados.py estoque/templates/estoque/grupos_produtos.html estoque/tests_precos_vinculados.py estoque/views_grupos_produtos.py
```
