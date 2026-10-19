# Conferência de estoque com confirmação única

Base: `ee36165be2e5b66d3189d954cbe37bf15a25b6c6` (commit informado como publicado).
Worktree: `.worktrees/conferencia-simplificada-ee36165`, HEAD destacado nessa base.
Data: 19/10/2026, America/Sao_Paulo.

## Interface entregue

Funcionário conta e dá OK; gestor confere os valores e escolhe **Atualizar estoque**
ou **Atualizar todos os produtos conferidos**. A seleção individual usa a rodada;
o lote inclui todos os aptos, inclusive fora da página atual, até o limite de 500.
Bloqueados, rejeitados, aplicados e produtos sem diferença ficam fora da seleção.

Cada produto mostra estoque registrado no OK, quantidade física, diferença,
estoque atual, saldo após atualização e data/hora do OK. **Ver movimentações**
continua disponível sem abertura obrigatória. Motivos de bloqueio aparecem no
próprio produto. Os detalhes técnicos permanecem recolhidos.

A confirmação apresenta produto, saldo atual → saldo final e diferença. Tem
**Cancelar**, **Confirmar atualização** e **Adicionar observação**, opcional e
recolhido. Não exige justificativa nem checkbox. O texto da confirmação é:
“Confira os saldos antes de atualizar o estoque.” O botão continua sendo a
autorização gerencial explícita, vinculada ao OK físico original na auditoria.

## Confirmação e segurança

`ajustes.previa_atualizacao` assina a seleção de rodadas e a assinatura das
respectivas evidências. A prévia expira em 20 minutos. GET só consulta; não
cria propostas nem autorizações. POST exige CSRF, sessão gerencial, permissão
`aprovar_ajuste` e confirmação expressa pelo botão.

`ajustes.atualizar` executa numa única `transaction.atomic`:

1. Verifica a flag, PostgreSQL e os três triggers de proteção da auditoria.
2. Trava conferência, itens, rodadas e produtos na ordem existente.
3. Reconhece a repetição da mesma seleção aplicada e devolve os registros
   existentes. Seleção parcialmente aplicada exige nova conferência.
4. Reavalia OK, confirmação física original, unidade, cadeia de movimentos,
   documentos, sinais e assinatura contra o resumo que o gestor confirmou.
5. Chama `decidir` para persistir a proposta aprovada, usando a observação ou
   um motivo automático. Grava também `confirmado_gestor`, com usuário,
   horário automático, confirmação física, assinatura, observação e indicação
   de confirmação única. Propostas antigas aprovadas mantêm seus dados originais;
   o evento adicional registra o gestor que confirmou a execução atual.
6. Chama `aplicar`, que revalida novamente evidência e movimentos sob os locks,
   usa o serviço de estoque e confere a atribuição do movimento no observador.

Aprovação, confirmação, estoque, movimento, decisão, aplicação e eventos só
persistem juntos. Falha em qualquer produto desfaz o lote inteiro, incluindo
as aprovações recém-criadas. A proposta rejeitada não é reativada.

A auditoria existente conserva usuário, datas/horas, lote, OK físico original,
movimentos e documentos considerados, diferença e saldos anterior/posterior.
Os triggers imutáveis existentes permanecem. Nenhum modelo ou migration novo.
Os serviços e handlers anteriores continuam protegidos e disponíveis para
compatibilidade, mas a interface normal usa somente a confirmação única.

O cálculo permanece **estoque atual + (contado no OK − esperado no OK)**.
Uma venda ou compra posterior nunca transforma a contagem antiga em saldo atual.
Se a evidência mudou depois do resumo, a operação é recusada; o gestor precisa
conferir o novo resumo. A observação opcional não substitui validação documental.

## Conferência nº 1

Não houve acesso nem escrita em produção nesta etapa. Os seis produtos não são
filtrados por nome/ID, removidos ou substituídos por fixtures. Referências da
última auditoria fornecida, que precisam ser recalculadas na operação futura:

