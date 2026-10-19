# Envio direto do recolhimento pelo painel

Worktree: `.worktrees/recolhimento-whatsapp-direto-20261019`.
Base: `8aba8e6a3dc79c9e3db23801ee1bf46978cfc289`.
Alterações preparadas para revisão; sem commit, push ou deploy.

## Causa e comportamento

O contexto do painel havia substituído o endereço do WhatsApp por `/locacoes/tarefas-operacionais/<id>/autorizar-recolhimento/?funcionario=...`. O botão lateral continuava sendo um link comum: seu clique abria a página administrativa. A confirmação do funcionário por link individual já funcionava; o defeito estava no início do envio.

Agora o botão real do painel faz POST para `locacoes:enviar_recolhimento`, enviando o funcionário selecionado e o CSRF atual. A sessão administrativa e as quatro permissões existentes são verificadas pelo servidor. O destinatário precisa continuar ativo, habilitado para checklist e com WhatsApp cadastrado.

O servidor gera ou reutiliza uma autorização da mesma tabela `AutorizacaoRecolhimento`, vinculada à tarefa e ao funcionário. A resposta contém a mensagem e o link preparados para o WhatsApp; a aba é reservada durante o clique para funcionar também no Chrome mobile. Não há passagem pela página administrativa e nenhum envio real é marcado como concluído apenas por abrir o WhatsApp. A tela diária usa o mesmo envio. Entregas mantêm o comportamento anterior.

O funcionário continua abrindo e confirmando o checklist sem login adicional, usando o mesmo validador de autorização individual existente. Confirmação, comprovante ao cliente, encerramento da locação e remoção das pendências não foram reimplementados.

## Segurança e repetição

- Mesmas permissões administrativas: `locacoes.autorizar_recolhimento`, `locacoes.add_conferenciarecolhimentolocacao`, `locacoes.change_locacao` e `locacoes.change_tarefaoperacionallocacao`. Nenhuma permissão nova foi criada ou concedida automaticamente.
- POST com CSRF obrigatório; GET nunca emite autorização. HTTPS obrigatório fora do desenvolvimento local.
- Bloqueio transacional da tarefa, na mesma ordem usada por emissão, revogação e confirmação. Pedidos simultâneos para o mesmo funcionário reutilizam a mesma autorização, com um único evento de emissão.
- Autorizações válidas são reutilizadas sem estender a validade. Expiração, revogação, funcionário diferente ou autorização já consumida exigem nova emissão, preservando as conferências anteriores. Tarefa concluída não recebe outro link.
- Para reutilizar sem armazenar o token em claro, novos tokens são derivados por HMAC-SHA256 do Django, com domínio próprio e identidade imutável da autorização, usando a chave existente da aplicação. O banco continua guardando somente SHA256 do token; a rota e o validador não mudaram. A emissão administrativa usa o mesmo mecanismo. `SECRET_KEY_FALLBACKS` é respeitado durante a reutilização.
- Links aleatórios emitidos antes desta correção não podem ser recuperados do hash: no primeiro novo envio são substituídos uma vez e o anterior é revogado. Conferências, recolhimentos parciais e comprovantes já registrados permanecem intactos.
- Emissão e revogação mantêm a auditoria existente. Nenhum token ou URL individual é incluído no evento. Logs do Django, servidor, CSRF e Gunicorn ocultam o token da URL, inclusive em erros.
- O comprovante do cliente continua somente leitura e não carrega a autorização operacional do funcionário.
- Cliques repetidos são bloqueados enquanto o envio está em andamento; após falha ou resposta perdida, tentar novamente reutiliza o mesmo link válido. Nenhum POST é repetido automaticamente.
- Sessão expirada mostra explicação no próprio painel. O responsável pode entrar novamente em outra aba e tentar de novo, sem perder o funcionário selecionado; o envio usa o cookie CSRF atual. Não há redirecionamento automático para o login offline.
- Se o navegador bloquear a nova janela, aparece um link para abrir o WhatsApp com a mensagem já preparada. Trocar o funcionário remove esse link anterior para evitar o destinatário errado.
- A proteção de vendas locais continua ativa. Somente os dois controles de envio do recolhimento usam a exceção operacional existente, mantendo autenticação no servidor. A URL do módulo foi atualizada por query string para não usar uma cópia antiga do cache; nenhum rascunho, cache ou armazenamento local é apagado.

## Arquivos alterados

- `locacoes/pickup_links.py`: emissão compartilhada, derivação e reutilização segura; endpoint POST.
- `locacoes/urls.py`: rota do envio.
- `locacoes/services.py`: endereço do novo envio no contexto do painel lateral.
- `locacoes/views.py`: mesmo endereço no contexto da tela diária.
- `estoque/templates/estoque/vendas_layout_teste.html`: integração do botão lateral, seleção do funcionário e carregamento do JavaScript.
- `locacoes/templates/locacoes/includes/checklist_grupo.html`: integração do botão da tela diária.
- `locacoes/templates/locacoes/checklist_operacional.html`: carregamento do JavaScript compartilhado.
- `static/locacoes/recolhimento-envio.js`: envio pelo botão real, retorno visual, bloqueio de repetição, janela do WhatsApp e tratamento das falhas.
- `static/offline/sales.js`: exceção restrita aos dois controles operacionais, sem liberar a montagem da venda.
- `locacoes/pickup_logging.py` e `locacoes/apps.py`: ocultação dos tokens nos logs.
- `offline/test_pickup_send.py`: 15 casos de testes do envio, incluindo navegador e concorrência.
- Este relatório.

Não há alterações de modelos, migrations, regras comerciais, cálculos de estoque ou configuração do Render/Neon. Nenhum registro real, inclusive o recolhimento nº 94, foi consultado ou alterado.

## Validação concluída

Executor: `python -m offline.validate_pickup_postgresql`, com PostgreSQL 17.11 criado em diretório temporário, identidade do cluster validada e conexões restritas ao loopback. Schema de teste preparado diretamente; execução de migrations bloqueada. Clusters encerrados ao final.

**78 casos aprovados**, considerando o último resultado de cada caso: 76 na regressão de envio, links, checklist, recuperação e sessão; dois complementos de logs e integração com venda protegida/tela diária. Foram reexecutados apenas os complementos afetados durante os ajustes finais.

O Chrome headless em 390 e 1366 px percorreu o botão lateral real: selecionar outro funcionário, clicar, preparar a URL do WhatsApp para esse destinatário, repetir o clique sem duplicar, abrir o link em outro perfil sem sessão administrativa, conferir 10 mesas e 40 cadeiras fictícias, confirmar uma única vez, acessar o comprovante, verificar seu compartilhamento ao cliente e a saída da tarefa das pendências.

Também foram cobertos CSRF, permissões, HTTPS, sessão expirada e recuperação com CSRF rotacionado, falha de rede, perda da resposta depois da emissão, popup bloqueado, troca de funcionário, expiração/revogação, links legados, rotação padrão da chave, rollback da emissão se a auditoria falhar, pedidos concorrentes, recolhimento parcial seguido de conclusão e preservação dos registros financeiros e do rascunho de venda.

Nenhuma mensagem foi enviada: os testes interceptam a abertura do WhatsApp e verificam telefone, texto e link. A aplicação externa não é contatada.

Capturas locais: `artifacts/recolhimento-envio/painel-390.png` e `painel-1366.png`. Logs e metadados de validação, com tokens ocultos, ficam na mesma pasta. A advertência de namespace `estoque` duplicado já pertence à base.
