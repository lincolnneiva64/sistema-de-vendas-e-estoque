# Comparação aritmética e referência temporal da contagem

Base: `b2dfaf8c9f6afab2456eaba08f74319aa326ed38`.
Worktree nova: `.worktrees/comparacao-contagem-b2dfaf8`, HEAD destacado.
Nenhuma alteração de outra worktree foi incorporada.

## Mapeamento anterior à implementação

- `views.linhas_revisao`: carrega OKs, recibos e evidências e obtém a projeção
  por `marco_fisico.comprovar`/`resultante`.
- `painel.resumo_painel`: monta diferença, percentual, estimativa e classificação
  da tabela. Antes, a falta de comprovação temporal impedia também a subtração.
- `services.salvar_contagem` e `contagem_admin.confirmar`: gravam `ok_em`,
  `sequencia_ok`, `esperado` e a confirmação imutável. Antes, reconfirmar a versão
  atual pelo editor administrativo capturava outra referência temporal.
- `services.recontar`: encerra a rodada atual e abre outra, preservando os registros.
- `ajustes.previa_atualizacao`, `atualizar`, `aplicar`, `bloquear`, `avaliar` e
  `marco_fisico.comprovar`: assinam, autorizam, travam e revalidam a aplicação.
  A projeção da tabela nunca é uma autorização desses serviços.

## Comparação exibida

A contagem original do OK, com sua unidade, permanece como valor principal.
O estoque registrado atual também permanece visível. Na mesma unidade, a
diferença aritmética é contagem original menos estoque exibido, mesmo quando
um movimento não comprovado impede a projeção operacional.

Quando existe relação congelada válida entre as unidades, a equivalência usada
na comparação aparece abaixo da contagem. O custo unitário é convertido pela
mesma relação. Em pacotes com avulsos, a conversão para UN reutiliza o total
físico original comprovado, evitando transformar 20 UN em 19,998 UN ao multiplicar
uma projeção de pacote com três casas. A equivalência aproximada em pacotes
recebe ≈. Sem relação comprovada, os dois valores e unidades continuam visíveis,
com indicação da limitação em vez de uma subtração de unidades incompatíveis.

`comparacao_atual` representa os números exibidos. `comparacao_operacional`
conserva separadamente a projeção comprovada e sua diferença. Esta última é
somente informativa; a aplicação segue reavaliando tudo no serviço existente.
O percentual usa diferença exibida / módulo do estoque exibido. Estoque zero
deixa o percentual indefinido. A estimativa usa essa mesma diferença e o custo
congelado na unidade da comparação. Os totais usam as mesmas bases.

Movimentos posteriores, impedimentos temporais e marcos já aplicados identificam
**Comparação com contagem anterior**. Essas linhas ficam neutras e fora das
contagens de falta/sobra física atual. Percentual e custo são identificados como
estimativas da comparação, sem autorizar ajuste. O motivo específico do
impedimento e a projeção operacional, quando disponível, ficam nos detalhes.

Para um marco já aplicado, a consulta reconhece o recibo existente e não exige
reconstruir o trecho anterior ao marco. Exibe a contagem histórica, o estoque
registrado e sua comparação sem inventar uma nova projeção operacional.
Confirmado, Aguardando confirmação e Revisão necessária continuam usando a
classificação publicada, com prioridade do recibo aplicado. Nenhum impedido sem
recibo passa a confirmado por causa da subtração exibida.

## Correção anterior versus nova contagem

O editor existente oferece duas intenções:

- **Corrigir contagem anterior**: preserva horário, sequência e estoque de
  referência daquele OK. Uma alteração acrescenta confirmação e auditoria,
  preservando os registros imutáveis anteriores. Os movimentos posteriores
  continuam sendo exigidos; a correção não elimina impedimentos temporais.
- **Nova contagem física feita agora**: limpa os campos para nova digitação e
  reutiliza `services.recontar`, abrindo outra rodada. O OK novo captura a
  referência atual sob lock do produto. A intenção e o vínculo entre as rodadas
  ficam auditados. Reenvios, inclusive após mudança de ciclo, retornam a rodada
  aceita sem criar outra. Falha na confirmação reverte a abertura da rodada.

Salvar os mesmos valores na correção anterior não cria confirmação, não avança
versão e não renova horário, sequência ou estoque de referência. A reconfirmação
idêntica pelo serviço do funcionário também preserva o OK. Um marco já aplicado
continua impedindo correção ou nova rodada nesse processo; seu reenvio não aplica
novamente a diferença. Não foram removidas permissões, CSRF, tokens ou locks.

## Casos direcionados

| Cenário fictício | Apresentação e proteção |
|---|---|
| Sacola 2Kg: estoque 2,000 MIL; contagem anterior 0,500 MIL | −1,500 MIL; −75%; custo da comparação; impedimento preservado |
| Saco Chopp Gde: OK 1,000 MIL; venda 0,300 MIL; estoque 0,700 MIL | contagem original 1,000 MIL; comparação +0,300 MIL sem rotular sobra física atual; projeção operacional 0,700 MIL e diferença operacional zero nos detalhes |
| Saco 1Kg: marco 0,100 MIL; compra +2,000; venda −0,200 | estoque 1,900 MIL; contagem 0,100 MIL; comparação −1,800 MIL; Confirmado, sem reconstrução anterior nem reaplicação |
| Zero contra zero | diferença zero; percentual indefinido; marco zero sem movimento de quantidade |
| Correção anterior após venda | a comparação exibida +0,200 não autoriza +0,200: o serviço aplica apenas a diferença operacional comprovada −0,100 |

