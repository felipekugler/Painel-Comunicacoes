"""Serviço de dados do Painel DIVCOM — MULTI-CAIXA (modelo híbrido).

Armazenamento:
- dados/_contatos/<PAJ>.json  → COMPARTILHADO por PAJ (dados do assistido):
  assistido, contatos{app/tel/whatsapp/email/endereco}, telefones[], emails[],
  enderecos[], whatsapp_config, whatsapp_numeros. Valem em qualquer caixa.
- dados/<slug_caixa>/<PAJ>.json → POR CAIXA: em_caixa + solicitacoes[]. Cada
  item é uma TRAMITAÇÃO (chave id_tramite) e carrega o SEU próprio estado:
  resumo_ia/sugestao_ia/tipo/editada + envio{} (fila) + enviados[] (histórico).
  (A chave de dados `solicitacoes` é histórica; o item que ela guarda é a tramitação.)

Um mesmo PAJ pode estar em várias caixas simultaneamente e ter várias tramitações
na mesma caixa — cada tramitação é tratada de forma independente (pendente /
gravado para envio / já enviado). Os contatos do assistido são únicos (por PAJ).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from ingestao import sisdpu_client
from services import analise_service, config_service

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL DIVCOM/Projeto
# Dados sincronizados FORA da pasta Projeto (ao lado dela).
DADOS_DIR = BASE_DIR.parent / "dados"
CONTATOS_DIR = DADOS_DIR / "_contatos"

CONTATOS_KEYS = ["app_sisdpu", "telefone", "whatsapp", "email", "endereco"]

# Teto de movimentações extraídas por PAJ em CADA sincronização. O scraping lê a
# tabela inteira do DOM de uma vez (custo ~igual para 5 ou 30), então extraímos
# até este total e guardamos todas. O nº padrão da config (n_movimentacoes) passa
# a ser apenas o CORTE de exibição/IA, não o limite de extração.
MOVS_EXTRACAO_MAX = 30

# Paleta de cores por caixa (identificação visual em Prontos, Histórico, etc.).
_PALETA_CAIXAS = ["#2563eb", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
                  "#db2777", "#ca8a04", "#dc2626", "#4f46e5", "#0d9488"]


def cor_caixa(slug: str) -> str:
    """Cor ESTÁVEL de uma caixa (mesma cor em qualquer tela). Atribuída pela ordem
    da caixa na config; com fallback por hash do slug (para caixas fora da lista,
    ex.: registros antigos do histórico)."""
    for i, c in enumerate(config_service.listar_caixas_todas()):
        if c.get("slug") == slug:
            return _PALETA_CAIXAS[i % len(_PALETA_CAIXAS)]
    h = int(hashlib.sha1((slug or "").encode("utf-8")).hexdigest(), 16)
    return _PALETA_CAIXAS[h % len(_PALETA_CAIXAS)]

# A concorrência das análises de IA (quantas rodam EM PARALELO no sync; 1 =
# sequencial) é configurável na tela de Configurações e lida DINAMICAMENTE no
# início da Fase 2 via config_service.get_analise_concorrencia() — sem restart.


def normalizar_paj(paj: str) -> str:
    m = re.match(r"\s*(\d{4})/(\d{3})-(\d+)", paj or "")
    if m:
        return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return (paj or "").strip().replace("/", "-") or "PAJ-DESCONHECIDO"


def _data_key(s: str) -> str:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}{m.group(2)}{m.group(1)}" if m else ""


def _dt_seg(s: str):
    """Converte 'dd/mm/aaaa [HH:MM[:SS]]' em segundos absolutos (p/ comparar
    proximidade). Sem hora → 00:00:00. Retorna None se não casar."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:[ T]+(\d{2}):(\d{2})(?::(\d{2}))?)?", s or "")
    if not m:
        return None
    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi, sec = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
    with contextlib.suppress(Exception):
        return _dt.date(yy, mm, dd).toordinal() * 86400 + h * 3600 + mi * 60 + sec
    return None


def _contatos_vazio() -> dict:
    return {k: False for k in CONTATOS_KEYS}


def _read_json(f: Path) -> dict:
    if f.exists():
        with contextlib.suppress(Exception):
            return json.loads(f.read_text(encoding="utf-8"))
    return {}


def _write_json(f: Path, dados: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


# ==================== CONTATOS (compartilhado por PAJ) ====================
def _contato_file(paj_norm: str) -> Path:
    return CONTATOS_DIR / f"{paj_norm}.json"


def ler_contato(paj_norm: str) -> dict:
    d = _read_json(_contato_file(paj_norm))
    d.setdefault("paj_norm", paj_norm)
    d.setdefault("assistido", "")
    d.setdefault("contatos", _contatos_vazio())
    for k in ("telefones", "emails", "enderecos", "whatsapp_numeros", "movimentacoes",
              "movs_selecao"):
        d.setdefault(k, [])
    d.setdefault("whatsapp_config", False)
    d.setdefault("movs_texto_ia", "")
    return d


def _gravar_contato(d: dict) -> None:
    _write_json(_contato_file(d["paj_norm"]), d)


def movs_para_ia(cont: dict) -> list[dict]:
    """Movimentações que vão à IA para este PAJ (de todas as extraídas):
    1) se houver RECORTE MANUAL (movs_texto_ia), ele PREVALECE — vira um bloco único;
    2) senão, as SELECIONADAS (movs_selecao, por seq);
    3) senão, as N mais recentes (corte padrão da config)."""
    todas = cont.get("movimentacoes", []) or []
    texto = (cont.get("movs_texto_ia") or "").strip()
    if texto:
        return [{"seq": "M", "data": "", "tipo": "recorte manual do usuário",
                 "fases": "", "descricao": texto[:4000]}]
    sel = cont.get("movs_selecao") or []
    if sel:
        alvo = {str(s) for s in sel}
        escolhidas = [m for m in todas if str(m.get("seq")) in alvo]
        if escolhidas:
            return escolhidas
    n = config_service.get_n_movimentacoes()
    return todas[:n]


def _chave_movs(cont: dict, movs_ia: list[dict]) -> str:
    """Componente de cache que reflete O QUE vai à IA (recorte manual ou seqs).
    Muda ⇒ a análise é refeita."""
    texto = (cont.get("movs_texto_ia") or "").strip()
    if texto:
        return "txt" + _desc_hash(texto)
    return "s" + ",".join(str(m.get("seq")) for m in movs_ia)


def _marcar_selecao(cont: dict) -> list[dict]:
    """Cópia das movimentações com flag `sel` (marcada para a IA) — alimenta os
    checkboxes do popup. Default: seleção salva; senão, as N mais recentes."""
    todas = cont.get("movimentacoes", []) or []
    sel = cont.get("movs_selecao") or []
    if sel:
        alvo = {str(s) for s in sel}
        marcada = lambda m: str(m.get("seq")) in alvo  # noqa: E731
    else:
        n = config_service.get_n_movimentacoes()
        recentes = {str(m.get("seq")) for m in todas[:n]}
        marcada = lambda m: str(m.get("seq")) in recentes  # noqa: E731
    return [{**m, "sel": marcada(m)} for m in todas]


def atualizar_contato_do_detalhe(paj_norm: str, det: dict) -> dict:
    """Atualiza o contato compartilhado com dados frescos do detalhamento.
    Preserva a escolha manual de WhatsApp."""
    d = ler_contato(paj_norm)
    if det.get("assistido"):
        d["assistido"] = det["assistido"]
    if det.get("contatos"):
        d["contatos"] = det["contatos"]
    d["telefones"] = det.get("telefones", [])
    d["emails"] = det.get("emails", [])
    d["enderecos"] = det.get("enderecos", [])
    d["movimentacoes"] = det.get("movimentacoes", [])
    d["contatos"]["whatsapp"] = bool(d.get("whatsapp_numeros"))
    d["atualizado_em"] = _dt.datetime.now().isoformat(timespec="seconds")
    _gravar_contato(d)
    return d


def definir_whatsapp(paj_norm: str, numeros: list[str]) -> dict:
    """Escolha MANUAL de números de WhatsApp (compartilhada entre caixas)."""
    d = ler_contato(paj_norm)
    numeros = [n.strip() for n in (numeros or []) if n and n.strip()]
    d["whatsapp_numeros"] = numeros
    d["whatsapp_config"] = True
    d.setdefault("contatos", _contatos_vazio())
    d["contatos"]["whatsapp"] = len(numeros) > 0
    _gravar_contato(d)
    return d


# ==================== POR CAIXA ====================
def _box_dir(slug: str) -> Path:
    return DADOS_DIR / slug


def _box_file(slug: str, paj_norm: str) -> Path:
    return _box_dir(slug) / f"{paj_norm}.json"


def _ler_box(slug: str, paj_norm: str) -> dict:
    return _read_json(_box_file(slug, paj_norm))


def _gravar_box(slug: str, dados: dict) -> None:
    _write_json(_box_file(slug, dados["paj_norm"]), dados)


def _desc_hash(texto: str) -> str:
    """Hash curto da descrição do trâmite, para compor a chave de cache da análise
    (`resumo_ia_chave`). Assim, quando a descrição do trâmite é (re)capturada ou
    muda, a chave muda e a IA re-analisa a tramitação — antes a chave dependia só
    das movimentações, então análises antigas não incorporavam a descrição."""
    t = (texto or "").strip()
    if not t:
        return "0"
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:8]


def _solic_de_item(item: dict, anterior: dict | None = None) -> dict:
    """Monta uma tramitação preservando o estado anterior (por
    id_tramite): análise (resumo/sugestão) e estado de envio ficam POR TRAMITAÇÃO."""
    a = anterior or {}
    return {
        "id_tramite": item.get("id_tramite", ""),
        "id": item.get("id", ""),
        "oficio_origem": item.get("oficio_origem", ""),
        "remetente": item.get("remetente", ""),
        "data_envio": item.get("data_envio", ""),
        "prazo": item.get("prazo", ""),
        "url_detalhe": item.get("url_detalhe", ""),
        # dados do popup "Detalhes do trâmite" (quando extraídos)
        "descricao_tramite": item.get("descricao_tramite") or a.get("descricao_tramite", ""),
        "atribuicao": item.get("atribuicao") or a.get("atribuicao", ""),
        # análise POR TRAMITAÇÃO
        "resumo_ia": a.get("resumo_ia", ""),
        "resumo_ia_chave": a.get("resumo_ia_chave", ""),
        "sugestao_ia": a.get("sugestao_ia", ""),
        "sugestao_tipo": a.get("sugestao_tipo", ""),  # classificação da IA
        "sugestao_tipo_editada": a.get("sugestao_tipo_editada", ""),  # classificação MANUAL (prevalece; não é sobrescrita por re-sync)
        "sugestao_editada": a.get("sugestao_editada", ""),
        "mov_seq": a.get("mov_seq", ""),  # movimentação vinculada (triagem separada)
        "anexos": a.get("anexos", []) or [],  # documentos a enviar (por tramitação)
        # estado de envio POR TRÂMITE
        "envio": a.get("envio", {}) or {},
        "enviados": a.get("enviados", []) or [],
    }


