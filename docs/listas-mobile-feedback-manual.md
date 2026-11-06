# Listas mobile: feedback e inclusão manual — 06/11/2026

## Ajuste posterior: resumo de selecionados e ações no fim da lista

- Causa dos zeros: o contador `data-itens-lista-mobile-total` usava apenas `data-na-lista=1` (Peguei), e o resumo final compartilhava `data-total-lista-pedido` com o total confirmado. Inclusões manuais começam pendentes; por isso não apareciam nesses indicadores. Não havia filtro de fornecedor nesses cálculos.
- O resumo mobile agora mostra `Produtos selecionados` e conta confirmados mais pendentes ativos, excluindo removidos. O total final tem marcador próprio e reaproveita a soma em centavos dos pendentes e confirmados já calculada, com a precisão e arredondamento existentes. A seção Já peguei conserva o total confirmado. Não há nova fonte de dados nem alteração do payload/regras de geração.
- Reutilizado o bloco de botões `btnGravarListaMobile`/`btnGravarGerarCompraMobile`, que já existia mas estava oculto na criação mobile. O bloco aparece depois da seção confirmada, no fim da página, usando as classes, handlers, formulário e bloqueios compartilhados existentes. No desktop volta à posição original e permanece oculto. Botões superiores e inclusão manual preservados.
- Testes novos em `estoque/tests_lista_resumo_rodape_mobile.py`: dois produtos sem vínculo, terceira inclusão, exclusão, edição de quantidade/preço, Peguei, salvar pelo rodapé e reabrir; lista longa com mais de 20 produtos, rolagem e comparação exata do payload dos botões superiores/inferiores em salvar e gerar compra, bloqueio de todos os botões durante envio e repetição sintética recusada, um único salvamento/compra por ação; rodapé oculto e total preservado no desktop.
- Dados fictícios e SQLite descartável, configuração apenas em memória e migrations desabilitadas. Sem Neon/Render, dados reais, migrations, commit, push ou deploy. Não foi usado aparelho físico nem feita nova avaliação de concorrência entre processos.
- Regressões antigas atualizadas: `tests_compra_mobile_total_browser.py` distingue selecionados de confirmados e declara explicitamente o estado pego na fixture que pretende editar confirmados, sem fazer listas legadas se confirmarem automaticamente. `tests_lista_confirmados_rodape.py` considera a ordem inicial mobile já aprovada, restringe as ações do rodapé oculto aos tamanhos mobile e aguarda o handler assíncrono de resize antes de medir posições. Desktop continua validado pelos testes da tabela, rodapé oculto, retorno ao DOM original e comparação com o template publicado.
- Resultado final por teste: 27/27 distintos aprovados nas últimas execuções aplicáveis, em rodadas separadas. A rodada de 27 testes teve 19 aprovados e falhas nas expectativas/fixtures antigas; após os ajustes, a reexecução de 9 teve 8 aprovados e uma medição antecipada de resize; a reexecução final dos 2 testes de rodapé passou em 11,241 s após aguardar o evento. Os 3 testes novos passaram na rodada de 27, incluindo os quatro caminhos da lista longa. Também passaram 12 testes existentes de coleta na rodada inicial, incluindo comparação desktop com template publicado, arredondamento e salvar pendentes. `git diff --check` sem erros; aviso preexistente de namespace duplicado e Broken pipe nas navegações.

## Ajuste posterior: nomes dos controles de inclusão manual

- O botão rosa-claro não havia sido removido nem ocultado: `mostrarBuscaInicialMobile()` abre a área automaticamente na lista nova, e `expandir(false)` mudava seu texto para `Cancelar`. Isso fazia desaparecer a identificação da ação de escolher produto.
- Reutilizados os três elementos existentes: botão `+ Escolher produto`, placeholder e nome acessível do campo `Pesquisar produto pelo nome` e botão `Adicionar à lista`. No mobile, o botão rosa mantém seu nome aberto/recolhido e, ao tocar, apresenta a área e dá foco à pesquisa mesmo quando já aberta. Após incluir, a área continua recolhendo e o foco retorna ao mesmo botão para escolher o próximo produto. No desktop, o botão conserva o comportamento de abrir/recolher e o texto `Cancelar` enquanto aberto.
- Sem alteração de CSS, cores, tamanhos, ordem, controles do painel, catálogo, cálculos, períodos ou geração automática. IDs e componentes preservados, sem controles ou listeners novos.
- Novo teste Chrome mobile a 400 px verifica nomes, visibilidade e unicidade do botão, primeira abertura, primeiro toque, sugestões e indicadores, quantidade zero recusada, duas inclusões (Skol vinculada e Amstel sem vínculo), ausência de gravações/vínculos e de consultas adicionais, recarga e ausência de overflow. Usa fixtures fictícias, SQLite descartável e migrations desabilitadas em memória, sem bancos externos ou configuração operacional. Sem teste em aparelho físico.
- Resultado: 17/17 testes em 54,361 s (`SelecaoManualMobileTests` e `tests_pesquisa_manual_catalogo`), incluindo regressões de resposta lenta/atrasada, foco, salvar/reabrir e catálogo completo. `git diff --check` sem erros. Permanecem avisos preexistentes de namespace repetido e Broken pipe de navegação. Sem migrations, commit, push ou deploy.

