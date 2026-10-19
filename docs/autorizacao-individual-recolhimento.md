# Recolhimento por link individual, sem login do funcionário

Verificação final, permissões, aplicação controlada e reversão:
[roteiro de publicação](publicacao-recolhimento-individual.md). A integração final
usa a migration **0017**, após a 0016 já existente na branch remota.

A versão preparada permite ao funcionário abrir e confirmar pelo link individual
sem conta ou login adicional. A autorização é o token específico emitido pelo
responsável autenticado. Não altera a sessão Django nem libera outras funções.
Ainda não foi aplicada em produção.

## Reutilização do projeto

Foi examinado o mecanismo de conferência externa de listas de compras em
`estoque/views.py`: `_liberar_conferencia_externa_lista_fornecedor`,
`_hash_token_conferencia_externa` e `_mensagem_bloqueio_token_conferencia_externa`.
Ele já usa token imprevisível, hash persistido, expiração e invalidação no servidor.
O recibo de pagamento possui token de consulta, que não serve para autorizar escrita.

Foi reutilizado o padrão do checklist externo, sem usar tabelas de compras para
autorizar locações. Os helpers existentes são específicos da lista de fornecedor,
não uma infraestrutura genérica de delegação. O novo módulo pequeno segue esse
padrão e aproveita o formulário, a validação física, as transações e o registro de
recolhimento existentes. Não foi criado outro sistema de login.

## Fluxo final

1. Na ação de envio do recolhimento, o responsável abre a tela de autorização.
   Nenhuma autorização é criada no GET.
2. Com sua conta administrativa habitual, seleciona um funcionário habilitado e
   faz POST em “Gerar link individual”. Precisa das três permissões operacionais
   existentes e da permissão específica `locacoes.autorizar_recolhimento`.
3. O servidor gera 32 bytes aleatórios criptográficos (`secrets.token_urlsafe(32)`),
   armazena somente SHA-256 e vincula a autorização à tarefa, funcionário, emissor,
   prazo de 24 horas e eventual conferência produzida. Outros links da tarefa são
   revogados na mesma transação. O botão abre o WhatsApp com o link individual.
4. O funcionário abre `/locacoes/recolhimento-individual/<token>/`, sem login.
   O nome do funcionário está fixado na autorização; alterar o campo no POST é recusado.
5. GET e POST verificam no servidor o hash, a validade, a revogação, o funcionário
   ainda habilitado e o estado da tarefa. O POST continua exigindo CSRF Django.
6. Confirmar usa o mesmo formulário e registro físico existentes. Transação e
   bloqueio da tarefa serializam confirmação, emissão e revogação. A autorização
   é consumida por uma única conferência; reenvios devolvem o comprovante existente.
   O token assinado do estado do formulário também continua obrigatório.
7. O sucesso abre o comprovante de consulta com aviso claro de registro. O botão
   WhatsApp compartilha apenas o comprovante, sem o token operacional.

Em recolhimento parcial, o mesmo link não registra silenciosamente o restante:
o responsável emite outro link para a próxima conferência. Renovar um acesso
expirado preserva os registros anteriores. Se o recolhimento já terminou, a tela
oferece o comprovante existente e não emite nova autorização de escrita.

## Segurança e auditoria

- A posse do link individual é a autorização delegada, limitada à tarefa e ao
  funcionário selecionado. Deve ser enviado somente a ele; o link não prova a
  identidade física de quem segura o celular e não impede encaminhamento voluntário.
- Emissão e revogação exigem autenticação administrativa, permissão específica e
  CSRF. Uma conta com apenas permissões de confirmar não pode delegar.
- Não há `csrf_exempt`, usuário autenticado artificialmente, senha fixa, conta
  compartilhada ou concessão de permissão geral na sessão do funcionário.
- Link inválido, expirado ou revogado apresenta uma explicação e orienta pedir outro
  ao depósito, sem encaminhar ao login offline.
- O comprovante do cliente permanece separado. POST anônimo nele é proibido e seus
  controles administrativos ficam ocultos para quem não tem as permissões normais.
- Emissão, substituição, revogação e confirmação ficam correlacionadas por tarefa,
  funcionário, autorização e usuário emissor. Eventos não incluem o token secreto.
