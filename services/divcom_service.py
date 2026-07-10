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
from pathlib import Path

from ingestao import sisdpu_client
from services import analise_service, config_service

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL DIVCOM/Projeto
# Dados sincronizados FORA da pasta Projeto (ao lado dela).
DADOS_DIR = BASE_DIR.parent / "dados"
CONTATOS_DIR = DADOS_DIR / "_contatos"

CONTATOS_KEYS = ["app_sisdpu", "telefone", "whatsapp", "email", "endereco"]

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
    for k in ("telefones", "emails", "enderecos", "whatsapp_numeros", "movimentacoes"):
        d.setdefault(k, [])
    d.setdefault("whatsapp_config", False)
    return d


def _gravar_contato(d: dict) -> None:
    _write_json(_contato_file(d["paj_norm"]), d)


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
def gravar_para_envio(slug: str, paj_norm: str, id_tramite: str, mensagem: str, meios: list[str]) -> dict:
    """Grava UMA tramitação na fila de envio (sai de "pendentes" e entra em
    "gravados para envio"). Guarda a mensagem a disparar e os meios escolhidos."""
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


def listar_fila(slug: str) -> list[dict]:
    """Itens gravados para envio (tramitações individuais + conjuntos)."""
    return montar_dashboard(slug)["fila"]


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
    d["movimentacoes"] = cont.get("movimentacoes", [])
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
    d["envio_gravado_em"] = envio.get("gravado_em", "")
    # já enviados por tramitação
    env = sol.get("enviados", []) or []
    d["enviados"] = env
    d["contato_enviado"] = len(env) > 0
    d["n_contatos"] = len(env)
    ult = env[-1] if env else {}
    d["contato_meios"] = ult.get("meios", [])
    d["contato_mensagem"] = ult.get("mensagem", "")
    d["contato_em"] = ult.get("em", "")
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
    """Nº de itens que exigem ação: triagem + pendentes + gravados para envio.
    Conjuntos contam como 1. Exclui os já enviados."""
    db = montar_dashboard(slug)
    return len(db["triagem"]) + len(db["pendentes"]) + len(db["fila"])


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
    d["envio_gravado_em"] = envio.get("gravado_em", "")
    env = conj.get("enviados", []) or []
    d["enviados"] = env
    d["contato_enviado"] = len(env) > 0
    d["n_contatos"] = len(env)
    ult = env[-1] if env else {}
    d["contato_meios"] = ult.get("meios", [])
    d["contato_mensagem"] = ult.get("mensagem", "")
    d["contato_em"] = ult.get("em", "")
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
    fila.sort(key=lambda d: d.get("envio_gravado_em", ""))
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
    if _sync_lock.locked():
        return {"ok": False, "erro": "Há uma sincronização em andamento. Aguarde."}
    box = _ler_box(slug, paj_norm)
    pend = _sols_pendentes(box) if box else []
    if len(pend) < 2:
        return {"ok": False, "erro": "PAJ não tem 2+ tramitações pendentes"}
    id_proc = pend[0].get("id", "")
    n_movs = config_service.get_n_movimentacoes()
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    async with _sync_lock:
        if id_proc:
            with contextlib.suppress(Exception):
                det = await sisdpu_client.detalhar_paj(id_proc, n_movs=n_movs)
                if det:
                    atualizar_contato_do_detalhe(paj_norm, det)
        cont = ler_contato(paj_norm)
        movs = cont.get("movimentacoes", []) or []
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
        if movs and analise_service.disponivel():
            ana = await asyncio.to_thread(
                analise_service.analisar_conjunto, movs, comunicados, caixa["nome"], tramites_info)
        seqs = ",".join(str(m.get("seq")) for m in movs)
        box["conjunto"] = {
            "tramite_ids": pend_ids,
            "resumo_ia": (ana or {}).get("resumo", ""),
            "sugestao_ia": (ana or {}).get("sugestao", ""),
            "sugestao_editada": "",
            "resumo_ia_chave": f"{seqs}|n{n_movs}|c{com_rev}|conj{','.join(pend_ids)}|d{desc_conj}",
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
    if _sync_lock.locked():
        return {"ok": False, "erro": "Há uma operação em andamento. Aguarde."}
    mapeamento = mapeamento or {}
    n_movs = config_service.get_n_movimentacoes()
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()
    cont = ler_contato(paj_norm)
    movs = cont.get("movimentacoes", []) or []
    movs_por_seq = {str(m.get("seq")): m for m in movs}
    seqs = ",".join(str(m.get("seq")) for m in movs)
    async with _sync_lock:
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
            if movs and analise_service.disponivel():
                ana = await asyncio.to_thread(
                    analise_service.analisar_paj, movs, comunicados, caixa["nome"], tinfo)
                if ana:
                    s["resumo_ia"] = ana.get("resumo", "")
                    s["sugestao_ia"] = ana.get("sugestao", "")
                    s["sugestao_tipo"] = ana.get("tipo", "")
                    s["resumo_ia_chave"] = (f"{seqs}|n{n_movs}|c{com_rev}|t{s.get('id_tramite', '')}"
                                            f"|m{mseq}|d{_desc_hash(s.get('descricao_tramite', ''))}")
            s["mov_seq"] = mseq
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


def gravar_conjunto(slug: str, paj_norm: str, mensagem: str, meios: list[str]) -> dict:
    """Grava o CONJUNTO de um PAJ na fila de envio."""
    from services import envio_service
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    meios = [m for m in (meios or []) if m in envio_service.MEIOS]
    box["conjunto"]["envio"] = {
        "status": "gravado", "mensagem": (mensagem or "").strip(),
        "meios": meios, "gravado_em": _dt.datetime.now().isoformat(timespec="seconds"),
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
_sync_lock = asyncio.Lock()


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

    if _sync_lock.locked():
        yield {"level": "erro", "msg": "Já existe uma sincronização em andamento."}
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
                    det = await sisdpu_client.detalhar_paj(id_proc, n_movs=n_movs)
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
                movs = det.get("movimentacoes", []) or []
                box = _ler_box(slug, paj_norm)
                pend = _sols_pendentes(box)
                if len(pend) >= 2:
                    yield {"level": "muted",
                           "msg": f"     {len(pend)} tramitações pendentes → análise adiada para a triagem "
                                  "(IA não chamada agora)."}
                elif movs:
                    seqs = ",".join(str(m.get("seq")) for m in movs)
                    # Só agenda se ALGUMA tramitação está com o cache desatualizado.
                    faltam = any(
                        s.get("resumo_ia_chave")
                        != f"{seqs}|n{n_movs}|c{com_rev}|t{s.get('id_tramite', '')}"
                           f"|d{_desc_hash(s.get('descricao_tramite', ''))}"
                        for s in box.get("solicitacoes", []))
                    if faltam:
                        trabalhos_ia.append({"paj_norm": paj_norm, "paj": paj,
                                             "movs": movs, "seqs": seqs})

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
                    mudou = False
                    for sol in box.get("solicitacoes", []):
                        chave = (f"{job['seqs']}|n{n_movs}|c{com_rev}|t{sol.get('id_tramite', '')}"
                                 f"|d{_desc_hash(sol.get('descricao_tramite', ''))}")
                        if sol.get("resumo_ia_chave") == chave:
                            continue
                        ana = await asyncio.to_thread(
                            analise_service.analisar_paj, job["movs"], comunicados, nome_caixa,
                            {"data_envio": sol.get("data_envio", ""),
                             "oficio_origem": sol.get("oficio_origem", ""),
                             "remetente": sol.get("remetente", ""),
                             "descricao_tramite": sol.get("descricao_tramite", "")})
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

    if _sync_lock.locked():
        return {"ok": False, "erro": "Há uma sincronização em andamento. Aguarde."}

    n_movs = config_service.get_n_movimentacoes()
    comunicados = config_service.comunicados_da_caixa(slug)
    com_rev = config_service.comunicados_rev()

    async with _sync_lock:
        try:
            det = await sisdpu_client.detalhar_paj(id_proc, n_movs=n_movs)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": str(e)}
        atualizar_contato_do_detalhe(paj_norm, det)
        movs = det.get("movimentacoes", []) or []
        if movs and analise_service.disponivel():
            box = _ler_box(slug, paj_norm)
            seqs = ",".join(str(m.get("seq")) for m in movs)
            movs_por_seq = {str(m.get("seq")): m for m in movs}
            mudou = False
            for sol in box.get("solicitacoes", []):  # re-analisa TODAS as tramitações
                mseq = str(sol.get("mov_seq", "") or "")
                tinfo = {"data_envio": sol.get("data_envio", ""),
                         "oficio_origem": sol.get("oficio_origem", ""),
                         "remetente": sol.get("remetente", ""),
                         "descricao_tramite": sol.get("descricao_tramite", "")}
                mf = movs_por_seq.get(mseq)  # se a tramitação foi vinculada a uma mov
                if mf:
                    tinfo["mov_foco"] = mf
                ana = await asyncio.to_thread(
                    analise_service.analisar_paj, movs, comunicados, caixa["nome"], tinfo)
                if ana:
                    sol["resumo_ia"] = ana.get("resumo", "")
                    sol["sugestao_ia"] = ana.get("sugestao", "")
                    sol["sugestao_tipo"] = ana.get("tipo", "")
                    sol["resumo_ia_chave"] = (f"{seqs}|n{n_movs}|c{com_rev}|t{sol.get('id_tramite', '')}"
                                              f"|m{mseq}|d{_desc_hash(sol.get('descricao_tramite', ''))}")
                    mudou = True
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

    if _sync_lock.locked():
        yield {"level": "erro", "msg": "Já existe uma operação em andamento."}
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

        aviso = " (MODO SIMULADO — nada será realmente enviado)" if modo == "simulado" else ""
        yield {"level": "info",
               "msg": f"Iniciando envio de {len(fila)} PAJ(s) — {caixa['nome']}{aviso}."}

        enviados = falhas = 0
        for i, t in enumerate(fila, 1):
            paj_norm = t["paj_norm"]
            conjunto = t.get("tipo") == "conjunto"
            ids = t.get("tramite_ids", []) if conjunto else [t.get("id_tramite", "")]
            meios = t.get("envio_meios", []) or []
            mensagem = t.get("envio_mensagem", "")
            nome = t.get("assistido") or "s/ nome"
            etiqueta = (f"conjunto de {len(ids)} tramitações" if conjunto
                        else f"tramitação {t.get('data_envio', '')}")
            if not meios:
                falhas += 1
                yield {"level": "erro",
                       "msg": f"[{i}/{len(fila)}] PAJ {t['paj']} ({nome}): sem meio selecionado — mantido na fila."}
                continue

            yield {"level": "novo",
                   "msg": f"[{i}/{len(fila)}] PAJ {t['paj']} — {etiqueta} — {nome} · "
                          f"{', '.join(envio_service.label_meio(m) for m in meios)}"}

            cont = ler_contato(paj_norm)
            ctx = {
                "paj": t["paj"], "paj_norm": paj_norm, "tramite_ids": ids,
                "assistido": t.get("assistido", ""),
                "mensagem": mensagem, "telefones": cont.get("telefones", []),
                "emails": cont.get("emails", []), "whatsapp_numeros": cont.get("whatsapp_numeros", []),
            }
            resultados, res_por_meio = [], {}
            for meio in meios:
                r = await envio_service.enviar(meio, ctx)
                r["meio"] = meio
                resultados.append(r)
                res_por_meio[meio] = r.get("status", "falha")
                nivel = "ok" if r.get("ok") else "erro"
                yield {"level": nivel,
                       "msg": f"     {envio_service.label_meio(meio)}: {r.get('detalhe', r.get('status'))}"}

            status = _status_geral(resultados)
            if status == "falha":
                falhas += 1
            else:
                enviados += 1

            reg = historico_service.registrar({
                "paj_norm": paj_norm, "paj": t["paj"], "assistido": t.get("assistido", ""),
                "id_tramite": ids[0] if len(ids) == 1 else "", "tramite_ids": ids,
                "conjunto": conjunto, "tramite_data": t.get("data_envio", ""),
                "caixa_slug": slug, "caixa_nome": caixa["nome"], "grupo": caixa["grupo"],
                "meios": meios, "mensagem": mensagem, "anexos": [],
                "status": status, "resultado": res_por_meio,
            })

            if status != "falha":
                box = _ler_box(slug, paj_norm)
                registro = {"em": reg["ts"], "meios": meios, "mensagem": mensagem,
                            "status": status, "resultado": res_por_meio, "historico_id": reg["id"]}
                if conjunto and box.get("conjunto"):
                    box["conjunto"].setdefault("enviados", []).append({**registro, "tramite_ids": ids})
                    box["conjunto"]["envio"] = {}
                else:
                    sol = _achar_solic(box, ids[0]) if box else None
                    if sol:
                        sol.setdefault("enviados", []).append(registro)
                        sol["envio"] = {}
                if box:
                    _gravar_box(slug, box)

        dur = time.monotonic() - t0
        yield {"level": "ok",
               "msg": f"Concluído em {dur:.0f}s — {enviados} enviado(s), {falhas} com falha."}
        yield {"done": True, "resumo": {"ok": True, "slug": slug, "enviados": enviados,
                                        "falhas": falhas, "modo": modo, "duracao": round(dur, 1)}}


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
