# Comprovantes de locações para clientes

Implementação local de 04/11/2026. Sem commit, push, deploy ou acesso a bancos operacionais.

## Comportamento

A entrega mantém sua conferência, compartilhamento e marcações verdes. Seu comprovante ganhou a apresentação personalizada aprovada.

Após o recolhimento, a confirmação existente apresenta o mesmo cartão usado pelo comprovante. A ação principal é **Enviar comprovante de recolhimento ao cliente**. Não há seletor de funcionários, cópia de instruções operacionais nem confirmação artificial de mensagem enviada. O clique prepara o WhatsApp do cliente; o envio depende da confirmação no aplicativo. Se uma janela for bloqueada, aparece o acesso à mensagem já preparada, sem emitir outro token.

O cartão mostra previsão, quantidades físicas recolhidas nesta conferência, condições, situação, pendências, avarias, responsável e horário do servidor em Belém. Somente quantidades em bom estado ficam verdes. Quebras ficam em amarelo. Perdas e descartes são apresentados como ocorrências, sem somá-los aos materiais fisicamente recolhidos. Os registros originais não são recalculados nem alterados.

Sem telefone, o envio fica desabilitado e nenhum token é emitido. O telefone deve ser corrigido pelo cadastro existente.

## Acesso do cliente

- Token de 32 bytes aleatórios (`secrets.token_urlsafe(32)`), exclusivo do comprovante; somente SHA-256 é armazenado, com índice único anulável.
- Validade de 30 dias, com revogação. Cada emissão substitui o hash anterior sob bloqueio transacional do comprovante. A auditoria registra emissão/revogação sem guardar links ou segredos. Falha na auditoria desfaz a rotação.
- O botão de envio também renova o acesso, inclusive depois dos 30 dias. Usuário autenticado precisa das permissões de recolhimento já existentes. O funcionário que acabou de confirmar pode preparar o compartilhamento exclusivamente de sua conferência usando sua sessão delegada ainda válida. O token entregue ao cliente é novo e independente; não concede essa sessão nem qualquer permissão operacional.
- Revogação disponível em **Acesso ao comprovante**, para quem já pode compartilhar. A renovação preserva o histórico e invalida o link anterior.
- A rota `/locacoes/comprovante-recolhimento/` entrega inicialmente uma página sem dados. O segredo vai no fragmento `#` do link: navegadores não enviam esse fragmento em URLs HTTP ou Referer. O JavaScript local envia o token no corpo de uma consulta POST protegida por CSRF. Essa consulta somente lê o comprovante vinculado ao hash; não aceita seleção de outra locação nem executa ações.
- Respostas com `no-store`, `noindex`, `nofollow`, `noarchive`, `no-referrer`, CSP restritiva e proteção contra enquadramento. A página do cliente não carrega recursos de terceiros, analytics, controles internos ou mensagens internas de sessão. Não grava snapshots offline.
- As rotas operacionais continuam protegidas. Nenhum token operacional é alterado ou colocado na mensagem do cliente. O mecanismo existente de logs seguros continua ativo; os novos segredos não entram no caminho ou na query dos acessos HTTP.

## Migration para futura publicação

**Necessária: `locacoes/0018_comprovante_cliente.py`, após a 0017.** Acrescenta somente hash, expiração e revogação ao comprovante. Registros anteriores ficam com esses campos nulos e podem receber acesso pelo botão de envio quando necessário.

O código mantém a proibição de editar conferências em `save()`. O serviço altera estritamente os três metadados de acesso e registra o evento correspondente na mesma transação.

A migration não foi aplicada ao Neon nem a qualquer banco operacional. Sua correspondência com os modelos e suas operações de ida e volta foram verificadas em PostgreSQL descartável. O executor normal de migrations permanece bloqueado pelo launcher de testes.

## Arquivos alterados ou acrescentados

