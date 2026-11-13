# Frisco — divergência de apresentação após 0c63432

Investigação isolada na base `0c634320894dff72d63041c251878935a825760f`.
As seções iniciais registram a investigação anterior à implementação. A continuação
ao final descreve o comando conjunto autorizado posteriormente. Nenhuma migration
de dados foi criada.

## Origem dos dois estados

`produto_editar` consulta o Produto pelo ID. `ProdutoForm.__init__` captura
`apresentacao_observada` antes da validação do ModelForm. No GET, atualiza a
instância com `refresh_from_db`. O JSON observado não infere embalagem pelo nome
nem utiliza a apresentação dos outros integrantes.

No POST, os campos visíveis usam os valores enviados. A validação do ModelForm
altera sua instância em memória; se o serviço recusar a gravação, a view reapresenta
o mesmo formulário vinculado aos dados enviados. O JSON observado continua com o
estado anterior. Assim, Sim/UN/18 preenchido após recusa não prova persistência.

Reprodução: três produtos sintéticos CX, Não, unidade secundária vazia e fator 0
podem formar e ativar um grupo válido de vendas por CX. O grupo ativo não exige
fracionamento. Em abertura nova, o navegador mostrou Não/vazio/0,00. Não apareceu
Sim/UN/18 espontaneamente. Ao propor Sim/UN/18 e mudar um preço, a mensagem foi:

> Configuração alterada: Venda fracionada: Não → Sim; Unidade fracionada:  → UN; Fator de conversão: 0 → 18. A apresentação define a compatibilidade do grupo ativo. Restaure o campo indicado para reajustar preços, ou revise o vínculo antes de mudar a apresentação. Nenhum dado foi salvo.

O espaço vazio acima corresponde à unidade ausente; o código desta base não
acrescenta o travessão de ausência citado no relato. A divergência semântica e o
bloqueio foram reproduzidos, mas não se afirma identidade literal desse detalhe.

O aviso JavaScript só entra nessa comparação quando algum dos quatro preços de
venda diverge numericamente dos observados. Ele não informa qual preço mudou.
O aviso Não → Sim não é explicado pela antiga vírgula no input numérico: o booleano
observado já é falso. No backend, a assinatura também trata unidade secundária e
fator como vazio/0 quando fracionamento está desativado, mesmo se houver resíduos.
No frontend, por sua vez, o JSON contém os campos crus capturados da instância.

`ProdutoForm.clean`, ao salvar com Não, zera fator e campos fracionados. É uma
origem possível de persistência desse estado. Sem histórico real não se pode
atribuir a ela a origem dos dados do Frisco Laranja. Também não se pode atribuir o
preenchimento Sim/UN/18 real a uma interação específica, autofill ou POST anterior.

## Por que não consegue regularizar individualmente

Há duas barreiras distintas:

1. Template e `salvar_formulario_produto` recusam mudança simultânea de apresentação
   e preços. A comparação faz sentido para os dois estados diferentes reproduzidos.
2. Mesmo sem reajustar preço, `Produto.save` chama `impedir_escrita_direta`, que
   proíbe mudanças estruturais de integrante de grupo ativo. O POST sem reajuste
   foi recusado por Unidade fracionada, sem alteração persistida.

A operação existente `regularizar_grupo` ativa comandos de preços, mas não muda a
apresentação dos integrantes. Não há comando coordenado de regularização de
apresentação. Logo, recomendar simplesmente salvar só a configuração não resolve
esse caso de grupo já ativo. Remover apenas o aviso frontend não resolveria a
barreira backend e poderia enfraquecer a proteção.

## Proposta mínima segura, ainda não implementada

Reutilizar a prévia e a confirmação do gerenciamento de grupo para uma operação
explícita de apresentação, separada de reajustes. Deve ler os integrantes atuais,
exibir persistido/proposto por produto e validar o conjunto final usando
`diagnosticar_grupo(..., validar_precos=False)` e as validações individuais.

Se a proposta de um único integrante já for compatível com os demais, pode salvar
somente esse integrante. Se todos estiverem em CX/Não/vazio/0, transformar apenas
Laranja em CX/Sim/UN/18 é incompatível: deve exigir proposta explícita para os
integrantes que precisam mudar e aplicar tudo na mesma transação. Não deduzir 18
pelo nome e não copiar configurações ou preços silenciosamente.