- HTTPS é obrigatório na emissão fora de desenvolvimento local. Páginas com token
  usam `no-store`, `noindex` e `Referrer-Policy: same-origin`: não enviam o segredo
  como referrer a sites externos e conservam o Origin necessário ao CSRF no POST
  nativo do Chrome. O fluxo delegado não carrega nem grava snapshots offline.
- Antes de publicar, os logs do servidor/proxy devem ocultar tokens do caminho
  `/locacoes/recolhimento-individual/.../`. O token é credencial restrita de tarefa.

## Validação e implantação pendente

Usado somente PostgreSQL descartável com schema criado diretamente dos modelos.
O executor de migrations permanece bloqueado no launcher. A migration preparada foi
comparada ao estado dos modelos pelo loader, sem aplicar operações.
Chrome foi executado com perfil novo, viewport mobile, Android user agent e toque
nos cenários do funcionário; não foi utilizado aparelho Android físico.

Comando da validação direcionada:

```powershell
& ../../venv/Scripts/python.exe -m offline.validate_pickup_postgresql offline.test_pickup_links offline.test_pickup_recovery offline.test_pickup_session.PickupSessionTests
```

As evidências da execução final ficam em `diagnostics/pickup-session/tests.log` e
`diagnostics/pickup-session/validation.json`; incluem o número de testes e a
confirmação do encerramento do cluster. **Resultado final: 33 testes aprovados em
PostgreSQL 17.11, em 20,742 segundos; cluster encerrado; `git diff --check` aprovado.**
Os casos novos cobrem emissão e revogação
por formulário nativo administrativo; funcionário em navegador sem login informando
10 mesas e 40 cadeiras fictícias; confirmação dupla e simultânea; compartilhamento
do comprovante sem segredo; CSRF; expiração/revogação depois de abrir o formulário;
troca indevida do funcionário; permissão específica; HTTPS; uso único parcial e
renovação preservando a conferência. O destino e texto do WhatsApp são verificados;
não é enviada mensagem real ao cliente ou funcionário.

Para uso real ainda são necessários revisão/autorização, aplicação controlada da
migration `locacoes.0017_autorizacao_recolhimento`, concessão da permissão específica
ao responsável pelo processo administrativo normal e publicação da versão.
**Nenhuma dessas ações foi executada.** Não se deve publicar o código que consulta
a nova tabela antes de preparar o schema autorizado. Os links antigos sem token
não passam a autorizar gravação: precisam ser substituídos pelo responsável.

Não houve alteração da locação 90, repetição de operação real, criação de conta ou
senha operacional, migrations em produção ou deploy. Commit e push foram autorizados
pelo usuário na etapa final; o roteiro vinculado registra essa verificação e os testes.

## Arquivos da autorização individual

| Arquivo | Alteração |
| --- | --- |
| `locacoes/models.py` | Registro persistente e permissão específica da autorização |
| `locacoes/migrations/0017_autorizacao_recolhimento.py` | Schema preparado; não aplicado |
| `locacoes/pickup_links.py` | Emissão, revogação e validação do link no servidor |
| `locacoes/urls.py` | Rotas administrativa e individual separadas |
| `locacoes/views.py` | Integração com a confirmação existente, auditoria e comprovante protegido contra POST anônimo |
| `locacoes/services.py` | Ação de envio conduz à autorização pelo depósito |
| `locacoes/pickup_security.py` | Falha CSRF contextual do link, sem login inesperado |
| `locacoes/templates/locacoes/recolhimento_autorizar.html` | Seleção, emissão, compartilhamento e revogação pelo responsável |
| `locacoes/templates/locacoes/recolhimento_link_indisponivel.html` | Orientação para link inválido, revogado, expirado ou erro de segurança |
| `locacoes/templates/locacoes/conferencia_recolhimento.html` | Funcionário fixo, confirmação e fluxo sem módulo offline |
| `locacoes/templates/locacoes/checklist_recolhimento_cliente.html` | Retorno de sucesso e consulta compartilhável sem poderes administrativos |
| `offline/test_pickup_links.py` | Testes específicos de autorização individual e Chrome mobile |
| `offline/validate_pickup_postgresql.py` | Inclusão dos novos testes no launcher descartável |

As correções anteriores de sessão, recuperação e idempotência continuam na mesma
worktree isolada; seus arquivos e evidências anteriores constam no relatório do incidente.
