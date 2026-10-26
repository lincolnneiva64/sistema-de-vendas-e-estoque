# Resolução das faltas pela correção da nota — etapa 3 local

## Resultado

Salvar a venda reconhece a quantidade já separada quando o mesmo item/produto
e unidade permanecem compatíveis. Não depende de clicar em Aplicar. A resolução
fica registrada na mesma transação de gravação da venda, em EventoVenda, sem
atualizar status, quantidades, observações ou timestamps dos itens históricos
da separação. Solicitação original e quantidade separada continuam disponíveis.

O reconhecimento é por item: correção parcial resolve somente a ocorrência
comprovada. Quando todas as ocorrências e alterações estiverem atendidas, a
situação operacional passa a Separada e a nota pode ficar pronta para envio.
Uma alteração de outro item, um acréscimo ou uma conferência física não atendida
mantém a pendência e bloqueia a liberação. GET não registra resolução.

## Regras e auditoria

- O fluxo normal de gravação captura a identidade anterior e os IDs dos eventos
  de quantidade produzidos por cada item, sob os bloqueios existentes.
- Quantidade da nota deve ser exatamente igual à separada, positiva, válida
  para unidade/conversão e inferior à solicitação original. Não há novo
  arredondamento: comparação usa Decimal e a precisão existente do sistema.
- Troca de produto/unidade, observação manual e identidade ambígua não são
  atendidas automaticamente. Os snapshots legados não têm ID original do
  produto; por isso também se verificam registros posteriores de troca/remoção
  depois da conferência, inclusive produtos homônimos. Uma resolução já
  comprovada por ID permanece válida se apenas o nome do cadastro mudar.
- Tipo de evento novo: `separacao_quantidade_ajustada`. A descrição guarda dados
  estruturados com separação, item da nota, produto, unidade, solicitado,
  separado, quantidade da nota e IDs específicos de eventos atendidos.
- O histórico da nota e a revisão de edição exibem descrição legível desse
  evento. O conteúdo estruturado não é apresentado como JSON ao funcionário.
- Reenvio sem alteração não duplica a resolução. Rollback da venda desfaz
  também o registro. Os eventos originais não são excluídos nem reescritos.
- Aumento posterior da nota ou mudança de identidade invalida a compatibilidade
  da ocorrência para a apresentação atual. O atendimento antigo permanece no
  histórico, e o evento realmente novo continua pendente.
- Correção já salva por versão anterior pode ser reconhecida no próximo
  salvamento. Eventos antigos de redução são associados somente quando nome e
  unidade identificam um único item e as quantidades são interpretáveis. Não
  há backfill, migration, escrita por GET ou tratamento especial para nota real.

Uma confirmação genérica de atendimento não comprova complementação física.
Esta entrega não presume material adicional separado nem exibe “Separação
complementada” sem prova física explícita. As rotas de consulta da etapa 2 e
os controles de quantidade insuficiente da etapa 1 continuam preservados.

## Apresentação e contadores

Falta ativa mostra “Quantidade insuficiente — falta 1”. Situações incompatíveis
mostram “Necessita conferência”, sem subtrair quantidades de unidades diferentes.
Quando resolvida, mostra “Quantidade ajustada na venda — resolvido”. A fila
mantém o histórico dos resolvidos em uma seção expansível discreta e recolhida
por padrão, separada das ocorrências ativas.

Contadores: A conferir, Problemas ativos e Resolvidos. Problemas ativos incluem
faltas e revisões adicionais que ainda exigem conferência; revisões do mesmo
produto/unidade já representadas por uma falta não são contadas duas vezes.
O contador de OK foi identificado como “OK originais” para não transformar
uma falta histórica em um OK físico que nunca ocorreu.

Sugestão Aplicar deixa de ser oferecida para uma ocorrência comprovadamente
resolvida. Quantidade já igual à separada, mas ainda sem resolução registrada,
orienta salvar a venda para validar o ajuste. Aplicar continua sendo apenas
o atalho da tela: as regras comerciais, estoque e financeiro permanecem no
serviço normal de gravação da nota.