Nomes de produtos aparecem somente nesta documentação e na descrição dos casos;
nenhuma regra depende de produto, ID, nome ou conferência específicos.

## Arquivos alterados/criados

- `inventario/painel.py`.
- `inventario/views.py`.
- `inventario/contagem_admin.py`.
- `inventario/services.py` (somente a reconfirmação idêntica em `salvar_contagem`).
- `inventario/templates/inventario/linha_tabela.html`.
- `inventario/templates/inventario/detalhes_tabela.html`.
- `inventario/templates/inventario/detalhe.html`.
- `inventario/templates/inventario/editar_contagem_admin.html`.
- `static/inventario/contagem_admin.js`.
- `inventario/tests_comparacao_contagem.py`.
- `inventario/tests_matematica_tabela.py`.
- `inventario/tests_situacao_apresentacao.py`.
- `inventario/tests_padronizacao_conferencia.py`.
- `inventario/tests_marco_fisico.py` (nova contagem agora declarada explicitamente).
- `docs/conferencia-comparacao-contagem-original.md` (este documento).

## Verificação e limites

35 testes direcionados distintos aprovados, exclusivamente pelo runner
`inventario.validar_confiabilidade_postgresql`: rodada final de 34 testes em
8,305 s e um teste existente de novo corte temporal em 0,338 s. Clusters finais
`inventario-confiabilidade-pg-lpehek59` e `inventario-confiabilidade-pg-1vaek9jq`,
ambos encerrados; host, porta, usuário e diretório temporários verificados.
Não foram executadas migrations nem auditoria geral.

Cobertura: casos obrigatórios, conversão congelada, avulsos, custo e percentual,
preservação dos números sem projeção, correção anterior, nova rodada, reenvio,
tokens obsoletos, mudança de intenção, ciclo finalizado, rollback, duplicidade
concorrente, movimentos posteriores, preservação de confirmações e aplicações
anteriores e separação entre diferença aritmética e diferença operacional.

Chrome com HTML fictício estático: 1366, 390 e 320 px, sete colunas, detalhes
recolhidos e ausência de rolagem horizontal. Capturas locais não rastreadas:
`diagnostics/matematica-tabela/tabela-{1366,390,320}.png`. Não foi realizado atendimento real nem medida de desempenho em
produção. A nova aritmética reutiliza os dados carregados, sem consultas por linha.

### Validação interativa final antes do commit

Executado somente o novo teste
`inventario.tests_comparacao_contagem.ContagemTemporalBrowserTests`, sem repetir
os 35 testes anteriores. Chrome headless, servidor local com requisições reais,
CSRF e sessão administrativa fictícia; PostgreSQL descartável pelo mesmo runner.
Resultado final: **1 teste aprovado em 9,205 s**, com subcenários em 1366 e 390 px.
Cluster `inventario-confiabilidade-pg-16nk5ve8` encerrado ao término.

Verificados no navegador: foco no campo de quantidade, cancelamento de ambas as
intenções sem gravação, reconfirmação sem novo OK, correção com horário e referência
preservados e nova contagem criando outra rodada com novo horário/estoque/sequência
de referência. A seleção de nova contagem limpa os campos e explica a mudança.
A diferença exibida +0,200 MIL permanece uma comparação temporal; a prévia
assinada de aplicação apresenta o saldo 0,700 → 0,600 MIL e diferença operacional
−0,100 MIL. Cancelar essa prévia não aplica nada. Nenhum desses cliques alterou
quantidades de estoque, compras ou vendas, nem mesmo no cenário fictício.

A primeira rodada interativa identificou que o campo de avulsos ausente no
formulário de produto sem avulsos chegava como vazio, enquanto o OK anterior podia
conter zero. Corrigida a igualdade física em `contagem_admin.mesma_contagem`,
compartilhada com `services.salvar_contagem`, para preservar o OK nessa reconfirmação.
Na rodada seguinte, ajustou-se apenas a asserção de separador decimal da prévia
ao ambiente local. A rodada final percorreu ambos os caminhos completos.

`ajustes.py`, `marco_fisico.py`, `confirmacao_estoque.py`, `confirmacao_ajustes.py`,
`encerramento.py` e `conversao.py` permanecem idênticos à base. Verificação AST
confirma que as demais funções de `services.py`, incluindo `recontar`, não mudaram.
`git diff --check` aprovado. Avisos preexistentes: namespace estoque duplicado e
staticfiles não coletados na worktree; não impediram a validação.

Não foram acessados dados de produção, Render ou Neon. Nenhuma compra, venda,
saldo, ajuste ou registro histórico real foi alterado. Configurações operacionais
permanecem intactas. Commit local autorizado separadamente após a validação,
restrito aos 15 arquivos listados. Capturas de diagnostics ficam fora do commit.
Sem push ou deploy.
