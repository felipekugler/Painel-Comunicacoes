"""Configuracao central do painel — aponta para o workspace Oficio Geral."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

OFICIO_GERAL = Path(
    os.getenv(
        "OFICIO_GERAL",
        str(Path.home() / "Desktop" / "Ofício Geral"),
    )
)
PAJS_DIR = OFICIO_GERAL / "PAJs"
PECAS_FEITAS_DIR = OFICIO_GERAL / "Peças Feitas"

# Texto exibido sob o titulo "Oficio Geral" na sidebar — identifica a unidade
# do defensor (ex: "2ª Categoria - DPU/AP"). Default generico para nao expor
# unidade especifica no codigo; cada usuario ajusta no .env.
OFICIO_DESCRICAO = os.getenv("OFICIO_DESCRICAO") or "Defensoria Pública da União"

# Scripts do workspace usados pelo painel
GERAR_DOCX_SCRIPT = OFICIO_GERAL / "gerar_docx.py"
GERAR_PECA_SCRIPT = OFICIO_GERAL / "gerar_peticao.py"

# Credenciais SISDPU (usadas pela ingestao automatica — carregadas lazy, so
# validadas quando a sincronizacao e' disparada).
SISDPU_USERNAME = os.getenv("SISDPU_USERNAME", "")
SISDPU_PASSWORD = os.getenv("SISDPU_PASSWORD", "")
RATE_LIMIT_SISDPU = int(os.getenv("RATE_LIMIT_SISDPU_SEG", "2"))
TIMEOUT_TOTAL = int(os.getenv("TIMEOUT_TOTAL_SEG", "3600"))
# Limite de anexos baixados do SISDPU por PAJ (protege contra PAJs gigantes).
# Se um PAJ tiver mais que isso, o sync baixa os N mais recentes e avisa no log.
MAX_ANEXOS_POR_PAJ = int(os.getenv("MAX_ANEXOS_POR_PAJ", "30"))

# Timeout por pagina no Tesseract (segundos). PDFs escaneados grandes ou
# corrompidos podem travar o OCR — esse limite garante progresso.
TIMEOUT_OCR_POR_PAGINA_SEG = int(os.getenv("TIMEOUT_OCR_POR_PAGINA_SEG", "30"))

# --- OCR de 1a linha: ocrmypdf (pre-processamento + Tesseract) ---
# Quando o PDF e' escaneado (texto nativo insuficiente), o OCR local tenta
# PRIMEIRO o ocrmypdf — pipeline robusto (Ghostscript rasteriza + deskew/clean/
# rotate antes do Tesseract) que resolve a maioria dos casos onde o Tesseract
# cru devolvia vazio. E' OFFLINE e sem custo de token. Se o binario `ocrmypdf`
# nao estiver no PATH (ou falhar), cai no Tesseract pagina-a-pagina (antigo).
# OCR_PREPROC=0 desliga e usa direto o Tesseract cru. Timeout do DOCUMENTO todo
# (ocrmypdf processa o PDF inteiro de uma vez, nao pagina-a-pagina).
OCR_PREPROC_ATIVO = os.getenv("OCR_PREPROC", "1") not in ("0", "false", "False", "")
OCR_PREPROC_TIMEOUT_SEG = int(os.getenv("OCR_PREPROC_TIMEOUT_SEG", "600"))

# Modelo usado pela ELABORACAO de pecas (Claude Code CLI), tanto individual
# quanto em lote. Default: Opus com janela de 1M tokens — o melhor pra analise
# juridica densa (le todos os anexos sem estourar contexto). Fixar aqui evita
# depender do default da conta (~/.claude/settings.json). Ajuste no .env se
# precisar: ELABORACAO_MODELO=opus / sonnet / claude-opus-4-8 / etc. Defina
# vazio ("") pra deixar o CLI escolher o default da conta.
ELABORACAO_MODELO = os.getenv("ELABORACAO_MODELO", "opus[1m]")

# Nivel de ESFORCO (--effort do Claude CLI) da elaboracao — controla quanto o
# modelo raciocina/gasta tokens. Valores: low, medium, high (default do CLI),
# xhigh, max. Default aqui: xhigh (racioco profundo recomendado pra Opus 4.x em
# tarefas analiticas; "max" rende ganho pequeno por custo bem maior). Fixar aqui
# evita depender do ambiente (CLAUDE_CODE_EFFORT_LEVEL). Vazio = default do CLI.
ELABORACAO_EFFORT = os.getenv("ELABORACAO_EFFORT", "xhigh")

# Triagem inteligente do "Elaborar todos" (modo automatico). Roteia a skill por
# PAJ: COM processo judicial -> SKILL_COM_PROCESSO (analisa/classifica e elabora);
# SEM processo -> SKILL_SEM_PROCESSO (triagem pre-processual). Casos classificados
# como HARD CASE pela analise elaboram a peca com ELABORACAO_EFFORT_HARD; os
# demais mantem ELABORACAO_EFFORT. Tudo configuravel no .env.
SKILL_COM_PROCESSO = os.getenv("SKILL_COM_PROCESSO", "analisar-processo")
SKILL_SEM_PROCESSO = os.getenv("SKILL_SEM_PROCESSO", "firac-triagem")
ELABORACAO_EFFORT_HARD = os.getenv("ELABORACAO_EFFORT_HARD", "max")

# Fallback OCR-LLM: quando o OCR local (ocrmypdf/Tesseract) AINDA falha (texto
# vazio/ilegivel/curto), um modelo BARATO (Sonnet) transcreve o PDF com visao
# ANTES da elaboracao, e o .txt melhorado fica cacheado pro Opus ler. Modelo/
# esforco proprios — tarefa mecanica, nao precisa do Opus xhigh.
#
# PDFs grandes sao transcritos EM BLOCOS de OCR_LLM_CHUNK_PAGINAS paginas (cada
# bloco = 1 chamada ao Sonnet; os textos sao concatenados no fim). Assim nao ha
# mais um teto que faca "pular" o anexo. OCR_LLM_TIMEOUT_SEG e' o limite POR
# BLOCO (nao pelo anexo inteiro).
#
# OCR_LLM_MAX_PAGINAS e' apenas um teto de SEGURANCA: anexos acima dele sao
# pulados (o Opus le o PDF direto) — protege contra autos de milhares de paginas
# (custo). Default 0 = SEM teto (transcreve tudo em blocos). OCR_LLM=0 desliga.
OCR_LLM_ATIVO = os.getenv("OCR_LLM", "1") not in ("0", "false", "False", "")
OCR_LLM_MODELO = os.getenv("OCR_LLM_MODELO", "sonnet")
OCR_LLM_EFFORT = os.getenv("OCR_LLM_EFFORT", "low")
OCR_LLM_TIMEOUT_SEG = int(os.getenv("OCR_LLM_TIMEOUT_SEG", "900"))  # por BLOCO
OCR_LLM_CHUNK_PAGINAS = int(os.getenv("OCR_LLM_CHUNK_PAGINAS", "40"))  # paginas/bloco ao Sonnet
OCR_LLM_MAX_PAGINAS = int(
    os.getenv("OCR_LLM_MAX_PAGINAS", "0")
)  # teto de seguranca; 0 = sem limite

# --- Re-disparo automatico apos renovacao da cota de uso do Claude ---
# Quando o limite de uso estoura no meio da elaboracao, o painel observa o
# horario de renovacao informado pelo CLI e re-dispara os PAJs pendentes
# COTA_MARGEM_MIN minutos depois. Se nao conseguir parsear o horario, usa o
# fallback. Re-tenta em cascata ate zerar os pendentes, limitado a
# COTA_MAX_CICLOS. O scheduler verifica a cada COTA_TICK_SEG segundos.
COTA_MARGEM_MIN = int(os.getenv("COTA_MARGEM_MIN", "10"))
COTA_FALLBACK_MIN = int(os.getenv("COTA_FALLBACK_MIN", "60"))
COTA_MAX_CICLOS = int(os.getenv("COTA_MAX_CICLOS", "12"))
COTA_TICK_SEG = int(os.getenv("COTA_TICK_SEG", "60"))

# Pasta onde DOCX/PDF gerados pelo docgen sao salvos.
# Default: <OFICIO_GERAL>/Peças Feitas
DOCGEN_OUT_DIR = Path(os.getenv("DOCGEN_OUT_DIR", str(OFICIO_GERAL / "Peças Feitas")))


def validar_paths() -> list[str]:
    """Verifica que OFICIO_GERAL/PAJS_DIR existem. Retorna lista de avisos
    (vazia se tudo ok). Usada no startup pelo app.py — nao crasha a importacao
    para que ferramentas como ruff/pytest possam importar sem precisar do
    workspace montado."""
    erros: list[str] = []
    if not OFICIO_GERAL.exists():
        erros.append(f"OFICIO_GERAL nao encontrado: {OFICIO_GERAL}")
    if not PAJS_DIR.exists():
        erros.append(f"PAJS_DIR nao encontrado: {PAJS_DIR}")
    return erros
