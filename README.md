# DPU-script-SIS

Interface web para o pipeline **dpuscript** — sistema de monitoramento para Defensores Públicos Federais.

> Desenvolvido para uso interno na DPU. Projeto pessoal, sem vínculo institucional oficial.

---

## O que é

O **DPU-script-SIS** é um painel web para Defensores/as Públicos/as Federais que usam o **SISDPU**. Centraliza num só lugar: organização e acompanhamento da caixa de entrada, triagem, gestão de prazos processuais, controle de peças.

O sistema tem dois blocos:

### Pipeline de ingestão (botão "Sincronizar caixa")

1. Autentica no SISDPU via Playwright headless usando credenciais do `.env`
2. Identifica novos PAJs e novas movimentações na caixa do defensor
3. Baixa peças em PDF e extrai texto — com **OCR automático** (Tesseract) em documentos escaneados
4. **Detecta prazos processuais** aplicando regras de rito (cível, JEF, penal, administrativo), dobra DPU, dias úteis vs. corridos e recesso forense

### Painel web (FastAPI + DaisyUI/Alpine)

- **Dashboard** com lista de todos os PAJs e filtros por foro, classificação e ofício
- **Detalhe do PAJ** — movimentações, peças baixadas (com texto extraído), notas pessoais
- **Prazos** — aba dedicada aos prazos detectados, com integração opcional ao Google Calendar
- **Watchlist** — PAJs marcados como acompanhamento prioritário, com motivo
- **Pipeline monitor** — histórico e logs das sincronizações já realizadas
- **Chat com Claude Code (via CLI, sem API paga)** — triagem assistida com skills dedicadas + chat livre persistente por PAJ
- **Busca textual** em movimentações e peças
- **Geração de DOCX/PDF** via scripts externos do workspace do defensor

---

## Pré-requisitos

- **Python 3.11+**
- **Workspace Ofício Geral** — a pasta onde ficam `PAJs/`, `Peças Feitas/`, `gerar_docx.py`, `gerar_peticao.py` e o `CLAUDE.md` do Defensor
- **Claude Code** com modelo Max (claude-opus ou claude-sonnet) — a elaboração das peças é feita pelo Claude via CLI, não por API paga
- **Playwright + Chromium** — para sincronizar a caixa SISDPU automaticamente
- **Tesseract OCR (idioma `por`)** — para extrair texto de PDFs escaneados das peças baixadas

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/tuliorm/DPU-script-SIS.git
cd DPU-script-SIS

# 2. Crie o ambiente virtual (requer Python 3.11+)
python -m venv .venv            # Windows
# python3.12 -m venv .venv      # macOS/Linux (use o 3.11+ do Homebrew; o python3 do sistema no macOS é 3.9)
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale o Chromium do Playwright
python -m playwright install chromium

# 5. Instale o Tesseract OCR (idioma "por")
#   Windows: baixe em https://github.com/UB-Mannheim/tesseract/wiki
#            marque "Portuguese"; caminho padrao C:\Program Files\Tesseract-OCR\
#   macOS:   brew install tesseract tesseract-lang
#   Linux:   sudo apt install tesseract-ocr tesseract-ocr-por
# Verifique (precisa estar no PATH):
tesseract --version

# 6. Configure o .env
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
# Edite .env e preencha:
#   OFICIO_GERAL    = caminho para o workspace do Defensor
#   SISDPU_USERNAME = seu usuario do SISDPU
#   SISDPU_PASSWORD = sua senha do SISDPU
```

---

## Configuração

Edite o arquivo `.env`:

```env
OFICIO_GERAL=C:\Users\<seu_usuario>\Desktop\Ofício Geral
# macOS/Linux: OFICIO_GERAL=/Users/<seu_usuario>/Desktop/Ofício Geral
OFICIO_DESCRICAO=2ª Categoria - DPU/UF
SISDPU_USERNAME=seu.usuario
SISDPU_PASSWORD=sua.senha
```

`OFICIO_GERAL` aponta para a pasta do Defensor — é onde ficam `PAJs/`, `Peças Feitas/`, os scripts `gerar_docx.py` / `gerar_peticao.py` e o `CLAUDE.md`. `OFICIO_DESCRICAO` é apenas o texto exibido na sidebar (cosmético). As credenciais SISDPU são usadas pela funcionalidade "Sincronizar caixa" do dashboard.

---

## Uso

```bash
# Iniciar o servidor (porta 8001)
.venv\Scripts\python.exe app.py     # Windows
# .venv/bin/python app.py           # macOS/Linux