| Produto | Referência anterior → posterior |
|---|---|
| Fermento Angest | 11 → 9 UN |
| Trigo sem fermento | 0,200 → 0 FD |
| Trigopan | 68 → 66 UN |
| Bril Condicionador | 4 → 0 UN, sujeito à conciliação da venda nº 907 |
| Trigo com fermento | Sem ajuste |
| Trigo 25 kg | Sem ajuste |

Nenhum desses saldos/IDs foi colocado como regra no código. Os testes usam
bancos e documentos sintéticos. Um cenário equivalente ao Bril valida
10 esperados, 6 contados, venda documentada de 6, estoque atual 4, diferença −4
e final 0, sem modificar o item da venda nem o OK. Sem evidência suficiente,
a conciliação existente continua bloqueando individualmente.

## Testes e prévias

Execução ampliada: **114 testes aprovados em 52,074 s**, PostgreSQL descartável,
com saída `OK`, zero falhas e cluster encerrado. Módulos:

- `tests_confirmacao_unica`, `tests_ajustes`, `tests_conciliacao_documental`;
- `tests_confiabilidade`, `tests_postgresql`, `tests_migrations_ajustes`;
- `tests_saldos_historicos`, `tests_saldo_inicial`.

Cobertura: aplicação individual/lote, repetição, duas confirmações concorrentes,
concorrência de vendas/compras e edição documental, prévia obsoleta, rollback
inclusive aprovação/evento de confirmação, documento conciliado, falta de evidência,
sem diferença, rejeição, seleção parcialmente aplicada, permissões, CSRF,
flag desabilitada e proteção contra alteração/exclusão da auditoria.

Chrome real via CDP: layout em 1366, 390 e 320 px, sem rolagem horizontal;
detalhes recolhidos; confirmação bloqueada com flag desligada; aplicação
individual mobile; lote desktop com duplo clique. Revalidação final do navegador
acrescenta lote mobile com duplo clique e seleciona explicitamente o botão
individual do produto. **Revalidação final: 4 testes aprovados em 12,115 s**, zero falhas e cluster encerrado.
Essa rodada testa a versão final dos testes de navegador após acrescentar mobile
e ajustar a seleção individual; a lógica de aplicação permaneceu a mesma da
suíte de 114 testes.

As imagens em `artifacts/ajustes/` são capturas do Django com dados fictícios.
A flag só é ligada por `override_settings` em testes no banco descartável;
a configuração do projeto permanece **INVENTARIO_AJUSTES_HABILITADOS = False**.

O executor restringe conexões ao host loopback, porta e diretório do cluster
recém-criado. Testes de migrations executam migrations existentes apenas nesse
banco descartável. Não foram executadas migrations em produção. Avisos existentes:
namespace `estoque` duplicado, diretório `staticfiles` não coletado no ambiente
local e eventual `Broken pipe` nas navegações automatizadas. Não são falhas da suíte.
Não se declara execução de todos os testes de todo o projeto.

## Finalização autorizada

Interface desktop/mobile aprovada pelo proprietário. Texto de confirmação
simplificado conforme solicitado, sem alterar os dados essenciais ou o fluxo.
A branch remota `backup/wip-pedidos-base-2026-06-04` foi consultada no GitHub e
apontava para `ee36165be2e5b66d3189d954cbe37bf15a25b6c6` antes da finalização.

Capturas, HTML de simulação e logs temporários permanecem apenas como artefatos
locais, excluídos do commit. Nenhum backup ou credencial integra a alteração.
Não há migrations novas; `sistema/settings.py` não foi modificado e mantém a
flag desabilitada. Não houve acesso ao estoque nem a documentos de produção.
A reforma geral de horários continua fora do escopo.

Commit e push sem force foram autorizados pelo proprietário. Deploy no Render,
migrations no Neon e ativação real permanecem sem autorização nesta etapa.


Validação final para commit: **115 testes aprovados em 57,423 s**, incluindo
os quatro testes Chrome desktop/mobile, após o ajuste do texto da confirmação.
Zero falhas; PostgreSQL descartável encerrado. Mantidos os avisos locais já
registrados acima. `git diff --cached --check` sem erros. Nenhuma migration nova
ou alteração de configuração integra o diff.
