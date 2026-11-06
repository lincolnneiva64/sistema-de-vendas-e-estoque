# Feedback do botão Gravar Venda — 06/11/2026

## Causa e escopo

O envio online protegido primeiro reserva a montagem no IndexedDB, com UUID e hash imutáveis, pelo mesmo finalizador usado no offline (`transport: online`). `completed()` protege os controles e o observador `offlineActions()` atualiza o botão. Esse observador considerava qualquer `localCompletion` sem número oficial como `Venda offline pendente`, ignorando transporte, status e recibo. Ele sobrescrevia o texto de processamento do template inclusive durante um POST online.

O resumo/diagnóstico já distinguia os estados. O ajuste limita-se ao feedback do botão e usa a operação carregada em `refreshCompletion()`, protegida pelo contador de geração existente. Não há nova operação, reenvio, consulta, regra financeira, alteração de estoque, UUID, hash ou idempotência.

| Situação | Botão |
| --- | --- |
| Montagem pronta; modal financeiro aberto mas ainda não confirmado | Gravar Venda |
| Processamento iniciado após validações e confirmação financeira, quando aplicável | Gravando venda... |
| Operação em envio, ainda sem recibo validado | Aguardando confirmação... |
| Confirmação oficial validada | Venda gravada! |
| Operação de origem offline efetivamente persistida, pendente, sem tentativa/recibo | Venda offline pendente |
| Resultado desconhecido, conflito ou estado sem evidência suficiente de confirmação | Verificar gravação |

O texto não concede permissão para gravar: bloqueios existentes da montagem, finalização, Web Locks, UUID/hash e reconhecimento oficial continuam responsáveis por isso. Consultar uma operação não encontrada ou perder uma resposta não libera a operação. Uma nova montagem continua tendo identidade própria pelo fluxo existente.

O modal à vista continua abrindo antes de `vendaGravacaoEmAndamento`; o texto de processamento só começa após confirmar a origem e validar os dados. Após sucesso, a limpeza visual mantém `Venda gravada!`; incluir os itens da próxima montagem restaura `Gravar Venda`. No adaptador local controlado, o feedback também segue envio, recibo e consulta existentes, sem alterar seus bloqueios.

## Arquivos

- `estoque/templates/estoque/vendas_layout_teste.html`: texto de processamento/sucesso/nova montagem e URLs dos módulos atualizados; nenhum CSS ou layout alterado.
- `static/offline/sales-draft-ui.js`: apresentação baseada em transporte, status e `validReceipt`.
- `static/offline/local-sales.js`: feedback visual do adaptador local controlado.
- `offline/urls.py` e `static/offline/service-worker.js`: namespace `sale-button-feedback-v1` e versão do cache de assets. Necessários porque workers antigos podem reutilizar módulos pela URL anterior. Endpoints de venda, consulta e sincronização permanecem intactos. Nenhuma limpeza de IndexedDB ou dados do navegador.
- `offline/tests_sale_button_feedback_browser.py`: testes dirigidos com Chrome real, perfis temporários e vendas fictícias.
- `offline/tests_online_sales_browser.py`: espera da perda de resposta pelo estado persistido, em vez de exigir `role=alert`; o diagnóstico existente pode apresentar esse caso como status quando permite iniciar uma montagem independente.

## Reproduzir a validação isolada

No PowerShell, na raiz do repositório, com as dependências existentes e Chrome instalado:

```powershell
@'
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'sistema.settings_test_compras'
import django
from django.conf import settings
assert settings.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3'
assert settings.DATABASES['default']['NAME'] == ':memory:'
settings.INSTALLED_APPS += ['inventario']
settings.MIGRATION_MODULES = {app: None for app in ['admin', 'auth', 'contenttypes', 'sessions', 'estoque', 'locacoes', 'offline', 'inventario']}
settings.MIDDLEWARE += ['offline.local_sales.LocalSalesMiddleware', 'sistema.acesso.AcessoComercialMiddleware']
settings.TEMPLATES[0]['OPTIONS']['context_processors'] += ['estoque.context_processors.ambiente_sistema']
django.setup()
from django.test.runner import DiscoverRunner
raise SystemExit(DiscoverRunner(verbosity=2, interactive=False).run_tests(['offline.tests_sale_button_feedback_browser']))
'@ | python -
```

O settings é independente: não importa configuração operacional nem `.env`. O banco é criado em diretório temporário e descartado pelo runner. Migrations desabilitadas apenas em memória; tabelas dos modelos criadas diretamente. O middleware local só autoriza a modalidade nos testes que a habilitam explicitamente e com loopback.

Os testes seguram ou perdem a resposta do POST/consulta para inspecionar o botão antes do recibo; fazem cliques repetidos, conferem vendas/efeitos financeiros e recuperam o mesmo UUID após recarga. Há cobertura de venda à vista, a prazo, resposta rápida/lenta, falha antes de chegar ao servidor, resposta perdida após commit, offline real persistido, consulta não encontrada, confirmação recuperada e próxima montagem.

Para uma conferência visual adicional, use apenas servidor/banco descartáveis e perfil temporário com dados fictícios. Na venda à vista, abrir o modal deve manter `Gravar Venda`; confirmar Caixa/Banco deve iniciar o processamento. Network > throttling permite observar a espera; não repita vendas após perder resposta. Reabra online para obter os novos assets; não limpe dados do site nem operações pendentes.

## Limitações

Rodada final: **20/20 em 45,215 s**. Executados o módulo novo (6 casos), `offline.tests_online_sales_browser` completo, os casos locais `test_anonymous_normal_button_f5_and_private_drafts` e `test_lost_response_f5_lookup_and_same_uuid`, e `test_online_unknown_result_allows_explicit_new_sale` de vendas consecutivas. Inclui perda de resposta após commit, recuperação automática somente por GET, consulta que confirma enquanto POST ainda está em andamento e resposta tardia sem apagar montagem nova, além de caixa/banco, consumo próprio e estoque fictícios sem duplicação. Para repetir a rodada ampla, acrescente esses labels à chamada `run_tests` acima.

O primeiro harness não tinha o context processor de ambiente e o middleware local usados pelas vendas; foram habilitados somente em memória, como no comando acima. Uma expectativa antiga exigia `role=alert` onde o diagnóstico existente usa `role=status`; agora o teste aguarda o estado persistido. A rodada final já contém esses ajustes. `git diff --check` sem erros. Permanecem o aviso preexistente de namespace `estoque` duplicado e mensagens Broken pipe durante navegação de testes.

Validação funcional em Chrome real com dados fictícios e SQLite descartável; não comprova concorrência PostgreSQL nem latência de produção. Não houve acesso a Neon/Render, vendas reais, migrations, commit, push ou deploy. Alterações preexistentes de locações preservadas.
