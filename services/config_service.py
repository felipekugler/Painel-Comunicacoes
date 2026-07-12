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

# Prazos (dias) pré-definidos, oferecidos como badges no "Gravar para envio".
# São ADICIONAIS ao prazo default (5) do campo e ao badge fixo "Urgente" (=0).
# Apenas valores POSITIVOS (>=1); o 0 é RESERVADO para "Urgente".
PRAZOS_PREDEF_PADRAO = [5, 10, 15, 30]

# Layout da tabela "Pendentes" no dashboard da caixa:
# - "simples": linha por PAJ; a análise/ações ficam numa linha RETRÁTIL (clique p/ abrir).
# - "completo": um PAJ por vez, em largura total, navegável por setas (carrossel).
LAYOUT_PENDENTES_PADRAO = "simples"
LAYOUTS_PENDENTES = ("simples", "completo")

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
        "layout_pendentes": LAYOUT_PENDENTES_PADRAO,
        "comunicados_rev": 1,
        "comunicados": [{"id": uuid.uuid4().hex[:8], **c} for c in _COMUNICADOS_SEED],
        "paragrafos": [{"id": uuid.uuid4().hex[:8], **p} for p in _PARAGRAFOS_SEED],
        "envio_modos": {"app_sisdpu": "simulado", "whatsapp": "simulado", "email": "simulado"},
        "prazos_predefinidos": list(PRAZOS_PREDEF_PADRAO),
        "caixas_disponiveis": [],
        "caixas": {},
    }


def ler_config() -> dict:
    if CONFIG_FILE.exists():
        with contextlib.suppress(Exception):
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            d.setdefault("n_movimentacoes", N_MOVIMENTACOES_PADRAO)
            d.setdefault("analise_concorrencia", ANALISE_CONCORRENCIA_PADRAO)
            d.setdefault("layout_pendentes", LAYOUT_PENDENTES_PADRAO)
            d.setdefault("comunicados", [])
            d.setdefault("paragrafos", [])
            d.setdefault("comunicados_rev", 1)
            d.setdefault("caixas_disponiveis", [])
            d.setdefault("prazos_predefinidos", list(PRAZOS_PREDEF_PADRAO))
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


# ---------------- layout da tabela "Pendentes" ----------------
def get_layout_pendentes() -> str:
    v = ler_config().get("layout_pendentes", LAYOUT_PENDENTES_PADRAO)
    return v if v in LAYOUTS_PENDENTES else LAYOUT_PENDENTES_PADRAO


def set_layout_pendentes(v: str) -> str:
    d = ler_config()
    d["layout_pendentes"] = v if v in LAYOUTS_PENDENTES else LAYOUT_PENDENTES_PADRAO
    _gravar(d)
    return d["layout_pendentes"]


