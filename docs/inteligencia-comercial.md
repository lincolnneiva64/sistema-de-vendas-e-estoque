# Inteligência comercial — entrega local preparada

Acesso administrativo pelo painel Financeiro → Relatórios Gerenciais → Resumo
de vendas. No resumo, usar Investigar vendas por classes e produtos.

## Escopo

- Análise de desempenho e participação dos clientes, com valores e percentuais,
  rankings financeiros, notas, dias de compra e histórico acessível.
- Investigação Classes → Produtos → Clientes → Notas, com filtros preservados.
- Classes sem faturamento nos dois períodos opcionais; totais sempre conciliados.
- Produtos com quantidades por unidade, preços médios, notas e clientes em expansão.
- Cabeçalhos clicáveis com ordenação global antes da paginação, incluindo ausência
  de base percentual sempre no final. Sem JavaScript, os links continuam funcionais.
- Atualização progressiva dos resultados, histórico do navegador e foco acessível.
- Tabelas responsivas e campos numéricos alinhados, sem conversões presumidas.
- Correspondência explicitamente aprovada VARIEDADES → Variedades (classe #34)
  apenas na análise. Nenhum outro nome é unido por similaridade. Os cadastros e
  as notas não são alterados. URLs anteriores dessa correspondência são aceitas.

## Integridade

Exclui canceladas e Consumo próprio. Identifica clientes e produtos por ID e
preserva notas como evidência. A diferença das classes concilia ao centavo com
as notas comerciais. Divergência financeira interrompe a investigação; não há
rateio artificial. Custos, margens e equivalências históricas não são inventados.
O agrupamento utiliza a classificação atual e a correspondência confirmada;
a nota não mantém uma classe histórica por item. Não há IA externa nem decisões
automáticas de desconto, compras ou estoque.

Os serviços financeiros-base e os gráficos publicados permanecem inalterados.
Nenhuma migration é necessária para estes arquivos. Não há comando cadastral
ou de regularização financeira associado a esta entrega.

## Validação e publicação

Executar os testes pelo runner PostgreSQL descartável do repositório. O runner
recusa o banco operacional; não usar manage.py test com as configurações reais.
A suíte do Plano Mestre e os contratos operacionais autenticados têm validação
própria. Há expectativas antigas de consultas/layout e fixtures operacionais
pendentes em suítes da base publicada; o relatório local de auditoria registra
as falhas anteriores e a comparação com a integração. Não declarar a suíte
completa aprovada enquanto essas pendências permanecerem.

O transporte DevTools dos testes foi corrigido para mensagens acima de 64 KB,
sem alterar rotas operacionais. A regressão de transporte verifica máscaras e
conteúdo nos três tamanhos de frame.

Reutilizar preparacao/publicacao-marco-zero na worktree oficial existente.
Selecionar arquivos explicitamente e revisar o diff. Commit, push e deploy
requerem autorização futura. Antes do push, confirmar no Render repositório,
branch, Auto-Deploy e comandos efetivos de build/start/pre-deploy. Não executar
build.sh como teste: o script existente chama um comando de publicação que pode
alterar o banco. A configuração efetiva do Render deve ser conferida no painel.

Backups, bancos, credenciais, logs, screenshots, diagnósticos operacionais,
propostas cadastrais e documentos com dados reais não fazem parte desta entrega.
A etapa Avaliação do comércio e sugestões não foi iniciada.
