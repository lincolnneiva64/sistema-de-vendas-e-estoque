# Regularização dos ajustes pendentes — verificação local

Verificado em 05/11/2026, preservando todas as alterações locais anteriores.
Nenhuma alteração de código foi necessária. Nenhum dado operacional consultado.

## Diagnóstico

O OK confirma a contagem; não autoriza a atualização do estoque. A finalização
administrativa também não aplica ajustes. O estado pendente é mantido enquanto
não houver decisão de aplicação. A configuração local efetiva foi consultada
sem conexão de banco: `INVENTARIO_AJUSTES_HABILITADOS=False`. Esse é um impedimento
concreto à execução local, independente da precisão decimal já corrigida.

Não foi verificada a configuração de nenhum servidor remoto nem a auditoria dos
produtos reais. Portanto, não é possível atribuir individualmente a pendência
real a uma tentativa recusada, ausência de confirmação ou configuração remota.

## Uso do fluxo existente

Com usuário autorizado, abrir a conferência e clicar **Revisar ajustes de estoque**.
O modal também atende conferências finalizadas, sem reabrir contagens. Conferir os
produtos aptos, saldos e bloqueios e clicar **Confirmar atualização**. A alternativa
existente é **Ações administrativas → Revisar ajustes pendentes**, com atualização
individual ou de todos os aptos. O acesso histórico e a prévia expiram em 20 minutos.

Se aparecer aplicação desabilitada, o responsável pelo ambiente deve autorizar e
configurar `INVENTARIO_AJUSTES_HABILITADOS=True` no processo Django, carregando a
configuração novamente. Não foi feita essa ativação. PostgreSQL e os três triggers
existentes de auditoria também são obrigatórios; não remover proteções para aplicar.

Bloqueios documentais exigem verificar as movimentações e sua comprovação. OK
incompatível exige revisão/recontagem pelo fluxo existente. Mudança após a prévia
exige nova revisão. Nenhum desses casos deve ser contornado sobrescrevendo estoque.

## Saldos demonstrados com dados fictícios

Sem movimento posterior, valores em PCT:

| Caso | Antes | Ajuste | Depois | Contagem original |
|---|---:|---:|---:|---|
| Falta em pacote de 6 | 4,000 | −0,167 | 3,833 | 3 PCT + 5 UN = 23 UN |
| Falta em pacote de 6 | 3,500 | −0,167 | 3,333 | 3 PCT + 2 UN = 20 UN |
| Sobra em pacote de 6 | 3,000 | +0,167 | 3,167 | 3 PCT + 1 UN = 19 UN |
| Resíduo em pacote de 12 | 7,084 | −0,001 | 7,083 | 7 PCT + 1 UN = 85 UN |

O último caso é normalização decimal, sem falta física comprovada. A representação
em UN permanece inteira e a projeção em PCT mantém seu resíduo explícito.

Com venda documentada de 1 PCT após o OK do primeiro caso: estoque atual 3,000,
ajuste −0,167, resultante 2,833. Não se substitui o saldo atual por 3,833.

A aplicação vincula proposta, confirmação original, decisão, movimento manual,
observador e eventos. Registra usuário, lote, saldo anterior, diferença e saldo
resultante; preserva embalagens, avulsos e representação física na evidência.
Reenvio retorna a mesma aplicação; concorrência não duplica o movimento. A tabela
existente passa a mostrar **Ajuste aplicado** e mantém o saldo aplicado histórico.

## Validação e pendências de testes

Runner existente `inventario.validar_confiabilidade_postgresql`, cluster exclusivo
`inventario-confiabilidade-pg-s7nqj6g8`, identidade restrita a loopback, porta,
diretório, banco e usuário fictícios. Encerrado ao terminar. Sem migrations.
O primeiro início no sandbox foi impedido pelo token restrito do Windows; a
execução fora do sandbox foi aprovada automaticamente.

80 testes em 39,242 s: 78 aprovados, duas falhas, sem skips. Seleção:

- `tests_projecao_fisica`, `tests_confirmacao_unica`;
- `tests_ajustes_modal`: PrecisaoModalTests, AjustesModalHTTPTests,
  AjustesModalConcorrenciaTests;
- `tests_ajustes`: PreparacaoTests, AplicacaoTests, ConcorrenciaAjustesTests;
- `tests_atualizacao_resultado.AtualizacaoResultadoTests`.

Passaram aplicação individual/lote, falta/sobra/resíduo, conferência finalizada,
venda posterior, reenvio, concorrência, auditoria, rollback, permissões e CSRF.

Pendências registradas: os testes `test_lote_mesmo_resultado_uma_confirmacao_sem_reaplicacao`
e `test_saldo_aplicado_historico_independente_do_atual`, em
`tests_atualizacao_resultado`, ainda esperam a ausência de `revisar-atualizacao`
e o texto antigo `Saldo registrado: 8 CX`. São expectativas incompatíveis com a
interface atual; o teste atual do modal exige a permanência do botão. Não se
alterou o layout aprovado para satisfazê-las. Devem ser atualizadas para conferir
os estados e detalhes atuais em manutenção dos testes; não são falhas de escrita
de estoque. Logs e `validation.json` permanecem no diretório temporário do cluster.

Não foi aberto navegador, criado ambiente de navegador, feita migration, commit,
push ou deploy. Nenhuma conexão ao Neon ou alteração de configuração operacional.