Usar os locks existentes do catálogo/grupo/produtos, validar versões, escrever
somente os campos de apresentação explicitamente confirmados e registrar evento
com antes/depois. Preservar IDs, vínculos, estoque, custos, preços e histórico
anterior. Se os preços fracionados existentes não atenderem à validação individual,
recusar com motivo específico; não criá-los ou recalculá-los automaticamente.
Manter a proteção de escrita direta fora desse comando transacional restrito.
Uma migration de dados global não é necessária nem está autorizada.

## Evidência e limites

Três testes novos em PostgreSQL 17.11 descartável, 15,388 s, todos aprovados:

- POST recusado conserva Sim/UN/18 no formulário, Não/vazio/0 observado, banco intacto.
- Apresentação sem reajuste é bloqueada nos três sabores sintéticos; vínculos e todos
  os campos persistidos dos produtos permanecem idênticos.
- Navegador real: GET não deriva fracionamento, proposta reproduz o aviso e não grava.

Cluster `inventario-disposable-pg-x6zjx9bc`, porta loopback 53261, encerrado; arquivo
`validation.json` confirma `failures: 0` e `state: stopped`. O runner terminou com
erro ao imprimir a seta Unicode no terminal CP1252 depois dos testes e do shutdown;
o log UTF-8 foi lido diretamente. A tentativa inicial no sandbox não conseguiu
iniciar o servidor; a execução local autorizada usou outro cluster novo.

Nenhum teste anterior foi repetido. Nenhum banco real, Render ou Neon foi acessado.
Maracujá e Morango reais não foram consultados: comprovou-se que a mesma regra os
afeta se tiverem esse estado, não que estejam de fato nesse estado em produção.
O estado relatado pode ser legado divergente da intenção operacional, mas é válido
para venda por CX; não comprova corrupção do banco. Nenhuma correção funcional,
commit, push, deploy ou migration de dados foi realizada.

## Continuação — alteração conjunta implementada

Na mesma worktree, preservando o diagnóstico e seus testes, foi implementado um
comando explícito de apresentação no endpoint existente de grupos. A edição do
produto detecta apresentação diferente da observada e, ao salvar, abre uma prévia
com os valores anteriores e propostos de **todos** os integrantes. O modal reutiliza
o estilo e o comportamento de confirmação existentes, com a pergunta solicitada,
**Sim, atualizar todos** e **Cancelar**. A confirmação de apresentação acontece
antes dos handlers de reajuste, em captura do submit.

A mensagem de escopo explica que somente apresentação será salva. Outras edições
do formulário, inclusive preços digitados, não são enviadas a esse comando. Depois
da confirmação, o cadastro é reaberto por GET, com mensagem de sucesso do servidor.
Cancelar conserva os registros e informa que os valores preenchidos são proposta
não salva. Falhas não geram reenvio automático; sem confirmação de resposta, o
usuário recebe orientação para reabrir e conferir os valores persistidos.

### Garantias e limites de segurança

- Reutilizados `serializar_grupos`, locks do catálogo/grupo/produtos,
  `diagnosticar_grupo`, `validar_precos_individuais` e a tabela existente de eventos.
  A proteção de escrita individual em `Produto.save`/QuerySet continua intacta.
- Prévia assinada, vinculada ao operador, proposta, integrantes, versão do grupo e
  hash completo de cada cadastro. Expira em dez minutos. Aplicação exige confirmação
  explícita e revalida a proposta sob os locks; mudança posterior ou reenvio antigo
  é recusado. Mantidas autorização de alteração de produto e proteção CSRF.
- Escrita restrita aos campos de apresentação mostrados, derivação explícita da
  unidade principal a partir da compra e timestamp. Não passa por `Produto.save`,
  para não disparar normalizações comerciais, somente pelo comando validado e
  auditado. Não grava formulário inteiro. A versão do grupo avança para invalidar
  prévias antigas. Evento `apresentacao_grupo` guarda antes/depois, integrantes e
  operador, sem alterações de preço; eventos anteriores não são modificados.
- Preços, custos, percentuais persistidos, saldos, mínimos, autoria de preços e
  vínculos são preservados. Nesta entrega inicial, o cálculo de custo fracionado no
  editor foi desativado para produtos vinculados. Essa proteção visual excessiva
  foi corrigida posteriormente, conforme a seção de custo fracionado ao final;
  a restrição de gravação do comando conjunto permanece a mesma.
