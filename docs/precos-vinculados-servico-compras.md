# Preços vinculados: serviço transacional e integração com compras

Registro histórico desta etapa. As pendências de interface descritas abaixo
foram tratadas na [Etapa 3](precos-vinculados-etapa-3.md), que contém o estado final.

Base: `6add971`, branch `backup/wip-pedidos-base-2026-06-04`.
Validação local: 09/10/2026. Sem commit, deploy ou acesso de escrita à produção.

## Limite desta etapa

Esta entrega continua a etapa preparatória preservada em
`docs/precos-vinculados-etapa-preparatoria.md`. Implementa o núcleo e a proteção
das compras. Não constitui a funcionalidade completa pronta para publicação.
Os 30 grupos e 82 integrantes informados pelo usuário não foram consultados nem
normalizados na produção. A migration mantém todos os grupos pendentes.

O backend aceita comandos explícitos e versionados; as interfaces atuais ainda
não os produzem integralmente. Não há botão público de regularização. Escritas
diretas de preços vinculados são recusadas, inclusive em grupos pendentes, para
não permitir propagação parcial ou contornar a evidência necessária às compras.
Essa restrição torna a publicação prematura inadequada.

## Implementado

- Serviço central em `estoque/services/precos_vinculados.py`: diagnóstico,
  regularização confirmada, alteração seletiva, entrada confirmada de integrantes
  e restauração de eventos comprovados.
- Quatro preços aplicáveis; `preco_venda` acompanha `preco_vista`. Qualquer membro
  pode iniciar um comando. Campos não alterados permanecem individuais.
- Compatibilidade de unidades, conversão e fracionamento; validação de prejuízo
  individual, incluindo o custo da unidade fracionada utilizado nas vendas.
- Referência explícita quando há divergência, prévia identificada por impressão
  digital e comparação de versão sob transação.
- Auditoria separada com operador, data, origem, versões, integrantes, valores
  anteriores/posteriores e autoria dos campos. Custos e estoques não são copiados.
- Bloqueio conservador único de escritores de grupos/preços/compras, seguido de
  bloqueio de grupos e produtos em ordem consistente. Produtos de todo o grupo
  são bloqueados antes das escritas de compras; vendas não recebem esse mutex.
- Aplicação/revisão de preços de compras utiliza o serviço central. Um lote que
  contém vários membros gera uma alteração coerente; pedidos contraditórios são
  recusados. Custos misturados com preços são validados pelo custo final e sofrem
  rollback integral se houver impedimento.
- Evidência de todos os integrantes e referências aos eventos tanto no item
  quanto na compra. A evidência sobrevive à reconstrução ou retirada de itens.
- Exclusão de compra restaura a cadeia comprovada integralmente. Alteração
  posterior, mudança de integrantes, evidência inválida ou snapshot legado de
  preços vinculados interrompe a exclusão e exige revisão manual; não restaura
  somente um membro. Estoque e financeiro ficam na mesma transação.
- Escritas diretas de preços e mudanças estruturais incompatíveis são bloqueadas.
  Forms/admin apresentam validação. O recálculo de preços pelo navegador ao mudar
  o custo fica desabilitado para produtos vinculados.

## Pendências obrigatórias antes de publicar

1. Integrar edição de produto/admin/conferência ao comando versionado central;
   hoje existe proteção, não propagação por essas interfaces.
2. Enviar `versao_grupo_produto_<ID>` nos formulários de revisão/aplicação de
   compras e tratar conflito de versão com releitura e confirmação. Sem versão,
   alteração efetiva de preços vinculados é recusada; custo isolado permanece
   independente.
3. Disponibilizar regularização com referência explícita, prévia e confirmação;
   integrar criação/entrada de grupos à confirmação inicial. A criação atual
   produz grupo pendente e a entrada legada em grupo ativo é bloqueada.
4. Testar essas interfaces de ponta a ponta, concluir regressão de textos dos
   grupos e rever todos os pontos de escrita antes de considerar a entrega final.

## Migration

