# Frisco 18/18G — bloqueio de cadastro e vínculo

Base: `8df98a4a4269e3da858f98ab90ee97930e9c61a8`.
Worktree isolada: `.worktrees/frisco-vinculo-8df98a4`, HEAD destacado. Sem commit, push, deploy ou acesso a produção.

## Causa reproduzida no editor

`ProdutoForm` observa a configuração e os preços persistidos. `produto_editar` renderiza `cadastrar_produto.html`, e o JavaScript compara esses valores observados com os controles enviados antes de permitir o comando seletivo de preços. O serviço `salvar_formulario_produto` repete as verificações sob bloqueio e transação.

O template montava manualmente campos HTML `type=number` com decimais localizados. No PostgreSQL de teste o fator estava salvo como `18.00`; o HTML saía com `value="18,00"`. O navegador sanitizava esse atributo para valor vazio. O editor organizado então exibia fator visual `0,00`, embora o JSON observado ainda registrasse `18.00`. Além disso, editar o fator visual escrevia `18,00` de volta no controle numérico, esvaziando-o novamente. Assim, a comparação de apresentação podia detectar uma mudança inexistente e exigir salvar a configuração.

Os preços principais à vista/a prazo eram renderizados da mesma forma, podendo ficar vazios no controle mesmo que o banco contivesse `14.70` e `15.50`, prejudicando a gravação e a comparação dos preços. A normalização de submit ocorre tarde demais para recuperar um valor já descartado pelo navegador.

Correção inicial de quatro linhas no template:

- Aplicar o filtro Django `unlocalize` nos atributos numéricos de fator, preço à vista e preço a prazo.
- Sincronizar o fator real com `toFixed(2)` (ponto decimal aceito pelo HTML), mantendo o formato brasileiro no campo visual.

A correção inicial não removeu bloqueios. Não há exceção para Frisco nem normalização de dados históricos. As regras de permissões, autoria, prévias, assinaturas, proteção contra versões antigas e transações foram mantidas. A continuação abaixo melhora as mensagens e a identificação dos candidatos, sem afrouxar essas regras.

## Regra de entrada no grupo

A mensagem **Entrada bloqueada. Confira os problemas apresentados na prévia** é lançada pelo JavaScript quando a resposta de `previa_adicao` contém `bloqueios`. Esse endpoint chama `diagnosticar_grupo(..., validar_precos=False)`: nesse ponto o bloqueio é estrutural, não decorre simplesmente de preços à vista/a prazo diferentes.

O diagnóstico compara unidade principal (`unidade_venda_1` ou compra), unidade fracionada, fator e `vende_fracionado`. Também recusa compra/principal divergentes, conversão incompleta e integrante excluído. A apresentação exibida como apenas CX é produzida quando o payload tem `vende_fracionado=False`; CX / 18 UN é produzido com fracionamento habilitado. Não se infere embalagem pelo nome.

Na reprodução com cinco produtos sintéticos:

- Laranja, Maracujá e Morango: CX/18 UN, fracionados e vinculados.
- Uva: CX, fracionamento desativado; mesmo com valores residuais UN/18, eles não são uma conversão ativa. A prévia aponta diferenças em `unidade_venda_2`, `fator_conversao` e `vende_fracionado`.
- Abacaxi: CX/18 UN; compatível e incluído sem novo salvamento de seu cadastro.
- Selecionar Uva e Abacaxi juntos bloqueia a entrada de ambos, sem vínculo parcial. Após configuração explícita do Uva pelo editor, persistida como CX/18 UN, a prévia passa e ambos podem entrar mediante confirmação, sem copiar preços, custos ou estoque.

Uma configuração preenchida numa resposta de formulário inválido não comprova persistência. Teste específico devolve o formulário com fracionamento preenchido, mas com erro obrigatório de categoria; o banco continua sem fracionamento e a prévia continua bloqueando corretamente.

## Verificação

PostgreSQL descartável criado pelo runner existente `validar_compra_timeout_pg.py`, com loopback, porta e diretório/identidade verificados, settings independentes de `.env` e encerramento no finally. Nenhuma migration criada ou aplicada em banco operacional; migrations existentes construíram somente o banco de teste.

