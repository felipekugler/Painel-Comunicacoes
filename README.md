# Painel de Comunicações

Painel web para **servidores da DPU** organizarem, de forma centralizada, o contato com
assistidos que aguardam alguma providência — avisos de perícia, pedidos de documentos,
orientações — por **quatro meios**: Aplicativo do SISDPU, WhatsApp, ligação telefônica e
carta (expedida pelos Correios).

> Desenvolvido para uso interno na DPU. Projeto pessoal, sem vínculo institucional oficial.

---

## O que é

O painel lê as **caixas de grupo do SISDPU** que você escolher, identifica em cada PAJ o
que precisa ser comunicado, ajuda a redigir a mensagem (com apoio opcional de IA), realiza
o contato pelo meio escolhido e **documenta tudo de volta no SISDPU** — movimentação,
tramitação e conclusão — deixando um histórico completo com comprovantes.

### Principais recursos

- **Multi-caixa**: gerencia quantas caixas de grupo forem habilitadas, cada uma com seus
  próprios textos e parágrafos padrão.
- **Quatro meios de contato**: Aplicativo SISDPU, WhatsApp (com QR Code próprio), Ligação
  telefônica (central de tentativas) e Carta (expedição real pelos Correios/e-Carta, com
  validação de CEP por múltiplos serviços).
- **Modo com IA (opcional)**: usa o **Claude Code CLI** já instalado na máquina — sem API
  paga — para resumir a tramitação e sugerir a mensagem. Sem ele, o painel roda em Modo
  Simples (100% manual).
- **Cofre de credenciais**: usuário/senha do SISDPU e dos Correios ficam cifrados em disco,
  protegidos por uma senha-mestra — nada em texto plano.
- **Concluir sem contato**: PAJ já tratado é concluído direto da caixa, sem precisar de
  nenhuma mensagem.
- **Dias não úteis / feriados**: prazos de tramitação são ajustados automaticamente para o
  próximo dia útil (fins de semana, feriados e recessos cadastrados).
- **Histórico e reenvio**: todo contato realizado fica registrado com log passo a passo,
  comprovantes e a opção de reenviar por outro meio.

---

## Pré-requisitos

- **Windows 10/11** (validado em Windows 11) — outras plataformas Unix-like devem funcionar,
  mas o fluxo é testado em Windows.
- **Python 3.11+**
- **Google Chrome** — usado pelo Playwright (SISDPU, Correios) e pelo WhatsApp Web.
- **Conta de acesso ao SISDPU**, vinculada às caixas de grupo que serão geridas.
- **Claude Code (CLI)** — opcional, só para o Modo com IA. Sem ele, o painel roda em Modo
  Simples.

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/felipekugler/Painel-Comunicacoes.git
cd Painel-Comunicacoes

# 2. Crie o ambiente virtual (Python 3.11+)
python -m venv .venv            # Windows
# python3.11 -m venv .venv      # macOS/Linux
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale o Chromium do Playwright
python -m playwright install chromium

# 5. (Opcional) Configure o .env
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

A maior parte da configuração (credenciais, caixas, textos padrão, feriados, modo de
envio) é feita **pela própria interface web**, não pelo `.env` — veja `.env.example` para
o que ainda vale a pena ajustar antes de subir o servidor.

---

## Uso

```bash
# Iniciar o servidor (porta 8002)
.venv\Scripts\python.exe app.py     # Windows
# .venv/bin/python app.py           # macOS/Linux

# Acessar no navegador
# http://localhost:8002
```

No Windows, o atalho `Painel-Comunicacoes.bat` faz a mesma coisa (e mata uma instância
anterior antes de subir uma nova).

No primeiro acesso a uma função do SISDPU, o painel pede para cadastrar usuário/senha e
criar uma senha-mestra — as credenciais ficam cifradas em disco a partir daí.

---

## Estrutura

```
Painel-Comunicacoes/
├── app.py                          # FastAPI + Uvicorn (porta 8002)
├── config.py                       # Configuração mínima (lê .env)
├── ingestao/                       # Clientes externos (Playwright)
│   ├── sisdpu_client.py              # Scraping/ações no SISDPU
│   ├── whatsapp_client.py            # WhatsApp Web (perfil persistente)
│   └── correios_client.py            # Expedição de carta (SMT/e-Carta)
├── routes/                         # Endpoints FastAPI
├── services/                       # Lógica de negócio (config, envio, análise IA…)
├── templates/                      # HTML (Jinja2)
├── static/                         # CSS e JS estáticos
├── Painel-Comunicacoes.bat         # Lançador (Windows)
├── WhatsApp-Login.bat              # Login do WhatsApp por QR Code
└── requirements.txt
```

Os dados (contatos, histórico, comprovantes, credenciais cifradas) ficam numa pasta
`dados/` **ao lado** de `Projeto/`, criada sozinha na primeira execução.

---

## Tecnologias

- **Backend:** FastAPI + Uvicorn
- **Frontend:** Jinja2 + CSS/JS próprios (sem framework de build)
- **Automação:** Playwright (SISDPU, WhatsApp Web, Correios)
- **Segurança:** `cryptography` (cofre de credenciais cifrado por senha-mestra)
- **IA (opcional):** Claude Code (CLI) — sem API paga

---

## Aviso

Ferramenta de apoio interno, em desenvolvimento. O uso é de responsabilidade do próprio
servidor e deve observar as normas de sigilo e proteção de dados. Os dados dos assistidos
ficam apenas na máquina local onde o painel roda — nunca em nuvem — e são ignorados pelo
Git (consulte o `.gitignore`).

---

## Licença

[MIT](LICENSE) — uso livre, sem garantias.