## Arquivos

- `estoque/services/pendencias_separacao.py`: leitura e registro das resoluções.
- `estoque/services/vendas.py`: identidade anterior e eventos do salvamento.
- `estoque/services/separacao_vendas.py`: projeção operacional, atendimento
  específico e interpretação decimal de eventos antigos de quantidade.
- `estoque/views.py`: contexto comum, sugestões, contadores e situação da fila.
- `estoque/models.py`: somente propriedade Python de descrição de evento;
  nenhum campo ou estrutura de banco alterado.
- Templates `separacao_vendas_fila.html`, `separacao_venda_detalhe.html`,
  `venda_detalhe.html` e `venda_editar_revisao.html`: apresentação e histórico.
- `estoque/tests_separacao_resolucoes.py`: cenários novos.
- `estoque/tests_separacao_insuficiente.py`: expectativa do novo texto da falta
  na fila, preservando a matriz da etapa 1.

## Validação final

44 testes aprovados em SQLite temporário privado, em 24,456 s: 14 testes novos
(13 de servidor e uma matriz de navegador), mais os 30 das etapas anteriores.
Os mesmos 13 testes novos de servidor passaram também em PostgreSQL
descartável em 3,126 s. O cluster foi encerrado ao final. Não são 57 testes
distintos: há repetição dos testes de servidor entre os dois bancos.

Cobertura: salvamento direto e com contexto de ajuste; duas faltas resolvidas;
correção parcial; reenvio; aumento posterior; produto homônimo trocado no mesmo
salvamento ou anteriormente; observação de outro evento físico não atendido;
correção legada; rollback; fração legítima e troca de unidade; atendimento
genérico sem comprovação de complemento; renomeação do mesmo produto; GET sem
gravação; leitura cacheada sem consultas individuais por produto; saldos de
estoque e conta a receber; snapshots integralmente preservados.

Edge/Chromium em 1440, 390 e 320 px: nove cenários da fila ativa/parcial/resolvida,
mais as matrizes das etapas 1 e 2. Sem overflow horizontal. Galeria fictícia em
`amostras/separacao-resolucoes/index.html`, com nove capturas. Celular emulado,
sem aparelho físico; não foi executada a suíte completa da aplicação.

Os runners locais usam configurações isoladas sem .env e criam tabelas
diretamente dos models, com executor de migrations bloqueado. PostgreSQL com
guardas de host, porta e diretório do cluster; nenhuma conexão operacional.
Runners: `amostras/testar_separacao_resolucoes.py` e
`amostras/validar_resolucoes_pg.py`. Prévia, runners e logs são artefatos locais.

Não houve acesso à venda real #1031, produção, migrations, commit, push ou
deploy. Trabalho local não relacionado preservado. `git diff --check` aprovado.

## Publicação aprovada

Revisão integral do diff confirmou os dez critérios solicitados: reconhecimento
automático ao salvar, faltas restantes ativas, correção parcial independente,
contadores consistentes, snapshots preservados, auditoria por item sem duplicar
reenvios, novas alterações sujeitas à conferência, apresentação coerente entre
fila/checklist/edição, regressões das etapas 1 e 2 cobertas e uso das regras
normais de estoque e financeiro, sem movimentação adicional pela resolução.

Conferidos os logs finais: 44 testes aprovados em SQLite e repetição dos 13
testes novos de servidor em PostgreSQL descartável, encerrado ao final.
Nenhuma nova execução foi necessária. Mantidas as limitações descritas acima.

Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, somente dos 12 arquivos de implementação,
testes e deste relatório. Prévia, runners, logs, temporários e bancos ficam
fora do commit; trabalhos locais não relacionados preservados.

Sem workflows de deploy versionados ou hooks locais ativos. Mensagem com
`[skip render]`, cujo bloqueio de auto-deploy foi confirmado na documentação
oficial: https://render.com/docs/deploys#skipping-an-auto-deploy.
Não houve acesso à produção, execução de migrations ou comando de deploy.