def upsert_paj(slug: str, paj: str, itens_tramites: list[dict], *, em_caixa: bool = True) -> dict:
    """Cria/atualiza o registro POR CAIXA de um PAJ. Cada tramitação (id_tramite)
    é independente, com seu próprio estado (análise + envio),
    preservado entre sincronizações."""
    paj_norm = normalizar_paj(paj)
    atual = _ler_box(slug, paj_norm)
    ant_por_id = {s.get("id_tramite", ""): s for s in atual.get("solicitacoes", [])}
    solicitacoes = [_solic_de_item(it, ant_por_id.get(it.get("id_tramite", "")))
                    for it in itens_tramites]
    solicitacoes.sort(key=lambda s: _data_key(s.get("data_envio", "")))
    dados = {
        "paj_norm": paj_norm,
        "paj": paj or atual.get("paj", ""),
        "em_caixa": em_caixa,
        "atualizado_em": _dt.datetime.now().isoformat(timespec="seconds"),
        "solicitacoes": solicitacoes,
    }
    # Preserva a decisão de triagem e o tratamento em conjunto entre syncs
    # (se o conjunto de pendentes mudar, ficam "stale" e são reavaliados —
    # ver _decisao_valida / montar_dashboard).
    if atual.get("triagem"):
        dados["triagem"] = atual["triagem"]
    if atual.get("conjunto"):
        dados["conjunto"] = atual["conjunto"]
    _gravar_box(slug, dados)
    return dados


def _achar_solic(box: dict, id_tramite: str) -> dict | None:
    for s in box.get("solicitacoes", []):
        if s.get("id_tramite") == id_tramite:
            return s
    return None


def definir_sugestao(slug: str, paj_norm: str, id_tramite: str, texto: str,
                     tipo: str | None = None) -> dict:
    """Edição MANUAL da sugestão de UMA tramitação: o texto e, opcionalmente, a
    CLASSIFICAÇÃO (tipo). `tipo=None` mantém a classificação atual; qualquer string
    (aplicada via dropdown de texto padrão) grava a classificação manual, que
    prevalece sobre a da IA e não é sobrescrita por re-sync."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {}
    s = _achar_solic(box, id_tramite)
    if not s:
        return {}
    s["sugestao_editada"] = (texto or "").strip()
    if tipo is not None:
        s["sugestao_tipo_editada"] = (tipo or "").strip()
    _gravar_box(slug, box)
    return box


# ==================== fila de envio (POR TRAMITAÇÃO) ====================
def _prazo_ok(prazo_dias) -> int:
    """Normaliza um prazo (dias) de resposta: inteiro >= 0 (0 = URGENTE), padrão 5."""
    try:
        n = int(prazo_dias)
    except (TypeError, ValueError):
        n = 5
    return n if n >= 0 else 0


def _prazos_envio(prazos: dict | None) -> dict:
    """Constrói os prazos POR MEIO (variáveis separadas) a partir de um dict
    {app_sisdpu, whatsapp, email}. Cada prazo é normalizado (>=1, padrão 5)."""
    prazos = prazos or {}
    return {
        "prazo_app": _prazo_ok(prazos.get("app_sisdpu", 5)),
        "prazo_whatsapp": _prazo_ok(prazos.get("whatsapp", 5)),
        "prazo_email": _prazo_ok(prazos.get("email", 5)),
    }


# Regex das movimentações de ADESÃO ao App (fase "Gerado pelo aplicativo sisdpu";
# descrição "Adesão ao Termo... Assistido: NOME CPF Acompanhante: ..."). Usado para
# saber QUEM aderiu e marcar o assistido certo na tela Notificar Assistido (App)
# quando o PAJ tem mais de um assistido/representante.
_RE_ADESAO_APP = re.compile(
    r"Ades[ãa]o ao Termo de Responsabilidade do Aplicativo do SISDPU", re.I)
# O texto extraído do SISDPU vem SEM espaços nas junções (ex.: "...SISDPUAssistido:
# MARIA AMELIA C DCPF Acompanhante:..."), então NÃO exigir espaço nem \b antes de CPF.
_RE_ADESAO_ASSISTIDO = re.compile(r"Assistido:\s*(.+?)\s*CPF", re.I)


def _aderidos_app(cont: dict) -> list[str]:
    """Nomes dos assistidos que aderiram ao App SISDPU (das movimentações de adesão)."""
    nomes: list[str] = []
    for m in (cont.get("movimentacoes") or []):
        desc = str(m.get("descricao") or "")
        if not _RE_ADESAO_APP.search(desc):
            continue
        mo = _RE_ADESAO_ASSISTIDO.search(desc)
        if mo:
            nome = mo.group(1).strip()
            if nome and nome not in nomes:
                nomes.append(nome)
    return nomes


def gravar_para_envio(slug: str, paj_norm: str, id_tramite: str, mensagem: str,
                      meios: list[str], somente_leitura: bool = False,
                      prazos: dict | None = None) -> dict:
    """Grava UMA tramitação na fila de envio (sai de "pendentes" e entra em
    "gravados para envio"). Guarda a mensagem a disparar e os meios escolhidos.
    `somente_leitura` (App SISDPU): o assistido só lê a notificação, não responde.
    `prazos` (dict por meio {app_sisdpu, whatsapp, email}): prazo de resposta em
    dias de cada meio (>=1, padrão 5); armazenados como prazo_app/whatsapp/email."""
    from services import envio_service
    box = _ler_box(slug, paj_norm)
    if not box:
        return {}
    s = _achar_solic(box, id_tramite)
    if not s:
        return {}
    meios = [m for m in (meios or []) if m in envio_service.MEIOS]
    s["envio"] = {
        "status": "gravado",
        "mensagem": (mensagem or "").strip(),
        "meios": meios,
        "somente_leitura": bool(somente_leitura),
        **_prazos_envio(prazos),
        "gravado_em": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    _gravar_box(slug, box)
    return box


def remover_da_fila(slug: str, paj_norm: str, id_tramite: str) -> dict:
    """Tira uma tramitação da fila de envio (volta a ser "pendente")."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {}
    s = _achar_solic(box, id_tramite)
    if not s:
        return {}
    s["envio"] = {}
    _gravar_box(slug, box)
    return box


# ==================== anexos ====================
# Documentos que serão enviados junto com a mensagem. Ficam vinculados à
# TRAMITAÇÃO (sol["anexos"]) ou ao CONJUNTO (box["conjunto"]["anexos"]) e
# persistem independentemente de gravar/remover da fila. Arquivos gravados em
# dados/_anexos/<PAJ>/<id_tramite|conjunto>/ (fora do Projeto).
ANEXOS_DIR = DADOS_DIR / "_anexos"
_ANEXO_MAX = 25 * 1024 * 1024  # 25 MB por arquivo
# Formatos aceitos pelo Aplicativo SISDPU (envio de anexos). Outros são rejeitados
# aqui para não quebrar o envio pelo app. DOC/DOCX/ODT/PNG/JPEG/GIF/PDF.
_ANEXO_EXTS = {"doc", "docx", "odt", "png", "jpg", "jpeg", "gif", "pdf"}
ANEXO_EXTS_LABEL = "DOC, DOCX, ODT, PNG, JPEG, GIF, PDF"


def _chave_anexo(tipo: str, id_tramite: str) -> str:
    return "conjunto" if tipo == "conjunto" else (id_tramite or "sem-id")


def _anexos_dir(paj_norm: str, chave: str) -> Path:
    return ANEXOS_DIR / paj_norm / chave


def _nome_seguro(nome: str) -> str:
    base = Path(nome or "arquivo").name
    base = re.sub(r"[^\w.\- ]+", "_", base).strip() or "arquivo"
    return base[:120]


def _anexos_do_alvo(box: dict, tipo: str, id_tramite: str):
    """Retorna (alvo, lista_de_anexos). alvo = conjunto (dict) ou a solicitação."""
    alvo = box.get("conjunto") if tipo == "conjunto" else _achar_solic(box, id_tramite)
    if alvo is None:
        return None, []
    return alvo, alvo.setdefault("anexos", [])


def caminho_anexo(paj_norm: str, tipo: str, id_tramite: str, arquivo: str) -> Path:
    return _anexos_dir(paj_norm, _chave_anexo(tipo, id_tramite)) / arquivo


def adicionar_anexos(slug: str, paj_norm: str, tipo: str, id_tramite: str,
                     arquivos: list[tuple[str, bytes]]) -> dict:
    """Salva `arquivos` [(nome_original, bytes)] no disco e registra os metadados
    no alvo (tramitação ou conjunto). Retorna {ok, anexos} ou {ok:False, erro}."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    alvo, anexos = _anexos_do_alvo(box, tipo, id_tramite)
    if alvo is None:
        return {"ok": False, "erro": "tramitação/conjunto não encontrado"}
    dest = _anexos_dir(paj_norm, _chave_anexo(tipo, id_tramite))
    dest.mkdir(parents=True, exist_ok=True)
    # Dedupe: não adiciona o mesmo documento duas vezes (mesmo nome, sem diferenciar
    # maiúsculas). Duplicados são ignorados e devolvidos em `duplicados`.
    existentes = {(a.get("nome") or "").strip().lower() for a in anexos}
    duplicados: list[str] = []
    rejeitados: list[str] = []  # formato não aceito pelo Aplicativo SISDPU
    for nome, dados in arquivos:
        if not dados:
            continue
        nome_base = Path(nome or "arquivo").name
        if Path(nome_base).suffix.lstrip(".").lower() not in _ANEXO_EXTS:
            rejeitados.append(nome_base)
            continue
        if nome_base.strip().lower() in existentes:
            duplicados.append(nome_base)
            continue
        if len(dados) > _ANEXO_MAX:
            return {"ok": False, "erro": f"'{nome}' excede o limite de 25 MB."}
        existentes.add(nome_base.strip().lower())
        aid = uuid.uuid4().hex[:12]
        seguro = _nome_seguro(nome)
        arquivo_salvo = f"{aid}__{seguro}"
        with contextlib.suppress(Exception):
            (dest / arquivo_salvo).write_bytes(dados)
        anexos.append({
            "id": aid, "nome": nome_base, "arquivo": arquivo_salvo,
            "tamanho": len(dados), "tipo": Path(seguro).suffix.lstrip(".").lower(),
            "incluido_em": _dt.datetime.now().isoformat(timespec="seconds"),
        })
    _gravar_box(slug, box)
    return {"ok": True, "anexos": anexos, "duplicados": duplicados, "rejeitados": rejeitados}


def remover_anexo(slug: str, paj_norm: str, tipo: str, id_tramite: str, anexo_id: str) -> dict:
    """Remove um anexo (arquivo do disco + metadado). Retorna {ok, anexos}."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    alvo, anexos = _anexos_do_alvo(box, tipo, id_tramite)
    if alvo is None:
        return {"ok": False, "erro": "tramitação/conjunto não encontrado"}
    dest = _anexos_dir(paj_norm, _chave_anexo(tipo, id_tramite))
    restantes = []
    for a in anexos:
        if a.get("id") == anexo_id:
            with contextlib.suppress(Exception):
                (dest / a.get("arquivo", "")).unlink()
        else:
            restantes.append(a)
    alvo["anexos"] = restantes
    _gravar_box(slug, box)
    return {"ok": True, "anexos": restantes}


def listar_fila(slug: str) -> list[dict]:
    """Itens gravados para envio (tramitações individuais + conjuntos)."""
    return montar_dashboard(slug)["fila"]