# ---------------- prazos pré-definidos (badges do "Gravar para envio") ----------------
def _norm_prazo(v) -> int | None:
    """Normaliza um prazo pré-definido: inteiro >= 1. None se inválido.
    O 0 NÃO é aceito aqui (é reservado para "Urgente", oferecido por badge fixo)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def get_prazos_predefinidos() -> list[int]:
    """Lista de prazos (dias) pré-definidos, ordenada (0 = 'sem prazo' primeiro)."""
    bruto = ler_config().get("prazos_predefinidos", PRAZOS_PREDEF_PADRAO)
    vals = sorted({n for n in (_norm_prazo(v) for v in (bruto or [])) if n is not None})
    return vals


def add_prazo_predef(valor) -> list[int]:
    n = _norm_prazo(valor)
    d = ler_config()
    atuais = [x for x in (_norm_prazo(v) for v in d.get("prazos_predefinidos", [])) if x is not None]
    if n is not None and n not in atuais:
        atuais.append(n)
    d["prazos_predefinidos"] = sorted(set(atuais))
    _gravar(d)
    return d["prazos_predefinidos"]


def del_prazo_predef(valor) -> list[int]:
    n = _norm_prazo(valor)
    d = ler_config()
    atuais = [x for x in (_norm_prazo(v) for v in d.get("prazos_predefinidos", [])) if x is not None]
    d["prazos_predefinidos"] = sorted({x for x in atuais if x != n})
    _gravar(d)
    return d["prazos_predefinidos"]


def comunicados_rev() -> int:
    return ler_config().get("comunicados_rev", 1)


# ---------------- modo de envio ----------------
# "simulado" (padrão): "Enviar Agora" não dispara nada de verdade, só simula e
# registra "[simulado]" — para validar o fluxo antes de configurar cada meio.
# "real": usa os handlers de envio_service (meios sem handler ficam pendentes).
# Modo de envio POR MEIO (permite ligar 1 meio em real e manter os outros simulados).
_MEIOS_ENVIO = ("app_sisdpu", "whatsapp", "email")
_MEIO_LABELS = {"app_sisdpu": "Aplicativo SISDPU", "whatsapp": "WhatsApp", "email": "E-mail"}


def _norm_modo(v) -> str:
    return "real" if str(v).strip().lower() == "real" else "simulado"


def get_envio_modos() -> dict:
    """Modo ('simulado'/'real') de CADA meio. Migra do antigo `envio_modo` global."""
    d = ler_config()
    modos = d.get("envio_modos")
    if not isinstance(modos, dict):  # migração do formato antigo (global)
        antigo = _norm_modo(d.get("envio_modo", "simulado"))
        modos = {m: antigo for m in _MEIOS_ENVIO}
    return {m: _norm_modo(modos.get(m, "simulado")) for m in _MEIOS_ENVIO}


def get_envio_modo_meio(meio: str) -> str:
    """Modo de UM meio ('simulado'/'real'). Padrão simulado."""
    return get_envio_modos().get(meio, "simulado")


def set_envio_modo_meio(meio: str, modo: str) -> dict:
    if meio not in _MEIOS_ENVIO:
        return get_envio_modos()
    d = ler_config()
    modos = get_envio_modos()
    modos[meio] = _norm_modo(modo)
    d["envio_modos"] = modos
    d.pop("envio_modo", None)  # some com o formato antigo
    _gravar(d)
    return modos


def get_envio_modo() -> str:
    """Resumo global: 'simulado' só se TODOS os meios estiverem simulados; senão 'real'.
    (Usado apenas para avisos/banners; o disparo consulta o modo POR MEIO.)"""
    return "simulado" if all(v == "simulado" for v in get_envio_modos().values()) else "real"


def get_whatsapp_headless() -> bool:
    """Se True, o Chrome do WhatsApp roda OCULTO. Padrão False (headed — o servidor
    vê a janela do envio acontecer na máquina dele)."""
    return bool(ler_config().get("whatsapp_headless", False))


# ---------------- abrir o painel em tela cheia ----------------
def get_abrir_tela_cheia() -> bool:
    """Se True, o lançador (Painel-DIVCOM.bat → abrir_painel.py) abre o painel em
    TELA CHEIA (Chrome/Edge --start-fullscreen, equivalente ao F11 no arranque).
    Padrão False (abre como aba do navegador padrão)."""
    return bool(ler_config().get("abrir_tela_cheia", False))


def set_abrir_tela_cheia(v: bool) -> bool:
    d = ler_config()
    d["abrir_tela_cheia"] = bool(v)
    _gravar(d)
    return d["abrir_tela_cheia"]


def get_whatsapp_intervalo_seg() -> int:
    """Intervalo (segundos) entre mensagens no ENVIO EM LOTE por WhatsApp — para
    imitar ritmo humano e reduzir risco de banimento. Padrão 25s (mín. 5)."""
    try:
        n = int(ler_config().get("whatsapp_intervalo_seg", 25))
    except (TypeError, ValueError):
        n = 25
    return max(5, n)


def set_whatsapp_intervalo_seg(seg: int) -> int:
    d = ler_config()
    try:
        n = max(5, int(seg))
    except (TypeError, ValueError):
        n = 25
    d["whatsapp_intervalo_seg"] = n
    _gravar(d)
    return n


def resumo_modos() -> str:
    """Texto curto do modo de cada meio, p/ logs/avisos. Ex.: 'App=REAL · WhatsApp=simulado'."""
    modos = get_envio_modos()
    return " · ".join(f"{_MEIO_LABELS[m]}={modos[m].upper() if modos[m]=='real' else modos[m]}"
                      for m in _MEIOS_ENVIO)


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


# ---------------- anexos PADRÃO (por comunicado ou parágrafo) ----------------
# Documentos pré-configurados que são anexados automaticamente à tramitação quando
# o texto/parágrafo padrão é APLICADO no dashboard. Os arquivos ficam no disco (fora
# do config); aqui guardamos só os metadados na lista `anexos` do dono. NÃO alteram
# comunicados_rev (não fazem parte do prompt da IA).
def _owner_padrao(d: dict, kind: str, oid: str, slug: str | None):
    lista = _lista_comunicados(d, slug) if kind == "comunicado" else _lista_paragrafos(d, slug)
    for o in lista:
        if o.get("id") == oid:
            return o
    return None


def owner_padrao(kind: str, oid: str, slug: str | None = None) -> dict | None:
    """Retorna o comunicado/parágrafo (dict) por id, ou None."""
    return _owner_padrao(ler_config(), kind, oid, slug)


def anexos_padrao(kind: str, oid: str, slug: str | None = None) -> list[dict]:
    o = owner_padrao(kind, oid, slug)
    return list(o.get("anexos", [])) if o else []


def set_anexos_padrao(kind: str, oid: str, slug: str | None, anexos: list[dict]) -> bool:
    """Grava a lista de anexos padrão do dono (comunicado/parágrafo). Não bump rev."""
    d = ler_config()
    o = _owner_padrao(d, kind, oid, slug)
    if o is None:
        return False
    o["anexos"] = anexos
    _gravar(d)
    return True
