# Unidade das compras — correção final da apresentação e da escolha explícita

Base: `44f889db7ed8abfdaae63c5f07b26c80621f15b7`.
Worktree: `.worktrees/compra-unidade-final-44f889d`, HEAD destacado. Sem commit, push ou deploy.

## Resultado

- Identificação permanente **Unidade**, com label associado ao select e estilo do formulário.
- Lista contém apenas a unidade base realmente aceita pelo fluxo de compras, além do placeholder `Selecione...` quando há uma correção pendente. Nunca inclui o valor inválido antigo, nem como opção desabilitada.
- Rascunho com `2,95` abre sem escolha confirmada, com campo discretamente destacado, aviso curto **Selecione a unidade.** abaixo do controle e orientação completa acima da tabela: **Unidade inválida. Selecione uma unidade cadastrada para continuar.**, acompanhada do produto.
- O aviso completo ocupa a largura da seção, evitando alargar a coluna ou fragmentar a linha da tabela. O aviso local fica em uma única linha. Ambos desaparecem imediatamente ao escolher UN.
- A tentativa de finalizar chama a validação existente antes das etapas de pagamento e preços, identifica o produto e focaliza o seletor. No mobile, abre a edição do item quando necessário. O tratamento de `invalid` do formulário mantém esse comportamento quando a validação nativa é acionada.
- Quantidade, preço, total e decisão de preços não são alterados pela escolha da unidade. Não há correção silenciosa no banco, exclusão de itens ou finalização automática. Cancelar a edição mobile conserva a pendência original.

## Reutilização e backend

Foram reaproveitados `compraPreencherUnidades`, `validarUnidadeCompra` e `_validar_unidade_compra`. Não há novo serviço, cadastro ou mecanismo paralelo. `_dados_compra_post` passa a exigir escolha não vazia no mesmo validador Django; POST com `2,95`, campo vazio ou unidade incompatível é recusado. A geração de listas e os demais chamadores mantêm o fallback preexistente para unidade base, por meio do parâmetro opcional do mesmo validador.

Códigos cadastrados como CX12 continuam válidos. Compras usam a unidade base (`unidade_venda_1`, com fallback para `unidade_compra`). Conversões fracionadas exclusivas de vendas não foram acrescentadas ao finalizador de compras. Não houve alteração dos serviços de preços, da lógica de estoque/financeiro, das conferências físicas ou de outros módulos. Nenhuma migration criada.

## Testes e inspeção visual

Execução final no runner existente `validar_compra_timeout_pg.py`, com cluster novo exclusivo `inventario-disposable-pg-mu9s6_xq`, loopback e identidade/diretório verificados pelo runner. Settings independentes do `.env` e nenhum acesso a bancos operacionais. Cluster encerrado ao terminar. Apenas migrations existentes construíram o banco descartável.

**12 testes aprovados em 19,498 s**, sem falhas ou testes ignorados:

- `estoque.tests_compra_unidade_selecao` (4): Django recusa unidade monetária e vazia; preserva rascunho; aceita CX12; navegador desktop/mobile não oferece valor inválido, corrige explicitamente, conserva quantidade/preço e restaura a pendência ao cancelar; linhas adicionadas por seleção real de produto recebem as opções corretas.
- `estoque.tests_compra_rascunho_unidade` (6): correção de legado, normalização, geração da lista, preservação da unidade após rejeição, exigência de escolha, rollback financeiro e reenvio, além da reprodução do parser original.
- `UnidadeFinalBrowserTests.test_percurso_desktop` e `test_percurso_mobile` (2): teste real de clique, foco e seleção por teclado nativo, do rascunho inválido até revisão e conclusão. Fixture independente: produto Skol Fictícia em UN, quantidade 15, custo 37, total 555; após finalizar, saldo 25 e um movimento financeiro de 555, inclusive após reenvio. Os valores da linha, o total e a prévia de revisão são comparados antes/depois da seleção, antes de qualquer conclusão.

Imagens efetivamente abertas e inspecionadas por visão, em `amostras/unidade-final/`:

- `1280-invalida.png` e `1280-corrigida.png`: label visível, unidade antiga ausente da lista, aviso curto em uma linha, orientação completa fora da linha e colunas alinhadas; quantidade 15,000, preço 37,00 e total R$ 555,00 preservados.
- `390-invalida.png` e `390-corrigida.png`: seletor ocupa a largura do campo no cartão de edição, label visível, destaque discreto; aviso desaparece e botões de salvar/cancelar permanecem utilizáveis.

Os PNGs são evidências locais separadas do código. `git diff --check` aprovado. Permanece o aviso Django preexistente `urls.W005` de namespace duplicado.

Limitações: Edge headless com viewport desktop 1280 e mobile 390; não foi usado celular físico. Compra #280 real não foi acessada, modificada ou finalizada. Não é possível confirmar seus valores persistidos sem acesso operacional, que não foi realizado.

## Arquivos alterados

- `estoque/templates/estoque/compras_nova.html`
- `estoque/views.py` (somente validador existente e chamada no parser do formulário)
- `estoque/tests_compra_unidade_selecao.py`
- `estoque/tests_compra_rascunho_unidade.py` (expectativa de unidade vazia passa a rejeição explícita)
- `estoque/tests_compra_unidade_final_browser.py`
- Este documento.
