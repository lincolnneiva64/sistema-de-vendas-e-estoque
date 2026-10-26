# Regularização administrativa de pendências de entrega

Implementação local; não autoriza consulta ou regularização em produção.
Nenhuma ocorrência real foi consultada. A quantidade real precisa ser apurada,
sem assumir que são exatamente 35. Não há corte por data ou número de venda.

## Funcionamento

As pendências continuam calculadas a partir de `EntregaRotaItem` e
`EntregaChecklistItem`. O encerramento administrativo acrescenta somente
`EventoVenda`, com data, usuário (ID e nome), justificativa, lote e ocorrência.
Não modifica vendas, itens, preços, estoque, financeiro, estados físicos,
checklists ou snapshots. Não é confirmação de entrega física.

A chave contém tipo (item/bloco), rota, bloco e venda. Para item, inclui também
checklist e item da venda. Há duas impressões SHA-256 com funções distintas:

- `versao`: estado completo consultado, incluindo observação, status e timestamps.
  Continua obrigatória e deve corresponder exatamente na execução do manifesto.
- `identidade`: criação dos registros, origem/reentrega e conteúdo físico dos
  checklists (item, produto, quantidade, unidade, carregado/entregue). Não inclui
  observação, status agregado, confirmação/conclusão agregada ou timestamp de
  atualização. Alterações secundárias não reabrem uma regularização ativa.

Somente chave completa e identidade regularizadas saem da lista, do contador e
das sugestões. Nova rota/item, nova criação mesmo com ID reutilizado, ou mudança
do conteúdo físico continuam visíveis. Se o status Cancelada transformar linhas
por item numa linha geral do bloco, esta só é coberta quando todos os checklists
fisicamente pendentes ainda correspondem a itens explicitamente regularizados.
Um único item novo ou revertido impede essa cobertura. Eventos malformados ou
manifestos anteriores sem identidade são rejeitados; devem ser consultados de novo.

A aba Resolvidas apresenta a classificação administrativa e sua justificativa;
os eventos permanecem também no histórico da nota. As consultas não gravam.
Não há modelo novo nem migration. A consulta acrescenta uma leitura em lote
da auditoria, sem uma consulta por pendência.

Execuções exigem usuário ativo com `is_superuser`, justificativa e confirmação
do hash do manifesto. Bloqueios transacionais e revalidação integral impedem
duplicação concorrente e aplicação parcial. A criação de rota revalida IDs sob
os mesmos bloqueios, inclusive se o formulário foi aberto antes da regularização.
Todos os escritores administrativos bloqueiam vendas, itens, blocos e checklists
na mesma ordem e releem os eventos após os bloqueios. No PostgreSQL, exigem
READ COMMITTED; outro isolamento é rejeitado sem gravação. Duas regularizações
ou reversões concorrentes não duplicam eventos. Se a reversão vence uma repetição
do lote antigo, esse lote é rejeitado; se a repetição vence, ela não grava outro
evento e a reversão seguinte encerra sua validade normalmente.

## Procedimento futuro: consulta e seleção, somente após autorização de acesso