- Execução inicial após corrigir a serialização: **23 testes aprovados em 7,744 s** (`tests_frisco_vinculo` e `InterfacesPrecosVinculadosTests`). Corresponde a 22 testes de backend e um percurso inicial de navegador.
- Ampliação final: **3 testes de navegador aprovados em 11,515 s**, cluster `inventario-disposable-pg-j61h5f2o` encerrado. Editor desktop 1280 e mobile 390 preservam fator 18,00 e preços 14,70/15,50, inclusive após evento de edição do fator com o mesmo valor. Um reajuste explícito do iniciador abre a confirmação seletiva e salva sem etapa redundante; os demais integrantes mantêm seus preços.
- A tela real de grupos foi percorrida pelo navegador: bloqueio de entrada conjunta, leitura das incompatibilidades, retirada do Uva da seleção, confirmação e inclusão do Abacaxi sem salvá-lo novamente. Quantidades, custos e os quatro preços foram comparados antes/depois.
- Backend cobre persistência da correção do Uva e inclusão posterior de ambos, ausência de vínculo parcial, formulário inválido sem persistência e manutenção do bloqueio para mudança real de apresentação junto com preços. A regressão existente cobre versões antigas, autorização e rollback.
- Imagens `amostras/frisco-vinculo/1280-configuracao.png`, `390-configuracao.png` e `previa-bloqueada.png` abertas e inspecionadas. São evidências locais, fora de publicação.
- `git diff --check` aprovado. Aviso Django preexistente `urls.W005` permanece.

## Limitações e estado real

O banco de produção não foi consultado. Não é possível afirmar que os dados reais de Uva, Abacaxi ou dos três integrantes atuais correspondem exatamente às fixtures. A diferença de apresentação informada é coerente com incompatibilidade de fracionamento, mas não substitui a prévia persistida real. Nenhum produto real foi salvo, alterado, vinculado ou desvinculado. A correção não ativa fracionamento automaticamente nem contorna divergências físicas reais. Navegador mobile emulado, sem aparelho físico.

## Continuação — critérios independentes A e B

A worktree e as alterações anteriores foram preservadas. Nenhum dos 22 testes de backend já aprovados na primeira etapa foi repetido sem necessidade. Os novos cenários usam os valores informados: Laranja CX/18 UN, fracionamento Sim, saldo 4 CX = 72 UN, custo 11,70/CX e 0,65/UN, vendas 15,80/16,00 por CX e 1,00/1,10 por UN. Os três integrantes atuais e os candidatos têm preços individuais diferentes para detectar qualquer cópia indevida.

### A — salvar, reabrir e salvar novamente

Causa técnica comprovada: o HTML localizado e a sincronização do fator com vírgula podiam esvaziar controles `number`. Isso fazia os valores enviados divergirem dos observados, acionando o bloqueio conjunto de configuração/preço. Comparações existentes normalizam as unidades e usam números/Decimal: `18`, `18.00` e `18,0` digitado no campo visual representam o mesmo fator depois da serialização corrigida.

Prova: `FriscoFluxoCompletoBrowserTests.test_ab_desktop_salvar_reabrir_e_vincular` e `test_ab_mobile_salvar_reabrir_e_vincular` abrem o Laranja, conferem todos os valores, clicam/tocam no botão Salvar, reabrem, digitam o mesmo fator `18,0`, salvam novamente e reabrem uma terceira vez. Nenhuma confirmação seletiva indevida é exigida. Preços, custo, saldo, conversão, autoria dos preços e vínculos existentes permanecem iguais. Não são criados eventos de reajuste nos dois salvamentos.

Também foi removido o fallback silencioso `fator = 1` no submit. O fator visual vazio permanece vazio no controle enviado; não é convertido para 1 ou 0 pelo sincronizador. O teste de fator vazio confirmou o valor enviado vazio, a rejeição pelo backend e a preservação do registro salvo com fator 18. Não há saneamento automático de registros históricos.

Para uma mudança verdadeira, os avisos frontend/backend agora identificam o campo e os valores anterior/proposto. No grupo ativo, explicam que a apresentação define a compatibilidade e permitem restaurar o campo para reajustar, ou revisar o vínculo para mudar a apresentação. Não dão a instrução genérica e potencialmente inútil de sair/reabrir. Produtos de grupo ainda pendente recebem orientação compatível com esse estado.

