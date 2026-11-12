# Marco físico geral — implementação isolada

Base: exportação de `git archive c3a0237`, em `.worktrees/marco-zero-c3a0237`.
Não contém alterações locais da árvore principal. Sem commit, push, deploy,
migration, acesso ao Neon ou leitura/escrita de dados operacionais.

## Regra e registros

O OK continua preservando a contagem e autoria em `ConfirmacaoContagem`, sem
escrever estoque. A confirmação escolhida, compatível com a rodada ativa, é o
corte da revisão/aplicação. A cadeia entre confirmações anteriores deixa de ser
exigida; documentos e eventos antigos permanecem intactos.

Na aplicação, sob os bloqueios existentes:

- Resultado = contado_base + efeitos efetivos comprovados depois da sequência do OK.
- Ajuste = resultado − estoque operacional imediatamente anterior à aplicação.
- Diferença original = contado_base − esperado no OK, preservada separadamente.

Cada escrita do observador entra uma vez. Atribuição imutável com unidade e
conversão congeladas comprova entradas, saídas, devoluções e ajustes manuais;
o operando absoluto de ajuste manual não é confundido com sua diferença.
Compras/vendas antigas sem atribuição usam a comprovação documental existente.
O ajuste da própria conferência não é somado novamente à sua projeção física.
Movimento posterior desconhecido, lacuna ou unidade incompatível não vira zero.

A regra é identificada por `marco_fisico_v1` nas evidências. Aplicações antigas
mantêm a interpretação anterior, sem reescrita. Um OK anterior não pode aplicar
por cima de um marco posterior aplicado para o mesmo produto. Os bloqueios por
produto serializam também conferências distintas.

Quando não há ajuste necessário, a proposta passa a aplicada e o recibo fica
em `EventoAjuste` imutável (`marco_sem_ajuste`) e `EventoInventario`, dentro da
mesma transação. Não são criados `MovimentoObservado`, movimentação manual ou
entrada/saída de quantidade zero. O serviço compartilhado de recibos normaliza
esse caso para consulta, reenvio, encerramento e apresentação. Não há novo saldo
persistido paralelo a `Produto.quantidade`.

## Arquivos alterados

Todos os caminhos abaixo são relativos à cópia isolada:

- `inventario/marco_fisico.py`: corte, comprovação, versão da regra e recibo sem ajuste.
- `inventario/ajustes.py`: cálculo, aplicação individual/lote, reenvio e projeção do resultado.
- `inventario/contexto_ajustes.py`: movimentos carregados a partir do OK escolhido.
- `inventario/services.py`: revisão compartilhada e proteção da contagem já aplicada.
- `inventario/conciliacao_documental.py`: encaminhamento para a comprovação do novo marco.
- `inventario/revisao_historico.py`: validação da versão nova e manutenção da versão antiga.
- `inventario/confirmacao_ajustes.py`: recibos HTTP para aplicações com e sem ajuste.
- `inventario/encerramento.py`: reconhecimento do marco sem movimento.
- `inventario/apresentacao.py`: equivalência pelo fator registrado, sem inferência pelo nome.
- `inventario/views.py`, `inventario/ajustes_views.py`, `inventario/historico.py`: consulta dos marcos e seus números.
- Templates em `inventario/templates/inventario/`: `ajustes.html`, `detalhe.html`,
  `detalhes_tabela.html`, `historico_conferencia.html`, `linha_tabela.html`.
- `inventario/tests_marco_fisico.py`: cenários direcionados com dados fictícios.
- Este documento.

Nenhum modelo, migration, CSS ou serviço de compra, venda ou financeiro foi alterado.
As sete colunas permanecem; informações adicionais ficam nos detalhes. Saldos sem
marco aplicado são identificados como estimados. Números registrados não dependem
de um alerta desaparecer para serem exibidos.

## Verificação

Runner: `inventario.validar_confiabilidade_postgresql`, com host, porta, usuário,
banco e diretório temporário verificados. PostgreSQL descartável, sem migrations.

- Rodada principal final: **17 testes aprovados em 46,983 s**, incluindo os seis
  cenários solicitados, devolução, unidade avulsa, leitura das duas versões de
  auditoria, reenvio, rollback e aplicações concorrentes com e sem ajuste.
- Após os últimos ajustes de interface: **2 testes aprovados em 1,274 s**,
  cobrindo recibo/histórico/interface de zero e rollback/reenvio de lote misto.
  Um desses testes repete um cenário da rodada anterior: **18 testes distintos**.
- Chrome headless: HTML real com dados fictícios e CSS local em 1366, 390 e
  320 px; sete colunas, detalhes recolhidos e ausência de transbordamento horizontal.
  Capturas em `diagnostics/marco-fisico/compacto-*.png`. Essa verificação é visual
  estática; os serviços e consultas HTTP foram exercitados pelos testes funcionais.
- Parsing AST dos arquivos Python alterados aprovado. Comparação dos arquivos
  com o arquivo original da exportação usada para identificar o escopo.
- Clusters das rodadas finais encerrados: `inventario-confiabilidade-pg-llf17ie2`
  e `inventario-confiabilidade-pg-wldfeyrg`. Logs e `validation.json` ficam no
  diretório temporário de cada cluster. Avisos: namespace `estoque` duplicado e
  ausência de `staticfiles` coletados na cópia; o teste visual carrega o CSS fonte.

## Limitações preservadas

- A execução real continua condicionada à configuração e às permissões existentes;
  nenhuma configuração foi ativada.
- Movimentações posteriores sem efeito/unidade comprováveis exigem resolução ou
  nova contagem. O histórico anterior não é usado para exigir essa resolução.
- Uma movimentação depois da prévia/aprovação invalida a assinatura antiga: o
  responsável precisa revisar a nova prévia. A transação recusa a aplicação antiga
  sem sobrescrever a movimentação; a nova prévia incorpora seu efeito.
- Mantida a precisão operacional de três casas e a preservação da contagem física
  original, projeção e resíduos. Não houve mudança global de representação.
- Sem validação em produção, auditoria geral ou testes de módulos não relacionados.
