# Princípios permanentes do sistema de vendas e estoque

Aplicam-se a todo o repositório. Antes de alterar uma área, consulte sua documentação e preserve as regras de negócio e os cuidados existentes. Planos e comandos documentados não constituem autorização para executá-los.

1. **Desempenho desde a concepção.** Trate abertura, pesquisa, navegação e gravação rápidas como requisitos. Evite consultas desnecessárias, carregamentos excessivos e processamento redundante. Investigue gargalos com evidências; quando necessário, meça antes e depois no mesmo cenário.

2. **Intuitividade.** Projete para funcionários sem familiaridade técnica. Prefira interfaces compreensíveis sem explicações e linguagem simples, familiar ao trabalho do depósito.

3. **Menos etapas e digitação.** Reutilize dados que o sistema já possui e automatize operações previsíveis, respeitando escolhas explícitas do usuário. Elimine confirmações repetidas e escolhas técnicas desnecessárias.

**4. Ergonomia operacional.** Não presumir que os hábitos do programador são os hábitos do usuário. Priorizar o fluxo real de trabalho, inclusive a posição das mãos, a sequência de digitação, o número de movimentos e as interrupções desnecessárias. Considerar teclado, teclado numérico, Enter, Tab, mouse e toque. Verificar foco e ordem de navegação. Preservar os padrões existentes quando forem úteis, mas permitir alternativas mais naturais.

5. **Segurança sem burocracia.** Preserve integridade transacional, auditoria, validações, autenticação, permissões, CSRF e proteção contra duplicidade, inclusive em reenvios e sincronização offline. Implemente as garantias nos bastidores e exponha apenas decisões necessárias. Não remova bloqueios ou dados para contornar falhas.

6. **Simplicidade primeiro.** Escolha a solução mais simples que resolva corretamente o problema. Não acrescente recursos, opções ou fluxos para hipóteses remotas sem necessidade demonstrada. Evolua gradualmente com base no uso real.

7. **Evitar retrabalho.** Entenda o fluxo completo antes de modificar código. Preserve funcionalidades e layouts aprovados, respeite particularidades de desktop e mobile e reutilize componentes e serviços quando apropriado. Limite as alterações ao escopo solicitado.

8. **Testar a experiência.** Faça verificações proporcionais à mudança para comprovar correção e ausência de regressões. Avalie cliques, digitação, tempo de resposta e clareza em cenários reais de atendimento e depósito, nos dispositivos afetados. Use ambiente e banco de teste isolados; confirme a configuração antes da execução e preserve bancos operacionais e manuais. Informe o que foi verificado e eventuais limitações.

9. **Controle das alterações.** Ideias em discussão não autorizam implementação. Não acesse nem modifique dados de produção sem autorização. Não faça commit, push, migrations ou deploy sem autorização explícita. Antes de push autorizado, confirme os gatilhos de deploy. Revise o estado local e preserve alterações preexistentes, arquivos não rastreados, segredos e backups; não use reset, clean ou stash para limpar o trabalho. Em integrações, combine os trechos necessários sem substituir arquivos inteiros por `ours`/`theirs`; siga os planos de revisão e integração aplicáveis. Não inclua bancos, credenciais, logs ou artefatos locais em publicações.

**10. Priorizar urgências e eliminar pendências na estabilidade.** Problemas que interrompem ou prejudicam operações essenciais têm prioridade. Durante sua resolução, pendências não urgentes podem ser adiadas e devem ser registradas. Assim que a operação estiver estabilizada, antes de iniciar melhorias de menor prioridade, revisar e resolver as pendências acumuladas, priorizando as que comprometem a manutenção, a confiabilidade e as futuras atualizações. Não deixar problemas conhecidos indefinidamente para depois.

**11. Reutilizar antes de criar.** Antes de implementar uma nova tela, editor, procedimento ou componente, identificar as funcionalidades existentes que já atendem à necessidade. Preferir reutilizá-las ou adaptá-las, preservando os fluxos conhecidos pelos funcionários. Criar uma interface nova somente quando houver necessidade comprovada que não possa ser atendida adequadamente pela existente.