`test_mudanca_real_informa_campo_e_permite_restaurar_sem_reabrir` passou: fator 18 → 20 junto com preço gera o motivo específico, não grava nada, e restaurar 18 permite abrir a confirmação seletiva no mesmo formulário. O bloqueio real foi preservado; ele não foi simplesmente ocultado.

### B — incluir Uva/Abacaxi e comprovar a persistência

No serviço, candidatos verdadeiramente compatíveis já eram aceitos. O bloqueio de uma seleção conjunta vinha da comparação estrutural de todos os escolhidos; a interface apresentava um erro genérico para o conjunto e não separava quem poderia entrar. A configuração incorreta/ausente após o formulário é uma origem possível de divergência persistida; não se presume que todo Uva esteja sem fracionamento.

A prévia agora reutiliza `diagnosticar_grupo` para cada candidato contra os integrantes atuais e devolve `avaliacao_adicao`: nome, compatibilidade e motivos específicos. A tela mostra, por exemplo, Uva e Abacaxi como **Pode ser incluído**, e o terceiro candidato como **Impedido. Fator de conversão: 12. Grupo: 18.** A seleção não é alterada automaticamente. Nenhum integrante é incluído parcialmente ou silenciosamente: o operador desmarca o impedido e confirma a nova prévia dos compatíveis.

Os mesmos dois testes A+B continuam até o grupo. Uva e Abacaxi estão efetivamente persistidos com CX/18 UN e são incluídos mediante confirmação. Depois há uma nova navegação/recarregamento da página e uma nova consulta do grupo: aparecem cinco integrantes, com Uva e Abacaxi, preservando os três anteriores. Os preços distintos de cada produto, os saldos, as conversões e a autoria continuam idênticos ao estado anterior à inclusão. Há somente o evento de entrada dos integrantes; suas alterações de preços estão vazias.

O candidato de fator 12 permanece fora do grupo. Uma tentativa de adicioná-lo diretamente pelo endpoint também é recusada, sem gravação parcial.

### Verificação da continuação

- Seis testes de backend de `FriscoDiagnosticoTests` passaram nesta continuação, incluindo rollback da seleção incompatível e a mensagem específica do fator.
- Percursos A+B com os valores exatos, desktop 1280 e mobile 390: dois testes aprovados em 15,595 s no cluster descartável `inventario-disposable-pg-b21qkzh8`, encerrado.
- Aviso de mudança real/restauração sem reabrir: um teste aprovado em 3,740 s no cluster `inventario-disposable-pg-kt4f8ecj`, encerrado.
- Checagem final com clique/toque real no botão Salvar: dois percursos A+B e dois casos adicionais (mudança real/restauração e fator vazio), **4 testes aprovados em 21,784 s**, cluster `inventario-disposable-pg-jgc7r7n9` encerrado. Os 22 testes anteriores não foram repetidos em bloco.
- Seis imagens em `amostras/frisco-completo/` foram abertas e inspecionadas: dados após os dois salvamentos, candidatos permitidos/impedido e cinco vínculos após recarregar, em desktop e mobile. São apenas evidências locais.
- A asserção de um teste existente de fracionamento foi atualizada da mensagem genérica antiga para a nova mensagem específica; as regras verificadas por ele continuam as mesmas.

Os critérios A e B foram comprovados em dados sintéticos. Não houve consulta ao Neon, intervenção nos Friscos reais, alteração da Compra #280, financeiro ou conferências. Nenhuma migration, commit, push ou deploy. O estado real dos cadastros continua não auditado por imposição desse limite, e não se afirma que os produtos reais já foram vinculados. Mobile foi emulado, sem aparelho físico. `git diff --check` aprovado; permanece apenas o aviso Django preexistente `urls.W005`.

## Arquivos

- `estoque/templates/estoque/cadastrar_produto.html`
- `estoque/templates/estoque/grupos_produtos.html`
- `estoque/services/precos_vinculados.py` (somente mensagens, sem alteração das condições de bloqueio)
- `estoque/views_grupos_produtos.py` (prévia individual, sem alterar o comando de inclusão)
- `estoque/tests_frisco_vinculo.py`
- `estoque/tests_frisco_fluxo_completo.py`
- `estoque/tests_precos_fracionados_diagnostico.py` (asserção da mensagem)
- Este documento.
