# Disponibilidade e alertas de locações

Base: `f07eba0`. Worktree isolada: `.worktrees/disponibilidade-alertas-f07eba0`. Commit local autorizado; push e publicação ainda não autorizados.

## Correções

1. Disponibilidade pelo pico de ocupação simultânea de mesas e cadeiras dentro do intervalo solicitado. Considera horário de entrega e o novo horário opcional de devolução, jogos de 1 mesa/4 cadeiras e avulsos. Devolução no mesmo instante da nova entrega libera o intervalo. Sem horário de devolução, considera o fim do dia. Material na rua com previsão ainda futura pode compor o planejamento; quando a previsão vence sem recolhimento confirmado, continua bloqueado, inclusive para reservas futuras. Nenhuma previsão registra devolução física. O quadro mantém o material ainda na rua separado do estoque disponível agora.
2. Aba rosa quando existem entregas pendentes de hoje; vermelha com pulsação contínua a 60 minutos ou menos da entrega, inclusive após o horário. Relógio atualizado a cada segundo usando o fuso configurado no sistema. Alertas anteriores de outras pendências continuam valendo.
3. Moldura vermelha de 3 px nas entregas pendentes de hoje, retirada quando a tarefa sai do painel ou muda de dia. Entregas futuras e recolhimentos não recebem essa moldura. Checklists, WhatsApp, botões, filtros e contadores preservados.

O formulário consulta novamente a disponibilidade ao mudar datas, horários ou quantidades. Criação e edição validam no servidor dentro de transação, com bloqueio da configuração compartilhada para serializar reservas concorrentes no PostgreSQL. Conflitos informam quantidades disponíveis. O horário previsto de devolução também aparece no detalhe e no agendamento operacional do recolhimento.

## Validação

- **312 testes de locações aprovados**, incluindo Chrome desktop/mobile, criação/edição, futuras, reutilização, conflitos de horário, intervalos consecutivos, pico simultâneo, avulsos, recolhimento atrasado não confirmado, registros sem horário e datas inválidas.
- Cenário reproduzido em dados sintéticos: patrimônio de 75 mesas/303 cadeiras, 65 jogos na rua, disponibilidade momentânea de 10 mesas/43 cadeiras e reserva futura de 15 jogos permitida após devolução prevista no prazo.
- JavaScript real da aba executado no Chrome com relógio controlado: rosa, limite exato de 60 minutos, atraso, amanhã, retirada do cartão, preservação de outro alerta, virada do dia e fuso local. Teste de renderização verifica moldura só nas entregas de hoje e retirada após resolução administrativa.
- **148 testes adicionais de integração executados: 145 aprovados e 3 falhas preexistentes**. As três falhas foram reproduzidas individualmente em uma cópia limpa de `f07eba0`: `VendaEdicaoUnificadaTests.test_resumo_financeiro_vendas_mostra_pendencias_gerais_apos_venda_atual_a_vista`, `test_tela_vendas_clique_edicao_usa_nome_real_sem_badge_estoque` e `test_tela_vendas_modo_edicao_nao_marca_salva_no_finally_de_erro`. As verificações de compras, ações laterais, entrega e recolhimento passaram.
- `makemigrations --check --dry-run`: nenhuma alteração de modelo sem migration. `git diff --check`: aprovado.
- Todos os testes usaram SQLite temporário descartável com `offline.test_settings`; nenhum acesso ao Neon. O bloqueio concorrente específico do PostgreSQL foi implementado, mas não exercitado por conexões PostgreSQL concorrentes nesta validação.
- Avisos existentes: namespace `estoque` duplicado e ausência de `staticfiles` coletados na worktree de teste.

Logs locais: `locacoes-final.log`, `integration-tests.log`, `baseline-regression.log`.

## Arquivos alterados

- `locacoes/models.py`
- `locacoes/forms.py`
- `locacoes/views.py`
- `locacoes/services.py`
- `locacoes/templates/locacoes/nova.html`
- `locacoes/templates/locacoes/detalhe.html`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `locacoes/migrations/0016_locacao_horario_previsto_devolucao.py` (novo)
- `locacoes/tests_disponibilidade_periodo.py` (novo)
- `locacoes/tests_alerta_lateral.py`
- `locacoes/tests_resolucao_administrativa.py`
- `locacoes/tests.py`
- `RELATORIO-disponibilidade-alertas.md` (este relatório)

## Publicação posterior

A migration 0016 apenas adiciona um campo nullable; não altera contratos, pagamentos, quantidades de patrimônio ou registros existentes. Foi aplicada somente nos bancos descartáveis de teste. Sua aplicação em produção depende da autorização de publicação.

Não foram executados push, deploy ou migrations em produção. A pasta principal e a worktree da Conferência de Estoque não foram alteradas. As regularizações já realizadas no Neon não foram alteradas nem desfeitas.

Verificação anterior ao commit: consulta exclusivamente de leitura à tabela `django_migrations`, em transação PostgreSQL com `transaction_read_only=on`, confirmou que a única migration pendente no grafo deste código é `locacoes.0016_locacao_horario_previsto_devolucao`. As migrations de locações até a 0015 estão aplicadas. Não houve escrita no banco.

O commit ativo no Render ainda depende de confirmação por acesso ao serviço: o GitHub não retornou deployments, checks ou status de implantação para comprovar que `f07eba0` continua ativo. A integração Render foi localizada, mas não está conectada nesta sessão. Essa verificação permanece necessária antes da publicação.