def listar_prontos_envio() -> list[dict]:
    """Fila de envio AGREGADA de TODAS as caixas habilitadas (página "Prontos para
    Envio"). Cada item ganha a caixa de ORIGEM (caixa_slug/caixa_nome/grupo)."""
    out: list[dict] = []
    for c in config_service.listar_caixas_habilitadas():
        for item in montar_dashboard(c["slug"])["fila"]:
            out.append({**item, "caixa_slug": c["slug"],
                        "caixa_nome": c["nome"], "grupo": c["grupo"]})
    out.sort(key=lambda d: (0 if d.get("envio_urgente") else 1, d.get("envio_gravado_em", "")))
    return out


def contar_prontos_envio() -> int:
    """Total de itens gravados para envio somando todas as caixas (badge do menu)."""
    return sum(len(montar_dashboard(c["slug"])["fila"])
               for c in config_service.listar_caixas_habilitadas())


# ==================== enriquecimento (merge caixa + contato) ====================
def _fmt_endereco(e: dict) -> str:
    linha1 = ", ".join(p for p in [e.get("logradouro", ""), e.get("numero", "")] if p)
    if e.get("complemento"):
        linha1 += f", {e['complemento']}"
    mun = e.get("municipio", "")
    if e.get("uf"):
        mun = f"{mun}/{e['uf']}" if mun else e["uf"]
    loc = ", ".join(p for p in [e.get("bairro", ""), mun] if p)
    cep = f"CEP {e['cep']}" if e.get("cep") else ""
    corpo = " - ".join(p for p in [linha1, loc, cep] if p)
    tipo = e.get("tipo", "")
    return f"{tipo}: {corpo}" if tipo else corpo


def _contato_derivado(cont: dict) -> dict:
    """Campos derivados dos CONTATOS compartilhados do PAJ (valem para qualquer
    tramitação): assistido, contatos, telefones/emails/endereços + textos e o
    estado de WhatsApp."""
    d: dict = {}
    d["assistido"] = cont.get("assistido", "")
    d["contatos"] = dict(cont.get("contatos") or _contatos_vazio())
    d["telefones"] = cont.get("telefones", [])
    d["emails"] = cont.get("emails", [])
    d["enderecos"] = cont.get("enderecos", [])
    d["movimentacoes"] = _marcar_selecao(cont)  # com flag `sel` p/ os checkboxes
    d["movs_texto_ia"] = cont.get("movs_texto_ia", "")
    d["whatsapp_numeros"] = cont.get("whatsapp_numeros", [])
    d["whatsapp_config"] = cont.get("whatsapp_config", False)
    d["telefones_txt"] = "\n".join(
        t.get("numero", "") + (f" — {t['obs']}" if t.get("obs") else "")
        for t in d["telefones"])
    d["emails_txt"] = "\n".join(d["emails"])
    d["enderecos_txt"] = "\n".join(_fmt_endereco(e) for e in d["enderecos"])
    tem_tel = len(d["telefones"]) > 0
    wa_nums = d["whatsapp_numeros"]
    if wa_nums:
        estado = "ativo"
    elif tem_tel and not d["whatsapp_config"]:
        estado = "pendente"
    elif d["whatsapp_config"]:
        estado = "dispensado"
    else:
        estado = "sem_telefone"
    d["whatsapp_estado"] = estado
    d["whatsapp_txt"] = "\n".join(wa_nums)
    d["contatos"]["whatsapp"] = len(wa_nums) > 0
    return d


def _enriquecer_tramite(box: dict, sol: dict, cont: dict | None = None) -> dict:
    """Item de UMA tramitação para o dashboard: dados da tramitação + contatos do
    PAJ + análise e estado de envio (tudo por tramitação)."""
    if cont is None:
        cont = ler_contato(box.get("paj_norm", ""))
    d = _contato_derivado(cont)
    d["paj_norm"] = box.get("paj_norm", "")
    d["paj"] = box.get("paj", "")
    d["em_caixa"] = box.get("em_caixa", True)
    d["id_tramite"] = sol.get("id_tramite", "")
    d["id"] = sol.get("id", "")
    d["oficio"] = sol.get("oficio_origem", "")
    d["remetente"] = sol.get("remetente", "")
    raw_env = sol.get("data_envio", "")
    d["data_envio"] = raw_env
    d["data_envio_key"] = _data_key(raw_env)
    m_env = re.match(r"(\d{2}/\d{2}/\d{4})(?:[ T]+(\d{2}:\d{2}(?::\d{2})?))?", raw_env)
    d["envio_data"] = m_env.group(1) if m_env else raw_env
    d["envio_hora"] = (m_env.group(2) or "") if m_env else ""
    d["prazo"] = sol.get("prazo", "")
    d["descricao_tramite"] = sol.get("descricao_tramite", "")
    d["atribuicao"] = sol.get("atribuicao", "")
    d["url_detalhe"] = sol.get("url_detalhe", "")
    # análise por tramitação
    d["resumo_ia"] = sol.get("resumo_ia", "")
    # A classificação MANUAL (via dropdown de texto padrão) prevalece sobre a da IA.
    d["sugestao_tipo"] = sol.get("sugestao_tipo_editada") or sol.get("sugestao_tipo", "")
    d["sugestao_editada"] = sol.get("sugestao_editada", "")
    d["sugestao_atual"] = sol.get("sugestao_editada") or sol.get("sugestao_ia") or ""
    # fila de envio por tramitação
    envio = sol.get("envio") or {}
    d["envio_status"] = envio.get("status", "")
    d["envio_mensagem"] = envio.get("mensagem", "")
    d["envio_meios"] = envio.get("meios", []) or []
    d["envio_somente_leitura"] = bool(envio.get("somente_leitura"))
    d["envio_prazo_app"] = envio.get("prazo_app", envio.get("prazo_dias", 5))  # fallback legado
    d["envio_prazo_whatsapp"] = envio.get("prazo_whatsapp", 5)
    d["envio_prazo_email"] = envio.get("prazo_email", 5)
    _prz_meio = {"app_sisdpu": d["envio_prazo_app"], "whatsapp": d["envio_prazo_whatsapp"], "email": d["envio_prazo_email"]}
    d["envio_urgente"] = any(_prz_meio.get(m) == 0 for m in d["envio_meios"])  # prazo 0 = urgente
    d["envio_falhas"] = envio.get("falhas") or {}  # {meio: detalhe} do último envio (reenvio)
    d["envio_gravado_em"] = envio.get("gravado_em", "")
    # anexos (documentos a enviar) por tramitação
    anx = sol.get("anexos", []) or []
    d["anexos"] = anx
    d["n_anexos"] = len(anx)
    d["tem_anexo"] = len(anx) > 0
    # já enviados por tramitação
    env = sol.get("enviados", []) or []
    d["enviados"] = env
    d["contato_enviado"] = len(env) > 0
    d["n_contatos"] = len(env)
    ult = env[-1] if env else {}
    d["contato_meios"] = ult.get("meios", [])
    d["contato_mensagem"] = ult.get("mensagem", "")
    d["contato_em"] = ult.get("em", "")
    d["contato_somente_leitura"] = bool(ult.get("somente_leitura"))
    return d


def listar_tramites(slug: str, somente_em_caixa: bool = True) -> list[dict]:
    """Lista as TRAMITAÇÕES da caixa (uma por item), FIFO por data."""
    itens: list[dict] = []
    bdir = _box_dir(slug)
    if bdir.is_dir():
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                if somente_em_caixa and not box.get("em_caixa", True):
                    continue
                cont = ler_contato(box.get("paj_norm", ""))
                for sol in box.get("solicitacoes", []):
                    itens.append(_enriquecer_tramite(box, sol, cont))
    itens.sort(key=lambda d: d.get("data_envio_key") or "99999999")
    return itens


def contar_em_caixa(slug: str) -> int:
    """Nº de itens que ainda falta TRATAR na caixa: triagem + pendentes.
    Conjuntos contam como 1. NÃO inclui os gravados aguardando disparo (esses têm
    contador próprio no menu "Prontos para Envio") nem os já enviados."""
    db = montar_dashboard(slug)
    return len(db["triagem"]) + len(db["pendentes"])


def menu_caixas() -> list[dict]:
    """Caixas habilitadas para a sidebar, cada uma com `qtd` = nº de PAJs na caixa."""
    out = []
    for c in config_service.listar_caixas_habilitadas():
        c = dict(c)
        c["qtd"] = contar_em_caixa(c["slug"])
        out.append(c)
    return out


# ==================== triagem / tratamento conjunto ====================
def _sol_pendente(s: dict) -> bool:
    return (s.get("envio") or {}).get("status") != "gravado" and not s.get("enviados")


def _sols_pendentes(box: dict) -> list[dict]:
    return [s for s in box.get("solicitacoes", []) if _sol_pendente(s)]


def _chave_sol(chave_movs: str, com_rev: str, sol: dict) -> str:
    """Chave de cache canônica de UMA tramitação (solicitação).

    Inclui o vínculo de movimentação (`mov_seq`) do "tratamento separado" SÓ quando
    houver — assim tramitações normais mantêm a chave sem `|m` (retrocompatível com
    as análises já gravadas) e as tratadas em separado batem com a chave que a
    triagem/reanálise gravou, evitando que o sync as reprocesse e sobrescreva o foco."""
    mseq = str(sol.get("mov_seq", "") or "").strip()
    m = f"|m{mseq}" if mseq else ""
    return (f"{chave_movs}|c{com_rev}|t{sol.get('id_tramite', '')}"
            f"{m}|d{_desc_hash(sol.get('descricao_tramite', ''))}")


def _decisao_valida(box: dict, pend_ids: list[str]) -> str:
    """Retorna a decisão de triagem SE ainda vale para o conjunto atual de
    tramitações pendentes; senão '' (precisa retriagem)."""
    tri = box.get("triagem") or {}
    if tri.get("decisao") in ("conjunto", "separado") and sorted(tri.get("ids", [])) == pend_ids:
        return tri["decisao"]
    return ""


def _sugerir_mapeamento(pend: list[dict], movs: list[dict]) -> dict:
    """Sugere o vínculo tramitação→movimentação por PAREAMENTO CRONOLÓGICO ORDINAL:
    tramitação mais recente ↔ movimentação mais recente, retrocedendo (assim as mais
    antigas casam com movimentações mais antigas). Retorna {id_tramite: seq}."""
    movs_ord = sorted((m for m in movs if _dt_seg(m.get("data", "")) is not None),
                      key=lambda m: _dt_seg(m.get("data", "")))
    pend_ord = sorted(pend, key=lambda s: _dt_seg(s.get("data_envio", "")) or 0)
    sug: dict = {}
    for i, s in enumerate(reversed(pend_ord)):  # i=0 → tramitação mais recente
        mov = movs_ord[-1 - i] if i < len(movs_ord) else None
        sug[s.get("id_tramite", "")] = str(mov.get("seq", "")) if mov else ""
    return sug


