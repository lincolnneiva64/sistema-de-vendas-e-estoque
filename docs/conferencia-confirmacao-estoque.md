# Confirmação do estoque e encerramento

Base: `f74248e3a88c8cc8037591a3ca7b9a56094a40c4`.
Worktree isolada: `.worktrees/padronizacao-conferencia-f74248e`, HEAD destacado.

## Regra de apresentação

A última coluna é **Confirmação do estoque**. Confirmado exige o recibo da
rodada atual, obtido pelo serviço existente `marco_fisico.recibos`: aplicação
com movimento ou evento auditado `marco_sem_ajuste`. Quantidade igual ao saldo,
diferença zero, decisão isolada ou estado Aplicado sem recibo não substituem
essa comprovação. Um marco já aplicado continua confirmado mesmo se uma
movimentação posterior impedir a comparação atual.

Sem recibo, o OK registrado aguarda confirmação. Impedimentos reais identificados
pela validação existente (sinalização posterior, projeção não comprovada,
incompatibilidade da contagem/cadastro e bloqueios da prévia) pedem revisão.
Produtos ainda sem OK exibem Aguardando contagem, sem serem confundidos com
marcos pendentes de contagens concluídas. Os estados não autorizam execução:
a elegibilidade definitiva é reavaliada pela prévia e pelo POST existentes.

O resumo separa produtos conferidos, marcos confirmados, marcos aguardando
confirmação e produtos que exigem revisão. A informação sobre correção de
quantidade permanece nas diferenças, nos saldos da prévia e nos detalhes.
As sete colunas, os detalhes recolhidos e a contagem original com unidade quando
a projeção não é comprovada permanecem preservados.

## Conferência nº 42

Não foi consultado nenhum registro de produção. Pelas regras anteriores, o
encerramento contava diferenças pendentes somente quando contado != esperado.
Assim, 47 OKs, 24 aplicações e zero diferenças pendentes **não** significam
47 marcos confirmados. Os outros 23 continuam sem confirmação comprovada e
precisam ser separados entre aguardando confirmação e revisão, conforme suas
evidências. Não se afirma que os 23 são elegíveis sem consultar a prévia real.

O teste sintético de classificação com 47 OKs, 24 recibos e nenhum impedimento
produz 24 confirmados e 23 aguardando confirmação. Outros testes exercitam
recibos reais, marcos zero e bloqueios no banco descartável.

## Reutilização e segurança

Foi identificado e reutilizado o modal `ajustes_modal`, a prévia assinada
`ajustes.previa_atualizacao` e o serviço `ajustes.atualizar`. A ação passou a
se chamar **Confirmar marcos pendentes em lote**. A prévia inclui todos os
elegíveis, inclusive diferença zero, e lista individualmente os excluídos,
incluindo rejeições e ausência de recibo compatível. Não foi criado serviço
paralelo de aplicação nem seleção que presuma diferença não zero.

Mantidos transação integral, locks ordenados de produtos/rodadas/documentos,
idempotência, validação da assinatura, revalidação antes da primeira escrita,
CSRF, permissões, flag de execução e auditoria. Mudança após a prévia recusa
o lote; falha em um produto reverte todos os registros novos. Zero continua
estabelecendo o marco sem movimento de quantidade zero.

O encerramento administrativo informa os quatro totais e revalida sua assinatura
no POST. Reconhece recibos, em vez de inferir aplicação por decisão ou saldo.
Os campos antigos `aplicados`, `sem_ajuste` e `diferencas_pendentes` permanecem
para compatibilidade, mas a interface usa os novos totais. Nenhum registro
histórico é reescrito. O encerramento normal também informa confirmados,
aguardando confirmação e revisão na resposta e na consulta após finalização.
Encerrar não chama o serviço de confirmação/aplicação.

Para evitar repetir o carregamento na abertura comum, o resumo administrativo
reutiliza a classificação já calculada para a tabela. A prévia completa reutiliza
os recibos já carregados; a associação entre linhas e marcos usa um dicionário.

## Arquivos alterados/criados

- `inventario/confirmacao_estoque.py`: classificação compartilhada somente leitura.
- `inventario/ajustes.py`: classificação na projeção de apresentação; sem alteração
  das funções de avaliação, autorização ou aplicação.
- `inventario/ajustes_modal.py`: contagem de marcos e impedidos individuais.
- `inventario/ajustes_views.py`: resumo da tela de confirmação existente.
- `inventario/encerramento.py`: totais por recibo e assinatura do encerramento.
- `inventario/views.py`: tabela, resumo e retorno de encerramento.
- `inventario/templates/inventario/acoes_administrativas.html`.
- `inventario/templates/inventario/ajustes.html`.
- `inventario/templates/inventario/ajustes_diretos.html`.
- `inventario/templates/inventario/atualizacao_resultado.html`.
- `inventario/templates/inventario/detalhe.html`.
- `inventario/templates/inventario/encerramento.html`.
- `inventario/templates/inventario/mobile.html`.
- `inventario/templates/inventario/previa_ajustes_modal.html`.
- `inventario/templates/inventario/situacao_atualizacao.html`.
- `static/inventario/ajustes_modal.js`: textos de confirmação e resultado.
- `inventario/tests_situacao_apresentacao.py`: expectativas dos novos estados.
- `inventario/tests_padronizacao_conferencia.py`: testes direcionados do fluxo.
- `docs/conferencia-confirmacao-estoque.md`: este registro.

## Validação e limites

- 14 testes direcionados aprovados em 3,675 s, PostgreSQL local descartável,
  pelo runner `inventario.validar_confiabilidade_postgresql`. Estado final do
  cluster `inventario-confiabilidade-pg-m9hzakef`: stopped; sem migrations.
- 5 testes de apresentação sem banco aprovados em 0,047 s.
- Cobertura: classificação 47/24/23, ausência de recibo, zero, aplicação com
  correção, bloqueio individual, assinatura desatualizada, rollback, reenvio,
  CSRF, permissão, flag desligada, encerramento normal/administrativo, token
  da tela e mudança dos totais após confirmação, sete colunas e contagem original.
- Verificação AST: `avaliar`, `bloquear`, `decidir`, `aplicar`, `atualizar` e
  `_gravar_aplicacoes` idênticos à base. `painel.py`, `marco_fisico.py`,
  `services.py` e `linha_tabela.html` também permanecem intactos.
- `git diff --check` aprovado.

A primeira tentativa de iniciar o cluster foi impedida pelo sandbox; a execução
fora dele usou o mesmo runner protegido, restrito ao novo host/porta/diretório.
Na primeira rodada, uma asserção procurava a quantidade como substring de HTML,
sem considerar os spans existentes; foi corrigida para verificar o texto renderizado.
Os avisos existentes de namespace estoque duplicado e staticfiles não coletado
não impediram os testes.

Não houve validação visual em navegador nem checagem independente do JavaScript
com Node (indisponível). Não foi medida a abertura da conferência real. O código
da matemática não mudou; não foram executadas suítes comerciais ou gerais.
Nenhuma configuração operacional foi ativada. Sem commit, push, deploy,
migrations, acesso a Neon/Render ou alteração de dados reais.