## Ajuste posterior: primeiro toque na pesquisa manual

- Reproduzida em Chrome a 400 px a perda da abertura: primeiro toque com catálogo em andamento, campo vazio e perda de foco sem clique externo; ao entregar a resposta, a lista ficava fechada. Com catálogo já pronto e foco normal, o primeiro toque abriu antes da correção. O teste não comprova que toda ocorrência relatada no aparelho tem a mesma sequência de foco.
- Os handlers de foco/clique retornavam na renderização durante `aria-busy`. A consulta fechava a lista e a resposta condicionava a abertura ao foco atual ou a texto digitado, sem registrar o pedido de abertura feito antes da resposta. Agora o pedido permanece durante a atualização e é encerrado pela seleção, Escape ou clique externo. A resposta não depende de `document.activeElement === busca`.
- Consolidada a tentativa de carregar o catálogo nos handlers já existentes de foco/clique. Nenhum listener de pointerdown adicionado: a instrumentação do toque real registrou pointerdown, blur do período, focus da pesquisa e click. Digitar e retornar ao campo reutilizam a renderização existente.
- Selecionar produto já incluído também termina com sugestões fechadas após devolver o foco à pesquisa. O botão de inclusão valida quantidade maior que zero antes de inserir: essa verificação estava ausente no cliente; produto válido e controle de duplicidade continuam exigidos.
- Teste novo reutiliza Chrome e fixtures existentes: primeiro/segundo toque, retorno após clique externo, digitação, seleção e indicadores, quantidade zero, inclusão válida, recarga, inclusão sem produto selecionado e resposta retida com perda de foco. Também verifica que toques/digitação não provocam novas consultas com catálogo carregado.
- Banco e configuração de teste descartáveis, dados fictícios, migrations desabilitadas em memória, sem configuração operacional ou acesso a Neon/Render. Nenhum commit, push ou deploy. Validação em viewport emulada, sem aparelho físico.
- Resultado: 16/16 em 52,065 s (`SelecaoManualMobileTests` e `tests_pesquisa_manual_catalogo`). O teste novo falhou antes do ajuste e passou depois. Catálogo de 1.004 produtos: 12 consultas, 0,167 s; sem consultas adicionais durante toques/digitação com catálogo pronto. Permanecem o aviso preexistente de namespace repetido e mensagens de Broken pipe nas navegações. `git diff --check` sem erros.

## Ajuste posterior: pesquisa manual em todo o catálogo

- Removido exclusivamente o filtro por vínculos na resposta `consulta_manual=1`. O catálogo mantém os filtros existentes `ativo=True` e `excluido=False`, os cálculos em lote e a ordenação sem duplicar produtos. A geração automática continua percorrendo somente os vínculos ativos do fornecedor. A consulta não grava vínculos nem altera fornecedor habitual.
- Atualizada a mensagem de catálogo vazio, sem alterações de layout, foco ou controle assíncrono.
- Validação desta correção: 13 testes existentes de seleção manual aprovados em Chrome real (incluindo buscas `ams`/`sko` com dois fornecedores, inclusão de Amstel sem vínculo, duplicidade e salvar/reabrir); 11 testes de `ColetaListaTests` aprovados; 2 testes novos de catálogo aprovados após corrigir os dados fictícios e expectativas. As rodadas não foram uma execução conjunta final.
- Ambiente desta validação: `sistema.settings_test_compras`, incluindo `inventario` e middleware comercial apenas em memória, SQLite temporário, todos os módulos de migrations desabilitados; tabelas criadas diretamente a partir dos modelos. Nenhuma migration executada ou criada. Nenhum acesso a Neon/Render, banco operacional, commit, push ou deploy.
- Desempenho sintético: 4 e 1.004 produtos exigiram 12 consultas em ambos os casos; medição final do catálogo maior: 0,168 s e 448.696 bytes de JSON. O volume transferido cresce com o catálogo; não foi medida carga de produção ou aparelho físico.
- Limitações do ambiente: a primeira tentativa de navegador falhou na conexão local; a execução fora do ambiente restrito aprovou os testes de navegador. Aviso preexistente de namespace `estoque` duplicado e mensagens de Broken pipe em navegação.


## Causas e correções

