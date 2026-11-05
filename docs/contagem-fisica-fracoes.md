# Contagem física: frações de pacote e avulsas

Correção local, exclusivamente funcional, preservando as alterações anteriores.
Nenhuma migration necessária ou executada. Nenhum acesso ao Neon ou dados reais.

## Causa e correção

`inventario/conversao.py::converter()` rejeitava toda parcela principal fracionária
quando a unidade-base não era contínua (KG/MIL), mesmo existindo fator físico
congelado. O mobile também recebia `step=1`, e o total físico no JavaScript aceitava
somente dígitos inteiros. Assim, 0,5 PCT era recusado antes ou durante a gravação.

A exceção de avulsas para produtos sem venda fracionada já existia: snapshot com
relação UN válida ou relação física confirmada pelo responsável no fluxo existente.
Ela foi reaproveitada, sem liberar venda por unidade nem criar campos novos.

A validação central agora aceita uma fração principal quando:

- A embalagem é PCT/CX/FD/DZ, há relação física em UN válida na conferência e
  fator inteiro positivo, sem inferência pelo nome ou consulta ao fator atual.
- A parcela principal multiplicada pelo fator é um inteiro exato. Não existe
  tolerância: por exemplo, 0,333 × 6 = 1,998 UN continua inválido.
- As avulsas são inteiras e não negativas. Os dois valores são somados e
  preservados, sem truncar, descartar ou normalizar as parcelas originais.

Exemplos com fator 12:

- `2 PCT + 7 UN = 31 UN`; projeção 2,583 PCT.
- `0,5 PCT + 0 UN = 6 UN`.
- `0,5 PCT + 7 UN = 13 UN`, e não 7 UN nem 31 UN.

O campo de avulsas representa unidades adicionais às que já foram informadas
na parcela principal. Não se devem repetir essas mesmas unidades nos dois campos.
Não é possível detectar automaticamente se o operador contou a mesma mercadoria
duas vezes. Mantidos os rótulos existentes e a exibição explícita da soma no total,
sem criar avisos, campos ou confirmações adicionais de interface.

O atributo funcional `step` passa a 0,001 apenas onde a regra física permite
frações; o servidor continua validando a integridade física. O total existente usa
aritmética inteira BigInt em milésimos, sem aproximação para autorizar uma unidade.
O aviso existente de arredondamento deixa de afirmar que o ajuste está indisponível,
pois essa afirmação contrariava o serviço de aplicação já corrigido anteriormente.

## Preservação da interface e das operações

Sem alteração em `mobile.html`, `produto_contagem.html` ou `contagem.css`.
Campos, ordem, disposição, opção Unidades avulsas e posição/formato do total
permanecem. Na edição administrativa, mudou apenas o atributo `step` do campo
existente. Não foram alteradas regras de venda, compra ou consumo próprio.

Guardar/confirmar continua usando `services.salvar_contagem()` e o fluxo
administrativo existente, com transações, versões, sessão e idempotência.
Contagem confirmada não altera automaticamente estoque. Os snapshots originais,
o total físico, a projeção e o tratamento de resíduos continuam preservados.

Sem relação física congelada, a fração permanece bloqueada: o responsável deve
usar a confirmação de relação já existente. O cadastro comercial não é alterado.

## Arquivos desta etapa

- `inventario/conversao.py`: elegibilidade física e validação exata da fração,
  reutilizando o mesmo conversor que soma principal e avulsas.
- `inventario/views.py`: passo funcional do campo e prévia usando o cadastro
  efetivo da conferência, inclusive relação física confirmada.
- `inventario/templates/inventario/editar_contagem_admin.html`: somente `step`.
- `static/inventario/avulsos_fisicos.js`: cálculo do total existente com frações,
  preservando parcelas e apresentação; correção do aviso funcional obsoleto.
- `inventario/tests.py`: substitui uma expectativa antiga de rejeição de 1,5 PCT
  por 1,001 PCT, que realmente representa fração de UN para o fator da fixture.
- `inventario/tests_contagem_fracoes.py`: novos casos fictícios.
- Este documento. Nenhuma alteração em modelos ou migrations.

## Testes

**51 testes aprovados, em 6,568 s, sem skips**, no PostgreSQL descartável
`inventario-confiabilidade-pg-h03js34a`, encerrado ao terminar. Identidade do servidor
verificada pelo runner por host, porta, usuário, banco e diretório temporário;
`migrations_executed=false`. Resumo em `diagnostics/contagem-fracoes/resultados.json`.

```powershell
venv/Scripts/python.exe -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_contagem_fracoes.ContagemFracoesTests inventario.tests_contagem_fracoes.RelacaoFracoesTests inventario.tests_avulsos_fisicos.ConversaoTests inventario.tests_relacao_fisica.RelacaoTests inventario.tests.SelecaoConversaoTests inventario.tests_projecao_fisica inventario.tests_confiabilidade.ConfiabilidadeHTTPTests
```

Cobertura: fatores 6/12/24; pacotes inteiros, meio/um quarto/um oitavo quando
fisicamente válidos, avulsas, zero, soma explícita, negativos, não finitos, excesso,
frações de UN, fator histórico ausente e cadastro atual diferente. Gravação mobile
HTTP, administração desktop, relação confirmada sem venda avulsa, preservação do
produto inteiro e da auditoria, reenvio sem duplicação e recusa de versão conflitante.
O exemplo de 31 UN também verifica que a conversão comercial continua rejeitando UN.
Incluídas regressões de sessão/CSRF/revogação e de projeção/aplicação concorrente.

As duas falhas da primeira rodada eram da nova fixture: o fator foi gravado como
inteiro Python no snapshot antes de recarregar sua representação Decimal do banco.
Corrigida somente a preparação fictícia; a validação de cadastro não foi relaxada.

`git diff --check` aprovado. Os testes HTTP verificam a estrutura dos formulários
mobile/desktop, o passo e a manutenção dos campos e do total. Não foi criado um
novo ambiente de navegador. A execução do JavaScript no navegador e a inspeção
visual habitual na porta 8000 continuam pendentes: seu banco de destino não foi
confirmado na etapa anterior, e páginas dinâmicas não foram abertas para evitar
acesso involuntário a banco operacional. Não confundir esses testes com uma nova
validação de geometria em navegador.

Para conferir depois de confirmar que a porta 8000 usa somente banco local, usar
o formulário habitual com dados fictícios e verificar 2 + 7, 0,5 + 0 e 0,5 + 7,
mantendo visível a soma. Não executar aplicação de ajustes em dados reais.
Frações que não cabem na precisão de entrada existente (três casas) devem ser
informadas pelo campo de UN; a precisão global não foi alterada.

Sem commit, push, deploy ou publicação. Aprovação funcional/visual do usuário
permanece pendente.