# Acessar no navegador
# http://localhost:8001
```

O painel mostra todos os PAJs detectados em `OFICIO_GERAL/PAJs/PAJ-*/`. Para cada PAJ você pode:
- Ver os detalhes extraídos do SISDPU (cabeçalho + movimentações)
- Ver peças baixadas em PDF (com texto extraído via OCR quando necessário)
- Acessar `.txt` extraídos e o `PROMPT_MAX.md` gerado dinamicamente
- Acionar a elaboração de peças pelo Claude via chat
- Gerar DOCX/PDF a partir de um `.txt` usando `gerar_docx.py` / `gerar_peticao.py`

**Sincronizar caixa:** o botão "Sincronizar caixa" no dashboard autentica no SISDPU via Playwright, lê a caixa de entrada, extrai movimentações, baixa peças em PDF e roda OCR. O progresso aparece em tempo real num modal com log de eventos (SSE).

---

## Estrutura

```
DPU-script-SIS/
├── app.py              # FastAPI + Uvicorn
├── config.py           # Configurações (lê .env)
├── ingestao/           # Pipeline de sincronização da caixa SISDPU
│   ├── sisdpu_client.py  # Playwright headless
│   ├── ocr.py            # Fitz + Tesseract
│   ├── parser.py         # sisdpu.txt → metadata.json
│   └── sincronizador.py  # orquestrador
├── routes/             # Endpoints (dashboard, PAJs, chat, docgen, sync)
├── services/           # Lógica de negócio
├── templates/          # HTML (Jinja2 + DaisyUI + Alpine.js)
├── static/             # CSS e JS estáticos
└── requirements.txt
```

---

## Workspace Ofício Geral

Este painel lê os dados da pasta apontada por `OFICIO_GERAL`, que contém:
- `PAJs/PAJ-YYYY-NNN-XXXXX/` — uma subpasta por PAJ (NNN = código de 3 dígitos da unidade) com `sisdpu.txt`, `metadata.json`, `Arquivos/`, peças geradas
- `Peças Feitas/` — peças anteriores do Defensor (usadas como contexto no PROMPT_MAX)
- `CLAUDE.md` — instruções do Defensor para o Claude Code
- `gerar_docx.py` e `gerar_peticao.py` — scripts que o botão "Gerar DOCX/PDF" invoca

---

## Tecnologias

- **Backend:** FastAPI + Uvicorn
- **Frontend:** DaisyUI (Tailwind CSS) + Alpine.js + HTMX
- **Ingestão:** Playwright (SISDPU), PyMuPDF, Tesseract OCR
- **IA:** Claude Code (claude-opus/sonnet) via CLI — sem API paga

---

## Aviso

Este projeto foi desenvolvido para uso pessoal por um Defensor Público Federal. Cada usuário é responsável, por sua conta e risco, do uso que for feito, sob sua exclusiva responsabilidade. O sistema funciona, mas ainda está em desenvolvimento e testes, por isso erros podem acontecer, sendo igualmente de responsabilidade do próprio usuário.

Ele **não armazena dados de assistidos** — os arquivos de processos ficam apenas na máquina local do Defensor e são ignorados pelo Git. Consulte o `.gitignore` para confirmar o que é e não é versionado.

Contribuições são bem-vindas. Abra uma issue ou PR.

---

## Licença

[MIT](LICENSE) — uso livre, sem garantias.
