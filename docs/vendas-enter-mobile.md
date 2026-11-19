# Enter em Vendas mobile

## Base e aprovacao

Worktree: `.worktrees/vendas-enter-mobile-20261119`.
Base: `b7061e141ec4fab7117095caafad98699e80d644`.
Branch oficial: `preparacao/publicacao-marco-zero`.

O usuario confirmou essa base como Live no Render em 18/11/2026, no servico
`sistema-de-vendas-e-estoque` (Python 3). O fetch explicito da branch oficial
confirmou o mesmo SHA. O fetch padrao busca apenas a branch de backup.
A candidata foi preparada sobre essa mesma base antes do fetch, inicialmente
confirmada pelo conector GitHub; nao houve troca de base.

O usuario testou o fluxo no Django local completo e aprovou a correcao.
Autorizou commit e push exclusivamente para a branch oficial, por fast-forward,
sem marcador de supressao do Render, permitindo Auto-Deploy. Nenhum deploy
manual ou migration esta autorizado nesta etapa.

## Diagnostico e correcao

- Operador prematuro: o classificador concatenava valor, texto, placeholder,
  id e name e buscava palavras como produto, quantidade e preco. Um Cliente
  chamado `Cliente Produtos` era confundido com etapa posterior ao Operador.
  O aviso foi reproduzido no codigo da base. No mobile, a classificacao usa
  controles reais e o keydown considera a origem do evento.
- Cliente seguia para Data. Agora segue para Vencimento apenas no mobile.
  Data continua editavel e seu Enter segue para Vencimento. Custo nao participa.
- Quantidade e Unidade dependiam do listener global, que tambem continha um
  fluxo legado baseado no foco atual e capaz de pular Unidade para produtos
  nao fracionados. Agora seus handlers locais sempre visitam Unidade; o fluxo
  legado permanece apenas no desktop. O salto intermitente no aparelho nao
  foi reproduzido diretamente no fixture original; a passagem por UN/PCT/DZ/KG
  foi verificada depois da correcao, posteriormente aprovada no sistema local.
- Existem dois lancadores de itens no template. A propagacao ja era cancelada
  pelo Preco no mobile; nao foi comprovada duplicacao nesse caminho original.
  O listener global agora nao trata Enter mobile. O Preco reutiliza somente
  `adicionarItemVendaAtual`, mantendo as validacoes e retornando ao Produto
  apenas no sucesso.
- A rotina de fechar teclado removia o foco do Produto apos sucesso. O Enter
  mobile agora preserva esse foco. Os focos agendados em 100/180 ms do proximo
  produto foram substituidos, apenas no mobile, por foco imediato. Nao foram
  acrescentados atrasos. Clique em Adicionar conserva seu comportamento.

Revisados keydown, blur, change, submit, autocompletes, seletores personalizados,
modais e bloqueios de Operador. Nao ha handler keyup no template. Enter nos
seletores cancela a acao nativa; confirmar uma opcao dispara change antes de
avancar. O bloqueio de Enter em Gravar/Confirmar Venda continua ativo.

## Escopo

Somente tres arquivos:

- `estoque/templates/estoque/vendas_layout_teste.html`.
- `estoque/tests_vendas_enter_mobile.py`.
- `docs/vendas-enter-mobile.md`.

A correcao funcional usa o breakpoint mobile existente (ate 980 px). Nenhum CSS,
layout desktop, endpoint, servico comercial, regra de estoque/custo/financeiro,
modulo offline, armazenamento, UUID/hash ou migration foi alterado. Os modulos
offline continuam observando a tabela confirmada e os eventos existentes.

## Verificacoes para publicacao

- `git diff --check`: sem erros.
- `manage.py check`: sem erros, com aviso preexistente `urls.W005`
  (namespace estoque duplicado).
- 24 testes de Enter, avisos, edicao e rascunhos aprovados.
- Compilacao do template e JavaScript inline pelo Edge incluida nos testes.
- O teste de reproducao original usa o SHA fixo da base, preservando sua
  validade depois do commit.

Cobertura: 30 selecoes de Cliente, sequencia completa, confirmacao de Produto,
seletores por clique/Enter, UN/PCT/DZ/KG, inclusao e atualizacao unicas, erros,
repeat/composicao/keyup, bloqueio de Gravar por Enter, ausencia de foco mobile
agendado, resize equivalente ao teclado e regressao desktop a 1366 px.

Testes executados em Edge/CDP com perfis e documentos temporarios, rede bloqueada
e funcoes controladas para dados e validacoes comerciais. A funcao real de
inclusao/edicao e os handlers sao reutilizados. Os testes de custo/estoque
verificam respeito ao retorno da validacao, nao sua matematica; a suite de
rascunhos verifica apresentacao, nao sincronizacao de ponta a ponta.
O teste fisico no Django local completo foi realizado e aprovado pelo usuario.

Para repetir os testes nesta worktree, com o Python do venv principal:

```powershell
python -m unittest estoque.tests_vendas_enter_mobile.EnterMobileTests estoque.tests_vendas_rascunhos_mobile.RascunhosMobileTests -v
```

O check foi executado pelo launcher manage.py com `sistema.settings_test_compras`,
SQLite `:memory:` confirmado e `inventario` acrescentado em memoria a
INSTALLED_APPS, sem importar settings operacional ou ler .env. A primeira chamada
com esse settings isolado, sem o complemento, falhou porque inventario ja aparece
nas URLs mas nao na lista de apps desse settings de testes. A configuracao do
processo foi completada e o check passou; nenhum settings do repositorio foi
alterado. Nenhum banco real ou migration foi acessado/executado.
