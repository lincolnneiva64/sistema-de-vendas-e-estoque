# Conferência de estoque — Etapa 2

Base: `b887962c0c639595af777b8e8777ec1a245c9917`.
Worktree isolada: `.worktrees/conferencia-etapa2-b887962` (HEAD destacado).

## Implementação

Na investigação do produto, uma seção recolhida leva à consulta gerencial por
rodada. A consulta é GET, privada, sem cache e usa a mesma autorização do painel.
Não adiciona consultas comerciais ao carregamento do painel da Etapa 1.

Seleciona a última entrada com `estoque_entrada_realizada=True` e recebimento
datado até a referência. Permite selecionar entradas anteriores e consultar até
a próxima compra ou até a referência da conferência. A referência é o OK da
rodada; sem OK, finalização ou criação, explicitamente sem contagem confirmada.
Não usa o relógio atual para deslocar a referência a cada visita.

Exibe fornecedor, datas comercial e de recebimento, referência interna e
quantidades atuais agrupadas pela unidade documental, sem conversão. A seleção
de compras tem 30 itens por página; vendas, itens removidos e movimentos têm 50.
As páginas preservam filtros. Vendas incluem link à consulta comercial existente,
responsável, quantidade, unidade, consumo próprio e estado atual de cancelamento.
Os relacionamentos exibidos são carregados com `select_related`.

Movimentos observados mostram sequência, origem, referência e números brutos
antes/depois. Incluem estornos, consumo, ajustes e origens não identificadas.
Documentos e movimentos são fontes separadas; não são somados duas vezes.
Compras aparecem em verde, vendas em azul, faltas em vermelho e pendências em
amarelo, sempre acompanhadas de texto. As seções começam recolhidas.

## Limitações comprovadas nas fontes

- `MovimentoObservado` não possui unidade/fator imutável. Não se pode certificar
  unidade histórica nem converter todos os movimentos com o cadastro atual.
- `ItemCompra` não possui estoque antes/depois, e documentos podem ser editados.
  Somar quantidades atuais não reconstrói com segurança a entrada original.
- Datas de vendas são comerciais, sem horário do evento. Os documentos dos dias
  de fronteira são candidatos e não comprovam participação no intervalo exato.
- Cancelamento, devolução e reversão apresentados nos documentos são estados
  atuais. Não se presume que esse estado já existia no momento da conferência.
- Itens removidos usam a data de criação do registro para o recorte, não a data
  comercial da venda; a data de devolução é mostrada separadamente.
- Compras sem marcação e data efetiva de entrada não são tratadas como recebidas.
  A referência fiscal não é um campo estruturado disponível na compra consultada.
- Ausência de movimentos observados não comprova ausência de atividade anterior.

Por isso, totais físicos de entradas/saídas, saldo reconstruído e estoque antes/
depois da compra ficam **Não disponível** nesta implementação. Não existe um
caminho que habilite esses cálculos por suposição. O esperado e a contagem do OK
são fatos registrados; a diferença é identificada como cálculo sobre esses fatos.
Mesmo no intervalo antigo, os valores do OK continuam identificados como do OK.
Nenhum cálculo depende de custo disponível.

## Integridade e validação

Nenhuma alteração de modelos ou migration. Nenhum serviço de escrita comercial,
aprovação de ajustes ou renovação de links foi modificado. Não houve acesso ao
banco operacional nem alteração da conferência real nº 1. Ajustes permanecem na
configuração anterior, desabilitados na base e no ambiente de teste.

Validação pelo runner existente `inventario.validar_confiabilidade_postgresql`,
com `inventario.tests_historico inventario.tests_painel`, usando o Python do
ambiente virtual do projeto. O runner cria um cluster local exclusivo, valida sua
identidade, cria apenas tabelas fictícias sem executar migrations, e encerra o
servidor ao final. Testes cobrem permissões, isolamento de rodada, GET sem mudança
de estoque, múltiplas compras, rascunhos, fronteiras temporais, cancelamentos,
devoluções, consumo, ajustes, origem desconhecida, saldos nulos, unidades distintas,
custo ausente, paginação e número constante de consultas com 101 itens de venda.
Navegador Chrome: desktop 1366 px, mobile 390/320 px e regressão visual da Etapa 1.

Validação anterior: **24 testes aprovados**, cluster `inventario-confiabilidade-pg-pcvve59a`
encerrado, sem migrations. `git diff --check` aprovado. Avisos preexistentes de
namespace `estoque` duplicado e pasta `staticfiles` ausente no ambiente de testes.
Capturas da regressão da Etapa 1 ficaram em `artifacts/painel/` nesta worktree.

### Correção da seleção ao paginar compras

A compra usada na consulta permanece como uma opção explicitamente selecionada
no início do seletor, mesmo fora da página atual de compras. A lista paginada
omite essa mesma compra para não duplicar a opção. A seleção e o histórico usam
o mesmo objeto autorizado e filtrado pela referência, sem consultas adicionais.

O teste de regressão primeiro reproduziu a falha na versão anterior: o navegador
selecionava a compra #31 enquanto o histórico consultava a #1. Após a correção,
o teste verifica 32 compras, ida e volta entre páginas, seleção de uma compra
antiga, submissão sem mudança e retorno à seleção original. A regressão também
foi executada no Chrome em desktop (1366 px) e mobile (390 px).

Resultado final da versão corrigida: **26 testes aprovados** (os 24 anteriores e
duas novas regressões), pelo mesmo runner e comando. Cluster exclusivo
`inventario-confiabilidade-pg-13mbvf6u` encerrado, sem migrations. Nenhuma mudança
de configuração, de dados operacionais ou de permissão foi necessária.

## Arquivos

- `inventario/historico.py`: consultas de investigação.
- `inventario/views.py`, `inventario/urls.py`: endpoint autorizado somente leitura.
- `inventario/templates/inventario/detalhe.html`: acesso recolhido no painel.
- `inventario/templates/inventario/historico.html`: interface de investigação.
- `inventario/templates/inventario/historico_paginacao.html`: navegação paginada.
- `static/inventario/historico.css`: apresentação responsiva e cores semânticas.
- `inventario/tests_historico.py`: testes de banco, autorização e navegador.
- Este documento.

Commit e push autorizados pelo usuário após correção, revisão e testes.
Capturas em `artifacts/` são locais e ficam fora do commit. Deploy não autorizado;
nenhuma ação de deploy ou mudança do Auto-Deploy do Render faz parte desta etapa.
