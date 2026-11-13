# Compra: seleção da unidade

Base: `835943620db514a93fcee18765a1782cab22745e`.
Worktree isolada: `.worktrees/compra-unidade-selecao-8359436`, HEAD destacado, sem commit.

## Alteração

O campo Unidade de `compras_nova.html` passou de input livre com datalist para select nativo. O preenchimento usa a unidade base já enviada pelo catálogo da página (`unidade_venda_1`, com fallback para `unidade_compra`), o mesmo contrato de `_validar_unidade_compra` no Django. Não há lista global arbitrária nem filtro que proíba números nos códigos cadastrados.

A seleção é preenchida ao abrir a página, selecionar/trocar produto, clonar linha, limpar item, cancelar edição mobile e recuperar o rascunho de sessão após cadastro rápido de produto. O select recebe o estilo mobile do campo anterior e respeita o bloqueio de edição por `inert`, mantendo sua participação no FormData. Quantidade e preço não são convertidos nem recalculados pela função de preenchimento da unidade.

Valor legado divergente, como `2,95`, aparece identificado como inválido em uma opção desabilitada; não integra as escolhas válidas e não pode ser escolhido novamente pelo operador. A escolha válida é exigida para enviar. A abertura e a edição visual não gravam no banco. Cancelar a edição mobile restaura o valor original. Não existe saneamento, exclusão de itens ou finalização automática.

A validação Django existente foi mantida e testada com POST adulterado. Não foram alteradas revisão de preços, finalização, estoque, financeiro ou conferências.

## Unidades e conversões

O finalizador de compras trabalha com quantidade e custo na unidade base. As conversões fracionadas de vendas não fazem parte desse contrato e não são oferecidas como alternativas na compra. Exemplo: produto CX12/UN com fator 12 oferece CX12 neste formulário; um produto cuja base é UN oferece UN. Códigos com números continuam disponíveis quando cadastrados como base. Não foi criada uma conversão nova nem feita interpretação automática de quantidade/preço.

## Verificação

Runner existente `validar_compra_timeout_pg.py`, com PostgreSQL novo descartável, loopback, porta/diretório/identidade validados e settings independentes do `.env`. Nenhuma conexão com Render/Neon ou bancos operacionais. Apenas migrations existentes foram usadas para montar o banco de teste; não há migration nova.

Comando: `python validar_compra_timeout_pg.py estoque.tests_compra_unidade_selecao estoque.tests_compra_rascunho_unidade`.

Resultado: 10 testes aprovados em 9,247 s; cluster `inventario-disposable-pg-7txxdxsk` encerrado. Cobertura: POST inválido recusado, preservação de rascunho legado, correção posterior para UN, CX12, dados da lista, garantia transacional preexistente e dois testes de navegador Edge, cada um em desktop 1280 e mobile 390. A tentativa real de digitar `2,95` em select não modifica a seleção válida nem cria opção. Atribuição programática de opção inválida continua bloqueada pela validade do controle. Linhas novas usam a seleção real de produto pelas sugestões com evento de digitação confiável.

A checagem ampliada de visibilidade/interação no modo Editar mobile e de restauração ao cancelar também passou: um teste reexecutado em 3,425 s no cluster `inventario-disposable-pg-fgcibbch`, encerrado pelo runner. `git diff --check` aprovado.

Limitações: navegador desktop com emulação de viewport mobile; não foi usado aparelho físico. Compra #280 e demais dados reais não foram consultados ou alterados. O aviso Django preexistente `urls.W005` permanece. Sem commit, push ou deploy.

## Arquivos

- `estoque/templates/estoque/compras_nova.html`
- `estoque/tests_compra_unidade_selecao.py`
- Este documento.
