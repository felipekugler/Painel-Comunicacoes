# Passo a Passo do Script de Extração de Documentos

## PASSO 1: Extrair informações básicas do PAJ
**Linhas:** 20-29

```javascript
const pageText = document.body.innerText;

// Busca pelo padrão "Número do PAJ AAAA/UUU-NNNNN"
const pajMatch = pageText.match(/Número\s+do\s+PAJ\s+(\d{4}\/\d{3}-\d+)/);
resultado.paj = pajMatch ? pajMatch[1] : 'Desconhecido';

// Busca pelo padrão "Assistido(s) NOME"
const assistidoMatch = pageText.match(/Assistido\(s\)\s+([A-Z\sÀ-ÿ]+?)(?:\n|Data|Decurso|CPF)/);
resultado.assistido = assistidoMatch ? assistidoMatch[1].trim() : 'Desconhecido';
```

**O que faz:** Lê todo o texto da página e procura pelos padrões de PAJ e Assistido

**Possível variação:** Se o formato do texto mudar, os regex não funcionam

---

## PASSO 2: Encontrar a tabela de movimentações dinamicamente
**Linhas:** 35-55

```javascript
const tables = document.querySelectorAll('table');
let tableMovimentacoes = null;

for (let i = 0; i < tables.length; i++) {
  const headers = Array.from(tables[i].querySelectorAll('thead th, tbody tr:first-child td'))
    .map(h => h.textContent.trim())
    .filter(h => h.length > 0);

  // Procura por uma tabela que tenha AMBAS as colunas
  if (headers.includes('Seq.') && headers.includes('Data/Hora')) {
    tableMovimentacoes = tables[i];
    tableIndex = i;
    break;
  }
}
```

**O que faz:** Procura por uma tabela que tenha as colunas exatas:
- `'Seq.'` (primeira coluna)
- `'Data/Hora'` (terceira coluna)

**Critério:** A tabela DEVE ter AMBOS esses headers

**Para 2025/029-03896:** Encontrou a tabela ✓
**Para 2026/029-01279:** Encontrou a tabela ✓
**Para 2026/029-01486:** Encontrou a tabela no índice 22 ✓

---

## PASSO 3: Verificar se há movimentações com ANEXO
**Linhas:** 60-94

```javascript
const rowsMovimentacoes = tableMovimentacoes.querySelectorAll('tbody tr');
const seqsComAnexo = [];

for (let i = 0; i < rowsMovimentacoes.length; i++) {
  const row = rowsMovimentacoes[i];
  const cells = row.querySelectorAll('td');

  if (cells.length < 3) continue;

  const seq = cells[0]?.textContent.trim();
  const anexoCell = cells[1];  // SEGUNDA COLUNA: Anexo
  const dataHora = cells[2]?.textContent.trim();
  const movimentacao = cells[3]?.textContent.trim();

  // ⚠️ VERIFICAÇÃO CRÍTICA: A célula de anexo DEVE ter um elemento clicável
  const temClicavel = !!anexoCell.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');

  if (temClicavel && seq && /^\d+$/.test(seq)) {
    seqsComAnexo.push({
      seq: seq,
      dataHora: dataHora,
      movimentacao: movimentacao,
      rowIndex: i
    });
  }
}
```

**O que faz:** 
1. Itera cada linha da tabela de movimentações
2. Verifica a **coluna 2** (cells[1]) que deve ser "Anexo"
3. Procura por um elemento clicável dentro dessa célula:
   - `<a>` (link)
   - `<button>`
   - `<img>` com onclick
   - `<span>` com onclick
   - ou qualquer elemento com `[role="button"]`

**Resultado esperado:**
- **2025/029-03896:** 18 movimentações com anexo ✓
- **2026/029-01279:** 13 movimentações com anexo ✓
- **2026/029-01486:** Esperado: 3 movimentações com anexo ✓

---

## PASSO 4: Para cada movimentação com anexo, CLICAR
**Linhas:** 99-116

```javascript
for (let idx = 0; idx < seqsComAnexo.length; idx++) {
  const mov = seqsComAnexo[idx];

  // Recupera a linha original
  const row = rowsMovimentacoes[mov.rowIndex];
  const anexoCell = row.querySelectorAll('td')[1];
  
  // Encontra o elemento clicável
  const clickable = anexoCell.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');

  if (!clickable) {
    resultado.debug.push(`  ✗ Não conseguiu encontrar elemento clicável`);
    continue;
  }

  // ⚠️ CLICA NO ELEMENTO
  clickable.click();
  resultado.debug.push(`  ✓ Clicou no anexo`);

  // ⚠️ AGUARDA 1500ms para o popup/modal abrir
  await new Promise(r => setTimeout(r, 1500));
}
```

