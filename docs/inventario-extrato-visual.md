# Prévia do extrato de estoque

Base direta: `c18d02b6da06d2a1faa3e69572e21aecf982a010`.
Worktree: `.worktrees/conferencia-extrato-c18d02b`, HEAD destacado.

## Apresentação

Uma tabela cronológica substitui os quadros separados de compras, vendas,
remoções/devoluções e observações digitais. Colunas: data e dia da semana em
português, movimento, entrada, saída e saldo após. Compras aparecem em verde,
vendas em azul e divergências da conferência em vermelho, acompanhadas de rótulos.
O saldo tem coluna destacada. No celular, cada linha se reorganiza com os nomes
das colunas, mantendo uma única tabela no HTML e sem rolagem horizontal.

O filtro identifica a compra selecionada, inclusive fora da página de opções,
e preserva os intervalos até a conferência ou até a próxima compra. Compras
continuam paginadas em 30 opções; o extrato é paginado globalmente em 50 registros,
ordenados pelo PostgreSQL antes do LIMIT/OFFSET. Não há combinação de páginas
independentes em memória que possa perder a ordem global. As consultas e o
carregamento permanecem limitados por página, sem consulta por registro.

Links abrem as páginas existentes de compra e venda. Observações digitais com
referência explícita `Compra #N` ou `Venda #N` também permitem abrir o documento;
não há associação presumida por data ou quantidade. Evidências adicionais e a
explicação das limitações começam recolhidas. Ao final, esperado, contado e
diferença usam somente os fatos do OK, identificados como pertencentes à
conferência mesmo ao consultar um intervalo anterior.

## Confiabilidade

A inspeção confirmou as limitações da Etapa 2: `MovimentoObservado` não congela
unidade/fator; `ItemCompra` não congela saldos antes/depois; documentos comerciais
podem mudar; `Venda.data_venda` não tem horário. Isso não impede exibir snapshots
históricos explícitos e validados das vendas. A implementação agora apresenta
saldo antes/depois e unidade gravada, com rótulo **Snapshot registrado**. Não usa
cadastro ou conversão atual para completar dados históricos ausentes. Registros
digitais sem unidade histórica continuam apenas como evidência bruta, sem saldo
físico presumido. Saldo inicial original e totais físicos não comprováveis
continuam **Não disponível**.

Entradas e saídas documentais mantêm a unidade de cada item, sem conversão ou
soma com observações digitais. Fontes são identificadas em cada linha para evitar
interpretar documento e observação como duas alterações somáveis do estoque.
Itens removidos mostram sua quantidade e o estado atual da devolução nos detalhes;
não se presume uma entrada física a partir desse estado. Cancelamento é exibido
como estado atual. As vendas são ordenadas pelo horário de criação do registro,
não pela data comercial editável, respeitando o fuso local. Esse horário não
comprova o momento físico. A seleção comercial dos dias de fronteira continua
explicitamente ressalvada; snapshots posteriores à referência não são certificados.

### Critérios de saldo

- Exige antes, movimento, depois e unidade histórica preenchidos, quantidade
  positiva, saldos não negativos e `antes - movimento = depois`.
- Exige unidade documental igual à unidade do snapshot e quantidade igual ao
  movimento. Conversões atuais, mesmo disponíveis, não são usadas para suprir
  um fator histórico ausente. Aliases só normalizam caixa e espaços.
- Edição do item ou evento ambíguo invalida a certificação. Uma edição de outro
  item só é ignorada se o nome exato estiver independentemente associado a outro
  produto na nota atual ou em seus itens removidos. Nome diferente, isoladamente,
  não é prova. Nomes coincidentes com o alvo não são descartados como outro item.
- Cancelamentos, devoluções, ajustes de venda quitada, múltiplos itens do alvo na
  mesma venda e instantes ambíguos impedem certificar os respectivos snapshots.
- A continuidade compara unidades e saldos, verificando operações intermediárias.
  Uma lacuna não é preenchida. Um snapshot posterior válido pode ser exibido como
  **Referência pontual · sequência interrompida**, sem propagar saldo pela lacuna.
- Uma compra sem snapshot só recebe saldo **Reconstruído e corroborado** se houver
  dois snapshots independentes validados, imediatamente anterior e posterior,
  uma única entrada entre eles na mesma unidade, e igualdade entre a soma e o
  saldo anterior da venda seguinte. Edições, cancelamento, outras operações,
  recebimentos sem data e divergências impedem a reconstrução.
- Movimentos digitais ou manuais intermediários, remoções/devoluções/reversões
  e ajustes interrompem a ponte. Não se presume duplicidade para descartá-los.
  Não se rateia um saldo entre duas compras ou múltiplos itens.
- A reconstrução é uma corroboração documental, não snapshot original do
  recebimento nem certificação de ausência de mudanças não auditadas antes do
  observador. Essa limitação aparece na interface.

As referências anterior/posterior são consultadas no banco mesmo quando fora da
página visível. A validação não depende de estado acumulado no navegador ou das
páginas já visitadas. Consultas em lote atendem a página de 50 registros e suas
âncoras. Até 500 evidências por fonte são examinadas; exceder esse teto impede
certificações dependentes da cobertura, em vez de assumir que o restante inexiste.

## Validação e prévia

