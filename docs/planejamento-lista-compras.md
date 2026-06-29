# Planejamento — Lista de Compras

## Objetivo

Criar uma Lista de Compras inteligente para ajudar a decidir o que comprar antes de lançar a compra real.

A Lista de Compras será uma pré-compra. Ela não finaliza compra automaticamente no começo.

---

## Fase 1 — Ligação Produto x Fornecedor

Criar uma ligação simples entre Produto e Fornecedor.

Regra:

- Um produto pode ter vários fornecedores.
- Um fornecedor pode vender vários produtos.
- A ligação deve ser simples.

Modelo sugerido:

ProdutoFornecedor

- produto
- fornecedor
- ativo, se necessário

Não guardar preço nessa tabela.
Não guardar endereço.
Não guardar telefone.
Não duplicar dados do produto nem do fornecedor.

Os preços continuam vindo do cadastro do Produto e das Compras.
Os dados do fornecedor continuam vindo do cadastro do Fornecedor.

---

## Fase 2 — Mostrar vínculos nas telas

No cadastro do produto:

- mostrar "Fornecedores deste produto";
- permitir adicionar fornecedor;
- permitir remover ou desativar fornecedor.

No cadastro do fornecedor:

- mostrar "Produtos deste fornecedor";
- permitir adicionar produto;
- permitir remover ou desativar produto.

---

## Fase 3 — Criar vínculo automático pela compra

Quando lançar uma compra:

- se Produto X foi comprado do Fornecedor Y;
- e ainda não existe vínculo Produto X x Fornecedor Y;
- o sistema cria o vínculo automaticamente.

Isso evita cadastrar tudo manualmente.

---

## Fase 4 — Primeira tela da Lista de Compras

Criar tela "Lista de Compras".

Filtros principais:

- fornecedor;
- período de análise das vendas;
- dias até próxima compra/reposição;
- dias para entrega;
- margem de alerta;
- margem de sugestão.

A tela deve mostrar os produtos ligados ao fornecedor escolhido.

---

## Fase 5 — Regra de cálculo

Para cada produto:

1. Ver quanto vendeu no período.
2. Ver estoque atual.
3. Calcular falta real:

   falta real = vendido previsto - estoque atual

4. Produto aparece na lista se estiver dentro da margem de alerta.
5. Sugestão de compra:

   sugestão = falta real + margem de sugestão

6. Arredondar para cima.
7. Quantidade sugerida deve ser editável.

---

## Margem de alerta

Serve apenas para decidir se o produto aparece na lista.

Ela não deve aumentar automaticamente a compra.

Exemplo:

- venda prevista: 28
- estoque atual: 30
- margem de alerta: 20%

Limite de alerta:

28 + 20% = 33,6

Como estoque 30 está abaixo de 33,6, o produto aparece como atenção.

Mas a sugestão pode ser 0, porque ainda tem mais estoque do que a venda prevista.

---

## Margem de sugestão

Serve para sugerir uma quantidade um pouco acima da falta real.

Exemplo:

- estoque atual: 10
- venda prevista: 18
- falta real: 8
- margem de sugestão: 20%

Cálculo:

8 + 20% = 9,6

Arredonda para:

10

Quantidade sugerida:

10

---

## Percentual real ao editar quantidade

A quantidade de compra deve ser editável.

Quando o usuário alterar a quantidade, o sistema deve mostrar o percentual real acima ou abaixo da falta.

Exemplo:

- falta real: 8
- comprar: 10

Percentual real:

+25%

Porque 10 é 25% acima de 8.

Outros exemplos:

- comprar 8 = 0%
- comprar 6 = -25%
- comprar 12 = +50%

---

## Fase 6 — Colunas da lista

Colunas sugeridas:

- Produto
- Estoque atual
- Vendido no período
- Falta real
- Sugestão automática
- Comprar
- Percentual acima/abaixo da falta
- Custo atual
- Total previsto

---

## Fase 7 — Transformar lista em Nova Compra

Depois que a Lista de Compras estiver funcionando:

- botão "Gerar Compra";
- levar fornecedor, produtos e quantidades para Nova Compra;
- usuário confere preços, pagamento e finaliza.

---

## Deixar para depois

- Data da última alteração de preço no cadastro do produto.
- Comparar fornecedores.
- Salvar lista como rascunho.
- Imprimir lista.
- Sugestões avançadas por produto.
- Margem diferente por categoria ou produto.
