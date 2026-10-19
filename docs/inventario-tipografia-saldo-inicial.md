# Tipografia da linha do tempo e apresentação do saldo inicial

Este documento registra a etapa inicial preservada. A consolidação posterior,
incluindo validação de eventos, ajustes manuais e OK terminal, está descrita em
[inventario-extrato-consolidado.md](inventario-extrato-consolidado.md).

Base publicada: `b99c6d87c1555329d3aa37315013e3f86af4c4c2`.
Worktree isolada: `.worktrees/conferencia-tipografia-b99c6d8`.
Sem commit, push, deploy ou migrations.

## Apresentação

Refinamento localizado de tamanho, peso e contraste na versão B: data 15 px,
operação 16 px, entrada/saída 17 px, rótulos e unidade 12 px. Saldos grandes,
grid, margens e preenchimentos dos cartões preservados. Títulos da comparação
física recebem peso maior. A versão A permanece disponível. Nenhuma informação
removida; evidências continuam fechadas por padrão. Prévia em 1366/390/320 px
com dados inteiramente fictícios e teste de altura compacta no desktop.

O cartão inicial deixava de usar um dado que o avaliador já disponibiliza:
`saldo_antes`. A apresentação agora o reutiliza somente na primeira página,
quando o primeiro movimento coincide exatamente com o início do intervalo,
não existe outro registro naquele instante e a operação possui saldo antes,
saldo depois e unidade certificados pelo avaliador existente. Compras exigem
o tipo “Reconstruído e corroborado”; vendas, “Snapshot registrado”.

Não se subtrai uma entrada do saldo posterior. Não se retroage uma operação
posterior ao início nem se converte a unidade atual do produto. Saldo zero
comprovado continua zero. Períodos personalizados anteriores à primeira operação,
lacunas, horários ambíguos e ausência de evidência mantêm “Não disponível”. Os
detalhes explicam que cada saldo posterior conserva sua própria origem. A origem
da abertura inclui documento consultável e as fontes e limitações já registradas.

O valor anterior de uma compra corroborada é uma reconstrução no instante da
compra, ainda que sua âncora tenha origem num snapshot anterior: não é rotulado
como snapshot original do recebimento.

## Verificação real exclusivamente de leitura

Conexão protegida já existente, sem mudança ou cópia de credenciais. TLS e
identidade comparados ao preflight anterior; `transaction_read_only=on` confirmado
desde a conexão. Guarda recusando SQL que não seja SELECT. Na execução concluída,
43 SELECTs ORM e um SELECT de identidade; encerramento com rollback e fechamento.
Nenhuma escrita no Neon, na conferência nº 1, em vendas, compras ou estoques.

O avaliador existente retornou dinamicamente os seguintes resultados para a
compra #116, recebida em 22/09/2026:

| Produto | Snapshot anterior | Entrada documental | Snapshot posterior | Abertura corroborada |
|---|---|---|---|---|
| Bril Condicionador #1119 | Venda #431: 10 − 6 = 4 UN | 40 UN | Venda #482: antes 44 UN, saída 4 UN, depois 40 UN | 4 UN |
| Trigopan #1118 | Venda #392: 6 − 4 = 2 UN | 100 UN | Venda #506: antes 102 UN, saída 10 UN, depois 92 UN | 2 UN |

Esses números são resultados da inspeção, não constantes do aplicativo ou dos
testes. As quantidades anteriores são corroboradas pelos registros independentes,
não apenas pela diferença entre saldo posterior e compra. Continua válida a
limitação do avaliador: corroboração documental não certifica integralmente todos
os movimentos antigos anteriores ao observador. A mesma regra genérica atende
qualquer produto/intervalo que passe nas validações existentes.

## Testes e integridade

**77 testes aprovados**, em 33,354 s, cluster PostgreSQL local descartável
`inventario-confiabilidade-pg-3eixmzsx`, encerrado. Runner existente, sem executor
de migrations. Inclui as 69 regressões anteriores, Chrome em 1366/390/320 px e
oito novos testes: origem corroborada em A/B, subtração insuficiente, cancelamento
antigo intermediário, unidades incompatíveis, snapshot editado/incompleto, zero,
período anterior à operação e apresentação pura sem SQL com ambiguidade/paginação.

`git diff --check` aprovado. `evidencia_saldos.py`, `saldos_historicos.py`,
`historico.py`, `periodos_historico.py`, models, views, services, URLs e settings
idênticos à base. Sem mudança em cálculo, reconstrução, filtros, períodos,
paginação, permissões, autenticação ou regras comerciais. Ajustes reais de estoque
continuam desabilitados. Avisos de teste preexistentes: namespace `estoque`
duplicado e pasta `staticfiles` ausente; estáticos servidos pelo servidor de testes.

Arquivos da alteração:

- `inventario/extrato.py`: projeção da evidência inicial, sem novas consultas.
- `inventario/templates/inventario/historico.html`: exibição da abertura e origem.
- `static/inventario/historico_b.css`: tipografia apenas na versão B.
- `inventario/tests_saldo_inicial.py`: regressões da apresentação.
- Este documento.

PNGs fictícios em `artifacts/extrato/extrato-b-{1366,390,320}.png`. Script pontual
de inspeção em `artifacts/`, fora dos arquivos da aplicação e sem credenciais.
