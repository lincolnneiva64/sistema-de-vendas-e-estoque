# Gelo Escama: fracionamento em SC e recuperação de conflito

Correção aprovada para commit e push; deploy manual, fora desta etapa.

## Trabalho recuperado e isolamento

A worktree original `.worktrees/correcao-gelo-25d212b`, base
`25d212b019dd92567a6bea86f41bbc38e5c40fb9`, já continha a correção de validação,
assets e testes. Seu conteúdo foi preservado integralmente.

A retomada está em `.worktrees/correcao-gelo-retomada-20261019`, baseada no
último commit da branch remota verificado nesta execução:
`a62b7d0727e90c7f89494b17a6368612b91b47dc`. A integração do template preserva
o envio direto do recolhimento pelo WhatsApp e todas as melhorias posteriores.
Nenhuma outra worktree foi modificada.

## Causa e regra

O adaptador offline usa o serviço oficial de vendas, que chama
`_quantidade_estoque_para_unidade_base` e `_validar_quantidade_produto_unidade`.
A função `_quantidade_valida_para_produto_unidade` aceitava a unidade secundária
configurada, mas restringia frações da unidade principal às embalagens de uma
lista que não incluía SC. Assim, mesmo com fracionamento e conversão válidos,
2,5 SC recebiam a mensagem de quantidade inteira. A validação local de estoque
também precisava acompanhar essa regra.

O cadastro documentado pelos testes da investigação anterior, com indicação de
confirmação pelo proprietário, é: principal SC, secundária BD, fator 8 e
`vende_fracionado=True`. Nesse cadastro, 2,5 SC × 8 = 20 BD inteiros; a quantidade
é permitida. Não foi consultado o cadastro atual de produção nesta retomada.
Se esses campos tiverem sido alterados, o servidor continuará usando o cadastro
vigente; nenhum fator, nome ou ID de produto foi fixado na regra.

A correção aceita a fração em SC somente quando a unidade principal é SC,
o fracionamento está habilitado, a unidade secundária existe, o fator é positivo
e a conversão corresponde a uma quantidade inteira na precisão oficial de
0,001. Exemplos com fator 8: 0,125; 0,25; 0,5 e 2,5 SC são válidos; 0,1; 0,3
e 0,333 SC continuam rejeitados. Quantidades inteiras e regras das outras
unidades permanecem preservadas.

O novo caminho de assets `sack-conversion-v1` evita módulos antigos em cache.
Não limpa IndexedDB, fila, histórico, rascunhos ou armazenamento do navegador.

## Operação original: recuperar sem reescrever ou duplicar

Referência informada: `5adb652d-f50d-4f5f-8ab1-905d3198c220`.
Nenhuma requisição de consulta, confirmação, revisão ou encerramento dessa
operação real foi executada. Não se afirma que ela já foi recuperada.

O servidor conserva o resultado definitivo por UUID. Reenviar a operação
original depois da correção devolve a mesma rejeição; não reexecuta a venda.
A recuperação utiliza a revisão de conflito já existente, com outro UUID
vinculado ao hash da original, preservando o histórico. Não exige alterar o
payload original, apagar a fila ou criar uma venda avulsa.

Após publicação autorizada, no mesmo navegador/perfil que guarda a pendência:

1. Abrir o diagnóstico da operação e escolher **Revisar venda**.
2. Conferir produto 1063, quantidade 2,5 SC, preço R$ 40,00, total R$ 100,00,
   cliente e data 18/10/2026. Preservar pagamento e origem efetivamente
   registrados; os testes usaram caixa fictício, sem inferir a origem real.
3. Escolher **Concluir revisão neste aparelho**. Isso prepara a substituta
   localmente, sem registrar uma venda oficial ainda.
4. Quando o sistema liberar, usar **Sincronizar manualmente** e confirmar.
5. Conferir o vínculo e o número da venda oficial. Em caso de resultado
   desconhecido, consultar/recuperar pelo fluxo existente; não criar outra venda.

