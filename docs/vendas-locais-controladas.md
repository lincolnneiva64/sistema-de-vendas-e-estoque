# Vendas locais controladas

Base: c149564. Implementação experimental, sem commit ou publicação.

## Funcionamento e segurança

Vendas normal abre sem login Django obrigatório. O funcionário operador é declarado pelo fluxo comercial e validado separadamente do usuário Django autenticado, quando existir. O envio local reutiliza OperacaoSincronizacao, UUID, hash, recibo, transações e o serviço comercial existente; não retorna silenciosamente ao envio legado.

A autorização técnica local exige habilitação explícita pelo servidor de desenvolvimento em loopback. DEBUG=True isoladamente não habilita a modalidade. Render permanece bloqueado por suas variáveis de ambiente. A validação verifica endereço do servidor, Host, origem, CSRF e rejeita cabeçalhos de encaminhamento. Isso pressupõe uma estação confiável: pessoas com acesso ao computador local podem operar a venda e declarar o funcionário; essa declaração não equivale a uma autenticação pessoal.

O shell offline conserva suas exigências de autenticação. Operações antigas não são migradas nem reassociadas. Ausência ou expiração da sessão Django não muda automaticamente a identidade dos rascunhos locais; trocas reais de usuário e ambiente continuam protegidas. A identidade local usa proprietário técnico assinado, ambiente e montagem independente por aba.

O comando é persistido antes do envio. Perda de resposta preserva UUID e conteúdo para consulta/repetição idempotente. Web Locks e controles de propriedade evitam sobrescrita entre abas. Recibos confirmados não podem regredir por resposta tardia.

## Correções de interface

O adaptador observa a tabela de itens confirmados e sales-draft-change no document. Enter no preço e Adicionar Item persistem o estado confirmado. O indicador salvo exige conclusão real da transação IndexedDB; pagehide não garante gravação. Rascunhos anteriores e montagens independentes são preservados.

A consulta assíncrona de detalhes do cliente preserva pagamento e vencimento alterados manualmente durante a consulta. Gerações de seleção rejeitam respostas antigas, inclusive A -> B -> A. Os padrões continuam aplicados na seleção inicial. Consumo próprio mantém estoque, CMV e despesa pelo valor da venda, sem financeiro ou conta a receber fictícios.

## Compatibilidade de banco e leitores

Antes de ativar vendas locais, todos os leitores antigos de recibos devem receber compat/receipt-reader-v1.patch. O patch funciona sem campos novos e rejeita consultas da modalidade local. Sem essa ponte, um leitor antigo pode revelar um recibo local associado ao mesmo usuário Django e identificadores conhecidos. Não habilitar a modalidade enquanto houver leitores antigos sem proteção.

0004_local_sale_principal adiciona campos com padrões permanentes no PostgreSQL: origin='authenticated', local_owner='' e declared_operator=''. O modelo usa os mesmos default e db_default. Assim, INSERTs de código antigo que omitem os campos continuam válidos. O actor passa a aceitar NULL estruturalmente, mas a restrição exige actor para operações autenticadas; operações locais exigem tipo venda_local, proprietário hexadecimal de 64 caracteres e operador declarado não vazio.

No PostgreSQL, 0004 instala a restrição NOT VALID: novas gravações já são verificadas, sem varrer imediatamente o histórico. 0005_validate_local_sale_principal valida os registros históricos em transação separada. SQLite aplica a restrição durante 0004 e não precisa de validação posterior específica.

As migrations limitam a espera de locks a dois segundos. A expansão ainda precisa de locks DDL breves; não há promessa de latência zero. Se o banco estiver ocupado, a migration aborta atomicamente e deve ser reagendada após análise, sem remover proteções. Não modificar registros reais para contornar uma falha de validação.

## Ordem de implantação proposta

1. Aprovar e distribuir a ponte de leitura a todos os servidores antigos; manter vendas locais desabilitadas.
2. Verificar leitores ativos e versões Django/PostgreSQL, backup e condições de locks.
3. Aplicar 0004 e verificar os três padrões permanentes e a restrição instalada.
4. Aplicar 0005 e verificar a validação histórica.
5. Distribuir código/assets atuais e ativar explicitamente apenas nas estações locais autorizadas. Render permanece desabilitado.
6. Validar operacionalmente notebook e depósito, preservando seus dados de navegador.

Rollback operacional: desabilitar a modalidade local e preservar o esquema expandido e a ponte de leitura. Não voltar a leitores antigos sem proteção nem remover colunas com operações locais existentes.

## Validação

Validação final concluída: 194 testes aprovados (140 PostgreSQL e 54 Chrome). Foram preservados 164 resultados já concluídos e retomados somente os 30 casos interrompidos; estes passaram em 246,168 segundos. Cluster temporário encerrado, sem falhas.

As versões reais c149564, 4785a58 e atual são executadas contra o mesmo PostgreSQL descartável. Os testes cobrem gravação antiga após expansão, conexão antiga já aberta, padrões persistentes, restrições antes/depois da validação, isolamento de recibos e controle negativo do leitor sem patch, UUID concorrente/divergente, perda de resposta, último estoque e rollback integral. Chrome usa perfis descartáveis.

Nenhuma migration ou gravação de teste deve atingir Neon. Nenhum cookie, IndexedDB ou rascunho habitual deve ser limpo.
