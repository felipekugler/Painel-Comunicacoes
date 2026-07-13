"""Analise da TRAMITACAO (a solicitacao de contato que ela pede) via Claude Code CLI.

Recebe as ultimas movimentacoes de um PAJ e pede ao Claude um resumo objetivo da
providencia de contato necessaria com o assistido. Invoca o `claude` local por
subprocess, passando o prompt via STDIN (no Windows, prompt longo com quebras de
linha como argumento e' truncado pelo wrapper .cmd) e lendo `--output-format json`.

Tarefa de PURO TEXTO (o modelo nao precisa de ferramentas), entao NAO usamos
`--permission-mode bypassPermissions`. Best-effort: qualquer falha retorna None.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from services.texto_util import colapsar_paragrafos, remover_emojis

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]

ANALISE_ATIVA = os.getenv("ANALISE_ATIVA", "1") not in ("0", "false", "False", "")
ANALISE_MODELO = os.getenv("ANALISE_MODELO", "sonnet")
ANALISE_TIMEOUT_SEG = int(os.getenv("ANALISE_TIMEOUT_SEG", "180"))


def _resolver_claude_cmd() -> list[str]:
    """Resolve o comando base do Claude CLI (no Windows, `claude.cmd` exige
    prefixo `cmd.exe /c`). Portado do chat_service do PAINEL SISDPU."""
    resolved = shutil.which("claude")
    if not resolved:
        return ["claude"]
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    return [resolved]


CLAUDE_CMD: list[str] = _resolver_claude_cmd()


def disponivel() -> bool:
    """True se o binario `claude` esta acessivel e a analise esta ativa."""
    return ANALISE_ATIVA and shutil.which("claude") is not None


_INSTRUCOES = (
    "Você auxilia servidores da Defensoria Pública da União na central de contatos "
    "com assistidos. Recebe (A) as últimas movimentações de um PAJ e (B) uma lista de "
    "COMUNICADOS PADRÃO (modelos de mensagem) aplicáveis à caixa em que este PAJ está.\n\n"
    "Faça TRÊS tarefas e responda em JSON:\n"
    "1. \"resumo\": em 1-2 frases objetivas, QUAL a providência de contato necessária "
    "com o assistido. Se não der para identificar, use \"Verificar movimentações manualmente\".\n"
    "2. \"tipo\": verifique se o caso se enquadra PERFEITA e INDUBITAVELMENTE em UM dos "
    "comunicados padrão. Se sim, retorne o texto EXATO do \"tipo\" correspondente. Se tiver "
    "QUALQUER dúvida, retorne null — não force o enquadramento.\n"
    "3. \"sugestao\": a mensagem PRONTA para enviar ao assistido, em linguagem simples e "
    "cordial. FORMATE em parágrafos curtos, um por linha, separados por UMA ÚNICA quebra de "
    "linha (\\n) — NUNCA use linha em branco/parágrafo vazio entre eles (NADA de \\n\\n). "
    "Também NÃO escreva tudo em um único bloco de texto corrido.\n"
    "   - IMPORTANTE sobre VARIÁVEIS: os comunicados podem conter campos entre [colchetes] "
    "(ex.: [processo], [chave], [juízo]). Para usar um comunicado, você DEVE preencher TODOS "
    "os seus [campos] com dados REAIS extraídos das movimentações. Se algum campo obrigatório "
    "NÃO puder ser determinado com certeza a partir das movimentações, NÃO use esse comunicado: "
    "retorne \"tipo\" = null e, em \"sugestao\", escreva EXATAMENTE "
    "\"Verifique o PAJ — dados padrão não identificados: \" seguido da lista dos campos que "
    "faltaram (ex.: [chave], [juízo]). NÃO coloque emoji nem símbolo antes desse aviso.\n"
    "   - Se \"tipo\" foi definido e todos os campos preenchidos, entregue o texto do comunicado "
    "já completo com os dados do caso. Se \"tipo\" for null e não for o caso do aviso acima, "
    "redija um texto específico e adequado a este caso concreto.\n\n"
    "SOMENTE TEXTO: NÃO use emojis, ícones nem símbolos gráficos de qualquer tipo em "
    "NENHUM campo (nem ⚠️, ✅, ❌, setas etc.) — apenas letras, números e pontuação comum. "
    "As mensagens são enviadas ao assistido como texto puro.\n"
    "ATENÇÃO AO FORMATO: dentro dos VALORES de texto do JSON, NUNCA use aspas duplas "
    "(\"). Se precisar citar algo (ex.: a descrição de um trâmite), use aspas simples "
    "('') ou não use aspas. Aspas duplas não escapadas quebram o JSON.\n"
    "Responda APENAS com um objeto JSON válido, sem cercas de código, no formato: "
    '{"resumo": "...", "tipo": "..." ou null, "sugestao": "..."}\n\n'
)


# Núcleo estável do aviso que a IA emite quando NÃO consegue preencher os dados
# obrigatórios de um comunicado padrão (ver _INSTRUCOES). Usado para DETECTAR o
# caso e destacar a sugestão em vermelho (conferência manual). Sem emoji — só texto.
_NUCLEO_CONFERENCIA = "dados padrão não identificados"


def precisa_conferencia_manual(sugestao: str | None) -> bool:
    """True quando a sugestão é o aviso de dados padrão não identificados — sinal de
    que o usuário precisa conferir o PAJ manualmente (destacar em vermelho no painel)."""
    return bool(sugestao) and _NUCLEO_CONFERENCIA in sugestao.lower()


def _desescapar_json(s: str) -> str:
    """Converte as sequências de escape JSON mais comuns em um valor extraído
    manualmente (usado só no fallback tolerante, quando json.loads falhou)."""
    return (
        s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
         .replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")
    ).strip()


def _extrair_campos_tolerante(t: str) -> dict | None:
    """Recupera resumo/tipo/sugestao de um JSON MALFORMADO — tipicamente quando o
    modelo deixa aspas duplas NÃO escapadas dentro dos valores (ex.: ecoa
    descrição: "Pedir documentos" dentro do resumo). Captura cada campo pelos
    limites das chaves conhecidas, tolerando aspas internas."""
    campos: dict = {}
    # resumo: do abre-aspas até o fecha-aspas seguido de , "tipo" OU , "sugestao"
    mr = re.search(r'"resumo"\s*:\s*"(.*?)"\s*,\s*"(?:tipo|sugestao)"\s*:', t, re.DOTALL)
    if mr:
        campos["resumo"] = _desescapar_json(mr.group(1))
    # tipo: null ou "texto" (só existe na análise individual)
    mt = re.search(r'"tipo"\s*:\s*(null|"(.*?)")\s*,\s*"sugestao"\s*:', t, re.DOTALL)
    if mt:
        campos["tipo"] = "" if mt.group(1) == "null" else _desescapar_json(mt.group(2) or "")
    # sugestao: do abre-aspas até o último fecha-aspas antes do } final
    ms = re.search(r'"sugestao"\s*:\s*"(.*)"\s*\}?\s*$', t, re.DOTALL)
    if ms:
        campos["sugestao"] = _desescapar_json(ms.group(1))
    return campos if (campos.get("resumo") or campos.get("sugestao")) else None


def _extrair_json(texto: str) -> dict | None:
    """Extrai o objeto JSON da resposta do modelo (tolera prosa/cercas ao redor).
    Se o JSON estiver malformado (erro comum: aspas duplas não escapadas dentro
    dos valores), cai numa extração tolerante por chaves conhecidas — evitando
    despejar o JSON cru no painel."""
    if not texto:
        return None
    # Remove cercas de código ```json ... ``` (se houver).
    t = texto.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t).strip()
    # 1) parse estrito do texto limpo.
    with contextlib.suppress(Exception):
        return json.loads(t)
    # 2) parse estrito do primeiro { ... } encontrado.
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        with contextlib.suppress(Exception):
            return json.loads(m.group(0))
    # 3) fallback tolerante (aspas internas não escapadas etc.).
    return _extrair_campos_tolerante(t)


def analisar_paj(movimentacoes: list[dict], comunicados: list[dict] | None = None,
                 caixa_nome: str = "", tramite_info: dict | None = None) -> dict | None:
    """Analisa as movimentacoes + comunicados padrao (da caixa) e retorna
    {"resumo", "tipo", "sugestao"} (ou None se indisponivel/erro/cota).

    - resumo: providencia de contato necessaria.
    - tipo: tipo do comunicado padrao em que o caso se enquadra com certeza, ou "".
    - sugestao: mensagem pronta ao assistido (com [variaveis] preenchidas), ou aviso
      de verificacao manual quando faltarem dados padrao obrigatorios.
    """
    if not ANALISE_ATIVA or not movimentacoes:
        return None

    linhas_mov = []
    for m in movimentacoes:
        desc = (m.get("descricao") or "").strip()[:1500]
        linhas_mov.append(
            f"[seq {m.get('seq')}] {m.get('data', '')} · {m.get('tipo', '')}"
            f"{' · ' + m['fases'] if m.get('fases') else ''}\n{desc}"
        )
    if comunicados:
        linhas_com = [f"- tipo: {c.get('tipo', '')}\n  texto: {c.get('texto', '')}" for c in comunicados]
        bloco_com = "\n".join(linhas_com)
    else:
        bloco_com = "(nenhum comunicado padrão cadastrado)"

    ctx_caixa = f'Este PAJ está na caixa "{caixa_nome}".\n\n' if caixa_nome else ""
    ctx_tramite = ""
    if tramite_info:
        # Descrição do trâmite: o texto que o ofício de origem escreveu ao ENCAMINHAR
        # esta tramitação. Muitas vezes é a fonte MAIS DIRETA do motivo do contato —
        # em PAJs cujas movimentações não explicam o motivo, pode ser a ÚNICA fonte.
        # Por isso entra SEMPRE que existir, independentemente do foco.
        desc_tram = (tramite_info.get("descricao_tramite") or "").strip()[:1500]
        if desc_tram:
            ctx_tramite = (
                "DESCRIÇÃO DA TRAMITAÇÃO (escrita pelo ofício de origem ao encaminhar "
                f'esta tramitação): "{desc_tram}"\n'
                "→ Este texto costuma ser a fonte MAIS DIRETA do motivo do contato. "
                "Quando as movimentações não deixarem o motivo claro, baseie o resumo "
                "e a sugestão PRINCIPALMENTE nesta descrição.\n\n"
            )
        mf = tramite_info.get("mov_foco")
        if mf:
            # Movimentação específica indicada MANUALMENTE pelo usuário (triagem
            # separada): a análise deve se basear NELA.
            desc = (mf.get("descricao") or "").strip()[:1500]
            ctx_tramite += (
                "FOCO OBRIGATÓRIO: a solicitação de contato DESTA tramitação corresponde "
                "EXCLUSIVAMENTE à movimentação abaixo (indicada manualmente). "
                "Baseie o resumo e a sugestão SOMENTE nesta movimentação (e, se ajudar, "
                "na descrição da tramitação acima); ignore solicitações de outras movimentações:\n"
                f"[seq {mf.get('seq')}] {mf.get('data', '')} · {mf.get('tipo', '')}\n{desc}\n\n"
            )
        else:
            partes = []
            if tramite_info.get("data_envio"):
                partes.append(f"enviada em {tramite_info['data_envio']}")
            if tramite_info.get("oficio_origem"):
                partes.append(f"pelo ofício de origem \"{tramite_info['oficio_origem']}\"")
            if tramite_info.get("remetente"):
                partes.append(f"por {tramite_info['remetente']}")
            if partes:
                ctx_tramite += (
                    "FOCO: analise ESPECIFICAMENTE a tramitação "
                    + " ".join(partes)
                    + ". Se houver várias solicitações nas movimentações, priorize a que corresponde "
                    "a essa tramitação.\n\n"
                )
    prompt = (
        _INSTRUCOES
        + ctx_caixa
        + ctx_tramite
        + "(A) MOVIMENTAÇÕES (mais recentes primeiro):\n"
        + "\n\n".join(linhas_mov)
        + "\n\n(B) COMUNICADOS PADRÃO APLICÁVEIS A ESTA CAIXA "
        + "(em ORDEM DE PRIORIDADE: os primeiros são específicos desta caixa; os "
        + "seguintes são gerais/subsidiários. Ao empatar, prefira SEMPRE o que "
        + "aparece PRIMEIRO na lista):\n"
        + bloco_com
    )

    result = _run_claude(prompt)
    if result is None:
        return None
    obj = _extrair_json(result)
    if not obj:
        # Fallback: sem JSON — usa o texto como sugestao e resumo.
        return {"resumo": result[:400], "tipo": "", "sugestao": result}

    tipo = obj.get("tipo")
    return {
        "resumo": colapsar_paragrafos((obj.get("resumo") or "").strip()),
        "tipo": (tipo or "").strip() if isinstance(tipo, str) else "",
        # só texto (sem emojis) e sem linha em branco entre parágrafos
        "sugestao": colapsar_paragrafos(remover_emojis(obj.get("sugestao") or "")),
    }


# Diretiva anti-alucinação — PREPENDIDA a TODA solicitação ao Claude (ver
# _run_claude, ponto único de chamada). Regra inegociável: nunca inventar dados.
_ANTI_ALUCINACAO = (
    "⚠️ REGRA ABSOLUTA E INEGOCIÁVEL — LEIA ANTES DE TUDO:\n"
    "NUNCA invente, presuma, deduza ou alucine qualquer informação. Use "
    "EXCLUSIVAMENTE os dados que estão EXPLÍCITOS no material fornecido abaixo. "
    "NÃO crie nem 'complete' números de processo, chaves, datas, prazos, nomes, "
    "endereços, valores, juízos ou quaisquer fatos que não estejam escritos no "
    "material. Se uma informação necessária NÃO estiver clara e expressa no "
    "material, é OBRIGATÓRIO declarar que ela não foi encontrada (use o marcador "
    "de campo faltante previsto nas instruções) em vez de arriscar um dado. "
    "É SEMPRE preferível sinalizar 'não identificado' a fornecer algo incorreto. "
    "Na dúvida, não preencha.\n\n"
)


def _run_claude(prompt: str) -> str | None:
    """Invoca o Claude CLI (prompt via STDIN) e devolve o texto do resultado
    (ou None em erro/timeout/cota). PREPENDE a diretiva anti-alucinação a todo
    prompt — este é o ponto único por onde passam todas as chamadas ao Claude."""
    prompt = _ANTI_ALUCINACAO + prompt
    cmd = [*CLAUDE_CMD, "-p", "--output-format", "json"]
    if ANALISE_MODELO:
        cmd += ["--model", ANALISE_MODELO]
    try:
        proc = subprocess.run(
            cmd, input=prompt.encode("utf-8"), capture_output=True,
            timeout=ANALISE_TIMEOUT_SEG, cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        log.warning("[analise] timeout ao chamar o Claude")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("[analise] falha ao invocar claude: %s", e)
        return None
    saida = proc.stdout.decode("utf-8", errors="replace").strip()
    if not saida:
        return None
    try:
        env = json.loads(saida)
    except json.JSONDecodeError:
        env = {"result": saida}
    if env.get("is_error"):
        return None
    result = env.get("result")
    if isinstance(result, dict):
        result = result.get("text", "")
    result = (result or "").strip()
    if any(s in result.lower() for s in ("session limit", "usage limit", "hit your")):
        log.warning("[analise] cota de uso atingida")
        return None
    return result


_INSTRUCOES_CONJUNTO = (
    "Você auxilia servidores da Defensoria Pública da União na central de contatos "
    "com assistidos. Este PAJ tem VÁRIAS tramitações pendentes (cada uma pede um "
    "contato com o assistido) que serão atendidas por UMA ÚNICA mensagem.\n\n"
    "Recebe (A) as últimas movimentações do PAJ, (B) a lista de tramitações pendentes "
    "e (C) comunicados padrão da caixa. Faça e responda em JSON:\n"
    "1. \"resumo\": ENUMERE todas as providências de contato pendentes identificadas "
    "nas movimentações E nas DESCRIÇÕES DOS TRÂMITES (ex.: \"1) enviar comprovante de "
    "residência e prontuário em 15 dias; 2) apresentar termo de outorga\"). A descrição "
    "de cada trâmite costuma indicar diretamente o motivo daquela tramitação — use-a. "
    "NÃO omita nenhuma. Se algo não puder ser identificado, diga \"verificar manualmente\".\n"
    "2. \"sugestao\": UMA única mensagem, cordial e em linguagem simples, que atenda a "
    "TODAS as tramitações acima de uma só vez. Formate em parágrafos curtos, um por linha, "
    "separados por UMA ÚNICA quebra de linha (\\n) — NUNCA use linha em branco/parágrafo vazio "
    "entre eles (NADA de \\n\\n). Se um comunicado padrão servir de base, use-o preenchendo "
    "seus [campos] com dados reais das movimentações; se faltar dado obrigatório, escreva no "
    "ponto \"[verificar: campo]\".\n\n"
    "SOMENTE TEXTO: NÃO use emojis, ícones nem símbolos gráficos de qualquer tipo em "
    "NENHUM campo (nem ⚠️, ✅, ❌, setas etc.) — apenas letras, números e pontuação comum. "
    "As mensagens são enviadas ao assistido como texto puro.\n"
    "ATENÇÃO AO FORMATO: dentro dos VALORES de texto do JSON, NUNCA use aspas duplas "
    "(\"). Se precisar citar a descrição de um trâmite, use aspas simples ('') ou não use "
    "aspas — aspas duplas não escapadas quebram o JSON.\n"
    "Responda APENAS com JSON válido, sem cercas: {\"resumo\": \"...\", \"sugestao\": \"...\"}\n\n"
)


def analisar_conjunto(movimentacoes: list[dict], comunicados: list[dict] | None,
                      caixa_nome: str, tramites_info: list[dict]) -> dict | None:
    """Análise CONJUNTA de várias tramitações do mesmo PAJ: sintetiza um único resumo
    (enumerando todas as tramitações) e uma única sugestão que atende a todas."""
    if not ANALISE_ATIVA or not movimentacoes:
        return None
    linhas_mov = []
    for m in movimentacoes:
        desc = (m.get("descricao") or "").strip()[:1500]
        linhas_mov.append(
            f"[seq {m.get('seq')}] {m.get('data', '')} · {m.get('tipo', '')}"
            f"{' · ' + m['fases'] if m.get('fases') else ''}\n{desc}")
    linhas_tram = [
        f"- tramitação de {t.get('data_envio', '')}"
        + (f" (ofício: {t.get('oficio_origem', '')})" if t.get('oficio_origem') else "")
        + (f", enviado por {t.get('remetente', '')}" if t.get('remetente') else "")
        + (f'\n  descrição do trâmite: "{(t.get("descricao_tramite") or "").strip()[:1500]}"'
           if (t.get("descricao_tramite") or "").strip() else "")
        for t in (tramites_info or [])
    ]
    bloco_com = ("\n".join(f"- tipo: {c.get('tipo', '')}\n  texto: {c.get('texto', '')}"
                           for c in comunicados) if comunicados
                 else "(nenhum comunicado padrão cadastrado)")
    ctx_caixa = f'Este PAJ está na caixa "{caixa_nome}".\n\n' if caixa_nome else ""
    prompt = (
        _INSTRUCOES_CONJUNTO + ctx_caixa
        + f"(B) TRÂMITES PENDENTES ({len(linhas_tram)}):\n" + "\n".join(linhas_tram)
        + "\n\n(A) MOVIMENTAÇÕES (mais recentes primeiro):\n" + "\n\n".join(linhas_mov)
        + "\n\n(C) COMUNICADOS PADRÃO DA CAIXA:\n" + bloco_com
    )
    result = _run_claude(prompt)
    if result is None:
        return None
    obj = _extrair_json(result)
    if not obj:
        return {"resumo": result[:400], "tipo": "", "sugestao": result}
    return {
        "resumo": colapsar_paragrafos((obj.get("resumo") or "").strip()),
        "tipo": "",  # síntese conjunta não se enquadra num único tipo
        # só texto (sem emojis) e sem linha em branco entre parágrafos
        "sugestao": colapsar_paragrafos(remover_emojis(obj.get("sugestao") or "")),
    }