A consulta autoritativa bloqueia revisão de original já confirmada, encerrada,
incompatível ou com substituta existente. O bloqueio transacional e a unicidade
existentes impedem que duas revisões concorrentes produzam duas vendas.
Repetições da mesma substituta retornam o recibo, sem novos efeitos comerciais.
Revisões continuam restritas ao transporte offline, como antes; o transporte
online não permite usar esse comando para contornar a recuperação controlada.

## Validação

**71 casos distintos aprovados**, contabilizando o último resultado de cada
caso. Regressão inicial: 63 casos, com uma asserção nova de transporte ajustada
para respeitar o bloqueio legítimo de revisão no endpoint online. Complementos:
9 casos, com um cenário de preço fictício online corrigido e reexecutado
isoladamente (1 caso aprovado). Não houve mudança funcional adicional para
acomodar os testes.

PostgreSQL 17.11 em clusters temporários exclusivos, host 127.0.0.1, porta
aleatória e identidade do diretório validada a cada conexão. Conexões fora dos
clusters e execução de migrations foram bloqueadas. O schema foi criado
diretamente a partir dos modelos de teste. Todos os clusters iniciados foram
encerrados ao final. Nenhum dado comercial foi consultado ou alterado.

Cobertura:

- API online e offline, frações válidas/inválidas em SC, conversão para BD,
  fator vindo do produto, ausência de conversão e fracionamento desabilitado.
- Paridade JavaScript/backend para SC, CX, PCT, DZ, UN, MIL e CT; regressão
  adicional da venda existente em KG e da unidade secundária.
- Chrome real: inclusão de 2,5 SC, finalização offline, envio ao servidor,
  botão de gravação online e tela existente de revisão de conflito.
- Caso fictício 2,5 SC × R$ 40,00: total R$ 100,00; uma venda, um item,
  um recebimento de R$ 100,00 e baixa única de 2,5 SC. Original imutável.
- Perda de resposta depois do commit, consulta do resultado, replay,
  revisão concorrente, concorrência PostgreSQL, auditoria e rollback.
- Autenticação, CSRF, identidade, hash, origem e estado da operação.

Evidências locais: `artifacts/gelo-escama/` contém `regressao.log`,
`complementos.log`, `online-final.log`, metadados dos clusters e `resumo.json`.
As falhas intermediárias de preparação dos testes permanecem nos logs, com
os resultados finais aprovados nos complementos correspondentes.
Há advertências preexistentes de namespace `estoque` duplicado e ausência de
`staticfiles` no ambiente de testes; os navegadores usam o servidor de estáticos
de teste. Não houve teste no perfil real nem envio da operação informada.

## Arquivos preparados

- `estoque/views.py`: validação oficial de SC e orientação de conversão.
- `estoque/templates/estoque/vendas_layout_teste.html`: guarda de inclusão e
  carregamento da versão corrigida dos assets.
- `static/offline/sales-stock.js`: mesma regra na validação local de estoque.
- `static/offline/sales-draft-ui.js`, `sales-drafts.js`: referências versionadas.
- `static/offline/service-worker.js`, `offline/urls.py`: assets versionados.
- `offline/tests_gelo_units.py`, `offline/tests_gelo_recovery_browser.py`,
  `offline/tests_sales_post_trial_browser.py`: cobertura específica e regressão.
- `offline/validate_gelo_postgresql.py`: executor isolado reaproveitado do
  executor PostgreSQL existente, com todas as migrations bloqueadas.
- Este relatório.

O diagnóstico opcional `docs/diagnostico-gelo-somente-leitura.js` e as evidências
geradas permanecem locais, fora do commit. O diagnóstico não foi executado no
navegador real.

Sem alterações de modelos ou migrations. Nenhuma alteração de autenticação,
permissões, locações ou infraestrutura remota. A recuperação real depende de
publicação e ação operacional posterior, que não foram executadas.