- O commit `6440275` inicializa a coleta antes de repetir a ação original. Durante essa espera, o botão acionado não mudava de texto. O controle existente agora recebe esse botão, mostra `Salvando...` e restaura seu rótulo no `finally`. Peguei, devolução e exclusão reutilizam o mesmo controle.
- Respostas JSON de rejeição 400/403/422 permitem tentar novamente. Conflito 409, resposta perdida ou sucesso sem versão continuam exigindo reabrir a lista: repetir uma escrita sem saber seu resultado seria inseguro. Locks, versões, CSRF e idempotência existentes no servidor foram preservados.
- A consulta de listas já tinha feedback, mas não impedia nova navegação/envio. Os handlers existentes agora impedem a repetição da mesma ação e restauram o estado no `pageshow`, inclusive ao voltar pelo navegador.
- O catálogo manual e os indicadores eram preparados apenas pelo GET que também gerava sugestões. A mesma view oferece uma consulta JSON somente de leitura, com os mesmos cálculos, histórico e políticas de produtos ativos/vínculos. No celular, a seleção do fornecedor e alterações do período atualizam o catálogo existente sem inserir cartões.
- O salvamento de listas novas exigia produtos marcados como Peguei e filtrava os pendentes. Produtos incluídos manualmente agora podem ser salvos pendentes. Gerar compra mantém a seleção de confirmados existente.
- A criação mobile também marcava todos os itens como pegos. O payload agora informa quais inclusões manuais continuam pendentes; o servidor aplica esse estado somente aos produtos válidos da própria lista, preservando os demais como confirmados.

## Arquivos de implementação

- `estoque/views.py`
- `estoque/templates/estoque/compras_sugestao_fornecedor.html`
- `estoque/templates/estoque/includes/lista_coleta_mobile.html`
- `estoque/templates/estoque/compras_listas_fornecedor.html`

Os testes novos reutilizam `estoque/tests_lista_selecao_manual_mobile.py` e `estoque/tests_coleta_operacoes_mobile.py`. `estoque/tests_consulta_listas_mobile.py` passou a comparar com HEAD e exigir altura igual: a expectativa antiga era reduzir os cartões em relação a `9b117de`, incompatível com preservar o layout aprovado nesta tarefa. Não há novo componente, migration, dependência ou infraestrutura de testes.

## Ambiente e limites da validação

Runner existente `validar_compra_timeout_pg.py`: cluster PostgreSQL novo em loopback, identidade verificada e conexões externas recusadas, banco e usuários fictícios, encerramento ao final. Migrations existentes aplicadas somente nesse banco descartável. Nenhuma configuração operacional ou `.env` importada.

Middleware comercial habilitado apenas em memória, pois o settings isolado original o omite. Nenhuma configuração persistente foi alterada.

Navegadores Chrome/Edge reais, perfis temporários, viewport mobile e toque emulado na coleta. Não houve teste em aparelho físico. Capturas geradas pelas suítes existentes são artefatos locais e não devem ser publicadas.

O ambiente restrito inicialmente impediu PostgreSQL e navegador; a execução local autorizada fora dessa restrição permitiu os testes. SQLite não foi usado como evidência de concorrência. Persistem os avisos preexistentes de namespace `estoque` repetido e mensagens de Broken pipe ao navegar.

Sem acesso a Neon/Render, dados reais, commit, push ou deploy. Alterações preexistentes em locações e arquivos locais preservados.

## Resultados finais

- Rodada ampla: 37/38 em 119,916 s. A única falha era a expectativa antiga de reduzir a altura dos cartões; a comparação desktop contra HEAD já havia passado.
- Após preservar pendentes na criação, reexecução dos testes afetados: 15/15 em 11,187 s. Inclui inclusão sem gerar, produto com estoque alto fora das sugestões, vendas no período, troca de datas e período, tentativa repetida de adicionar, salvar e reabrir mantendo quantidade e estado pendente. Inclui primeiro toque retido, repetição recusada e recuperação após rejeição JSON, além de regras de criação, geração, permissões e concorrência.
- Teste visual atualizado: 1/1 em 24,345 s. Desktop 1280/1440, estados pendentes/histórico/canceladas; mobile 320/360/390, ações, alturas preservadas, ausência de overflow e erros JavaScript.
- A rodada ampla também aprovou edição, Peguei, exclusão, remoção, devolução, salvar/reabrir, geração automática anterior, resposta perdida, sucesso sem versão e conflito real 409. Não houve nova rodada conjunta após os ajustes finais; os testes afetados foram reexecutados.
- `git diff --check` sem erros. Nenhuma falha funcional conhecida remanescente. Não foi medida carga em catálogo de produção nem realizada validação em celular físico.

Para reproduzir com o runner existente, habilitar somente em memória `sistema.acesso.AcessoComercialMiddleware` em `sistema.settings_test_compras`, importar `validar_compra_timeout_pg` e chamar `main()`, passando os módulos `estoque.tests_coleta_lista_browser`, `estoque.tests_lista_selecao_manual_mobile`, `estoque.tests_coleta_operacoes_mobile`, `estoque.tests_coleta_lista_mobile` e `estoque.tests_consulta_listas_mobile` como argumentos. O runner exige binários locais de PostgreSQL e os navegadores já utilizados pelas suítes.