def _item_triagem(box: dict, pend: list[dict], cont: dict) -> dict:
    d = _contato_derivado(cont)
    d["tipo"] = "triagem"
    d["paj_norm"] = box.get("paj_norm", "")
    d["paj"] = box.get("paj", "")
    d["url_detalhe"] = pend[0].get("url_detalhe", "") if pend else ""
    d["oficio"] = pend[0].get("oficio_origem", "") if pend else ""
    movs = cont.get("movimentacoes", [])
    d["movimentacoes"] = movs
    sug = _sugerir_mapeamento(pend, movs)
    # tramitações ordenadas por data (mais antiga primeiro) — reforça a cronologia.
    pend_ord = sorted(pend, key=lambda s: _dt_seg(s.get("data_envio", "")) or 0)
    d["tramites"] = [{"id_tramite": s.get("id_tramite", ""), "data_envio": s.get("data_envio", ""),
                      "oficio": s.get("oficio_origem", ""), "remetente": s.get("remetente", ""),
                      "prazo": s.get("prazo", ""), "resumo_ia": s.get("resumo_ia", ""),
                      "descricao_tramite": s.get("descricao_tramite", ""),
                      "mov_sugerida": sug.get(s.get("id_tramite", ""), "")}
                     for s in pend_ord]
    d["n_tramites"] = len(pend)
    return d


def _enriquecer_conjunto(box: dict, cont: dict) -> dict:
    conj = box.get("conjunto") or {}
    ids = conj.get("tramite_ids", [])
    sols = [s for s in box.get("solicitacoes", []) if s.get("id_tramite") in ids]
    sols.sort(key=lambda s: _data_key(s.get("data_envio", "")))
    prim = sols[0] if sols else {}
    d = _contato_derivado(cont)
    d["tipo"] = "conjunto"
    d["paj_norm"] = box.get("paj_norm", "")
    d["paj"] = box.get("paj", "")
    d["em_caixa"] = box.get("em_caixa", True)
    d["tramite_ids"] = ids
    d["n_tramites"] = len(ids)
    d["oficio"] = prim.get("oficio_origem", "")
    d["url_detalhe"] = prim.get("url_detalhe", "")
    d["remetente"] = prim.get("remetente", "")
    d["data_envio"] = prim.get("data_envio", "")
    d["data_envio_key"] = _data_key(prim.get("data_envio", ""))
    d["prazo"] = next((s.get("prazo") for s in sols if s.get("prazo")), "")
    d["datas_tramites"] = [s.get("data_envio", "") for s in sols]
    d["descricoes_tramites"] = [s.get("descricao_tramite", "") for s in sols]
    d["resumo_ia"] = conj.get("resumo_ia", "")
    d["sugestao_tipo"] = conj.get("sugestao_tipo_editada", "")  # conjunto só tem classificação MANUAL
    d["sugestao_editada"] = conj.get("sugestao_editada", "")
    d["sugestao_atual"] = conj.get("sugestao_editada") or conj.get("sugestao_ia") or ""
    envio = conj.get("envio") or {}
    d["envio_status"] = envio.get("status", "")
    d["envio_mensagem"] = envio.get("mensagem", "")
    d["envio_meios"] = envio.get("meios", []) or []
    d["envio_somente_leitura"] = bool(envio.get("somente_leitura"))
    d["envio_prazo_app"] = envio.get("prazo_app", envio.get("prazo_dias", 5))  # fallback legado
    d["envio_prazo_whatsapp"] = envio.get("prazo_whatsapp", 5)
    d["envio_prazo_email"] = envio.get("prazo_email", 5)
    _prz_meio = {"app_sisdpu": d["envio_prazo_app"], "whatsapp": d["envio_prazo_whatsapp"], "email": d["envio_prazo_email"]}
    d["envio_urgente"] = any(_prz_meio.get(m) == 0 for m in d["envio_meios"])  # prazo 0 = urgente
    d["envio_falhas"] = envio.get("falhas") or {}  # {meio: detalhe} do último envio (reenvio)
    d["envio_gravado_em"] = envio.get("gravado_em", "")
    anx = conj.get("anexos", []) or []
    d["anexos"] = anx
    d["n_anexos"] = len(anx)
    d["tem_anexo"] = len(anx) > 0
    env = conj.get("enviados", []) or []
    d["enviados"] = env
    d["contato_enviado"] = len(env) > 0
    d["n_contatos"] = len(env)
    ult = env[-1] if env else {}
    d["contato_meios"] = ult.get("meios", [])
    d["contato_mensagem"] = ult.get("mensagem", "")
    d["contato_em"] = ult.get("em", "")
    d["contato_somente_leitura"] = bool(ult.get("somente_leitura"))
    return d


def montar_dashboard(slug: str) -> dict:
    """Classifica os itens da caixa em 4 grupos: triagem, pendentes, fila (gravados)
    e enviados. Itens têm `tipo` = 'tramite' | 'conjunto' | 'triagem'."""
    triagem, pendentes, fila, enviados = [], [], [], []
    bdir = _box_dir(slug)
    if bdir.is_dir():
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                if not box.get("em_caixa", True):
                    continue
                cont = ler_contato(box.get("paj_norm", ""))
                sols = box.get("solicitacoes", [])
                # tramitações já resolvidas individualmente entram sempre.
                for s in sols:
                    if (s.get("envio") or {}).get("status") == "gravado":
                        fila.append({**_enriquecer_tramite(box, s, cont), "tipo": "tramite"})
                    elif s.get("enviados"):
                        enviados.append({**_enriquecer_tramite(box, s, cont), "tipo": "tramite"})
                pend = _sols_pendentes(box)
                if not pend:
                    continue
                pend_ids = sorted(s.get("id_tramite", "") for s in pend)
                if len(pend) < 2:
                    pendentes.append({**_enriquecer_tramite(box, pend[0], cont), "tipo": "tramite"})
                    continue
                decisao = _decisao_valida(box, pend_ids)
                conj = box.get("conjunto") or {}
                if decisao == "conjunto" and sorted(conj.get("tramite_ids", [])) == pend_ids:
                    item = _enriquecer_conjunto(box, cont)
                    if item["envio_status"] == "gravado":
                        fila.append(item)
                    elif item["contato_enviado"]:
                        enviados.append(item)
                    else:
                        pendentes.append(item)
                elif decisao == "separado":
                    for s in pend:
                        pendentes.append({**_enriquecer_tramite(box, s, cont),
                                          "tipo": "tramite", "de_triagem_separado": True})
                else:
                    triagem.append(_item_triagem(box, pend, cont))
    triagem.sort(key=lambda d: d.get("paj", ""))
    pendentes.sort(key=lambda d: d.get("data_envio_key") or "99999999")
    # Urgentes (prazo 0) primeiro na fila; depois por ordem de gravação.
    fila.sort(key=lambda d: (0 if d.get("envio_urgente") else 1, d.get("envio_gravado_em", "")))
    enviados.sort(key=lambda d: d.get("contato_em", ""), reverse=True)
    return {"triagem": triagem, "pendentes": pendentes, "fila": fila, "enviados": enviados}


def _achar_conjunto(box: dict):
    return box.get("conjunto") or None


async def tratar_conjunto(slug: str, paj_norm: str) -> dict:
    """Trata em CONJUNTO as tramitações pendentes de um PAJ: re-lê o detalhamento,
    pede ao Claude UMA síntese (resumo + sugestão) que cubra todas e grava como
    conjunto. Marca a decisão de triagem = conjunto."""
    caixa = config_service.get_caixa(slug)
    if not caixa:
        return {"ok": False, "erro": "caixa desconhecida"}
    if _ocupado_exclusivo():
        return {"ok": False, "erro": "Há uma sincronização/envio em andamento. Aguarde."}
    box = _ler_box(slug, paj_norm)
    pend = _sols_pendentes(box) if box else []
    if len(pend) < 2:
        return {"ok": False, "erro": "PAJ não tem 2+ tramitações pendentes"}
    id_proc = pend[0].get("id", "")
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    async with _triagem_ctx():  # roda em paralelo com outras triagens (até _TRIAGEM_MAX)
        if id_proc:
            with contextlib.suppress(Exception):
                async with _browser_lock:  # navegador é sessão única — serializa este passo
                    det = await sisdpu_client.detalhar_paj(id_proc, n_movs=MOVS_EXTRACAO_MAX)
                if det:
                    atualizar_contato_do_detalhe(paj_norm, det)
        cont = ler_contato(paj_norm)
        movs_ia = movs_para_ia(cont)  # respeita recorte manual / seleção / corte padrão
        box = _ler_box(slug, paj_norm)
        pend = _sols_pendentes(box)
        pend_ids = sorted(s.get("id_tramite", "") for s in pend)
        tramites_info = [{"data_envio": s.get("data_envio", ""), "oficio_origem": s.get("oficio_origem", ""),
                          "remetente": s.get("remetente", ""),
                          "descricao_tramite": s.get("descricao_tramite", "")} for s in pend]
        # Hash das descrições (ordem-independente) — entra na chave de cache p/ que
        # o conjunto seja re-analisado quando as descrições dos trâmites mudarem.
        desc_conj = _desc_hash("||".join(sorted((s.get("descricao_tramite") or "") for s in pend)))
        ana = None
        if movs_ia and analise_service.disponivel():
            ana = await asyncio.to_thread(
                analise_service.analisar_conjunto, movs_ia, comunicados, caixa["nome"], tramites_info)
        box["conjunto"] = {
            "tramite_ids": pend_ids,
            "resumo_ia": (ana or {}).get("resumo", ""),
            "sugestao_ia": (ana or {}).get("sugestao", ""),
            "sugestao_editada": "",
            "resumo_ia_chave": f"{_chave_movs(cont, movs_ia)}|c{com_rev}|conj{','.join(pend_ids)}|d{desc_conj}",
            "envio": {}, "enviados": [],
        }
        box["triagem"] = {"decisao": "conjunto", "ids": pend_ids}
        _gravar_box(slug, box)
    return {"ok": True}


async def tratar_separado(slug: str, paj_norm: str, mapeamento: dict | None = None) -> dict:
    """Tratamento SEPARADO: cada tramitação pendente é analisada individualmente,
    FOCADA na movimentação que o usuário vinculou (mapeamento id_tramite→mov_seq).
    Tramitações sem vínculo usam o foco padrão (por data). Depois marca decisão."""
    caixa = config_service.get_caixa(slug)
    if not caixa:
        return {"ok": False, "erro": "caixa desconhecida"}
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    if _ocupado_exclusivo():
        return {"ok": False, "erro": "Há uma sincronização/envio em andamento. Aguarde."}
    mapeamento = mapeamento or {}
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    cont = ler_contato(paj_norm)
    todas = cont.get("movimentacoes", []) or []
    movs_por_seq = {str(m.get("seq")): m for m in todas}  # foco pode ser qualquer mov extraída
    movs_ia = movs_para_ia(cont)  # contexto que vai à IA (recorte/seleção/corte padrão)
    chave_movs = _chave_movs(cont, movs_ia)
    async with _triagem_ctx():  # roda em paralelo com outras triagens (até _TRIAGEM_MAX)
        box = _ler_box(slug, paj_norm)
        pend = _sols_pendentes(box)
        pend_ids = sorted(s.get("id_tramite", "") for s in pend)
        for s in pend:
            mseq = str(mapeamento.get(s.get("id_tramite", ""), "") or "").strip()
            mov_foco = movs_por_seq.get(mseq)
            tinfo = {"data_envio": s.get("data_envio", ""), "oficio_origem": s.get("oficio_origem", ""),
                     "remetente": s.get("remetente", ""),
                     "descricao_tramite": s.get("descricao_tramite", "")}
            if mov_foco:
                tinfo["mov_foco"] = mov_foco
            s["mov_seq"] = mseq  # definido ANTES da chave (que lê mov_seq via _chave_sol)
            if movs_ia and analise_service.disponivel():
                ana = await asyncio.to_thread(
                    analise_service.analisar_paj, movs_ia, comunicados, caixa["nome"], tinfo)
                if ana:
                    s["resumo_ia"] = ana.get("resumo", "")
                    s["sugestao_ia"] = ana.get("sugestao", "")
                    s["sugestao_tipo"] = ana.get("tipo", "")
                    s["resumo_ia_chave"] = _chave_sol(chave_movs, com_rev, s)
        box["triagem"] = {"decisao": "separado", "ids": pend_ids}
        box.pop("conjunto", None)
        _gravar_box(slug, box)
    return {"ok": True}