- `locacoes/models.py`: três metadados de acesso no comprovante.
- `locacoes/migrations/0018_comprovante_cliente.py`: migration necessária.
- `locacoes/customer_receipts.py`: emissão, renovação, revogação e consulta restrita.
- `locacoes/urls.py`: rotas próprias de compartilhamento e leitura.
- `locacoes/views.py`: mensagem do cliente, contexto do cartão e apresentação após confirmação; retirada do envio antigo a funcionários no comprovante.
- `sistema/acesso.py`: registro das duas rotas novas que validam sua própria autorização; regras operacionais mantidas.
- `locacoes/templates/locacoes/checklist_entrega_cliente.html`: apresentação personalizada.
- `locacoes/templates/locacoes/checklist_recolhimento_cliente.html`: cartão, marcações, envio ao cliente e controle de acesso.
- `locacoes/templates/locacoes/comprovante_cliente_acesso.html`: página inicial sem dados.
- `static/locacoes/comprovante-cliente.js`: consulta usando fragmento e corpo POST.
- `static/locacoes/comprovante-envio-cliente.js`: envio explícito, tratamento de janela bloqueada e revogação.
- `offline/test_customer_receipts.py`: cobertura nova, migration e navegador.
- `offline/test_pickup_checklist.py`: expectativas do novo envio e helper de navegador.
- `offline/test_pickup_links.py`: compartilhamento após confirmação delegada e preservação do acesso do funcionário.
- `offline/test_pickup_recovery.py`: novo botão no cenário de recuperação.
- `offline/test_pickup_session.py`: consulta anônima pelo link próprio e origem local nos testes.
- `offline/test_pickup_send.py`: obtenção de CSRF em rota permitida ao usuário de teste.
- Este relatório.

## Validação

Executor existente `offline.validate_pickup_postgresql`, com cluster temporário exclusivo, loopback, identidade do diretório e porta conferidas, conexões fora do cluster recusadas e encerramento ao final. Não foram enviados WhatsApps reais.

Resultado da regressão final relacionada: **73 testes aprovados em 56,031 segundos**, com o cluster encerrado. Um teste complementar de privacidade confirmou que mensagens internas de sessão nunca são renderizadas na consulta pública (**1 aprovado em 0,274 segundo**, outro cluster encerrado). Total: **74 verificações aprovadas**. `git diff --check` aprovado.

Cobertura: entrega completa pela rota existente e mensagem ao cliente; recolhimento completo/parcial, com e sem avarias; perdas e descartes; ausência de telefone; token válido, adulterado, expirado, revogado e renovado; isolamento entre dois comprovantes de locações distintas; bloqueios de escrita e de escalada de autorização; CSRF e HTTPS; preservação de estoque, financeiro, campos da conferência e autorizações de funcionários; rollback da emissão; repetição e concorrência da confirmação; recuperação de sessão; consistência e reversibilidade da migration.

Chrome headless em 390 e 1366 px: leitura sem login operacional, ausência de transbordamento horizontal e recursos externos, mensagem personalizada, janela bloqueada, renovação e revogação pela interface. Capturas fictícias locais em `tmp_whatsapp_visual_comprovante/mobile.png` e `desktop.png`, pasta ignorada pelo Git. Inspeção visual do cartão mobile realizada.

A tentativa inicial da suíte ampliada percorreu também vendas, detalhes e painel lateral. Esses testes usam um operador sem permissões para essas páginas: as respostas 403 impedem inicializar `salesDraftUI` ou abrir o detalhe. A execução direcionada verifica explicitamente que essas permissões continuam negadas. Os fluxos gerais dessas páginas não são declarados validados por esta melhoria; seu código não foi ampliado para contornar os bloqueios. Expectativas antigas de compartilhamento nos testes relacionados foram atualizadas.

## Limitações e cuidados antes de publicar

- O cliente precisa de JavaScript e acesso à internet. Não houve teste em aparelho físico nem envio real pelo aplicativo WhatsApp.
- Quem possuir o link pode ler o comprovante até expiração ou revogação. O link pode permanecer no histórico do navegador e na conversa do WhatsApp, como esperado para esse tipo de acesso.
- Na publicação futura, aplicar a 0018 com autorização própria, disponibilizar os dois scripts e conferir a origem HTTPS configurada em `CHECKLIST_BASE_URL`/`SISTEMA_ONLINE_URL`.
- A arquitetura evita o segredo nos logs HTTP de URL. A infraestrutura de hospedagem não foi alterada nem inspecionada nesta etapa: eventual captura de corpos de requisição/resposta por proxy ou APM deve permanecer desativada nesses endpoints antes da publicação. Não há analytics ou captura de corpo configurados no código dessas páginas.
- O aviso de namespace `estoque` duplicado já existe na base e permanece fora deste escopo.
