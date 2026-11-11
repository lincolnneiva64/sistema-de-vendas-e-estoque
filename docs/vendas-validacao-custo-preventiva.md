# Conferência preventiva de custos na venda

Complemento posterior: a proteção do cadastro e das revisões de compra foi
auditada e corrigida separadamente em
[Proteção de preços no cadastro e nas compras](cadastro-protecao-precos-custo.md).
O cálculo de custo fracionado passou a ser compartilhado, mantendo a regra da
venda. Nesta conferência posterior, 44 testes específicos de venda foram
reexecutados e aprovados; os 82 abaixo registram a rodada da entrega inicial.

## Comportamento

A conferência acontece antes de criar o comando de venda, reservar sua sequência
ou inserir uma operação na fila. A montagem mantém sua identidade e seus campos.
Um preço abaixo do custo pode ser digitado e editado; somente a conclusão é
bloqueada. O modal lista os produtos envolvidos, unidade, custo e preço, abre o
editor existente em outra aba e permite consultar o cadastro novamente. Atualizar
os dados não conclui nem envia a venda automaticamente.

O endpoint POST `/api/vendas/online/custos/` é somente leitura, exige CSRF e
permissão de consulta de produtos ou a autorização LOCAL já existente. Ele usa
uma consulta de produtos por lote e as mesmas funções de custo por unidade e
arredondamento de preço do serviço comercial. Não altera produtos ou compras.

A conferência cobre o envio online protegido, a conclusão offline, o adaptador
LOCAL, o envio tradicional e a conclusão de novos rascunhos de revisão. Operações
já concluídas continuam seguindo o contrato anterior de identidade e recibo;
não são reescritas, reenviadas ou convertidas em novas operações por esta mudança.

O backend comercial continua intacto como última proteção. Não há autorização
automática de prejuízo, alteração de custo histórico ou tentativa de corrigir
automaticamente o cadastro com um valor informado pelo operador.

## Referência offline e limites

- Online, cada finalização consulta o custo atual, sem cache da resposta.
- Offline, o catálogo salvo fornece apenas uma referência. O modal informa que
  essa referência pode estar desatualizada; ela nunca é apresentada como consulta
  atual do cadastro. Custo fracionado explícito tem prioridade; na ausência dele,
  aplica-se a divisão pelo fator com arredondamento decimal half-even em centavos.
- Ausência de catálogo, custo nulo/zero, produto indisponível ou unidade não
  reconhecida impede concluir, mantendo a montagem disponível para edição.
- Falha de autenticação ou resposta inválida não é tratada como validação positiva.
  A conclusão offline pode usar a referência salva em uma falha de comunicação.
- O editor exige conexão e mantém suas permissões atuais. Sem conexão, o botão
  explica essa necessidade e preserva a montagem na aba original.
- A atualização no modal confere os custos atuais e atualiza a referência visual;
  não troca preços digitados, conversões das linhas, estoque de referência nem o
  catálogo offline inteiro. Para preparar um catálogo novo para uso posterior
  sem conexão, continua disponível o procedimento existente de preparação.
- Mudança de custo no servidor depois da consulta, ou depois da preparação do
  catálogo offline, ainda pode produzir rejeição no backend. A prevenção não é
  uma reserva de preço/custo nem pode conhecer alterações feitas sem conexão.

## Verificação isolada

82 testes distintos aprovados ao longo das rodadas, incluindo 15 novos testes.
A reexecução direcionada após os ajustes dos fixtures/interceptadores passou
com 28 testes. O acabamento do modal foi verificado novamente nos oito testes
novos de navegador, inclusive viewport de 390 x 844 e inspeção de captura com
dados fictícios. Avisos existentes de namespace `estoque` duplicado e Broken pipe
durante encerramento de abas não impediram a validação.

Cobertura: múltiplos produtos, igualdade custo/preço, conversão fracionada,
arredondamento, alteração do custo, catálogo desatualizado/ausente, falha de
consulta, acesso/CSRF, modalidade LOCAL, preservação da montagem e da identidade,
respostas perdidas, recuperação oficial, duas abas e revisão. Um teste mantém
uma venda oficial fictícia com seis produtos e compara estoque, financeiro,
histórico e operações antes/depois da conferência.

As correções de cadastro são simuladas no banco fictício como mudanças feitas
por outro atendimento. O formulário existente de produto foi reutilizado, sem
alteração de seu fluxo ou de suas regras de gravação.

Os testes usam `sistema.settings_test_compras`, que não importa a configuração
operacional, SQLite exclusivo em TEMP, schema criado diretamente pelo executor
`inventario.test_runner_confiabilidade.SemMigrationsRunner` e perfis Chrome novos.
Nenhum executor de migrations foi usado. Não houve teste de concorrência em
PostgreSQL nesta intervenção.

Preparação reproduzível, apenas para testes locais:

```powershell
@'
import os, tempfile
from pathlib import Path
os.environ['DJANGO_SETTINGS_MODULE'] = 'sistema.settings_test_compras'
import django
from django.conf import settings
settings.INSTALLED_APPS += ['inventario']
settings.MIDDLEWARE += ['offline.local_sales.LocalSalesMiddleware', 'sistema.acesso.AcessoComercialMiddleware']
settings.TEMPLATES[0]['OPTIONS']['context_processors'] += ['estoque.context_processors.ambiente_sistema']
settings.OFFLINE_ENVIRONMENT_ID = 'offline-isolated-tests'
root = tempfile.TemporaryDirectory(prefix='inventario-confiabilidade-', ignore_cleanup_errors=True)
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(Path(root.name) / 'synthetic.sqlite3')}}
settings.TEST_RUNNER = 'inventario.test_runner_confiabilidade.SemMigrationsRunner'
django.setup()
from django.test.utils import get_runner
raise SystemExit(bool(get_runner(settings)(verbosity=2, interactive=False).run_tests([
    'offline.tests_sale_costs', 'offline.tests_sale_costs_browser',
])))
'@ | ./venv/Scripts/python.exe -
```

## Arquivos desta intervenção

```text
docs/vendas-validacao-custo-preventiva.md
estoque/templates/estoque/vendas_layout_teste.html
offline/sale_costs.py
offline/views.py
offline/urls.py
offline/templates/offline/sale_revision.html
offline/tests_sale_costs.py
offline/tests_sale_costs_browser.py
offline/tests_online_sales_browser.py
offline/tests_sale_revisions_browser.py
sistema/acesso.py
static/offline/sales-costs.js
static/offline/sales-drafts.js
static/offline/sales-draft-ui.js
static/offline/local-sales.js
static/offline/sales-revisions.js
static/offline/sales-revision-ui.js
static/offline/service-worker.js
```

Os testes antigos distinguem a consulta POST de custos de uma gravação comercial;
o fixture de revisão recebe explicitamente a permissão de consultar Vendas.
O namespace `sale-cost-check-v1` e o cache v49 atualizam somente os assets
necessários. Uma futura publicação deve atualizar o Service Worker online, sem
limpar os dados do site.

## Preservação

Neon, venda #1278, operações reais de Zeca Caroço e Sta. Bárbara, IndexedDB real,
compras e dados operacionais não foram acessados ou modificados. A alteração
preexistente de `sales-closures.js` e os trabalhos de locações foram preservados.
Sem migrations, commit, push ou deploy.