def desfazer_triagem(slug: str, paj_norm: str) -> dict:
    """Volta o PAJ para a triagem (desfaz conjunto/separado)."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    box.pop("triagem", None)
    box.pop("conjunto", None)
    _gravar_box(slug, box)
    return {"ok": True}


def gravar_conjunto(slug: str, paj_norm: str, mensagem: str, meios: list[str],
                    somente_leitura: bool = False, prazos: dict | None = None) -> dict:
    """Grava o CONJUNTO de um PAJ na fila de envio."""
    from services import envio_service
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    meios = [m for m in (meios or []) if m in envio_service.MEIOS]
    box["conjunto"]["envio"] = {
        "status": "gravado", "mensagem": (mensagem or "").strip(),
        "meios": meios, "somente_leitura": bool(somente_leitura),
        **_prazos_envio(prazos),
        "gravado_em": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    _gravar_box(slug, box)
    return box


def remover_conjunto_da_fila(slug: str, paj_norm: str) -> dict:
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    box["conjunto"]["envio"] = {}
    _gravar_box(slug, box)
    return box


def definir_sugestao_conjunto(slug: str, paj_norm: str, texto: str,
                              tipo: str | None = None) -> dict:
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    box["conjunto"]["sugestao_editada"] = (texto or "").strip()
    if tipo is not None:
        box["conjunto"]["sugestao_tipo_editada"] = (tipo or "").strip()
    _gravar_box(slug, box)
    return box


# ==================== sincronização ====================
# _sync_lock: EXCLUSIVO para operações que dirigem o navegador (Playwright é sessão
#   única) e/ou são longas: sincronizar, sincronizar_paj, envio, reanálise.
# Triagens (tratar_conjunto/separado) são majoritariamente IA (subprocesso, já
#   paralelizável) e escrevem só o box do PRÓPRIO PAJ → podem rodar em PARALELO
#   entre si (até _TRIAGEM_MAX), mas são MUTUAMENTE EXCLUSIVAS com o grupo exclusivo
#   acima (para não haver corrida de escrita nos boxes / no navegador).
_sync_lock = asyncio.Lock()
_TRIAGEM_MAX = 4
_triagem_sem = asyncio.Semaphore(_TRIAGEM_MAX)  # até 4 triagens simultâneas
_triagem_ativas = 0            # nº de triagens em andamento (grupo exclusivo recusa se > 0)
_browser_lock = asyncio.Lock()  # serializa o passo de navegador (detalhar_paj) entre triagens


def _ocupado_exclusivo() -> bool:
    """True se há sincronização/envio/reanálise em andamento (grupo exclusivo).
    Usado pelas TRIAGENS para recusar enquanto uma op exclusiva roda."""
    return _sync_lock.locked()


def _pode_exclusivo() -> bool:
    """Pode iniciar uma op EXCLUSIVA (sync/envio/reanálise)? Não, se já há uma
    exclusiva OU alguma triagem em andamento (evita corrida de escrita nos boxes)."""
    return not _sync_lock.locked() and _triagem_ativas == 0


@contextlib.asynccontextmanager
async def _triagem_ctx():
    """Contexto de uma triagem: conta como ativa (para o grupo exclusivo recusar) e
    limita a _TRIAGEM_MAX simultâneas. O incremento ocorre ANTES de qualquer await."""
    global _triagem_ativas
    _triagem_ativas += 1
    try:
        async with _triagem_sem:
            yield
    finally:
        _triagem_ativas -= 1


def _ids_em_caixa(slug: str) -> set[str]:
    ids: set[str] = set()
    bdir = _box_dir(slug)
    if bdir.is_dir():
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("em_caixa", False):
                    for s in d.get("solicitacoes", []):
                        if s.get("id_tramite"):
                            ids.add(s["id_tramite"])
    return ids


async def sincronizar_stream(slug: str):
    """Sincroniza UMA caixa (por slug), emitindo eventos de progresso."""
    caixa = config_service.get_caixa(slug)
    if not caixa:
        yield {"level": "erro", "msg": f"Caixa '{slug}' não está habilitada."}
        yield {"done": True, "resumo": {"ok": False, "erro": "caixa_desconhecida"}}
        return

    if not _pode_exclusivo():
        yield {"level": "erro", "msg": "Já existe uma operação em andamento (sincronização/envio/triagem)."}
        yield {"done": True, "resumo": {"ok": False, "erro": "em_andamento"}}
        return

    nome_grupo = caixa["grupo"]
    nome_caixa = caixa["nome"]
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    n_movs = config_service.get_n_movimentacoes()

    async with _sync_lock:
        t0 = time.monotonic()
        yield {"level": "info", "msg": f'{nome_caixa}: conectando ao SISDPU e abrindo "{nome_grupo}"…'}

        try:
            res = await sisdpu_client.listar_caixa_grupo(nome_grupo)
        except Exception as e:  # noqa: BLE001
            yield {"level": "erro", "msg": f"Falha ao acessar o SISDPU: {e}"}
            yield {"done": True, "resumo": {"ok": False, "erro": str(e)}}
            return

        # Atualiza a lista de caixas descobertas (para a tela de Configurações).
        with contextlib.suppress(Exception):
            config_service.set_caixas_disponiveis(res.get("grupos_disponiveis", []))

        if not res.get("encontrado"):
            # A caixa some da página de grupos quando não há mais PAJs nela
            # (todos tratados/concluídos). Tratamos como caixa VAZIA: esvazia o
            # dashboard (marca em_caixa=False) e informa que está vazia.
            yield {"level": "info", "msg": f"Caixa {nome_grupo} vazia neste momento."}
            saidos: list[str] = []
            bdir = _box_dir(slug)
            if bdir.is_dir():
                for f in bdir.glob("PAJ-*.json"):
                    with contextlib.suppress(Exception):
                        d = json.loads(f.read_text(encoding="utf-8"))
                        if d.get("em_caixa", True):
                            d["em_caixa"] = False
                            _gravar_box(slug, d)
                            saidos.append(d.get("paj", ""))
            if saidos:
                yield {"level": "info", "msg": f"{len(saidos)} PAJ(s) saíram da caixa."}
            dur = time.monotonic() - t0
            yield {"level": "ok", "msg": f"Concluído em {dur:.0f}s — caixa vazia."}
            yield {"done": True, "resumo": {"ok": True, "slug": slug, "grupo": nome_grupo,
                                            "qtd_pajs": 0, "qtd_solicitacoes": 0,
                                            "novos": 0, "saidos": len(saidos),
                                            "duracao": round(dur, 1)}}
            return

        itens = res.get("itens", []) or []
        por_paj: dict[str, list[dict]] = {}
        for item in itens:
            por_paj.setdefault(normalizar_paj(item.get("paj", "")), []).append(item)
        total_pajs, total_envios = len(por_paj), len(itens)

        conhecidos = _ids_em_caixa(slug)
        novos_ids = {it.get("id_tramite") for it in itens} - conhecidos
        n_novos = sum(1 for it in itens if it.get("id_tramite") in novos_ids)

        yield {"level": "ok", "msg": f"Caixa carregada: {total_pajs} PAJ(s) e {total_envios} tramitação(ões)."}
        yield {"level": ("novo" if n_novos else "info"),
               "msg": f"{n_novos} tramitação(ões) nova(s) desde a última sincronização."}
        if comunicados:
            yield {"level": "info", "msg": f"{len(comunicados)} comunicado(s) padrão aplicável(is) a esta caixa."}

        pajs_atuais: set[str] = set()
        trabalhos_ia: list[dict] = []  # análises coletadas na Fase 1, rodadas em paralelo na Fase 2
        i = 0
        for paj_norm, itens_paj in por_paj.items():
            i += 1
            paj = itens_paj[0].get("paj", "")
            upsert_paj(slug, paj, itens_paj, em_caixa=True)
            pajs_atuais.add(paj_norm)
            novos_neste = [it for it in itens_paj if it.get("id_tramite") in novos_ids]

            assistido, resumo_contatos, det = "", "", {}
            id_proc = itens_paj[0].get("id", "")
            if id_proc:
                try:
                    det = await sisdpu_client.detalhar_paj(id_proc, n_movs=MOVS_EXTRACAO_MAX)
                except Exception as e:  # noqa: BLE001
                    det = {}
                    yield {"level": "erro",
                           "msg": f"[{i}/{total_pajs}] PAJ {paj}: falha ao ler detalhamento ({e})."}
                if det:
                    cont = atualizar_contato_do_detalhe(paj_norm, det)
                    assistido = cont.get("assistido", "")
                    c = cont.get("contatos", {})
                    marcas = []
                    if c.get("app_sisdpu"):
                        marcas.append("App")
                    if cont.get("telefones"):
                        marcas.append(f"{len(cont['telefones'])} tel")
                    if cont.get("emails"):
                        marcas.append("e-mail")
                    if cont.get("enderecos"):
                        marcas.append("endereço")
                    resumo_contatos = ", ".join(marcas) or "sem contatos"

            etiqueta = "NOVO" if novos_neste else "atualizado"
            extra = f" · {len(novos_neste)} tramitação(ões) nova(s)" if novos_neste else ""
            yield {"level": ("novo" if novos_neste else "muted"),
                   "msg": f"[{i}/{total_pajs}] {etiqueta} • PAJ {paj} — "
                          f"{assistido or 's/ nome'} · {resumo_contatos}{extra}"}

            # Análise POR TRAMITAÇÃO (cada tramitação tem seu resumo + sugestão).
            # Aqui apenas COLETAMOS o que precisa ser analisado; as chamadas de IA
            # rodam EM PARALELO na Fase 2 (abaixo), o que é o gargalo do sync.
            # OTIMIZAÇÃO: PAJs com 2+ tramitações pendentes vão OBRIGATORIAMENTE para
            # a triagem (onde a IA roda em conjunto ou separado). Por isso NÃO
            # os analisamos aqui — evita análises iniciais desperdiçadas.
            if id_proc and analise_service.disponivel():
                cont = ler_contato(paj_norm)
                movs_ia = movs_para_ia(cont)  # respeita recorte manual / seleção / corte padrão
                chave_movs = _chave_movs(cont, movs_ia)
                box = _ler_box(slug, paj_norm)
                pend = _sols_pendentes(box)
                if len(pend) >= 2:
                    yield {"level": "muted",
                           "msg": f"     {len(pend)} tramitações pendentes → análise adiada para a triagem "
                                  "(IA não chamada agora)."}
                elif movs_ia:
                    # Só agenda se ALGUMA tramitação PENDENTE está com o cache
                    # desatualizado. NÃO reprocessa gravadas/enviadas (análise
                    # congelada) nem tramitações tratadas em separado cuja chave (com
                    # foco) já bate — evita desperdício e a sobrescrita do foco da triagem.
                    faltam = any(
                        s.get("resumo_ia_chave") != _chave_sol(chave_movs, com_rev, s)
                        for s in box.get("solicitacoes", []) if _sol_pendente(s))
                    if faltam:
                        trabalhos_ia.append({"paj_norm": paj_norm, "paj": paj,
                                             "movs": movs_ia, "chave_movs": chave_movs})

            if id_proc:
                await asyncio.sleep(1.0)

        # ===== Fase 2: análises de IA EM PARALELO (até _ANALISE_CONCORRENCIA) =====
        # Cada job cobre UM PAJ (box) — assim cada arquivo JSON é escrito por uma
        # única tarefa (sem corrida de escrita); o paralelismo é ENTRE PAJs.
        if trabalhos_ia:
            conc = config_service.get_analise_concorrencia()
            paralelo = f"até {conc} em paralelo" if conc > 1 else "uma de cada vez (sequencial)"
            yield {"level": "info",
                   "msg": f"Analisando {len(trabalhos_ia)} PAJ(s) com IA ({paralelo})…"}
            sem = asyncio.Semaphore(conc)

            async def _analisar_box(job: dict) -> list[dict]:
                logs: list[dict] = []
                async with sem:
                    box = _ler_box(slug, job["paj_norm"])
                    if not box:
                        return logs
                    cont = ler_contato(job["paj_norm"])
                    movs_por_seq = {str(m.get("seq")): m
                                    for m in (cont.get("movimentacoes") or [])}
                    mudou = False
                    for sol in box.get("solicitacoes", []):
                        if not _sol_pendente(sol):
                            continue  # gravadas/enviadas: análise congelada, não tocar
                        chave = _chave_sol(job["chave_movs"], com_rev, sol)
                        if sol.get("resumo_ia_chave") == chave:
                            continue
                        tinfo = {"data_envio": sol.get("data_envio", ""),
                                 "oficio_origem": sol.get("oficio_origem", ""),
                                 "remetente": sol.get("remetente", ""),
                                 "descricao_tramite": sol.get("descricao_tramite", "")}
                        mf = movs_por_seq.get(str(sol.get("mov_seq", "") or "").strip())
                        if mf:  # respeita o foco do "tratamento separado" (triagem)
                            tinfo["mov_foco"] = mf
                        ana = await asyncio.to_thread(
                            analise_service.analisar_paj, job["movs"], comunicados, nome_caixa, tinfo)
                        dt = sol.get("data_envio", "")
                        if ana:
                            sol["resumo_ia"] = ana.get("resumo", "")
                            sol["sugestao_ia"] = ana.get("sugestao", "")
                            sol["sugestao_tipo"] = ana.get("tipo", "")
                            sol["resumo_ia_chave"] = chave
                            mudou = True
                            logs.append({"level": "ok",
                                         "msg": f"     ✓ PAJ {job['paj']} · tramitação de {dt}: análise recebida"})
                        else:
                            logs.append({"level": "erro",
                                         "msg": f"     ✗ PAJ {job['paj']} · tramitação de {dt}: IA não retornou (ver manualmente)"})
                    if mudou:
                        _gravar_box(slug, box)
                return logs

            tarefas = [asyncio.create_task(_analisar_box(j)) for j in trabalhos_ia]
            for fut in asyncio.as_completed(tarefas):
                for lg in await fut:
                    yield lg

        # PAJs que saíram desta caixa.
        saidos: list[str] = []
        bdir = _box_dir(slug)
        if bdir.is_dir():
            for f in bdir.glob("PAJ-*.json"):
                with contextlib.suppress(Exception):
                    d = json.loads(f.read_text(encoding="utf-8"))
                    if d.get("em_caixa", True) and d.get("paj_norm") not in pajs_atuais:
                        d["em_caixa"] = False
                        _gravar_box(slug, d)
                        saidos.append(d.get("paj", ""))
        if saidos:
            yield {"level": "info", "msg": f"{len(saidos)} PAJ(s) saíram da caixa: {', '.join(saidos)}"}

        dur = time.monotonic() - t0
        resumo = {"ok": True, "slug": slug, "grupo": nome_grupo,
                  "qtd_pajs": len(pajs_atuais), "qtd_solicitacoes": total_envios,
                  "novos": n_novos, "saidos": len(saidos), "duracao": round(dur, 1)}
        yield {"level": "ok",
               "msg": f"Concluído em {dur:.0f}s — {len(pajs_atuais)} PAJ(s), "
                      f"{n_novos} novo(s), {len(saidos)} saíram."}
        yield {"done": True, "resumo": resumo}


async def sincronizar(slug: str) -> dict:
    """Versão não-streaming (drena o gerador)."""
    resumo: dict = {"ok": False}
    async for ev in sincronizar_stream(slug):
        if ev.get("done"):
            resumo = ev.get("resumo", resumo)
    return resumo


async def sincronizar_todas_stream():
    """Sincroniza TODAS as caixas habilitadas, em sequência, emitindo os eventos
    de cada uma (com um cabeçalho por caixa) e um resumo final agregado."""
    caixas = config_service.listar_caixas_habilitadas()
    if not caixas:
        yield {"level": "erro", "msg": "Nenhuma caixa habilitada."}
        yield {"done": True, "resumo": {"ok": False, "erro": "sem_caixas"}}
        return

    t0 = time.monotonic()
    tot_pajs = tot_novos = tot_saidos = 0
    falhas = 0
    for idx, cx in enumerate(caixas, 1):
        yield {"level": "novo", "msg": f"══ [{idx}/{len(caixas)}] {cx['nome']} ══"}
        async for ev in sincronizar_stream(cx["slug"]):
            if ev.get("done"):
                r = ev.get("resumo", {}) or {}
                if r.get("ok"):
                    tot_pajs += r.get("qtd_pajs", 0)
                    tot_novos += r.get("novos", 0)
                    tot_saidos += r.get("saidos", 0)
                else:
                    falhas += 1
                continue  # não propaga o "done" individual
            yield ev

    dur = time.monotonic() - t0
    yield {"level": "ok",
           "msg": f"Todas as caixas concluídas em {dur:.0f}s — {len(caixas)} caixa(s), "
                  f"{tot_pajs} PAJ(s), {tot_novos} novo(s), {tot_saidos} saíram"
                  + (f", {falhas} com falha" if falhas else "") + "."}
    yield {"done": True, "resumo": {"ok": True, "caixas": len(caixas),
                                    "qtd_pajs": tot_pajs, "novos": tot_novos,
                                    "saidos": tot_saidos, "falhas": falhas,
                                    "duracao": round(dur, 1)}}


async def sincronizar_paj(slug: str, paj_norm: str) -> dict:
    """Re-sincroniza UM PAJ de uma caixa: refresca o contato (detalhamento) e
    RE-EXECUTA a análise (resumo + sugestão), sem varrer a caixa inteira.
    Não altera as tramitações (não relê a caixa de grupo)."""
    caixa = config_service.get_caixa(slug)
    if not caixa:
        return {"ok": False, "erro": "caixa desconhecida"}
    box = _ler_box(slug, paj_norm)
    solic = box.get("solicitacoes", []) if box else []
    id_proc = solic[0].get("id") if solic else ""
    if not id_proc:
        return {"ok": False, "erro": "PAJ sem id de processo (sincronize a caixa primeiro)"}

    if not _pode_exclusivo():
        return {"ok": False, "erro": "Há uma operação em andamento (sincronização/envio/triagem). Aguarde."}

    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()

    async with _sync_lock:
        try:
            det = await sisdpu_client.detalhar_paj(id_proc, n_movs=MOVS_EXTRACAO_MAX)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": str(e)}
        atualizar_contato_do_detalhe(paj_norm, det)
        cont = ler_contato(paj_norm)
        movs_ia = movs_para_ia(cont)
        todas = cont.get("movimentacoes", []) or []
        movs_por_seq = {str(m.get("seq")): m for m in todas}
        chave_movs = _chave_movs(cont, movs_ia)
        if movs_ia and analise_service.disponivel():
            box = _ler_box(slug, paj_norm)
            mudou = await asyncio.to_thread(
                _reanalisar_box, box, cont, comunicados, com_rev, caixa["nome"],
                movs_ia, movs_por_seq, chave_movs)
            if mudou:
                _gravar_box(slug, box)
    return {"ok": True}


def _reanalisar_box(box: dict, cont: dict, comunicados: list, com_rev: str,
                    caixa_nome: str, movs_ia: list, movs_por_seq: dict, chave_movs: str,
                    limpar: bool = False) -> bool:
    """Re-executa a análise de IA de um box (SEM scraping), respeitando o estado:
    conjunto → re-analisa o conjunto; senão → cada tramitação (com mov_foco se
    houver vínculo da triagem separada). Usa movs_ia (recorte manual / seleção /
    corte padrão) como contexto enviado à IA. Retorna True se algo mudou.

    `limpar=True` (botão "Reanálise pela IA"): DESCARTA a análise anterior — resumo,
    sugestão, classificação da IA E os overrides manuais (texto e tipo editados) —
    e gera uma análise totalmente nova (ignora o cache)."""
    mudou = False
    conj = box.get("conjunto")
    if box.get("triagem", {}).get("decisao") == "conjunto" and conj:
        ids = conj.get("tramite_ids", [])
        pend = [s for s in box.get("solicitacoes", []) if s.get("id_tramite") in ids]
        tramites_info = [{"data_envio": s.get("data_envio", ""), "oficio_origem": s.get("oficio_origem", ""),
                          "remetente": s.get("remetente", ""),
                          "descricao_tramite": s.get("descricao_tramite", "")} for s in pend]
        desc_conj = _desc_hash("||".join(sorted((s.get("descricao_tramite") or "") for s in pend)))
        chave = f"{chave_movs}|c{com_rev}|conj{','.join(sorted(ids))}|d{desc_conj}"
        if limpar:
            for k in ("resumo_ia", "sugestao_ia", "sugestao_editada",
                      "sugestao_tipo_editada", "resumo_ia_chave"):
                conj[k] = ""
            mudou = True
        if conj.get("resumo_ia_chave") != chave:
            ana = analise_service.analisar_conjunto(movs_ia, comunicados, caixa_nome, tramites_info)
            if ana:
                conj["resumo_ia"] = ana.get("resumo", "")
                conj["sugestao_ia"] = ana.get("sugestao", "")
                conj["resumo_ia_chave"] = chave
                mudou = True
        return mudou
    for sol in box.get("solicitacoes", []):
        mseq = str(sol.get("mov_seq", "") or "")
        tinfo = {"data_envio": sol.get("data_envio", ""),
                 "oficio_origem": sol.get("oficio_origem", ""),
                 "remetente": sol.get("remetente", ""),
                 "descricao_tramite": sol.get("descricao_tramite", "")}
        mf = movs_por_seq.get(mseq)
        if mf:
            tinfo["mov_foco"] = mf
        chave = _chave_sol(chave_movs, com_rev, sol)
        if limpar:
            for k in ("resumo_ia", "sugestao_ia", "sugestao_tipo",
                      "sugestao_tipo_editada", "sugestao_editada", "resumo_ia_chave"):
                sol[k] = ""
            mudou = True
        elif sol.get("resumo_ia_chave") == chave:
            continue
        ana = analise_service.analisar_paj(movs_ia, comunicados, caixa_nome, tinfo)
        if ana:
            sol["resumo_ia"] = ana.get("resumo", "")
            sol["sugestao_ia"] = ana.get("sugestao", "")
            sol["sugestao_tipo"] = ana.get("tipo", "")
            sol["resumo_ia_chave"] = chave
            mudou = True
    return mudou


def definir_selecao_movs(paj_norm: str, selecao: list | None, texto: str | None) -> dict:
    """Persiste, no contato do PAJ, QUAIS movimentações vão à IA: `selecao` (lista
    de seqs marcadas nos checkboxes) e `texto` (recorte manual, que PREVALECE).
    Não reanalisa — quem chama decide (a rota chama reanalisar_paj em seguida)."""
    cont = ler_contato(paj_norm)
    sel: list[int] = []
    for s in (selecao or []):
        try:
            sel.append(int(s))
        except (TypeError, ValueError):
            pass
    cont["movs_selecao"] = sel
    cont["movs_texto_ia"] = (texto or "").strip()[:4000]
    _gravar_contato(cont)
    return cont


async def reanalisar_paj(slug: str, paj_norm: str) -> dict:
    """Reanálise pela IA (botão da seção de Ações): DESCARTA a análise anterior e
    gera uma nova, usando as movimentações JÁ extraídas (SEM voltar ao SISDPU) e a
    seleção/recorte atual do contato. Cobre conjunto e tramitações individuais."""
    caixa = config_service.get_caixa(slug)
    if not caixa:
        return {"ok": False, "erro": "caixa desconhecida"}
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    if not _pode_exclusivo():
        return {"ok": False, "erro": "Há uma operação em andamento (sincronização/envio/triagem). Aguarde."}
    if not analise_service.disponivel():
        return {"ok": True}  # IA desativada — nada a fazer

    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    cont = ler_contato(paj_norm)
    movs_ia = movs_para_ia(cont)
    todas = cont.get("movimentacoes", []) or []
    movs_por_seq = {str(m.get("seq")): m for m in todas}
    chave_movs = _chave_movs(cont, movs_ia)
    if not movs_ia:
        return {"ok": False, "erro": "Nenhuma movimentação selecionada para a IA"}

    async with _sync_lock:
        box = _ler_box(slug, paj_norm)
        mudou = await asyncio.to_thread(
            _reanalisar_box, box, cont, comunicados, com_rev, caixa["nome"],
            movs_ia, movs_por_seq, chave_movs, True)  # limpar=True: descarta e refaz
        if mudou:
            _gravar_box(slug, box)
    return {"ok": True}


# ==================== disparo em lote da fila (Enviar Agora) ====================
def _status_geral(resultados: list[dict]) -> str:
    """Consolida o status de um PAJ a partir dos resultados por meio."""
    if not resultados:
        return "vazio"
    ok = [r for r in resultados if r.get("ok")]
    if not ok:
        return "falha"
    if len(ok) < len(resultados):
        return "parcial"
    return "simulado" if any(r.get("status") == "simulado" for r in ok) else "enviado"


def _achar_item_fila(fila: list[dict], paj_norm: str, tipo: str, id_tramite: str) -> dict | None:
    """Localiza um item da fila por PAJ + tipo (tramite/conjunto) + id_tramite."""
    for it in fila:
        if it.get("paj_norm") != paj_norm:
            continue
        if tipo == "conjunto":
            if it.get("tipo") == "conjunto":
                return it
        elif it.get("tipo") != "conjunto" and it.get("id_tramite") == id_tramite:
            return it
    return None


async def _enviar_item_stream(slug, caixa, t, modo, rotulo=""):
    """Dispara UM item da fila (tramitação ou conjunto), por cada meio; registra no
    histórico global + no box e o remove da fila. Yields eventos de log e, por
    último, o sentinela {"_status": <status>}. Deve rodar DENTRO do _sync_lock."""
    from services import envio_service, historico_service
    paj_norm = t["paj_norm"]
    conjunto = t.get("tipo") == "conjunto"
    ids = t.get("tramite_ids", []) if conjunto else [t.get("id_tramite", "")]
    meios = t.get("envio_meios", []) or []
    mensagem = t.get("envio_mensagem", "")
    nome = t.get("assistido") or "s/ nome"
    etiqueta = (f"conjunto de {len(ids)} tramitações" if conjunto
                else f"tramitação {t.get('data_envio', '')}")
    if not meios:
        yield {"level": "erro",
               "msg": f"{rotulo}PAJ {t['paj']} ({nome}): sem meio selecionado — mantido na fila."}
        yield {"_status": "falha"}
        return

    n_anx = len(t.get("anexos", []) or [])
    anx_txt = f" · 📎 {n_anx} anexo(s)" if n_anx else " · sem anexos"
    yield {"level": "novo",
           "msg": f"{rotulo}PAJ {t['paj']} — {etiqueta} — {nome} · "
                  f"{', '.join(envio_service.label_meio(m) for m in meios)}{anx_txt}"}

    cont = ler_contato(paj_norm)
    anexos_meta = t.get("anexos", []) or []
    tipo_alvo = "conjunto" if conjunto else "tramite"
    anexos_ctx = [{**a, "caminho": str(caminho_anexo(paj_norm, tipo_alvo,
                   "" if conjunto else ids[0], a.get("arquivo", "")))}
                  for a in anexos_meta]
    ctx = {
        "paj": t["paj"], "paj_norm": paj_norm, "tramite_ids": ids,
        "id_processo": t.get("id", ""),          # id interno do PAJ (p/ o Playwright abrir a tela)
        "assistido": t.get("assistido", ""),
        "app_aderidos": _aderidos_app(cont),     # nomes que aderiram ao App (multi-assistido)
        "mensagem": mensagem, "telefones": cont.get("telefones", []),
        "emails": cont.get("emails", []), "whatsapp_numeros": cont.get("whatsapp_numeros", []),
        "somente_leitura": bool(t.get("envio_somente_leitura")),
        "prazo_app": _prazo_ok(t.get("envio_prazo_app", 5)),
        "prazo_whatsapp": _prazo_ok(t.get("envio_prazo_whatsapp", 5)),
        "prazo_email": _prazo_ok(t.get("envio_prazo_email", 5)),
        "anexos": anexos_ctx,
    }
    # Prazo de cada meio (variáveis separadas) e id que agrupa as linhas do disparo.
    prazo_por_meio = {
        "app_sisdpu": _prazo_ok(t.get("envio_prazo_app", 5)),
        "whatsapp": _prazo_ok(t.get("envio_prazo_whatsapp", 5)),
        "email": _prazo_ok(t.get("envio_prazo_email", 5)),
    }
    envio_id = uuid.uuid4().hex[:12]  # agrupa as linhas (UMA por meio) do mesmo disparo
    base_reg = {
        "envio_id": envio_id,
        "paj_norm": paj_norm, "paj": t["paj"], "assistido": t.get("assistido", ""),
        "oficio": t.get("oficio", ""),
        "id_tramite": ids[0] if len(ids) == 1 else "", "tramite_ids": ids,
        "conjunto": conjunto, "tramite_data": t.get("data_envio", ""),
        "tipo": t.get("sugestao_tipo", ""),  # classificação do texto padrão
        "caixa_slug": slug, "caixa_nome": caixa["nome"], "grupo": caixa["grupo"],
        "mensagem": mensagem, "anexos": anexos_meta,
        "anexo_tipo": tipo_alvo, "anexo_ref": ("" if conjunto else ids[0]),
    }

    # UMA linha de histórico POR MEIO (status/prazo próprios + campos de baixa reservados).
    ok_meios, fail_meios, falhas, ok_ids = [], [], {}, []
    algum_real = False
    ultimo_ts = ""
    for meio in meios:
        r = await envio_service.enviar(meio, ctx)
        st = r.get("status", "falha")
        detalhe = r.get("detalhe", st)
        reg = historico_service.registrar({
            **base_reg,
            "meio": meio, "meios": [meio],
            "prazo": prazo_por_meio.get(meio, 5),
            "somente_leitura": bool(t.get("envio_somente_leitura")) if meio == "app_sisdpu" else False,
            "status": st, "detalhe": detalhe,
            # reservados p/ controle futuro de resposta/baixa (por meio):
            "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
        })
        ultimo_ts = reg["ts"]
        yield {"level": ("ok" if r.get("ok") else "erro"),
               "msg": f"     {envio_service.label_meio(meio)}: {detalhe}"}
        if r.get("ok"):
            ok_meios.append(meio)
            ok_ids.append(reg["id"])
            if st == "enviado":
                algum_real = True
        else:
            fail_meios.append(meio)
            falhas[meio] = detalhe

    # status consolidado (só para o resumo do lote).
    if not ok_meios:
        status = "falha"
    elif fail_meios:
        status = "parcial"
    else:
        status = "enviado" if algum_real else "simulado"

    # Box: meios OK viram um registro de enviados e saem; meios com FALHA PERMANECEM
    # na fila (com marcação `falhas`) para reenvio no próximo "Enviar Agora".
    box = _ler_box(slug, paj_norm)
    if box:
        alvo = box.get("conjunto") if conjunto else _achar_solic(box, ids[0])
        if alvo is not None:
            if ok_meios:
                entry = {"em": ultimo_ts, "meios": ok_meios, "mensagem": mensagem,
                         "status": ("enviado" if algum_real else "simulado"),
                         "envio_id": envio_id, "historico_ids": ok_ids, "anexos": anexos_meta,
                         "somente_leitura": bool(t.get("envio_somente_leitura")) if "app_sisdpu" in ok_meios else False}
                if conjunto:
                    entry["tramite_ids"] = ids
                alvo.setdefault("enviados", []).append(entry)
            if fail_meios:
                envio = alvo.get("envio") or {}
                envio["meios"] = fail_meios       # só os que faltam reenviar
                envio["falhas"] = falhas          # {meio: detalhe} — marcação da falha
                envio["falha_em"] = ultimo_ts
                alvo["envio"] = envio
            else:
                alvo["envio"] = {}                # tudo OK → sai da fila
        _gravar_box(slug, box)
    yield {"_status": status}


async def enviar_fila_stream(slug: str):
    """Dispara TODOS os PAJs gravados para envio da caixa, por cada meio, emitindo
    eventos de progresso. Ao final de cada PAJ: grava no histórico global + no
    registro do PAJ e o remove da fila."""
    from services import envio_service, historico_service

    caixa = config_service.get_caixa(slug)
    if not caixa:
        yield {"level": "erro", "msg": f"Caixa '{slug}' não está habilitada."}
        yield {"done": True, "resumo": {"ok": False, "erro": "caixa_desconhecida"}}
        return

    if not _pode_exclusivo():
        yield {"level": "erro", "msg": "Já existe uma operação em andamento (sincronização/envio/triagem)."}
        yield {"done": True, "resumo": {"ok": False, "erro": "em_andamento"}}
        return

    modo = config_service.get_envio_modo()
    async with _sync_lock:
        t0 = time.monotonic()
        fila = listar_fila(slug)
        if not fila:
            yield {"level": "info", "msg": "Nenhum PAJ gravado para envio nesta caixa."}
            yield {"done": True, "resumo": {"ok": True, "enviados": 0, "falhas": 0, "duracao": 0}}
            return

        yield {"level": "info",
               "msg": f"Iniciando envio de {len(fila)} PAJ(s) — {caixa['nome']} · modos: {config_service.resumo_modos()}."}

        enviados = falhas = 0
        for i, t in enumerate(fila, 1):
            async for ev in _enviar_item_stream(slug, caixa, t, modo, rotulo=f"[{i}/{len(fila)}] "):
                if "_status" in ev:
                    if ev["_status"] == "falha":
                        falhas += 1
                    else:
                        enviados += 1
                else:
                    yield ev

        dur = time.monotonic() - t0
        yield {"level": "ok",
               "msg": f"Concluído em {dur:.0f}s — {enviados} enviado(s), {falhas} com falha."}
        yield {"done": True, "resumo": {"ok": True, "slug": slug, "enviados": enviados,
                                        "falhas": falhas, "modo": modo, "duracao": round(dur, 1)}}


async def enviar_todas_filas_stream():
    """"Enviar Agora" GLOBAL da página "Prontos para Envio": dispara a fila de TODAS
    as caixas habilitadas, uma caixa por vez (cada uma via enviar_fila_stream, que
    pega o _sync_lock em sua vez), com um cabeçalho por caixa e resumo agregado."""
    caixas = config_service.listar_caixas_habilitadas()
    tot_env = tot_falhas = 0
    algum = False
    for cx in caixas:
        if not listar_fila(cx["slug"]):
            continue
        algum = True
        yield {"level": "novo", "msg": f"══ {cx['nome']} ══"}
        async for ev in enviar_fila_stream(cx["slug"]):
            if ev.get("done"):
                r = ev.get("resumo", {}) or {}
                tot_env += r.get("enviados", 0)
                tot_falhas += r.get("falhas", 0)
                continue
            yield ev
    if not algum:
        yield {"level": "info", "msg": "Nenhum PAJ gravado para envio."}
    yield {"level": "ok",
           "msg": f"Concluído — {tot_env} enviado(s)"
                  + (f", {tot_falhas} com falha" if tot_falhas else "") + "."}
    yield {"done": True, "resumo": {"ok": True, "enviados": tot_env, "falhas": tot_falhas,
                                    "modo": config_service.get_envio_modo()}}


async def enviar_selecao_stream(selecao: list[dict]):
    """"Enviar Agora" da página "Prontos para Envio" restrito a uma SELEÇÃO.
    selecao: lista de {slug, paj_norm, id_tramite, tipo('tramite'|'conjunto')}.
    Envia SOMENTE esses itens (de quaisquer caixas)."""
    if not _pode_exclusivo():
        yield {"level": "erro", "msg": "Já existe uma operação em andamento (sincronização/envio/triagem)."}
        yield {"done": True, "resumo": {"ok": False, "erro": "em_andamento"}}
        return
    modo = config_service.get_envio_modo()
    async with _sync_lock:
        t0 = time.monotonic()
        cache_fila: dict = {}
        itens: list[tuple] = []
        for s in (selecao or []):
            slug = s.get("slug")
            caixa = config_service.get_caixa(slug)
            if not caixa:
                continue
            if slug not in cache_fila:
                cache_fila[slug] = listar_fila(slug)
            it = _achar_item_fila(cache_fila[slug], s.get("paj_norm"),
                                  s.get("tipo", "tramite"), s.get("id_tramite", ""))
            if it:
                itens.append((slug, caixa, it))
        itens.sort(key=lambda x: 0 if x[2].get("envio_urgente") else 1)  # urgentes primeiro
        if not itens:
            yield {"level": "info", "msg": "Nada a enviar (seleção vazia ou já processada)."}
            yield {"done": True, "resumo": {"ok": True, "enviados": 0, "falhas": 0}}
            return

        yield {"level": "info",
               "msg": f"Enviando {len(itens)} item(ns) selecionado(s) · modos: {config_service.resumo_modos()}."}
        enviados = falhas = 0
        for i, (slug, caixa, t) in enumerate(itens, 1):
            async for ev in _enviar_item_stream(slug, caixa, t, modo,
                                                 rotulo=f"[{i}/{len(itens)}] {caixa['nome']} · "):
                if "_status" in ev:
                    if ev["_status"] == "falha":
                        falhas += 1
                    else:
                        enviados += 1
                else:
                    yield ev
        dur = time.monotonic() - t0
        yield {"level": "ok",
               "msg": f"Concluído em {dur:.0f}s — {enviados} enviado(s), {falhas} com falha."}
        yield {"done": True, "resumo": {"ok": True, "enviados": enviados, "falhas": falhas,
                                        "modo": modo, "duracao": round(dur, 1)}}


# ==================== reset do painel ====================
def resetar_painel(total: bool) -> dict:
    """Reseta o painel. Move os dados OPERACIONAIS (PAJs/tramitações por caixa,
    contatos e histórico) para uma lixeira (reversível). Se `total`, também
    restaura o config ao padrão de fábrica (config_divcom.json.bak é salvo antes).
    Se parcial, PRESERVA a parametrização (textos/parágrafos/caixas/ajustes)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    modo = "total" if total else "parcial"
    lixeira = DADOS_DIR / "_lixeira" / f"reset-{modo}-{ts}"
    lixeira.mkdir(parents=True, exist_ok=True)

    caixas_movidas = 0
    for d in list(DADOS_DIR.iterdir()):
        # move as pastas POR CAIXA (não as internas _contatos/_historico/_lixeira)
        if d.is_dir() and not d.name.startswith("_"):
            with contextlib.suppress(Exception):
                shutil.move(str(d), str(lixeira / d.name))
                caixas_movidas += 1

    if CONTATOS_DIR.exists():
        with contextlib.suppress(Exception):
            shutil.move(str(CONTATOS_DIR), str(lixeira / "_contatos"))
    hist_dir = DADOS_DIR / "_historico"
    if hist_dir.exists():
        with contextlib.suppress(Exception):
            shutil.move(str(hist_dir), str(lixeira / "_historico"))
    if ANEXOS_DIR.exists():
        with contextlib.suppress(Exception):
            shutil.move(str(ANEXOS_DIR), str(lixeira / "_anexos"))

    CONTATOS_DIR.mkdir(parents=True, exist_ok=True)  # recria vazio

    if total:
        config_service.resetar_config()

    return {"ok": True, "modo": modo, "caixas_limpas": caixas_movidas,
            "lixeira": str(lixeira)}