- Primeira ativação de fracionamento com base CX preservada pode conservar saldo
  4 CX e mínimo existentes. O estoque equivalente de 72 UN é apenas a exibição da
  conversão explicitamente confirmada; o saldo gravado continua 4 CX.
- Troca de unidade base/principal com saldo ou mínimo é bloqueada. Mudança de
  conversão fracionada já ativa também é bloqueada com saldo/mínimo. Havendo
  histórico, a validação lê as relações de produtos por lote, sem alterá-las:
  mudança de base ou conversão já usada exige revisão; ativação inicial recusa
  registros com unidade diferente da base ou unidade antiga desconhecida.
  Histórico explícito em CX pode permanecer intacto ao ativar CX/18 UN.
- Unidade principal legada divergente não é corrigida silenciosamente com saldo.
  Não há fallback de fator 1. Unidade deve existir nas opções reconhecidas do
  cadastro; códigos legítimos com números continuam aceitos.
- Preço fracionado persistido incompatível com a validação individual (por exemplo,
  abaixo do custo) bloqueia o conjunto com nome e motivo. O comando não inventa
  preços nem aproveita um reajuste digitado para contornar a incompatibilidade.
  Grupos pendentes também passam por essa validação antes da operação conjunta;
  não são ativados ou reajustados automaticamente.

### Verificação da implementação

PostgreSQL descartável, settings independentes de `.env`, loopback e identidade do
diretório conferidos pelo runner existente. As migrations **existentes** construíram
somente os bancos descartáveis. Nenhuma migration nova, banco operacional ou Neon.

- Rodada final do comando: **15 testes aprovados em 17,824 s**, cluster
  `inventario-disposable-pg-_si2jgsd`, encerrado. Doze testes de backend e três de
  navegador: confirmação de CX/18 UN nos três, preços distintos preservados,
  reabertura, cancelamento, recusa individual com rollback, falha forçada na segunda
  escrita, assinatura adulterada, versão antiga, reenvio, permissão, CSRF, fator
  vazio/zero, unidade inválida, código UN2 e históricos compatíveis/incompatíveis.
  Também comparados os movimentos observados e sequências de estoque antes/depois.
- Proteção adicional de unidade principal legada divergente: **1 teste aprovado
  em 0,204 s**, cluster `inventario-disposable-pg-hjeffj_q`, encerrado. Com saldo,
  a proposta não normaliza a unidade principal divergente de um integrante.
- Rodada direcionada de navegador: **7 testes aprovados em 30,922 s**, cluster
  `inventario-disposable-pg-j7dbdxln`, encerrado. Inclui os três percursos novos e
  quatro casos anteriores adaptados ao novo comportamento: mudança real bloqueada
  por saldo com restauração sem sair, fator vazio e configuração pendente com
  preços fracionados incompatíveis em desktop/mobile. A recusa não aparenta sucesso.
- Os três testes do diagnóstico foram preservados. O navegador foi adaptado para
  comprovar que a divergência agora abre a prévia conjunta, sem gravar antes de
  confirmar; os dois POSTs individuais continuam recusados. Os três passaram na
  rodada dirigida intermediária.
- Imagens de desktop 1280 e mobile 390 em `amostras/apresentacao-vinculada` foram
  abertas e inspecionadas: prévia com produtos, confirmação/cancelamento acessíveis,
  erro específico com confirmação cinza e desativada, e cadastros persistidos com
  CX/18 UN, saldo 4 e preços individuais. Mobile emulado; não houve aparelho físico.

Na primeira execução, a importação de um utilitário de clique fez o unittest
descobrir também dez testes de compras, que não eram necessários. A importação foi
corrigida para referenciar o módulo. Nove passaram; um falhou ao consultar
`#erroSalvarPreRevisaoCompra` depois de uma navegação, quando o elemento já não
existia. O mesmo teste foi executado isoladamente na worktree anterior sem esta
implementação (`fafce2f`, árvore equivalente à base publicada) e apresentou a
mesma falha: cluster `inventario-disposable-pg-i1wnsgn8`, 6,582 s, encerrado.
Portanto essa falha também existe na base; não se afirma que sua causa interna
foi resolvida. Pendência registrada: revisar a sincronização do teste de resposta
perdida de compras em tarefa própria, sem afrouxar as garantias de duplicidade.
Nenhum arquivo do fluxo de compras foi alterado nesta tarefa.