No ambiente correto, conferir previamente configuração e identidade do banco.
Não executar migrations. Os comandos abaixo são procedimentos, não autorização.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python manage.py consultar_pendencias_entrega_admin | Out-File -Encoding utf8 manifesto-consulta.json
```

O comando usa transação READ ONLY no PostgreSQL e `query_only` no SQLite.
Sua saída JSON usa escapes Unicode (`ensure_ascii=True`) para atravessar
terminais e redirecionamentos sem substituir acentos ou outros caracteres.
Ao ler o JSON, os nomes originais são restaurados. O hash continua calculado
sobre o corpo sem `hash`, em JSON canônico (`sort_keys=True`, separadores
compactos, `ensure_ascii=False`) codificado em UTF-8. Indentação, escapes Unicode
e BOM não mudam os valores lidos; substituição de caracteres na saída muda.
Não recalcular o hash para aceitar um arquivo divergente: descartar a cópia
corrompida e gerar um novo manifesto com este comando corrigido, conferindo-o
novamente. Os hashes de banco, identidade e versão não foram alterados.
Exporta rota, venda, cliente, produto (ou bloco sem item), quantidade, unidade,
situação, chave completa, identificador e versão. Inclui total real, lote e hash;
o manifesto é vinculado ao banco por impressão da configuração sem credenciais.
Não visita checklists nem cria registros ausentes.

Conferir cada ocorrência com o responsável e separar o conjunto histórico
aprovado. Se houver outras pendências, gerar um manifesto selecionado repetindo
`--identificador` para cada chave aprovada, em vez de editar manualmente o JSON:

```powershell
python manage.py consultar_pendencias_entrega_admin --identificador "CHAVE_1" --identificador "CHAVE_2" | Out-File -Encoding utf8 manifesto-aprovado.json
```

Guardar o manifesto fora do Git em local restrito: contém dados de clientes.
Não aprovar só pela quantidade ou pelos números de venda. Guardar aprovação
com lista explícita e hash; qualquer mudança requer nova consulta e aprovação.

## Simulação e execução futura, após aprovação expressa

```powershell
python manage.py regularizar_pendencias_entrega_admin manifesto-aprovado.json --usuario ID_ADMIN --justificativa "Ocorrências conferidas pelo responsável; resolvidas operacionalmente."
```

Sem `--executar`, somente simula. Conferir resultado, chaves e justificativa.
Após autorização específica para esse manifesto e banco:

```powershell
python manage.py regularizar_pendencias_entrega_admin manifesto-aprovado.json --usuario ID_ADMIN --justificativa "JUSTIFICATIVA_APROVADA" --executar --confirmar-hash HASH_DO_MANIFESTO
```

Guardar o resultado com os IDs dos eventos. Repetir a execução não duplica
eventos. Se qualquer ocorrência mudar ou desaparecer, todo o lote é rejeitado.
Lote anteriormente revertido não pode ser reaplicado: exige novo manifesto.

Depois: consultar novamente, conferir que apenas as chaves aprovadas saíram;
validar contador, aba Resolvidas, histórico e sugestões de rota. O contador só
será zero se não restarem outras ocorrências abertas. Não forçar zero ocultando
novas pendências. Conferir que vendas e dados físicos continuam idênticos.

## Reversão, também mediante autorização

Selecionar IDs dos eventos de regularização, não IDs de checklist:

```powershell
python manage.py reverter_pendencias_entrega_admin ID_EVENTO_1 ID_EVENTO_2 --usuario ID_ADMIN --justificativa "MOTIVO_DA_REVERSAO"
```

Conferir simulação e seu campo `confirmacao`. Para efetivar após aprovação:

```powershell
python manage.py reverter_pendencias_entrega_admin ID_EVENTO_1 ID_EVENTO_2 --usuario ID_ADMIN --justificativa "MOTIVO_DA_REVERSAO" --executar --confirmar-hash HASH_DA_SIMULACAO
```

Acrescenta evento reversor; não apaga o original nem altera a venda. Repetição
não duplica reversões. Uma reversão antiga não desfaz regularização posterior.
A ocorrência volta à lista se ainda atender às regras físicas originais.

## Validação local

Dados fictícios, SQLite temporário e PostgreSQL descartável com schema criado
diretamente; migrations bloqueadas e configurações sem carregar `.env`.
45 testes: 39 passam no SQLite (seis exclusivos do PostgreSQL)
e 45 passam no PostgreSQL. Abrangem itens, blocos, parcial/cancelada, lote de
35 com nova ocorrência posterior, idempotência, reversão, manifesto divergente,
rollback integral, permissões, concorrência, IDs antigos, contadores, sugestões,
GET sem gravação e snapshots de preservação comercial/física/financeira.
Incluem os 12 testes existentes da listagem e regressão do erro 500.
Incluem mudanças secundárias em item/bloco, transição para apresentação de bloco
cancelado, nova ocorrência, proteção contra IDs reutilizados e ambas as ordens de
regularização/reversão concorrentes com comprovação de espera pelo bloqueio.
Incluem manifesto com 35 ocorrências serializado em JSON e relido de arquivo,
com/sem escapes Unicode e BOM, além de saída por terminal ASCII com substituição
de caracteres, simulação do arquivo exportado e rejeição de conteúdo adulterado.

Limites: SQLite valida regras funcionais, mas não oferece `SELECT FOR UPDATE`;
a garantia de concorrência foi comprovada em PostgreSQL. Uma reutilização exata
de todas as chaves, criação e conteúdo físico seria indistinguível sem um novo
identificador persistido; novas ocorrências devem usar os registros próprios do
fluxo normal ou reversão explícita. Não há detecção de um ciclo físico que termine
com exatamente o mesmo conteúdo e identidades sem evento intermediário persistido.

Nenhuma alteração em CSS, layout, editor de Vendas, offline ou fila de separação.
Os runners e logs locais ficam em `amostras/`, fora dos arquivos a publicar.
