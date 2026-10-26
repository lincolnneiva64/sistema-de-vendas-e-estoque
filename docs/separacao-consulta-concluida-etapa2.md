# Checklist concluído em consulta — etapa 2

## Comportamento

Checklist anteriormente concluído abre em consulta, com itens recolhidos,
data de conclusão, conferente registrado e quantidades/resultados históricos.
Registros legados sem data/conferente mostram informação não registrada,
sem inventar dados. São considerados os marcos existentes de conclusão e
revisão, além do estado Separada. Faltas anteriormente confirmadas permanecem
com pendência física; concluir conferência não as transforma em estoque separado.

Não há controles para alterar quantidade/status nem botão de concluir novamente.
Voltar para fila permanece visível. As rotas de gravação também protegem a
conclusão: tentativa de alterar item concluído recebe 409; POST do formulário
concluído volta à consulta sem reprocessar. Há nova conferência da condição
depois do lock, preservando o histórico em requisições concorrentes.

O GET do detalhe não chama sincronização nem marca revisão: calcula somente
informações para exibição. Não muda estado físico, datas, quantidades ou
snapshots e não registra atendimento. Checklists nunca concluídos continuam
editáveis; sincronização existente nas operações de escrita foi preservada.

Fila e detalhe compartilham a leitura da pendência de revisão: indicador já
persistido ou eventos físicos ainda não atendidos, posteriores ao marco de
conclusão/revisão e ao último atendimento. Divergência histórica isolada não
recria pendência. Evento novo continua gerando aviso, mesmo se a descrição
antiga não puder ser interpretada para montar o detalhamento numérico.
Consulta não limpa pendências antigas nem as considera atendidas.

A fila mostra alterações posteriores como ALTERACAO APOS SEPARACAO, com
aviso genérico quando não houver detalhamento legível. O atendimento explícito
existente foi preservado. O fluxo de edição da venda e sua lógica de atendimento
não foram alterados nesta etapa; tampouco o botão Aplicar.

Primeiro toque/clique/acionamento de teclado no produto expande; segundo no
mesmo produto recolhe. Isso vale para consulta e edição, sem gravação. Nenhum
produto abre automaticamente ao entrar. ARIA acompanha a expansão. As ações
de confirmação e avanço durante o trabalho continuam existentes.

## Implementação

- `estoque/views.py`: modo de consulta, proteção de gravação, GET sem mutação
  e critério de aviso compartilhado com a fila.
- `estoque/services/separacao_vendas.py`: permite reutilizar os mesmos eventos
  na consolidação, sem consultar duas vezes e sem mudar o comportamento dos
  chamadores existentes que não fornecem a lista.
- `estoque/templates/estoque/separacao_venda_detalhe.html`: histórico em consulta,
  expansão/recolhimento e controles de edição preservados no modo editável.
- `estoque/templates/estoque/separacao_vendas_fila.html`: aviso genérico para
  alteração posterior sem detalhamento interpretável.
- `estoque/tests_separacao_consulta.py`: testes da etapa 2.
- `estoque/tests_separacao_insuficiente.py`: acompanha a entrada recolhida e
  a consulta histórica após recarga, preservando todas as verificações da etapa 1.

## Verificação

30 testes aprovados em 16,486 s na rodada final:

- Oito novos: sete testes de servidor e uma matriz de navegador.
- Os 17 testes da etapa 1, com a adaptação de apresentação descrita acima.
- Cinco regressões existentes de snapshot original, atendimento explícito,
  divergência histórica atendida, marco da última revisão e KG reconciliado.

Servidor: concluído sem divergência; alteração posterior; alteração já atendida;
novo evento após atendimento; revisão/falta preservadas; POST direto bloqueado;
GET de checklist aberto desatualizado também sem mutação. Comparação integral
de separações, seus itens, itens da venda e eventos; zero INSERT/UPDATE/DELETE
na abertura do detalhe monitorada por CaptureQueriesContext.

Edge/Chromium em 1440, 390 e 320 px: seis cenários de consulta/edição com um
produto OK e outro pendente, expansão/recolhimento por mouse/toque, nenhuma
gravação de checklist por essas interações, saída para fila sem reprocessamento,
sem overflow horizontal. A matriz da etapa 1 cobre nove fluxos de UN, CX e MIL,
passos, rascunho, confirmação por botão/Enter e persistência. Consulta final
mostra quantidades salvas e não contém controles de edição.

Somente SQLite temporário privado, dados fictícios, configuração isolada sem
.env, schema criado diretamente dos models e executor de migrations bloqueado.
Servidor de teste em loopback e perfil temporário de navegador. Runner local:
`venv/Scripts/python.exe -B amostras/testar_separacao_consulta.py`.
Capturas e galeria em `amostras/separacao-consulta/` e log local em
`amostras/consulta-testes.local.log`. Nenhum banco operacional foi utilizado.

Limitações: celular emulado, sem aparelho físico; a suíte completa da aplicação
não foi executada. O parser preexistente de descrições antigas de quantidade
pode não produzir detalhes numéricos; essa pendência fica registrada para uma
revisão específica. O aviso genérico desta etapa impede ocultar a necessidade
de conferência, sem reescrever eventos ou mexer na edição da venda.

`git diff --check` aprovado. Trabalho local não relacionado preservado. Validação
sem migrations, acesso à produção ou deploy.

## Publicação aprovada

Implementação e prévia aprovadas pelo responsável. Revisão final confirmou os
oito pontos solicitados e o resultado da rodada final de 30 testes aprovados.
Código e testes não mudaram desde essa execução; nenhuma verificação adicional
foi necessária. Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, com `[skip render]`, somente dos arquivos
de implementação, testes e deste relatório. Prévias, logs, temporários e bancos
de teste permanecem fora do commit. Nenhum deploy autorizado.
