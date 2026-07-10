# Estrutura da Tabela de Movimentações - SISDPU

## CRÍTICO: Ordem das Colunas

A tabela de movimentações do SISDPU tem a seguinte estrutura de índices de células (`<td>`):

| Índice | Nome HTML | Campo Extraído | Tipo | Exemplo |
|--------|-----------|-----------------|------|---------|
| 0 | Seq. | `seq` | string (número) | "11", "10", "9" |
| 1 | Anexo | (ignorado) | icon/checkbox | (clipe se houver) |
| 2 | Data/Hora | `data` | timestamp | "07/05/2026 13:31:32" |
| 3 | **Movimentação** | `movimentacao` | string | "MOVIMENTAÇÃO MANUAL" |
| 4 | **Fases** | `fases` | string | "° Decurso de prazo;" |
| 5 | Descrição | `descricao` | long text | "PAJ em decurso alterado..." |
| 6 | - | (ignorado) | empty/checkbox | - |
| 7 | Usuário | `usuario` | string | "SISDPU", "(d) Nome" |
| 8+ | Ações | (ignorado) | links/buttons | - |

## ⚠️ ERRO COMUM: Inversão de cells[3] e cells[4]

**ERRADO:**
```javascript
const fases = (cells[3]?.textContent || '').trim();       // ❌ ERRADO
const movimentacao = (cells[4]?.textContent || '').trim(); // ❌ ERRADO
```

**CORRETO:**
```javascript
const movimentacao = (cells[3]?.textContent || '').trim(); // ✓ CERTO
const fases = (cells[4]?.textContent || '').trim();       // ✓ CERTO
```

## Características Distintivas para Validação

- `movimentacao`: **NÃO** começa com "°" (ex: "MOVIMENTAÇÃO MANUAL", "Gabinete")
- `fases`: **SEMPRE** começa com "°" (ex: "° Decurso de prazo;", "° Narrativa;")

Se você vir o oposto, as colunas foram **invertidas**.

## Localização do Código

- **Extração:** `ingestao/sisdpu_client.py` linhas ~394-433
  - Função que executa JavaScript no navegador
  - `querySelectorAll('table tbody tr')` + `cells[index]`

- **Validação:** `ingestao/test_sisdpu_structure.py`
  - Testa todos os PAJs após sincronização
  - Detecta inversão automaticamente
  - Chamado em: `sincronizador.py` após salvar metadata.json

## Histórico

**2026-06-27:** Inversão descoberta após sincronização de todos os PAJs
- Root cause: `cells[3]` e `cells[4]` trocados em sisdpu_client.py
- Impacto: 17 PAJs sincronizados com dados invertidos
- Solução: 
  1. Código corrigido em sisdpu_client.py
  2. Script de correção retroativa executado
  3. Testes automatizados adicionados
  4. Validação integrada ao sincronizador

## Testes Automatizados

Execute manualmente:
```bash
python ingestao/test_sisdpu_structure.py
```

Ou durante desenvolvimento:
```python
from ingestao import test_sisdpu_structure
ok, problemas = test_sisdpu_structure.validar_paj_apos_sync("PAJ-2026-029-01235")
```

## Ações se Detectar Problema

1. **Verificar o HTML do SISDPU** - A estrutura pode ter mudado
2. **Inspecionar elemento** - Abrir F12 → Elements → verificar índices reais
3. **Atualizar sisdpu_client.py** - Se a ordem das colunas mudou no SISDPU
4. **Rodar correção retroativa** - Se houver PAJs já sincronizados:
   ```bash
   python -c "
   from pathlib import Path
   import json
   # Script de correção...
   "
   ```
5. **Executar testes** - `python ingestao/test_sisdpu_structure.py`