Runner: `python -m inventario.validar_confiabilidade_postgresql
inventario.tests_saldos_historicos inventario.tests_historico inventario.tests_painel`
com o Python do venv do projeto.
Somente cluster PostgreSQL descartável local, com identidade verificada e criação
de tabelas de teste sem executor de migrations. Navegador Chrome em 1366, 390 e
320 px. A suíte inclui regressões anteriores de permissões, contagem cega,
paginação das compras, custo ausente, unidades distintas, cancelamentos,
devoluções, intervalos e quantidade constante de consultas. Novos testes cobrem
as quatro fontes na mesma cronologia, ausência de saldos presumidos, paginação
global sem perda/duplicação, datas em português com virada de dia em UTC,
responsividade, cores, comparação final e abertura efetiva de compras/vendas.

Validação anterior do visual: **30 testes aprovados**, cluster exclusivo
`inventario-confiabilidade-pg-bya46r57` encerrado. `git diff --check` aprovado.
Avisos preexistentes: namespace `estoque` duplicado e pasta `staticfiles` ausente
no ambiente de teste; os arquivos estáticos da prévia foram servidos pelo servidor
de testes e suas cores/layout foram verificados no navegador.

Validação final dos saldos: **49 testes aprovados** no cluster descartável
`inventario-confiabilidade-pg-z17vo2s_`, encerrado. Inclui 19 testes específicos de
evidência, além das 30 regressões do extrato/painel, e execução no Chrome em
1366/390/320 px. Cobertura adicional: snapshots completos/incompletos,
inconsistência aritmética, quantidade editada, edições de outros itens comprovadas,
nomes ambíguos, eventos desconhecidos, conversão incompatível, mudança de unidade,
compra sem âncora/saldo original, múltiplas compras sem rateio, ajustes,
cancelamento/devolução, teto de evidências, datas ambíguas, snapshot posterior ao
OK, paginação independente de estado acumulado e consulta do extrato só com SELECT.

Capturas com dados inteiramente fictícios:

- `artifacts/extrato/extrato-1366.png`
- `artifacts/extrato/extrato-390.png`
- `artifacts/extrato/extrato-320.png`

## Escopo

Arquivos: `inventario/extrato.py`, `inventario/historico.py`,
`inventario/templates/inventario/historico.html`, `static/inventario/historico.css`,
`inventario/evidencia_saldos.py`, `inventario/saldos_historicos.py`,
`inventario/tests_historico.py`, `inventario/tests_saldos_historicos.py` e este documento. Nenhuma mudança de modelos,
migrations, configurações, lógica comercial ou autorização. O banco operacional
foi consultado exclusivamente em leitura, conforme a verificação abaixo; a
conferência real nº 1 foi preservada. Ajustes reais de
estoque permanecem desabilitados. Commit e push autorizados após a revisão final;
deploy manual pendente, sem alteração do Auto-Deploy.

## Devoluções de vendas anteriores ao intervalo

Cancelamentos são procurados em todos os itens do produto, independentemente da
data original da venda. A cronologia usa exclusivamente
`estoque_devolvido_cancelamento_em`, com fim exclusivo e uma linha por
venda/produto. A linha permite abrir a venda original. Quantidade, unidade e saldo
da devolução permanecem “Não disponível”: o marcador na venda não preserva um
snapshot imutável do que foi efetivamente devolvido. Não são calculados a partir
das quantidades atuais ou das conversões atuais, nem somados ao observador.

Uma devolução entre duas âncoras interrompe a reconstrução e a continuidade,
mesmo quando os saldos das pontas coincidem. Coincidência de horário com a âncora
também impede presumir a ordem. Marcador de devolução sem data efetiva bloqueia
reconstruções e continuidade e exibe aviso, sem posicionar o evento na data da
venda ou do cancelamento. Data preenchida com marcador inconsistente também
interrompe o trecho. Snapshots válidos posteriores continuam referências
pontuais; nenhum saldo é propagado pela lacuna.

Sete regressões adicionais cobrem esses casos, devolução fora do trecho e
deduplicação por venda/produto com observador concomitante. A prévia no Chrome
inclui devolução de venda anterior, com documento consultável e saldo indisponível
em desktop e mobile.

Validação final após a correção: **56 testes aprovados** (26 de evidências e 30
regressões do extrato/painel), em 21,232 s. Cluster PostgreSQL descartável
`inventario-confiabilidade-pg-9fkz7ctf` encerrado, sem migrations. Chrome validado
em 1366/390/320 px, incluindo a devolução de venda antiga, cinco colunas,
links, cores, ausência de rolagem horizontal e filtros/paginação preservados.

## Verificação real, exclusivamente de leitura

O avaliador `inventario.saldos_historicos.analisar` foi executado sobre registros
reais do produto #1118, compra #116 e conferência nº 1. Conexão reutilizada da
configuração protegida existente, sem gravá-la, copiar credenciais ou gerar
artefatos de conexão. Identidade comparada ao preflight anterior de produção,
TLS ativo e `transaction_read_only=on` desde a conexão. Uma guarda adicional
recusou qualquer SQL que não começasse por SELECT. Foram 21 SELECTs nessa
verificação; a conexão terminou com rollback e fechamento.

O algoritmo retornou dinamicamente:

| Documento | Antes (UN) | Depois (UN) | Origem |
|---|---:|---:|---|
| Compra #116 | 2 | 102 | Reconstruído e corroborado |
| Venda #506 | 102 | 92 | Snapshot registrado |
| Venda #718 | 92 | 88 | Snapshot registrado |
| Venda #759 | 88 | 78 | Snapshot registrado |
| Venda #869 | 78 | 68 | Snapshot registrado |

O OK permaneceu com esperado 68 UN e contado 66 UN. Estes valores são resultado
da consulta real, não constantes usadas pelo aplicativo ou fixtures. Testes e
capturas usam produtos e números sintéticos diferentes. Nenhuma alteração de
produção, estoque, compras, vendas, conferência, credencial ou configuração foi feita.
