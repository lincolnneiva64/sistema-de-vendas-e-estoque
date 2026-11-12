# Restaurar encerramento administrativo com pendências

Base: `637457a3ab90841df6cc23e9ed174cb1ce6b6f79`.
Worktree isolada: `.worktrees/encerramento-pendencias-637457a`.
Nenhum acesso à conferência real nº 42 ou a dados operacionais.

## Causa

`views.detalhe` só apresenta o formulário quando `encerramento.previa` consegue
gerar o resumo assinado. A atualização anterior reutilizou `ok_referencia` nesse
resumo também para contagens ainda pendentes. A recusa de uma confirmação
administrativa antiga ambígua propagava ValueError, suprimindo a prévia e,
consequentemente, o formulário inteiro.

A exigência continua correta para aplicar estoque. Ela não deve impedir o
encerramento administrativo consciente e explícito sem aplicação.

## Correção limitada ao encerramento

Reutilizados o mesmo formulário, endpoint, prévia assinada e
`encerramento.finalizar`. Não há um segundo mecanismo de finalização.

Quando a contagem tem registro original compatível, mas a prova física/temporal
é impedida, o resumo conserva o motivo como pendência em vez de suprimir o
encerramento. Continuam obrigatórias a rodada atual única, a presença de OK,
a consistência dos valores registrados e a compatibilidade com a confirmação
imutável. Inconsistência entre uma rodada e um recibo aplicado não é ignorada.

A tela mostra quantos produtos foram conferidos, quantas referências foram
aplicadas e quais produtos permanecem aguardando confirmação ou revisão. Cada
pendência tem nome, situação, motivo, quantidade/unidade e horário registrados.
O checkbox obrigatório declara expressamente a autorização de encerrar mantendo
as pendências sem resolução e sem aplicar ajustes. O servidor também exige a
confirmação, as permissões existentes, CSRF e token válido do responsável.

O evento existente `conferencia_finalizada` passa a guardar a lista de pendências,
motivos e dados da rodada/confirmacão, inclusive IDs, versão, contagem, saldo de
referência, unidade, embalagens, avulsos, sequência e horário original. O horário
é serializado em ISO completo, preservando microssegundos. Os registros anteriores
não são alterados. Após o encerramento, as pendências preservadas ficam em
detalhes na própria conferência e na auditoria existente.

Os dados constam da assinatura e são recalculados pelo finalizador existente.
A página reutiliza as linhas que já carregou; a consulta de confirmação ganhou
somente uma opção interna para devolver essas linhas junto dos totais. Sua
resposta padrão e suas regras de classificação permanecem iguais.

## Arquivos alterados/criados

- `inventario/encerramento.py`: resumo, motivos e dados preservados na prévia.
- `inventario/confirmacao_estoque.py`: opção de leitura detalhada para reutilização.
- `inventario/views.py`: reaproveita as linhas já consultadas na prévia.
- `inventario/templates/inventario/encerramento.html`: autorização explícita e lista
  das pendências antes/depois do encerramento.
- `inventario/tests_encerramento_pendencias.py`: casos direcionados.
- `docs/encerramento-administrativo-pendencias.md`: este registro.

## Verificação

10 cenários direcionados distintos aprovados em PostgreSQL descartável pelo
runner protegido `inventario.validar_confiabilidade_postgresql`:

- Formulário disponível para confirmação administrativa antiga ambígua.
- GET/POST com token da tela, encerramento, histórico e repetição idempotente.
- Aplicação continua bloqueada antes e depois do encerramento.
- Ausência de checkbox não encerra; permissões, CSRF e versão continuam exigidos.
- Lista mista com aplicação existente, confirmação pendente e revisão.
- Rollback integral do encerramento se a gravação da auditoria falhar.
- Encerramento normal com pendência sem aplicar ajustes.
- Ausência de OK continua impedindo encerramento.
- Diferença zero pendente permanece sem aplicação ao encerrar.
- Token da tela com confirmados, pendentes e revisão continua funcionando.

Snapshots confirmaram estoque, rodadas, confirmações, decisões, aplicações,
eventos de ajuste, movimentos e movimentações manuais intactos. O único evento
novo esperado é o registro normal do encerramento administrativo.

A rodada inicial encontrou nove cenários aprovados e uma falha na preservação
exata do horário na nova cópia da auditoria: DjangoJSONEncoder reduzia os
microssegundos. Corrigida a serialização para ISO completo; três cenários afetados
foram aprovados em 1,149 s. A verificação final GET/POST da ambiguidade foi aprovada
isoladamente em 0,480 s. Clusters finais `inventario-confiabilidade-pg-1jqgnyim`
e `inventario-confiabilidade-pg-cx_2pwdt`, ambos encerrados; sem migrations.

Verificação AST: `encerramento.finalizar` e `permitir` permanecem intactos.
Também permanecem idênticos à base: `ajustes.py`, `marco_fisico.py`, `services.py`,
`contagem_admin.py`, `painel.py`, `models.py` e `linha_tabela.html`. Matemática,
marco físico, bloqueios de aplicação e proteção contra duplicidade não mudaram.
`git diff --check` aprovado.

Limitações: validação com dados fictícios e HTML real via cliente HTTP, sem nova
navegação interativa em navegador e sem consulta ao caso real. Os motivos antigos
continuam exigindo resolução própria se futuramente houver intenção de aplicar
estoque. Encerrar não os resolve nem revalida como contagem física.

Sem staging, commit, push, deploy, alteração de configuração, banco operacional
ou dados reais. Avisos locais existentes: namespace estoque duplicado e diretório
staticfiles não coletado; não impediram a verificação.