# ==================== migração do formato antigo (flat) ====================
def _migrar_flat() -> None:
    """Move dados/PAJ-*.json (formato antigo, plano) para dados/divcom/ (caixa)
    + dados/_contatos/ (compartilhado). Executado uma vez no import."""
    flat = list(DADOS_DIR.glob("PAJ-*.json"))
    if not flat:
        return
    for f in flat:
        paj_norm = f.stem
        # Já migrado? (registro da caixa existe) — só remove o plano, NUNCA
        # sobrescreve dados novos.
        if _box_file("divcom", paj_norm).exists():
            with contextlib.suppress(Exception):
                f.unlink()
            continue
        d = _read_json(f)
        if not d:
            with contextlib.suppress(Exception):
                f.unlink()
            continue
        paj_norm = d.get("paj_norm") or f.stem
        # registro por caixa (divcom)
        box = {
            "paj_norm": paj_norm, "paj": d.get("paj", ""),
            "em_caixa": d.get("em_caixa", True),
            "atualizado_em": d.get("atualizado_em", ""),
            "solicitacoes": d.get("solicitacoes", []),
            "resumo_ia": d.get("resumo_ia", ""), "resumo_ia_chave": d.get("resumo_ia_chave", ""),
            "sugestao_ia": d.get("sugestao_ia", ""), "sugestao_tipo": d.get("sugestao_tipo", ""),
            "sugestao_editada": d.get("sugestao_editada", ""),
        }
        _gravar_box("divcom", box)
        # contato compartilhado
        cont = ler_contato(paj_norm)
        if d.get("assistido"):
            cont["assistido"] = d["assistido"]
        if d.get("contatos"):
            cont["contatos"] = d["contatos"]
        for k in ("telefones", "emails", "enderecos", "whatsapp_numeros"):
            if d.get(k):
                cont[k] = d[k]
        if d.get("whatsapp_config"):
            cont["whatsapp_config"] = d["whatsapp_config"]
        _gravar_contato(cont)
        with contextlib.suppress(Exception):
            f.unlink()


