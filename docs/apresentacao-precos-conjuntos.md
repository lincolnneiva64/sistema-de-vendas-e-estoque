# Apresentação e preços vinculados — retomada

Base: `f28813e20c467355873093915e7da260064e3dbf`.
Worktree: `.worktrees/apresentacao-precos-f28813e`.

As quatro alterações recuperadas (82 inserções, 11 remoções) já implementavam
formulário com quatro preços opcionais, validação da proposta final de cada
integrante, prévia assinada com apresentação e preços, confirmação explícita e
gravação conjunta pelo serviço de preços. Foram preservadas integralmente.
A retomada completou os testes e esta documentação.

A transação de `serializar_grupos` cobre bloqueios de catálogo/grupo/produtos,
revalidação da assinatura, atualização dos produtos, versão do grupo e auditoria.
Cada UPDATE contém a apresentação e os preços finais; não existe salvamento
intermediário da apresentação com preços zerados. Custos individuais são usados
na validação e permanecem intactos. Sem mudança de preços, o comando continua
preservando os preços individuais. Demais campos do formulário não são enviados.

## Verificações

Somente fixtures sintéticas Frisco Laranja, Maracujá e Morango. Custos distintos
11,70 / 12,70 / 13,70; preços fracionados anteriores zero; proposta CX/18 UN,
preços 15,80 / 16,00 / 1,00 / 1,10. Nenhuma consulta aos produtos reais.

Runner existente `validar_compra_timeout_pg.py`: settings independentes de `.env`,
cluster PostgreSQL novo em loopback, guarda de host/porta/usuário/banco/diretório,
migrations existentes somente no banco descartável e encerramento no finally.
O sandbox Windows impediu o pg_ctl de criar token restrito; execuções fora dele
foram aprovadas automaticamente. Nenhuma alteração no runner ou nas proteções.

- 21 testes aprovados em 27,040 s, cluster `inventario-disposable-pg-00pr3q__`.
  Cinco novos cenários (três backend e dois navegador), além dos 16 testes
  anteriores de apresentação. Confirmação, cancelamento, preços antigos zerados,
  persistência/reabertura dos três sabores, custo individual, assinatura alterada,
  reenvio, rollback na segunda escrita, preservação de campos e movimentos.
- Rodada adicional: 37 testes em 10,948 s; 35 aprovados e duas falhas em compras,
  cluster `inventario-disposable-pg-u3lebes2`. Os dois novos testes passaram:
  custo fracionado persistido maior que a proposta/versão obsoleta e falha da
  auditoria revertendo produtos e versão do grupo. A suíte de serviço passou,
  incluindo concorrência entre operadores e compras/reversão.
- Chrome desktop 1280 e mobile emulado 390: prévia mostra os três produtos,
  quatro preços anteriores/novos e custos individuais. Cancelar não escreve;
  confirmar salva e reabrir cada produto comprova persistência. Modal sem
  extrapolar a largura. Capturas abertas e inspecionadas visualmente.
- `git diff --check` e compilação Python do novo módulo aprovados.

## Limitações e pendências

Mobile emulado, sem aparelho físico; sem medição de latência de produção.
As duas falhas adicionais são
`BloqueiosPrecosTests.test_authorized_revision_records_human_actor` (400/200) e
`BloqueiosPrecosTests.test_purchase_restore_requires_price_permission` (302/403).
Não afrouxar as expectativas de autorização nem alterar compras para mascará-las.

Evidências e script de comparação da base ficam apenas em `amostras/`, fora da
entrega de código. Nenhum arquivo da pasta principal, locações ou pagamentos foi
editado. Sem Neon, dados operacionais, migrations novas, commit, push ou deploy.

Arquivos da entrega: os quatro arquivos recuperados, mais
`estoque/tests_apresentacao_precos_conjuntos.py` e este documento.

Comparação das duas falhas: reproduzidas identicamente em 0,258 s no cluster
`inventario-disposable-pg-hes3oavu`, encerrado. Um import loader carregou os três
módulos Python alterados diretamente de `git show f28813e:<arquivo>`, somente em
memória, antes de iniciar o Django. O template alterado não participa desses
POSTs. Portanto as falhas já ocorrem com o backend original; permanecem registradas
para manutenção de compras fora deste escopo. Nenhum arquivo foi restaurado ou
substituído para realizar a comparação. Total das rodadas da implementação:
56 testes aprovados e duas falhas preexistentes reproduzidas. Aviso preexistente
`urls.W005` mantido.