`0131_precos_vinculados_auditoria.py`: cria controle de serialização e auditoria,
adiciona estado/versão de grupo e listas de eventos em Compra/ItemCompra.
A operação de dados cria somente o registro de controle. Nenhum preço é alterado
e nenhum grupo é ativado automaticamente. `makemigrations --check --dry-run`
não encontrou mudanças adicionais. Não foi aplicada no Neon.

## Validação

| Execução final por conjunto | Aprovados | Falhos | Ignorados |
| --- | ---: | ---: | ---: |
| Serviço e regressões relacionadas de compras, SQLite isolado | 83 | 0 | 0 |
| Navegador: preparação, revisão e prévia de correção | 13 | 0 | 0 |
| Serviço e concorrência de finalização, PostgreSQL local isolado | 25 | 0 | 0 |
| Grupos existentes | 20 | 2 | 0 |

São contagens por execução; 21 testes do serviço foram repetidos no PostgreSQL.
As duas falhas de grupos são expectativas de texto em
`test_contagem_apos_remover_e_renomear_preserva_vinculos` e
`test_listagem_com_e_sem_vinculo`, reproduzidas com a base anterior. Não foram
mascaradas nem corrigidas nesta etapa. A verificação Django mantém o aviso
preexistente `urls.W005` de namespace duplicado.

Cobertura nova: quatro preços, qualquer membro, fracionamento, inativos,
referência/versão/confirmação, entrada/saída e incompatibilidade, prejuízo,
rollback, compra e revisão pelo endpoint, restauração em cadeia, alteração
posterior, retirada de item, histórico de vendas e dados individuais.
PostgreSQL verifica disputa de dois membros, revisão de compra versus operador,
restauração versus operador, e finalização concorrente de compra.

O teste existente de recarga do navegador passou a aguardar efetivamente o novo
documento antes de consultar o campo. A corrida foi reproduzida com o backend
anterior, sem alteração comercial para contorná-la.

PostgreSQL temporário: loopback `127.0.0.1:50498`, banco base
`linked_prices_isolated`, banco de testes `test_linked_prices_isolated`.
Identidade e diretório de dados foram verificados antes dos testes. Instância
desligada ao terminar; diretório temporário preservado em
`C:\Users\Camila Neiva\AppData\Local\Temp\codex-linked-prices-pg-20zmn4om`.
Não houve serviço Windows, instalação administrativa ou alteração do `.env`.

## Riscos e limites

- Mutex global serializa gravações protegidas; prioriza segurança e pode reduzir
  paralelismo. Monitorar duração antes de considerar bloqueios mais granulares.
- Snapshots históricos apenas individuais não comprovam restauração do grupo:
  exigem revisão manual, sem tentar inferir preços anteriores.
- Compatibilidade técnica não prova equivalência física de apresentações:
  a confirmação humana continua necessária.
- Escritas por SQL externo ou por APIs ORM que deliberadamente contornem os
  caminhos protegidos não estão garantidas por constraints de banco. Não usar
  tais caminhos para administrar preços.
- As interfaces pendentes e as duas falhas de regressão impedem declarar esta
  etapa pronta para publicação.

## Manifesto seletivo desta etapa e da preparação preservada

Não executar staging global. Conferir o diff antes de selecionar estes 16 arquivos:

```text
docs/precos-vinculados-etapa-preparatoria.md
docs/precos-vinculados-servico-compras.md
estoque/admin.py
estoque/forms.py
estoque/grupos_produtos.py
estoque/migrations/0131_precos_vinculados_auditoria.py
estoque/models.py
estoque/services/precos_compra.py
estoque/services/precos_vinculados.py
estoque/templates/estoque/cadastrar_produto.html
estoque/templates/estoque/grupos_produtos.html
estoque/tests_compra_pre_revisao.py
estoque/tests_precos_vinculados.py
estoque/tests_precos_vinculados_servico.py
estoque/views.py
estoque/views_grupos_produtos.py
```

Excluir `separacao_vendas_fila.html`, `.worktrees/`, backups, diagnósticos,
arquivos temporários e todos os demais arquivos preexistentes fora do manifesto.
Nenhum arquivo foi colocado em staging. `git diff --check` passou.