### Arquivos da entrega

- `estoque/services/apresentacao_vinculada.py`
- `estoque/views_grupos_produtos.py`
- `estoque/templates/estoque/includes/apresentacao_vinculada.html`
- `estoque/templates/estoque/cadastrar_produto.html`
- `estoque/tests_apresentacao_vinculada.py`
- `estoque/tests_frisco_divergencia.py` (diagnóstico preservado e atualizado)
- `estoque/tests_frisco_fluxo_completo.py` (expectativas do fluxo alterado)
- `estoque/tests_precos_fracionados_diagnostico.py` (expectativas do fluxo alterado)
- Este documento.

`amostras/` contém somente evidências locais, fora da entrega de código. Não houve
commit, push, deploy, intervenção na Compra #280 ou alteração de dados reais.
`git diff --check` aprovado; os arquivos novos também foram verificados quanto a
espaços no fim de linha. Espaços antigos do template foram preservados para não
introduzir limpeza fora do escopo.

## Correção pontual — cálculo visual do custo fracionado

Base publicada: `81f6d3e01077e04f99dfe273597b2c44c08fd643`.
Worktree isolada: `.worktrees/custo-fracionado-81f6d3e`.

Causa: `atualizarPrecoCompraFracionado()` retornava imediatamente quando havia
grupo vinculado. Como a mesma função atende à inicialização do formulário, à
edição da compra e à edição do fator, todos esses percursos deixavam de calcular.
O campo mostrava o custo fracionado persistido (por exemplo, zero), em vez do custo
derivado de 11,70 / 18 = 0,65.

Correção funcional de duas linhas: remover o retorno exclusivo de vinculados,
reutilizando o cálculo e o arredondamento existentes. O formulário volta a mostrar
0,65 na abertura com CX/18 UN, recalcula ao editar compra/fator e esvazia o custo
derivado quando o fator está vazio ou zero. Nenhum fallback para fator 1.

O valor calculado é o valor atual do formulário. Abrir ou editar campos não grava
o banco. O salvamento normal do cadastro continua sendo uma ação separada; esta
correção não modifica suas regras. A operação **conjunta de apresentação** continua
enviando exclusivamente apresentação/versão/assinatura/CSRF e gravando apenas os
campos permitidos de apresentação. Não envia nem persiste custos calculados,
preços, quantidades ou mínimos. Seu serviço transacional e a confirmação publicada
não foram alterados.

Os testes de apresentação distinguem agora o custo calculado visível de 0,65 do
custo persistido de zero na fixture; a comparação integral dos registros continua
exigindo que a confirmação conjunta preserve preços, custos, saldos e mínimos.
Novos percursos de navegador desktop/mobile verificam o carregamento, compra 18
com fator 18 (1,00), fator 24 (0,75), fator vazio/zero sem fallback, restauração de
11,70/18 e recusa/cancelamento de uma mudança insegura, sem gravar campos comerciais.

Validação: **7 testes direcionados aprovados em 24,181 s**, PostgreSQL descartável
`inventario-disposable-pg-qrpwn_si`, encerrado pelo runner. Dois percursos novos de
cálculo em navegador, três percursos publicados de confirmação/cancelamento e
incompatibilidade em desktop/mobile, mais preservação/reenvio e rollback forçado
de falha na segunda escrita. Comparados todos os campos persistidos dos produtos,
vínculos, eventos, movimentos observados e mínimos, sem gravação colateral de custo.
As imagens `amostras/custo-fracionado/1280-custo-calculado.png` e
`390-custo-calculado.png` foram abertas e inspecionadas: mostram compra 11,70,
CX/18 UN e custo fracionado 0,65. Mobile emulado, sem aparelho físico.

O serviço de apresentação, seu modal de confirmação, o endpoint de aplicação e o
serviço de preços permanecem idênticos à base. `git diff --check` aprovado.
Permanece o aviso preexistente Django `urls.W005`.

Não houve acesso aos Friscos reais, banco operacional ou Neon. Sem commit, push,
deploy ou migrations novas. Evidências visuais ficam somente em `amostras/`.
