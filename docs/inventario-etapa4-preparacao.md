# Etapa 4: conciliação documental e propostas da conferência nº 1

Base: 7e696c2c483cbdae305e2c56d4a820ff17679170. Worktree isolada.

Removidos os bloqueios pelo número da conferência na avaliação e na aplicação.
`INVENTARIO_AJUSTES_HABILITADOS = False` permanece obrigatório. Nenhum ajuste ou
aprovação foi executado em produção. Não há migration nem configuração nova.

Preparação em leitura e aprovação gerencial usam as mesmas validações das demais
conferências. A diferença é contado_base − esperado no OK; saldo proposto é
estoque atual + diferença. Confirmações originais, cadastro congelado, sinais
físicos, continuidade e saldo/sequência atuais continuam sendo verificados.

## Conciliação documental

Vendas posteriores ao último OK são elegíveis quando há um único item do produto
e um único movimento observado com a referência exata da venda. A unidade do
item e do snapshot deve coincidir com a base congelada no OK; quantidade, baixa
e saldos do snapshot/observador devem coincidir. Não se usam fatores atuais.
A criação da venda e o movimento devem estar em um trecho temporal de até cinco
segundos; divergências maiores bloqueiam sem inferência. Movimentos entre
confirmações anteriores ao último OK não são liberados por esta regra.

Cancelamentos, devoluções, remoções/ajustes do próprio item, eventos ambíguos,
unidades incompatíveis e sequências descontínuas bloqueiam individualmente.
Outras origens (compras, devoluções, ajustes) sem snapshot histórico equivalente
continuam bloqueadas. Conciliação textual e coincidência de saldos não bastam.

Reutilizado o classificador de edições do extrato: nome diferente só é aceito
quando associado independentemente a outro produto da nota, sem colisão com o
nome do alvo. Edições do alvo ou genéricas não são liberadas. Eventos benignos de
visualização/WhatsApp não expiram propostas.

Evidências materiais entram na assinatura, com documento, item, observador,
unidade, quantidade, saldos, sequência e horários. São revalidadas ao aprovar e
aplicar. Estoque e documentos ficam travados até o commit; documento ocupado
recusa a execução. Uma falha reverte integralmente o lote. Idempotência,
permissões, confirmação expressa e auditoria permanecem. A conciliação digital
não certifica o instante da entrega física; revisão gerencial continua exigida.

## Avaliação real somente leitura em 19/10/2026 às 06:12 Brasília

TLS, identidade de produção e transaction_read_only=on confirmados. Nenhuma
proposta gravada. Valores pontuais devem ser reavaliados antes de decidir.

| Produto | Esperado OK | Contado base | Diferença | Atual | Proposto | Situação |
|---|---:|---:|---:|---:|---:|---|
| 171 Fermento Angest | 11 UN | 9 UN | -2 UN | 11 UN | 9 UN | Pendente de revisão |
| 223 Trigo sem fermento | 0,200 FD | 0 FD | -0,200 FD | 0,200 FD | 0 FD | Pendente de revisão |
| 224 Trigo com fermento | 2,100 FD | 2,100 FD | 0 FD | 2,100 FD | 2,100 FD | Sem ajuste |
| 1066 Trigo 25Kg | 1 SC | 1 SC | 0 SC | 1 SC | 1 SC | Sem ajuste |
| 1118 Trigopan | 68 UN | 66 UN | -2 UN | 68 UN | 66 UN | Pendente de revisão |
| 1119 Bril | 10 UN | 6 UN | -4 UN | 4 UN | 0 UN | Pendente de revisão |

Nos três primeiros produtos com diferença não havia movimentos relevantes.
Bril: observador #21, sequência 1, venda #907, item #3539: saída de 6 UN com
snapshot 10 → 4 UN, após o OK. Edição às 11:17 acrescentou produto #763, sem
comprometer o Bril. Assim, 6 contados − 6 vendidos = 0 UN e 4 atuais − 4 de
diferença = 0 UN. IDs/valores reais são somente diagnóstico, nunca regras ou
fixtures do código. A situação sem ajuste é classificação em leitura: não cria
proposta, movimento nem decisão fictícia para os dois produtos sem diferença.

## Horários e permanência

- OK físico: `ConfirmacaoContagem.ok_em` conserva cada confirmação; o último OK
  também está em `RodadaContagem.ok_em`. Não são regravados pela preparação.
- Aprovação: `PropostaAjuste.criado_em`, responsável, confirmação original e
  `EventoAjuste.criado_em` registram a decisão e evidência congelada.
- Aplicação futura: `AplicacaoAjuste.aplicado_em`, `DecisaoAjuste.aprovado_em`,
  movimento manual e eventos registram horários, usuário e lote.
- Preparação/auditoria somente de leitura não gera registro permanente com seu
  próprio horário no banco. O horário deste relatório é referência local; a
  assinatura sozinha não comprova o instante da consulta. A revisão gerencial
  gravada recebe o horário da aprovação, não um horário retroativo do OK.

Mantidas as proteções da migration 0007; não foi criada migration para registrar
consultas nem alteradas as notas de vendas e compras.

## Validação

Versão final: 102 testes aprovados em PostgreSQL descartável (54,722 segundos),
incluindo Chrome desktop/mobile, ajustes individuais/lote, edição de outro
produto, eventos ambíguos, snapshots, rollback, edição documental concorrente,
prévia antiga após nova venda, concorrência de vendas/compras e auditoria.
Executor `inventario.validar_confiabilidade_postgresql` com módulos
`tests_conciliacao_documental`, `tests_ajustes`, `tests_confiabilidade`,
`tests_postgresql`, `tests_migrations_ajustes`, `tests_saldos_historicos` e
`tests_saldo_inicial`. Cluster encerrado. `git diff --check` sem erros.

Avisos do ambiente: namespace `estoque` duplicado e ausência de `staticfiles`
coletados em algumas verificações HTTP locais; nenhum teste dessa suíte falhou.

Permanece documentada a divergência preexistente de
`tests_ensaio_piloto.PublicHttpsTests`: espera 403 no acesso anônimo a
`/inventario/`, enquanto `login_required` retorna 302 para login. Reproduzida no
commit publicado e na worktree na etapa anterior. Autenticação não foi alterada;
não se declara aprovação integral de todos os testes do projeto.