**O que faz:** Clica em cada elemento de anexo e aguarda 1.5 segundos para o popup abrir

**Possível variação:** Se o tempo for insuficiente, o popup pode não estar pronto quando o script tenta extrair

---

## PASSO 5: Encontrar a tabela de documentos dentro do popup
**Linhas:** 121-142

```javascript
const allTables = document.querySelectorAll('table');
let tableDoc = null;

// ⚠️ PROCURA DE TRÁS PARA FRENTE (últimas 40 tabelas)
// Isso é proposital: popups aparecem no final da DOM
for (let j = allTables.length - 1; j >= Math.max(0, allTables.length - 40); j--) {
  const t = allTables[j];
  const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
    .map(h => h.textContent.trim())
    .filter(h => h.length > 0);

  // Procura por uma tabela com a coluna "Arquivo"
  if (headers.includes('Arquivo')) {
    tableDoc = t;
    resultado.debug.push(`  ✓ Tabela de documentos encontrada`);
    break;
  }
}
```

**O que faz:** Procura pela tabela de documentos dentro do popup
- Começa do FINAL da lista de tabelas (onde popups aparecem)
- Procura por uma tabela que tenha a coluna `'Arquivo'`

**Critério de identificação:** A tabela DEVE ter a coluna `'Arquivo'`

---

## PASSO 6: Extrair os documentos da tabela
**Linhas:** 147-169

```javascript
const documentos = [];
const docRows = tableDoc.querySelectorAll('tbody tr');

for (const docRow of docRows) {
  const docCells = docRow.querySelectorAll('td');
  if (docCells.length === 0) continue;

  // ⚠️ ASSUMINDO A ORDEM DAS COLUNAS:
  // [0] = Tipo de Arquivo
  // [1] = Descrição
  // [2] = Nome do Arquivo
  // [3+] = Operações (links, botões, etc)

  const tipoArquivo = docCells[0]?.textContent.trim() || '';
  const descricao = docCells[1]?.textContent.trim() || '';
  const nomeArquivo = docCells[2]?.textContent.trim() || '';

  // Procura por um link de download em qualquer célula
  const linkVis = docRow.querySelector('a');
  const urlDownload = linkVis?.href || '';

  if (nomeArquivo) {
    documentos.push({
      tipoArquivo: tipoArquivo,
      descricao: descricao,
      nomeArquivo: nomeArquivo,
      urlDownload: urlDownload
    });
  }
}
```

**O que faz:** Para cada linha da tabela de documentos:
1. Lê a **célula 0** → Tipo de Arquivo
2. Lê a **célula 1** → Descrição
3. Lê a **célula 2** → Nome do Arquivo
4. Procura por um `<a>` em qualquer lugar da linha → URL do download

**⚠️ PONTO CRÍTICO:** A ordem das colunas é rígida:
- Coluna 0 = Tipo
- Coluna 1 = Descrição
- Coluna 2 = Nome do Arquivo

Se a ordem variar, a extração falha!

---

## PASSO 7: Fechar o popup
**Linhas:** 188-189

```javascript
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await new Promise(r => setTimeout(r, 700));
```

Envia a tecla ESC e aguarda 700ms para o popup fechar

---

## Variáveis Potencialmente Problemáticas

### 1. **Estrutura da tabela de documentos (PASSO 6)**
Se as colunas estiverem em ordem diferente:
- `docCells[0]` pode não ser o tipo
- `docCells[1]` pode não ser a descrição
- `docCells[2]` pode não ser o nome do arquivo

### 2. **Timing (PASSO 4)**
Se 1500ms não for suficiente para o popup abrir, a extração não encontra a tabela

### 3. **Elemento clicável (PASSO 3 e 4)**
Se o elemento clicável tiver uma estrutura diferente, `querySelector` pode não encontrá-lo

### 4. **Headers das tabelas**
Se os headers tiverem espaços extras ou variações de capitalização, o `.includes()` não funcionará

---

## O que você precisa verificar no PAJ 2026/029-01486:

1. **Qual é a ordem exata das colunas na tabela de documentos?**
   - Leia: Tipo | Descrição | Arquivo | Operações
   - Ou: Seq. | Descrição | Tipo de Documento | Arquivo | Detalhes

2. **O elemento clicável na coluna Anexo é qual tipo?**
   - `<a>`?
   - `<button>`?
   - `<img>`?
   - `<span>` com onclick?

3. **Qual é exatamente o texto dos headers da tabela de documentos?**
   - "Arquivo" ou "Nome do Arquivo"?
   - "Tipo de Arquivo" ou "Tipo de Documento"?

4. **O popup precisa de mais de 1500ms para abrir?**
