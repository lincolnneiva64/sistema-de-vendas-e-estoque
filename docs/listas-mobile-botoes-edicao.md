# Botões da edição mobile de listas salvas — 05/11/2026

## Causa reproduzida

O listener de captura de `includes/lista_coleta_mobile.html` chama `pronto()`
antes de permitir os cliques. Para listas salvas com `coleta_mobile={}` ou
`null`, `estado.iniciada` ainda não existe. O listener executava
`preventDefault()` e `stopImmediatePropagation()` para **Editar**, **Peguei** e
**Excluir**, impedindo os handlers já existentes de receber o clique.

Os três casos foram reproduzidos separadamente no navegador antes da correção:
Editar não abria os campos, Peguei não criava o cartão confirmado e Excluir não
abria o modal. Os botões estavam acessíveis pelo teste de `elementFromPoint`;
o bloqueio era lógico. O teste existente que inicia a coleta manualmente passou.

## Fluxo e alteração

- Template principal e handlers: `estoque/templates/estoque/compras_sugestao_fornecedor.html`.
- Cartão com os três botões: `estoque/templates/estoque/includes/lista_fornecedor_produto_card.html`.
- Controle compartilhado corrigido: `estoque/templates/estoque/includes/lista_coleta_mobile.html`.
- GET e POST: `/compras/listas-fornecedor/<pk>/editar/`, view `compras_lista_fornecedor_editar`.
- O POST recebe `acao_coleta_mobile` para iniciar/mover e `lista_payload` para salvar;
  `gerar_compra=1` salva a lista e gera a compra pelo fluxo existente.
- Serviço de estado, versão e concorrência: `estoque/services/coleta_lista.py`.

O primeiro clique em uma das três ações inicia a coleta pelo POST existente com
todos os itens pendentes. Somente após a confirmação do servidor, o mesmo botão
é acionado novamente para executar seu handler original. A escolha manual de
início continua disponível. Uma lista já iniciada não é reiniciada.

Nenhum HTML de layout, CSS, endpoint, modelo ou migration foi alterado.
Permanecem os bloqueios de gravação simultânea, conflito e resposta perdida,
assim como as confirmações de edição e exclusão.

## Validação isolada

Os testes novos reutilizam `estoque/tests_coleta_lista_browser.py` e seu navegador
CDP existente. Usam Edge/Chromium real com toque/mobile emulado a 390 px e com
mouse/viewport desktop de 390 px. Não houve teste em aparelho celular físico.

Fluxo coberto: abrir lista salva sem iniciar manualmente → Editar → Confirmar →
Peguei → Excluir outro produto com confirmação → Salvar alterações → reabrir →
editar novamente → Salvar alterações e Gerar Compra. Conferem quantidade,
preço, unidade e total salvos, exclusão, estado confirmado e compra com os dados
atualizados. Os testes verificam erros JavaScript, ausência de sobreposição dos
botões e preservação do estoque fictício. Há testes separados de Peguei e
Excluir como primeira ação.

O runner cria PostgreSQL descartável em loopback, valida a identidade do cluster
e recusa outras conexões. Não importa settings operacionais nem `.env`.
As migrations existentes são aplicadas exclusivamente ao banco descartável.

O settings isolado original omite `sistema.acesso.AcessoComercialMiddleware`,
presente no settings operacional. Por isso o teste
`test_permissoes_csrf_e_finalizadas` recebe 200 em vez de 403 para um usuário
sem permissão. A falha foi reproduzida também com o include original de HEAD.
Para validar as permissões, a suíte foi reexecutada com o middleware habilitado
somente em memória, sem alterar arquivos de configuração:

```powershell
@'
import sistema.settings_test_compras as isolated
isolated.MIDDLEWARE = [*isolated.MIDDLEWARE, 'sistema.acesso.AcessoComercialMiddleware']
import validar_compra_timeout_pg
raise SystemExit(validar_compra_timeout_pg.main())
'@ | .\venv\Scripts\python.exe - estoque.tests_coleta_lista_browser estoque.tests_coleta_operacoes_mobile estoque.tests_coleta_lista_mobile
```

Resultado da rodada final com middleware: **31 de 32 testes passaram** em
73,582 segundos. O restante, `test_peguei_pendente_bloqueia_salvamentos_e_outro_peguei`,
falhou ao localizar um botão após a rolagem; passou na reexecução isolada com a
mesma configuração e código, em 4,039 segundos. Assim, os 32 testes distintos
estão aprovados pelo resultado mais recente; não houve nova rodada conjunta.
Os quatro testes novos passaram, incluindo ambos os fluxos completos.
Os clusters foram encerrados ao final. O runner mantém os logs no diretório
temporário de cada cluster. Permanece registrada a omissão do middleware no
settings de testes original; ele não foi modificado nesta correção.

Sem acesso ao Neon, alteração de dados reais, commit, push ou deploy.
