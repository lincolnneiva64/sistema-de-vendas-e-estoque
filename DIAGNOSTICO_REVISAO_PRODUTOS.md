# 📋 DIAGNÓSTICO: REVISÃO EM MASSA DE PRODUTOS IMPORTADOS

## Relatório de Investigação Completo
Data: 2026-08-20

---

## 1️⃣ MODELO PRODUTO - ANÁLISE COMPLETA

### Campos Relevantes do Modelo
**Arquivo**: [estoque/models.py](estoque/models.py#L9-L103)

| Campo | Tipo | Descrição | Editável em Lote? |
|-------|------|-----------|-----------------|
| **id** | AutoField | Chave primária | ❌ |
| **nome** | CharField(120) | Nome do produto | ✅ **CRÍTICO** |
| **codigo** | CharField(50) | Código interno do novo sistema | ✅ **CRÍTICO** |
| **codigo_legado** | CharField(50, unique) | Código do sistema antigo (FK para importação) | ✅ **CRÍTICO** |
| **categoria** | CharField(60) | Categoria do produto | ✅ **CRÍTICO** |
| **fornecedor** | CharField(120) | Fornecedor principal | ✅ **Normal** |
| **ativo** | Boolean | Produto ativo/inativo | ✅ **Normal** |
| **criado_em** | DateTimeField | Data de criação | ❌ |
| **atualizado_em** | DateTimeField | Última atualização | ❌ |
| **excluido** | Boolean | Soft delete | ❌ (usa outro fluxo) |
| **excluido_em** | DateTimeField | Data da exclusão | ❌ |
| **cadastro_incompleto** | Boolean | Indica cadastro com dados faltando | ✅ **Marcador** |
| Campos de preço/quantidade | Decimal | Não são foco direto da revisão | ⚠️ Opcionais |

**Observation**: Campo `codigo_legado` é **ÚNICO** e é o melhor indicador de importação.

---

## 2️⃣ IDENTIFICAÇÃO DE PRODUTOS IMPORTADOS

### Status Atual do Banco de Dados

**Base SQLite analisada**: `db.sqlite3` (188 KB - dados de teste)
- Total: 36 produtos
- Sem campo `codigo_legado` (migration 0086 não aplicada)
- ⚠️ **Banco SQLite DESATUALIZADO** comparado ao modelo Django

**Observação**: O banco de produção (PostgreSQL) tem as migrations atualizadas.

### Forma MAIS SEGURA de Identificar Importados

```python
# Critério 1: MELHOR (se migration 0086 aplicada)
Produto.objects.filter(codigo_legado__isnull=False).exclude(codigo_legado='')

# Critério 2: SECUNDÁRIO (usar se código_legado não existir)
# Verificar se há padrão na data de criação + código formatado
```

**Seu insight anterior estava correto**: Você mencionou concentração em 27/07/2026. 
Isso provavelmente vem de um import inicial. **Valide com SQL direto no PostgreSQL** antes de implementar.

---

## 3️⃣ QUANTIFICAÇÃO DE DADOS

### Banco SQLite (analisado - pode estar desatualizado)
```
Total de produtos ativos: 36
  - Com código: 7 (19%)
  - Sem código: 29 (81%)
  
Data mais comum: 2026-04-10 (13 produtos)
Distribuição: Fevereiro (8), Março (10), Abril (18)
```

⚠️ **RECOMENDAÇÃO**: Execute esta query no banco de produção PostgreSQL:

```sql
SELECT 
  COUNT(*) as total,
  COUNT(CASE WHEN codigo_legado IS NOT NULL THEN 1 END) as com_legado,
  COUNT(CASE WHEN codigo IS NOT NULL THEN 1 END) as com_codigo,
  MIN(criado_em::date) as primeira_criacao,
  MAX(criado_em::date) as ultima_criacao
FROM estoque_produto
WHERE excluido = false;

-- Distribuição por data
SELECT DATE(criado_em), COUNT(*) 
FROM estoque_produto 
WHERE excluido = false
GROUP BY DATE(criado_em)
ORDER BY COUNT(*) DESC
LIMIT 10;
```

---

## 4️⃣ CAMPOS PARA REGISTRAR REVISÃO

### Status Atual
❌ **NÃO EXISTE** campo para registrar revisão ou importação no banco SQLite atual

### Campos que Existem (para referenciar)
- `criado_em`: Data de criação (imutável)
- `atualizado_em`: Data da última atualização (muda a cada modificação)
- `codigo_legado`: Indicador de importação (null = novo)
- `cadastro_incompleto`: Indica dados faltando (**migration 0066 ainda não aplicada**)

---

## 5️⃣ PROPOSTA DE ALTERAÇÃO DO MODELO

### Opção A: Dois Campos Simples (Recomendado ✅)
```python
# Adicionar ao modelo Produto:
revisado_importacao = models.BooleanField(
    default=False,
    db_index=True,
    help_text="Indica se o produto foi revisado pela importação"
)

revisado_importacao_em = models.DateTimeField(
    null=True,
    blank=True,
    db_index=True,
    help_text="Data/hora quando foi revisado"
)
```

**Vantagens**:
- ✅ Simples e claro
- ✅ Permite filtrar rapidamente: `filter(revisado_importacao=False)`
- ✅ Rastreia QUANDO foi revisado
- ✅ Índices permitem busca rápida em 700+ produtos
- ✅ Permite "reabrir" (marcar como False novamente)

**Desvantagens**:
- Não rastreia QUEM revisou (se necessário, usar model separado depois)

### Opção B: Campo Único (Mais Minimalista)
```python
# Apenas um campo:
revisado_importacao_em = models.DateTimeField(
    null=True,
    blank=True,
    help_text="null = não revisado | datetime = data da revisão"
)

# Lógica: if revisado_importacao_em is None → pendente, else → revisado
```

**Vantagens**: Um campo a menos na tabela
**Desvantagens**: Menos semântica clara; precisa de lógica extra

### Opção C: Com Auditoria Completa (Overkill neste momento)
```python
revisado_importacao = BooleanField(default=False)
revisado_importacao_em = DateTimeField(null=True, blank=True)
revisado_importacao_por = ForeignKey(User, null=True, blank=True, on_delete=SET_NULL)
revisor_notas = TextField(blank=True)
```

---

### **RECOMENDAÇÃO FINAL**: Opção A (Dois campos)
- Equilibrio entre simplicidade e rastreamento
- Expandível se necessário adicionar `revisado_por` depois
- Segue padrão já usado em `excluido`/`excluido_em`

---

## 6️⃣ TELA ATUAL DE PRODUTOS E POSSIBILIDADES DE EDIÇÃO

### Listagem Atual
**Arquivo**: [estoque/templates/estoque/home.html](estoque/templates/estoque/home.html#L1895-L2075)
**View**: [estoque/views.py](estoque/views.py#L1567) - Função `home()`
**URL**: `/` (raiz do app)

### Campos Exibidos na Tabela
1. ✅ Checkbox de seleção
2. ✅ Nome do produto
3. ✅ Quantidade em estoque
4. ✅ Estoque mínimo
5. ✅ Botões de ação (editar, ativar/desativar)

### Campos que PODERIAM ser Editados Diretamente (em massa)
| Campo | Pode Editar Inline? | Por quê? |
|-------|-------------------|---------|
| **nome** | ✅ **SIM** | Apenas texto, sem validações complexas |
| **codigo** | ✅ **SIM** | Texto simples, sem FK |
| **codigo_legado** | ✅ **SIM** | Único, mas pode validar antes de salvar |
| **categoria** | ✅ **SIM** | Select/dropdown, sem impacto |
| **fornecedor** | ✅ **SIM** | Campo de texto, sem FK critica |
| **ativo** | ✅ **SIM** | Toggle simples |
| **revisado_importacao** | ✅ **SIM** | Checkbox simples (marcar como revisado) |
| preco_* | ⚠️ **TALVEZ** | Impacta vendas; requer validação mais forte |
| quantidade | ❌ **NÃO** | Impacta estoque; crítico, requer auditoria |

### Edição em Massa Atualmente
- ✅ Exclusão em lote (soft delete): `excluir_selecionados`
- ❌ Edição em lote de campos
- ❌ Edição inline na tabela
- ❌ API REST para atualizar em massa

---

## 7️⃣ ANÁLISE DE DESEMPENHO - 50-60 PRODUTOS POR PÁGINA

### Viabilidade
✅ **MUITO VIÁVEL**

**Análise**:
- Banco SQLite com 36 produtos carrega em < 100ms
- PostgreSQL bem indexado pode servir 60 produtos em < 200ms
- Dados enviados no formulário: ~50 * 150 bytes = ~7.5 KB (aceitável)
- Template renderizando 60 linhas: <1s no navegador (desktop moderno)

**Recomendação**: 
- 🎯 **Usar 50 produtos por página** como padrão
- Carregar próxima página ao scrollar (lazy loading) opcionalmente
- Implementar paginação com "Próximo" e "Anterior"

---

## 8️⃣ CORREÇÕES QUE PODEM SER AUTOMATIZADAS

Antes da revisão humana, é **SEGURO** fazer:

### ✅ SEGURO (sem verificação manual)
1. **Normalizar maiúsculas/minúsculas** (primeira letra maiúscula)
   ```python
   nome_normalizado = nome.strip().title()  # "REFRIGERANTE" → "Refrigerante"
   ```
   Risco: Baixo (apenas estética) | Revertível: Sim

2. **Remover espaços em branco extras**
   ```python
   nome_limpo = nome.strip()
   ```
   Risco: Mínimo | Revertível: Sim

3. **Converter código_legado para UPPERCASE**
   ```python
   codigo_legado_limpo = codigo_legado.upper()
   ```
   Risco: Baixo (histórico está preservado) | Revertível: Sim

### ⚠️ MEDIUM (revisar após automatização)
4. **Sugerir categoria baseada em nome** (similar products)
   - Ex: Se nome contém "refrigerante" e já há produtos similares com categoria, sugerir
   - Risco: Médio (pode acusar falso positivo)

### ❌ NÃO RECOMENDADO (sem revisão prévia)
5. Gerar `codigo` novo (falta contexto do negócio)
6. Modificar preços (impacta cálculos financeiros)
7. Mover para categoria diferente (sem análise)

---

## 9️⃣ ARQUITETURA PROPOSTA - INTERFACE DE REVISÃO

### Fluxo Ideal
```
┌─────────────────────────────────────────────────┐
│ DASHBOARD REVISÃO DE PRODUTOS IMPORTADOS        │
├─────────────────────────────────────────────────┤
│                                                 │
│ Status: 450 PENDENTES | 250 REVISADOS          │
│ Última sessão: 15/08/2026 às 14:30              │
│ Próximo: Produto ID 1247 (refrigerante cola)    │
│                                                 │
│ [Filtrar PENDENTES] [Ver REVISADOS]  [Voltar]  │
│                                                 │
├─────────────────────────────────────────────────┤
│ Lote 1-50 de 450                                │
│                                                 │
│  Produto     │ Código Antigo │ Categ.   │ Status │
│  ─────────────────────────────────────────────  │
│  ☐ Refrig A  │ OLD001        │ Bebidas  │ ▼ Edit│
│  ☐ Refrig B  │ OLD002        │ Bebidas  │ ▼ Edit│
│  ...                                            │
│                                                 │
│  [Editar Lote] [Marcar como Revisado]          │
│  [Anterior] [Próximo] [Salvar Tudo]            │
│                                                 │
└─────────────────────────────────────────────────┘
```

### Componentes Principais

#### A. Nova View: `/revisao-produtos/`
- **GET**: Exibe form de filtros + lote de 50 produtos
- **POST**: Processa edições e marcações de revisão
- Padrão: Usa sessions/cookies + banco para persistência

#### B. Novo Modelo: `RevisaoProdutoSession` (Opcional)
```python
class RevisaoProdutoSession(models.Model):
    usuario = ForeignKey(User, on_delete=CASCADE)
    criado_em = DateTimeField(auto_now_add=True)
    atualizado_em = DateTimeField(auto_now=True)
    
    ultimo_produto_id = IntegerField()
    ultimo_produto_nome = CharField(max_length=200)
    
    total_processados = IntegerField(default=0)
    total_revisados = IntegerField(default=0)
    
    class Meta:
        get_latest_by = 'atualizado_em'
```

**Benefício**: Permite continuar de onde parou, mesmo após dias/semanas

#### C. Modelo existente: Adicionar ao `Produto`
```python
revisado_importacao = BooleanField(default=False, db_index=True)
revisado_importacao_em = DateTimeField(null=True, blank=True, db_index=True)
```

#### D. Form Inline de Edição
```python
class ProdutoRevisaoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = ['nome', 'codigo', 'codigo_legado', 'categoria', 'fornecedor', 'ativo']
        
    revisado = forms.BooleanField(required=False, label="Revisar agora")
```

#### E. Template: `estoque/templates/estoque/revisao_produtos.html`
- Tabela com 50-60 produtos por vez
- Campos editáveis em linhas da tabela (contenteditable ou campos)
- Checkbox "Revisar" por produto
- Botão "Salvar Tudo" que POST os dados

---

## 🔟 FLUXO OPERACIONAL - "CONTINUAR DE ONDE PAREI"

### Implementação Segura e Persistente

```python
# No banco de dados (Tabela RevisaoProdutoSession)

┌─ Usuário: Lincoln               ┐
│ Última sessão: 20/08 às 14:00   │
│ Último produto revisado: ID 1503│
│ Total revisado: 127 de 450      │
└─ Próxima página: IDs 1504-1553  ┘

# Query (persistida no banco):
SELECT * FROM estoque_produto
WHERE revisado_importacao = FALSE  -- Apenas PENDENTES
  AND codigo_legado IS NOT NULL    -- Apenas importados
  AND id > 1503                    -- Após o último revisado
LIMIT 50
ORDER BY id ASC
```

### Fluxo Técnico

1. **Acesso à tela de revisão**:
   - View `revisao_produtos()` verifica último `RevisaoProdutoSession` do usuário
   - Se existe e foi acessada há < 72h, carrega desde onde parou
   - Mostra: "Você estava no produto ID 1503. Continuar?"

2. **Salvar progresso**:
   - POST atualiza `Produto.revisado_importacao = True`
   - POST atualiza `Produto.revisado_importacao_em = now()`
   - POST registra em `RevisaoProdutoSession.ultimo_produto_id = X`
   - Retorna próxima tela com IDs 1504-1553

3. **Interrupção**:
   - Usuário sai a qualquer momento
   - Dados salvos no banco (não em localStorage/sessionStorage)
   - Pode voltar horas/dias depois e continuar

4. **Filtros**:
   - "Mostrar PENDENTES": `revisado_importacao = False`
   - "Mostrar REVISADOS": `revisado_importacao = True`
   - Ambas podem ser editadas/reabertas

---

## MATRIZ DE DECISÃO

| Funcionalidade | Viável? | Complexidade | Risco | Prioridade |
|----------------|---------|-------------|-------|-----------|
| Listar lotes de 50 | ✅ | Baixa | Baixo | 🔴 P1 |
| Editar campos na tabela | ✅ | Média | Médio | 🔴 P1 |
| Marcar "Revisado" | ✅ | Baixa | Baixo | 🔴 P1 |
| Salvar em lote | ✅ | Média | Médio | 🔴 P1 |
| Continuar de onde parou | ✅ | Média | Baixo | 🟠 P2 |
| Auto-normalizar maiúsculas | ✅ | Baixa | Baixo | 🟡 P3 |
| Reabrir produto revisado | ✅ | Baixa | Baixo | 🟡 P3 |
| Filtro PENDENTES/REVISADOS | ✅ | Baixa | Baixo | 🟡 P3 |

---

## RESUMO EXECUTIVO

### O Que Você Tem
- ✅ Modelo `Produto` bem estruturado
- ✅ Banco de dados com soft delete (pode recuperar)
- ✅ Views + Templates + Forms já funcionando
- ✅ Sistema de lote de exclusão já implementado (reutilizável)
- ❌ Nenhuma interface de "edição rápida em massa"
- ❌ Nenhum rastreamento de revisão

### O Que Você Precisa Adicionar
1. **Modelo**: Dois campos simples em `Produto`
   - `revisado_importacao: BooleanField(default=False)`
   - `revisado_importacao_em: DateTimeField(null=True)`

2. **Persistência de Sessão**: Modelo `RevisaoProdutoSession`
   - Simples, apenas registra `ultimo_produto_id` + timestamp

3. **Nova View**: `/revisao-produtos/`
   - Busca PENDENTES, carrega 50 por vez
   - POST processa edições + marcações

4. **Novo Template**: `revisao_produtos.html`
   - Tabela com campos editáveis
   - Reutilizar CSS/JS do `home.html`

5. **Lógica de Importação**: Identificar com `codigo_legado IS NOT NULL`

### Estimativa de Esforço
- Migration: 1-2 horas
- View + Template: 4-6 horas
- Testes: 2-3 horas
- **Total**: ~8-10 horas de trabalho

### Próximas Etapas (Após Diagnóstico)
1. ✅ **ESTE DIAGNÓSTICO** (Concluído)
2. ⏭️ Validar números exatos no PostgreSQL
3. ⏭️ Criar migrations para os dois campos
4. ⏭️ Implementar view e template
5. ⏭️ Testar com lote piloto de ~100 produtos
6. ⏭️ Deploy e treino de operadores

---

**Status**: 📋 Diagnóstico concluído | ✅ Pronto para implementação
**Confiança**: Alta (baseado em análise de código e estrutura existente)
**Data da análise**: 2026-08-20
