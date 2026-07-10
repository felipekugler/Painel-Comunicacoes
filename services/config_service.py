"""Configurações do Painel DIVCOM (persistidas em config_divcom.json).

- n_movimentacoes: quantas das últimas movimentações do PAJ enviar ao Claude.
- comunicados: modelos de texto padrão GERAIS (fallback p/ caixas sem os próprios).
- comunicados_rev: contador incrementado a cada mudança em QUALQUER comunicado —
  entra na chave de cache da análise.
- caixas_disponiveis: nomes de todas as caixas de grupo vistas na sincronização.
- caixas: caixas HABILITADAS no painel — dict slug -> {grupo, nome, comunicados}.
  Cada caixa pode ter comunicados próprios (com variáveis em [colchetes]); se não
  tiver, aplicam-se os comunicados gerais.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import unicodedata
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = BASE_DIR / "config_divcom.json"

N_MOVIMENTACOES_PADRAO = 5

# Quantas análises de IA rodam EM PARALELO na sincronização (1 = sequencial).
ANALISE_CONCORRENCIA_PADRAO = 6
ANALISE_CONCORRENCIA_MAX = 8

CAIXA_DIVCOM_GRUPO = "01 - PREV_DIVCOM"

_COMUNICADOS_SEED = [
    {"tipo": "Comunicado de perícia médica",
     "texto": "Olá! Aqui é da Defensoria Pública da União. Informamos que foi agendada uma "
              "perícia médica no seu processo. Anote a data, o horário e o local e compareça "
              "levando documento de identidade e todos os seus laudos e exames médicos. "
              "Qualquer dúvida, estamos à disposição."},
    {"tipo": "Pedido de informações ou documentos",
     "texto": "Olá! Aqui é da Defensoria Pública da União. Para dar andamento ao seu caso, "
              "precisamos que você nos envie alguns documentos/informações. Por favor, "
              "responda esta mensagem com o material solicitado assim que possível."},
    {"tipo": "Aviso de ajuizamento de ação",
     "texto": "Olá! Aqui é da Defensoria Pública da União. Informamos que sua ação judicial "
              "foi ajuizada. Vamos acompanhar o processo e avisaremos sobre os próximos passos. "
              "Não é necessário comparecer no momento."},
    {"tipo": "Orientação para novo requerimento administrativo",
     "texto": "Olá! Aqui é da Defensoria Pública da União. Orientamos que você faça um novo "
              "requerimento administrativo junto ao INSS. Podemos ajudar com as orientações "
              "necessárias — entre em contato conosco."},
]

_PARAGRAFOS_SEED = [
    {"titulo": "Desfecho",
     "texto": "Qualquer dúvida, estamos à disposição.\nDPU Curitiba"},
]


# ---------------- slug / nome de caixa ----------------
def slugify(texto: str) -> str:
    """"01 - PREV_DIVCOM" -> "divcom"; "01- PREV_ANÁLISE BI" -> "analise-bi"."""
    t = texto or ""
    # remove o prefixo "NN - PREV_" / "NN- PREV_" se houver
    m = re.search(r"PREV[_\s-]*(.+)$", t, re.IGNORECASE)
    if m:
        t = m.group(1)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "caixa"


def nome_caixa(grupo: str) -> str:
    """"01 - PREV_AJUIZADOS" -> "Caixa Ajuizados"."""
    t = grupo or ""
    m = re.search(r"PREV[_\s-]*(.+)$", t, re.IGNORECASE)
    base = m.group(1) if m else t
    base = base.replace("_", " ").strip().title()
    return f"Caixa {base}"


def _default() -> dict:
    """Padrão de fábrica = painel de primeiro uso: NENHUMA caixa (o usuário
    identifica com "Atualizar lista" e habilita), mas COM as mensagens/parágrafos
    -semente como Mensagens Gerais (exemplos de como os textos padrão funcionam)."""
    return {
        "n_movimentacoes": N_MOVIMENTACOES_PADRAO,
        "analise_concorrencia": ANALISE_CONCORRENCIA_PADRAO,
        "comunicados_rev": 1,
        "comunicados": [{"id": uuid.uuid4().hex[:8], **c} for c in _COMUNICADOS_SEED],
        "paragrafos": [{"id": uuid.uuid4().hex[:8], **p} for p in _PARAGRAFOS_SEED],
        "envio_modo": "simulado",
        "caixas_disponiveis": [],
        "caixas": {},
    }


def ler_config() -> dict:
    if CONFIG_FILE.exists():
        with contextlib.suppress(Exception):
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            d.setdefault("n_movimentacoes", N_MOVIMENTACOES_PADRAO)
            d.setdefault("analise_concorrencia", ANALISE_CONCORRENCIA_PADRAO)
            d.setdefault("comunicados", [])
            d.setdefault("paragrafos", [])
            d.setdefault("comunicados_rev", 1)
            d.setdefault("caixas_disponiveis", [])
            d.setdefault("caixas", {})  # pode ficar vazio (reset total / primeiro uso)
            return d
    d = _default()
    _gravar(d)
    return d


def _gravar(d: dict) -> None:
    # Backup de 1 nível: preserva a versão anterior antes de sobrescrever, para
    # permitir recuperação em caso de perda acidental de configuração.
    if CONFIG_FILE.exists():
        with contextlib.suppress(Exception):
            shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_suffix(".json.bak"))
    CONFIG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _bump_rev(d: dict) -> None:
    d["comunicados_rev"] = int(d.get("comunicados_rev", 1)) + 1


def resetar_config() -> None:
    """Restaura o config ao padrão de fábrica (usado no RESET TOTAL). O _gravar
    salva config_divcom.json.bak antes, então é reversível."""
    _gravar(_default())


# ---------------- nº de movimentações ----------------
def get_n_movimentacoes() -> int:
    n = ler_config().get("n_movimentacoes", N_MOVIMENTACOES_PADRAO)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = N_MOVIMENTACOES_PADRAO
    return max(1, min(n, 30))


def set_n_movimentacoes(n: int) -> int:
    d = ler_config()
    d["n_movimentacoes"] = max(1, min(int(n), 30))
    _gravar(d)
    return d["n_movimentacoes"]


# ---------------- concorrência da análise IA ----------------
def get_analise_concorrencia() -> int:
    n = ler_config().get("analise_concorrencia", ANALISE_CONCORRENCIA_PADRAO)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = ANALISE_CONCORRENCIA_PADRAO
    return max(1, min(n, ANALISE_CONCORRENCIA_MAX))


def set_analise_concorrencia(n: int) -> int:
    d = ler_config()
    d["analise_concorrencia"] = max(1, min(int(n), ANALISE_CONCORRENCIA_MAX))
    _gravar(d)
    return d["analise_concorrencia"]


def comunicados_rev() -> int:
    return ler_config().get("comunicados_rev", 1)


# ---------------- modo de envio ----------------
# "simulado" (padrão): "Enviar Agora" não dispara nada de verdade, só simula e
# registra "[simulado]" — para validar o fluxo antes de configurar cada meio.
# "real": usa os handlers de envio_service (meios sem handler ficam pendentes).
def get_envio_modo() -> str:
    modo = str(ler_config().get("envio_modo", "simulado")).strip().lower()
    return modo if modo in ("simulado", "real") else "simulado"


def set_envio_modo(modo: str) -> str:
    d = ler_config()
    d["envio_modo"] = "real" if str(modo).strip().lower() == "real" else "simulado"
    _gravar(d)
    return d["envio_modo"]


# ---------------- caixas ----------------
def set_caixas_disponiveis(grupos: list[str]) -> None:
    """Atualiza a lista de caixas de grupo vistas na sincronização."""
    d = ler_config()
    # mantém a ordem, remove duplicatas
    vistos, lista = set(), []
    for g in grupos:
        g = (g or "").strip()
        if g and g not in vistos:
            vistos.add(g)
            lista.append(g)
    if lista:
        d["caixas_disponiveis"] = lista
        _gravar(d)


def listar_caixas_disponiveis() -> list[str]:
    return ler_config().get("caixas_disponiveis", [])


def listar_caixas_habilitadas() -> list[dict]:
    """Lista as caixas habilitadas: [{slug, grupo, nome}] na MESMA ORDEM da lista
    de caixas identificadas (caixas_disponiveis). Caixas cujo grupo não está na
    lista vão para o fim (mantendo a ordem de inserção)."""
    d = ler_config()
    caixas = d.get("caixas", {})
    ordem = {g: i for i, g in enumerate(d.get("caixas_disponiveis", []))}
    out = [{"slug": s, "grupo": c.get("grupo", ""), "nome": c.get("nome", s)}
           for s, c in caixas.items() if c.get("habilitada", True)]
    out.sort(key=lambda c: ordem.get(c["grupo"], 10_000))
    return out


def caixa_habilitada(grupo: str) -> bool:
    return any(c.get("grupo") == grupo and c.get("habilitada", True)
               for c in ler_config().get("caixas", {}).values())


def listar_caixas_todas() -> list[dict]:
    """TODAS as caixas com entrada no config (habilitadas E desabilitadas), com a
    flag `habilitada`. Usada para montar as abas de Configurações (a aba de uma
    caixa desabilitada aparece inativa, mas seus dados seguem preservados)."""
    d = ler_config()
    caixas = d.get("caixas", {})
    ordem = {g: i for i, g in enumerate(d.get("caixas_disponiveis", []))}
    out = [{"slug": s, "grupo": c.get("grupo", ""), "nome": c.get("nome", s),
            "habilitada": bool(c.get("habilitada", True))}
           for s, c in caixas.items()]
    out.sort(key=lambda c: (not c["habilitada"], ordem.get(c["grupo"], 10_000)))
    return out


def excluir_caixa(slug: str) -> bool:
    """Remove por completo a entrada de uma caixa (aba + comunicados + parágrafos).
    Só permitido para caixas DESABILITADAS (não sincronizadas)."""
    d = ler_config()
    c = d.get("caixas", {}).get(slug)
    if c is None:
        return False
    if c.get("habilitada", True):
        return False  # não exclui caixa sincronizada
    d["caixas"].pop(slug, None)
    _bump_rev(d)
    _gravar(d)
    return True


def limpar_caixa(slug: str) -> bool:
    """Zera os comunicados e parágrafos padrão de uma caixa (mantém a entrada)."""
    d = ler_config()
    c = d.get("caixas", {}).get(slug)
    if c is None:
        return False
    c["comunicados"] = []
    c["paragrafos"] = []
    _bump_rev(d)  # comunicados entram na chave de cache da IA
    _gravar(d)
    return True


def get_caixa(slug: str) -> dict | None:
    c = ler_config().get("caixas", {}).get(slug)
    if not c:
        return None
    return {"slug": slug, "grupo": c.get("grupo", ""), "nome": c.get("nome", slug),
            "comunicados": c.get("comunicados", []), "paragrafos": c.get("paragrafos", [])}


def habilitar_caixa(grupo: str, on: bool) -> None:
    """Habilita/desabilita uma caixa de grupo no painel.

    Ao DESABILITAR, a entrada da caixa é PRESERVADA (só marca habilitada=False),
    mantendo seus comunicados e parágrafos padrão — reabilitar restaura tudo.
    """
    d = ler_config()
    caixas = d.setdefault("caixas", {})
    slug = slugify(grupo)
    c = caixas.get(slug)
    if on:
        if c is None:
            caixas[slug] = {"grupo": grupo, "nome": nome_caixa(grupo),
                            "comunicados": [], "paragrafos": [], "habilitada": True}
        else:
            c["habilitada"] = True
    elif c is not None:
        c["habilitada"] = False  # NÃO apaga comunicados/parágrafos
    _gravar(d)


def comunicados_da_caixa(slug: str) -> list[dict]:
    """Comunicados aplicáveis à caixa, com SUBSIDIARIEDADE: os PRÓPRIOS da caixa
    (prioridade) MAIS os GERAIS (subsidiários). Próprios primeiro; dedup por id.
    Assim, um caso que não casa com nenhum texto da caixa pode cair nos gerais."""
    d = ler_config()
    proprios = d.get("caixas", {}).get(slug, {}).get("comunicados", [])
    gerais = d.get("comunicados", [])
    vistos, out = set(), []
    for c in [*proprios, *gerais]:
        if c.get("id") not in vistos:
            vistos.add(c.get("id"))
            out.append(c)
    return out


# ---------------- comunicados (gerais OU de uma caixa) ----------------
def _lista_comunicados(d: dict, slug: str | None):
    if slug:
        return d.get("caixas", {}).get(slug, {}).setdefault("comunicados", [])
    return d.setdefault("comunicados", [])


def listar_comunicados(slug: str | None = None) -> list[dict]:
    return _lista_comunicados(ler_config(), slug)


def add_comunicado(tipo: str, texto: str, slug: str | None = None) -> dict | None:
    tipo, texto = (tipo or "").strip(), (texto or "").strip()
    if not tipo or not texto:
        return None
    d = ler_config()
    novo = {"id": uuid.uuid4().hex[:8], "tipo": tipo, "texto": texto}
    _lista_comunicados(d, slug).append(novo)
    _bump_rev(d)
    _gravar(d)
    return novo


def update_comunicado(cid: str, tipo: str, texto: str, slug: str | None = None) -> bool:
    tipo, texto = (tipo or "").strip(), (texto or "").strip()
    if not tipo or not texto:
        return False
    d = ler_config()
    for c in _lista_comunicados(d, slug):
        if c.get("id") == cid:
            c["tipo"], c["texto"] = tipo, texto
            _bump_rev(d)
            _gravar(d)
            return True
    return False


def delete_comunicado(cid: str, slug: str | None = None) -> bool:
    d = ler_config()
    lista = _lista_comunicados(d, slug)
    antes = len(lista)
    lista[:] = [c for c in lista if c.get("id") != cid]
    if len(lista) != antes:
        _bump_rev(d)
        _gravar(d)
        return True
    return False


# ---------------- parágrafos padrão (gerais OU de uma caixa) ----------------
# São inseridos MANUALMENTE no texto da sugestão (via checkbox no "Ver/Editar").
# NÃO entram na análise da IA — por isso NÃO alteram comunicados_rev.
def _lista_paragrafos(d: dict, slug: str | None):
    if slug:
        return d.get("caixas", {}).get(slug, {}).setdefault("paragrafos", [])
    return d.setdefault("paragrafos", [])


def listar_paragrafos(slug: str | None = None) -> list[dict]:
    return _lista_paragrafos(ler_config(), slug)


def paragrafos_para_caixa(slug: str) -> list[dict]:
    """Parágrafos disponíveis para inserção numa caixa: os PRÓPRIOS da caixa MAIS
    os GERAIS (os gerais valem em todos os textos). Dedup por id."""
    d = ler_config()
    proprios = d.get("caixas", {}).get(slug, {}).get("paragrafos", [])
    gerais = d.get("paragrafos", [])
    vistos, out = set(), []
    for p in [*proprios, *gerais]:
        if p.get("id") not in vistos:
            vistos.add(p.get("id"))
            out.append(p)
    return out


def add_paragrafo(titulo: str, texto: str, slug: str | None = None) -> dict | None:
    titulo, texto = (titulo or "").strip(), (texto or "").strip()
    if not titulo or not texto:
        return None
    d = ler_config()
    novo = {"id": uuid.uuid4().hex[:8], "titulo": titulo, "texto": texto}
    _lista_paragrafos(d, slug).append(novo)
    _gravar(d)
    return novo


def update_paragrafo(pid: str, titulo: str, texto: str, slug: str | None = None) -> bool:
    titulo, texto = (titulo or "").strip(), (texto or "").strip()
    if not titulo or not texto:
        return False
    d = ler_config()
    for p in _lista_paragrafos(d, slug):
        if p.get("id") == pid:
            p["titulo"], p["texto"] = titulo, texto
            _gravar(d)
            return True
    return False


def delete_paragrafo(pid: str, slug: str | None = None) -> bool:
    d = ler_config()
    lista = _lista_paragrafos(d, slug)
    antes = len(lista)
    lista[:] = [p for p in lista if p.get("id") != pid]
    if len(lista) != antes:
        _gravar(d)
        return True
    return False