def _migrar_local_dados() -> None:
    """Move a pasta de dados que ficava DENTRO do Projeto (BASE_DIR/dados) para
    o novo local, ao lado do Projeto (DADOS_DIR). Executado uma vez no import."""
    old = BASE_DIR / "dados"
    if not old.exists() or old.resolve() == DADOS_DIR.resolve():
        return
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    for item in old.iterdir():
        dest = DADOS_DIR / item.name
        if not dest.exists():
            with contextlib.suppress(Exception):
                shutil.move(str(item), str(dest))
    with contextlib.suppress(Exception):
        if not any(old.iterdir()):
            old.rmdir()


def _migrar_para_tramite() -> None:
    """Migra o estado que ficava no NÍVEL DO PAJ (resumo/sugestão/envio/
    contatos_enviados) para a PRIMEIRA tramitação de cada box (modelo por tramitação).
    Idempotente: só age quando encontra chaves de nível-PAJ."""
    if not DADOS_DIR.exists():
        return
    for slugdir in DADOS_DIR.iterdir():
        if not slugdir.is_dir() or slugdir.name.startswith("_"):
            continue
        for f in slugdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                chaves_legado = ("resumo_ia", "resumo_ia_chave", "sugestao_ia",
                                 "sugestao_tipo", "sugestao_editada", "envio", "contatos_enviados")
                if not any(k in box for k in chaves_legado):
                    continue
                sols = box.get("solicitacoes", [])
                if sols:
                    # anexa o estado legado à tramitação MAIS ANTIGA (data + menor
                    # id_tramite) — provável alvo do contato já feito.
                    def _tri_key(s):
                        idt = s.get("id_tramite", "")
                        try:
                            n = int(idt)
                        except (TypeError, ValueError):
                            n = 10 ** 18
                        return (_data_key(s.get("data_envio", "")) or "99999999", n)
                    alvo = min(sols, key=_tri_key)
                    alvo.setdefault("resumo_ia", box.get("resumo_ia", ""))
                    alvo.setdefault("resumo_ia_chave", box.get("resumo_ia_chave", ""))
                    alvo.setdefault("sugestao_ia", box.get("sugestao_ia", ""))
                    alvo.setdefault("sugestao_tipo", box.get("sugestao_tipo", ""))
                    alvo.setdefault("sugestao_editada", box.get("sugestao_editada", ""))
                    if not alvo.get("envio"):
                        alvo["envio"] = box.get("envio", {}) or {}
                    if not alvo.get("enviados"):
                        alvo["enviados"] = box.get("contatos_enviados", []) or []
                    for s in sols:
                        for k in ("resumo_ia", "resumo_ia_chave", "sugestao_ia",
                                  "sugestao_tipo", "sugestao_editada"):
                            s.setdefault(k, "")
                        s.setdefault("envio", {})
                        s.setdefault("enviados", [])
                for k in chaves_legado:
                    box.pop(k, None)
                _write_json(f, box)


_migrar_local_dados()
DADOS_DIR.mkdir(parents=True, exist_ok=True)
CONTATOS_DIR.mkdir(exist_ok=True)
_migrar_flat()
_migrar_para_tramite()
