# Fase B — capacidade numérica e unidades da auditoria

Correção local dos dois bloqueios identificados no piloto individual. Não altera
escritores, observador, triggers, cálculos, conversões ou modelos comerciais.

## Capacidade demonstrada

`DetalheMovimentoEstoque.quantidade_entrada` passa de numeric(18,9) para
numeric(27,9). O CHECK permite valores finitos estritamente entre −10^18 e 10^18.
São preservadas nove casas decimais, sem alterar a precisão do estoque.

O limite decorre dos campos e validadores existentes:

- `Produto.quantidade`, embalagens, avulsos e `contado_base`: numeric(12,3).
  O conversor exige total exato e projeção operacional menores que 10^9.
- Fator comercial: numeric(10,2), portanto menor que 10^8. A relação física
  confirmada na conferência permite fator inteiro positivo menor que 10^9.
- Quantidade física = quantidade-base exata × fator; logo, para qualquer contagem
  aceita pelo conversor atual, a quantidade física é menor que 10^18.
- Os produtos dos operandos de três casas com fator comercial de duas casas
  precisam de até cinco casas decimais; relações físicas inteiras precisam de
  até três. As nove casas da auditoria comportam esses valores sem perda.

Casos executados: 100.000.000 DZ = 1.200.000.000 UN; fator comercial máximo
99.999.999,99 na conversão SC/KG; relação física confirmada de 999.999.999 UN;
embalagens próximas ao limite, avulsos discretos/contínuos e saldos KG/MIL com
três casas próximos ao máximo operacional. O próprio campo foi testado nos
extremos ±999.999.999.999.999.999,999999999, com leitura exata de volta.

## Unidades

`unidade_base` e `unidade_entrada` conservam max_length=20. Validador e CHECK
exigem ao menos um caractere não branco (`\S`), sem lista de unidades, restrição
a ASCII, normalização ou alteração de texto. Foram aceitas M³, UN/FD, UN, DZ,
PCT, FD, KG, MIL e uma sigla de vinte caracteres. Vazio, somente espaços,
tabulação/quebra de linha e vinte e um caracteres foram rejeitados no modelo
e no banco. A consistência da conversão continua protegida pelo CHECK existente.

## Migration

`0011_capacidade_unidades_detalhe_movimento` depende da 0010 e altera somente os
três campos e os dois CHECKs correspondentes da tabela complementar. 0009 e
0010 permanecem intactas. Nenhum backfill ou UPDATE de registros.

A cadeia real 0009 → 0010 → 0011 foi aplicada no PostgreSQL descartável com um
complemento legado preenchido e imutabilidade ativa. Uma falha DDL injetada
após ampliar a coluna reverteu a migration; reaplicação bem-sucedida preservou
o registro anterior. Atualização/exclusão do complemento continuam bloqueadas.
Falha real de CHECK na inserção durante a regularização reverteu toda a operação.

Retorno operacional futuro deve conservar o esquema ampliado: reduzir novamente
a capacidade/restringir unidades pode falhar após receber dados agora válidos.
Não houve aplicação de migrations em banco operacional.

## Testes e isolamento

98 testes aprovados em 32,220 s, cluster descartável
`inventario-confiabilidade-pg-u685sme_`, encerrado pelo runner protegido. Incluem
10 testes de limites/migration, 12 da estrutura original, 18 do piloto e 58
regressões comerciais/de conferência. Reenvios e concorrência preservaram um
complemento por movimento; lote e manual comum continuaram sem complemento.

```
venv/Scripts/python.exe -B -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_limites_auditoria inventario.tests_detalhe_movimento inventario.tests_rastreabilidade_individual estoque.tests_estoque_manual inventario.tests_integracao inventario.tests_confirmacao_unica inventario.tests_ajustes.PreparacaoTests inventario.tests_ajustes.AplicacaoTests
```

As fixtures de migration passaram a restaurar versões posteriores do esquema e
a remover somente suas próprias proteções temporárias, com verificação da identidade
do cluster. Isso evita que um teste deixe tabela antiga/função para o próximo;
não altera migrations nem proteções da aplicação. O teste antigo que rejeitava
sigla minúscula foi ajustado para testar unidade somente em branco.

Autodetector sem divergência modelo/migrations e `git diff --check` aprovado.
Avisos preexistentes: namespace `estoque` duplicado e pasta `staticfiles` ausente.
Não se declara execução de toda a suíte do projeto.

Nenhum bloqueio adicional encontrado no escopo testado. A integração em lote
continua fora do escopo e ainda exige projeto de identidade, inserção em lote,
desempenho e rollback conjunto. Sem Neon/Render, saldos reais, commit, push ou
deploy; trabalhos locais anteriores preservados.
