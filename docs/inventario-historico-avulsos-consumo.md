# Consulta histórica: avulsos e consumo próprio

Correção local de 05/11/2026, restrita à leitura. Nenhuma migration necessária.

## Apresentação e evidências

- Quantidade documental comprovada aparece como `Saída −4 UN`; a baixa-base fica
  secundária, por exemplo `Baixa registrada no estoque: −0,333 PCT`.
- Consumo próprio participa da associação documental com referência exata,
  produto, origem compatível, saldos idênticos e ausência de múltiplos candidatos.
  O observador associado aparece nos detalhes como evidência, sem nova saída.
- A consulta aceita snapshot-base aritmeticamente consistente sem certificar a
  conversão documental. Cancelamentos, edições inseguras, limites excedidos,
  observadores conflitantes e dados incompletos continuam bloqueando o saldo.
- Saldos avulsos são pontuais: não servem para reconstruir compras nem propagar
  saldos através de lacunas. Não há consulta ao fator atual para prová-los.
- A diferença decimal inferior à equivalência de uma unidade inteira recebe uma
  explicação. Não se atribui sua causa automaticamente a arredondamento.

O validador `evidencia_saldos.snapshot`, a conciliação, os serviços de estoque,
as gravações comerciais e as conferências não foram alterados.

## Arquivos

`inventario/extrato.py`, `extrato_apresentacao.py`, `extrato_unidades.py`,
`saldos_historicos.py`, `saldos_apresentacao.py`, templates
`historico_unidades_evidencia.html` e `historico_conferencia.html`, testes
`tests_extrato_unidades.py` e `tests_saldos_historicos.py`, e este documento.
As alterações anteriores em locações foram preservadas.

## Validação

115 testes aprovados em PostgreSQL 17.11 descartável em 24,257 segundos, sem
skips. Cluster `inventario-confiabilidade-pg-voxfj4ld`, encerrado ao terminar;
`migrations_executed=false`. O runner restringe host, porta, usuário, banco e
diretório do servidor antes de usar os dados fictícios. Nenhum acesso operacional.

Comando (sem classes de navegador):

```powershell
venv/Scripts/python.exe -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_extrato_unidades.UnidadesTests inventario.tests_extrato_unidades.SnapshotBaseTests inventario.tests_extrato_duplicacoes.VendasDuplicadasTests inventario.tests_extrato_duplicacoes.RegularizacaoTests inventario.tests_saldos_historicos.SaldosHistoricosTests inventario.tests_extrato_cronologico.CronologiaTests inventario.tests_ajustes.PrecisaoFisicaTests inventario.tests_ajustes.PreparacaoTests inventario.tests_ajustes.AplicacaoTests inventario.tests_ajustes.ConcorrenciaAjustesTests
```

Cobertura: pacotes, UN/PCT, outras unidades, consumo de 4 UN, venda de 3 UN,
associação única e ambígua, snapshots incompletos/inconsistentes, fatores atuais
alterados, edições, cancelamentos, paginação, SELECTs limitados sem escrita,
diferença de −0,001 preservada, auditoria e concorrência de ajustes.

Uma primeira seleção ampla incluiu classes de navegador indevidamente. Nela,
`RegularizacaoBrowserTests.test_desktop_mobile_evidencias_recolhidas_e_sem_rolagem`
falhou na verificação de largura da página (linha 232); a causa e a existência na
base anterior não foram comprovadas. Essa validação visual permanece pendente;
não foi alterado CSS para contorná-la. Não há navegador/prévia criado para entrega.
As demais falhas iniciais de expectativas antigas e espaçamento foram corrigidas
e suas classes estão incluídas na rodada final aprovada.

Os registros reais da Amstel não foram consultados. A exibição de cada saldo real
depende das evidências efetivamente gravadas, sem garantia de eliminar todas as
lacunas históricas. Sem commit, push ou deploy.
