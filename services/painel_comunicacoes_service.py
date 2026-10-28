"""Serviço de dados do Painel de Comunicações — MULTI-CAIXA (modelo híbrido).

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
from services.texto_util import colapsar_paragrafos, remover_emojis

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL COMUNICACOES/Projeto
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
        # estado do fluxo de LIGAÇÕES TELEFÔNICAS e de CORRESPONDÊNCIA (por trâmite) —
        # preservados entre sincronizações para não perder a rotina em andamento.
        "ligacao": a.get("ligacao", {}) or {},
        "correspondencia": a.get("correspondencia", {}) or {},
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


def url_detalhe_do_paj(slug: str, paj_norm: str) -> str:
    """URL de detalhamento do PAJ no SISDPU (de qualquer tramitação do box). Usado
    para o Histórico linkar o número do PAJ mesmo em registros antigos sem a URL."""
    box = _ler_box(slug, paj_norm)
    for s in (box.get("solicitacoes", []) if box else []):
        if s.get("url_detalhe"):
            return s["url_detalhe"]
    return ""


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
    s["sugestao_editada"] = colapsar_paragrafos(remover_emojis(texto))  # só texto — sem emojis
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


def _paj_tem_endereco(paj_norm: str) -> bool:
    """True se o PAJ tem ao menos um endereço com CEP (postável). Sem endereço a CARTA
    fica INDISPONÍVEL — não se grava por carta nem se expede correspondência."""
    cont = ler_contato(paj_norm)
    return any((e.get("cep") or "").strip() for e in (cont.get("enderecos") or []))


def gravar_para_envio(slug: str, paj_norm: str, id_tramite: str, mensagem: str,
                      meios: list[str], somente_leitura: bool = False,
                      prazos: dict | None = None, tramitar_grupo: str | None = None) -> dict:
    """Grava UMA tramitação na fila de envio (sai de "pendentes" e entra em
    "gravados para envio"). Guarda a mensagem a disparar e os meios escolhidos.
    `somente_leitura` (App SISDPU): o assistido só lê a notificação, não responde.
    `prazos` (dict por meio {app_sisdpu, whatsapp, email}): prazo de resposta em
    dias de cada meio (>=1, padrão 5); armazenados como prazo_app/whatsapp/email.
    `tramitar_grupo`: se informado, o PAJ é tramitado p/ essa caixa de grupo APÓS
    envio + movimentação bem-sucedidos ('' ou None = não tramitar).

    `meios` é CUMULATIVO e pode incluir, além dos meios de mensagem (app_sisdpu/whatsapp),
    os canais 'telefone' e 'carta' — que NÃO enviam mensagem: encaminham o PAJ para as
    centrais de Ligações / Correspondências (levando a mensagem como base do registro/carta)."""
    from services import envio_service
    todos = meios or []
    # 1) Canais de ENCAMINHAMENTO (cumulativos) — cada um grava no próprio trilho.
    if "telefone" in todos:
        enfileirar_ligacao(slug, paj_norm, id_tramite, mensagem=mensagem)
    # Carta só quando há endereço postável (defesa; a UI já desabilita a opção sem endereço).
    if "carta" in todos and _paj_tem_endereco(paj_norm):
        encaminhar_correspondencia(slug, paj_norm, id_tramite, mensagem=mensagem)
    # 2) Meios de MENSAGEM (app/whatsapp/email) — fila de "Prontos para Envio".
    msg_meios = [m for m in todos if m in envio_service.MEIOS]
    box = _ler_box(slug, paj_norm)  # re-lê após os encaminhamentos p/ preservar ligacao/correspondencia
    if not box:
        return {}
    s = _achar_solic(box, id_tramite)
    if not s:
        return {}
    if msg_meios:
        s["envio"] = {
            "status": "gravado",
            "mensagem": colapsar_paragrafos(remover_emojis(mensagem)),  # só texto — sem emojis
            "meios": msg_meios,
            "somente_leitura": bool(somente_leitura),
            "tramitar_grupo": (tramitar_grupo or "").strip(),
            **_prazos_envio(prazos),
            "gravado_em": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        _gravar_box(slug, box)
    return box


def _remover_meio_ou_zerar(envio: dict, meio: str | None) -> dict:
    """Remove APENAS `meio` da lista `envio.meios` (e sua falha, se houver). Se `meio`
    não for informado, não estiver na lista, ou for o ÚNICO meio, ZERA o envio inteiro
    (volta a "pendente"). Retorna o novo dict de envio ({} = pendente)."""
    envio = envio or {}
    meios = list(envio.get("meios") or [])
    if meio:
        if meio not in meios:
            return envio  # nada a remover (no-op, não zera por engano)
        if len(meios) > 1:
            meios.remove(meio)
            envio["meios"] = meios
            if isinstance(envio.get("falhas"), dict):
                envio["falhas"].pop(meio, None)
            return envio
        return {}  # era o único meio -> volta a "pendente"
    return {}  # sem meio informado -> zera tudo (remoção completa)


def remover_da_fila(slug: str, paj_norm: str, id_tramite: str, meio: str | None = None) -> dict:
    """Tira uma tramitação da fila de envio. Se `meio` for dado e o PAJ tiver OUTROS
    meios pendentes, remove só aquele meio (continua gravado nos demais); senão (único
    meio ou sem meio) volta a ser "pendente" na caixa de origem."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {}
    s = _achar_solic(box, id_tramite)
    if not s:
        return {}
    s["envio"] = _remover_meio_ou_zerar(s.get("envio"), meio)
    _gravar_box(slug, box)
    return box


# ==================== anexos ====================
# Documentos que serão enviados junto com a mensagem. Ficam vinculados à
# TRAMITAÇÃO (sol["anexos"]) ou ao CONJUNTO (box["conjunto"]["anexos"]) e
# persistem independentemente de gravar/remover da fila. Arquivos gravados em
# dados/_anexos/<PAJ>/<id_tramite|conjunto>/ (fora do Projeto).
ANEXOS_DIR = DADOS_DIR / "_anexos"
_ANEXO_MAX = 25 * 1024 * 1024  # 25 MB por arquivo
# Comprovantes de envio por WhatsApp (PDF de 2 páginas gerado no envio).
COMPROVANTES_WA_DIR = DADOS_DIR / "_comprovantes_whatsapp"
# Comprovantes de postagem de CARTA (PDF do SMT/Correios, baixado ao efetivar).
COMPROVANTES_CARTA_DIR = DADOS_DIR / "_comprovantes_carta"
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


# ==================== anexos PADRÃO (por comunicado / parágrafo) ====================
# Documentos pré-configurados em Configurações que, ao APLICAR o texto/parágrafo
# padrão no dashboard, são copiados automaticamente para os anexos da tramitação.
# Arquivos ficam em dados/_anexos_padrao/<owner_id>/; metadados no config (via
# config_service). Ao copiar para a tramitação, marcamos padrao_owner/padrao_kind
# para permitir remoção seletiva depois (quando o usuário desfaz a aplicação).
ANEXOS_PADRAO_DIR = DADOS_DIR / "_anexos_padrao"


def _anexos_padrao_dir(owner_id: str) -> Path:
    return ANEXOS_PADRAO_DIR / owner_id


def caminho_anexo_padrao(owner_id: str, arquivo: str) -> Path:
    return _anexos_padrao_dir(owner_id) / arquivo


def adicionar_anexos_padrao(kind: str, owner_id: str, slug: str | None,
                            arquivos: list[tuple[str, bytes]]) -> dict:
    """Salva documentos padrão de um comunicado/parágrafo e registra os metadados
    no config. Mesmas regras de formato/tamanho/dedupe dos anexos de tramitação."""
    if config_service.owner_padrao(kind, owner_id, slug) is None:
        return {"ok": False, "erro": "texto/parágrafo não encontrado"}
    anexos = config_service.anexos_padrao(kind, owner_id, slug)
    dest = _anexos_padrao_dir(owner_id)
    dest.mkdir(parents=True, exist_ok=True)
    existentes = {(a.get("nome") or "").strip().lower() for a in anexos}
    duplicados, rejeitados = [], []
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
        arquivo_salvo = f"{aid}__{_nome_seguro(nome)}"
        with contextlib.suppress(Exception):
            (dest / arquivo_salvo).write_bytes(dados)
        anexos.append({
            "id": aid, "nome": nome_base, "arquivo": arquivo_salvo,
            "tamanho": len(dados), "tipo": Path(arquivo_salvo).suffix.lstrip(".").lower(),
            "incluido_em": _dt.datetime.now().isoformat(timespec="seconds"),
        })
    config_service.set_anexos_padrao(kind, owner_id, slug, anexos)
    return {"ok": True, "anexos": anexos, "duplicados": duplicados, "rejeitados": rejeitados}


def remover_anexo_padrao(kind: str, owner_id: str, slug: str | None, anexo_id: str) -> dict:
    """Remove um anexo padrão (arquivo + metadado) do comunicado/parágrafo."""
    anexos = config_service.anexos_padrao(kind, owner_id, slug)
    restantes = []
    for a in anexos:
        if a.get("id") == anexo_id:
            with contextlib.suppress(Exception):
                (_anexos_padrao_dir(owner_id) / a.get("arquivo", "")).unlink()
        else:
            restantes.append(a)
    if not config_service.set_anexos_padrao(kind, owner_id, slug, restantes):
        return {"ok": False, "erro": "texto/parágrafo não encontrado"}
    return {"ok": True, "anexos": restantes}


def _copiar_anexos_padrao_para(anexos: list, paj_norm: str, tipo: str, id_tramite: str,
                               kind: str, owner_id: str, padrao: list) -> list[str]:
    """Copia os anexos PADRÃO (`padrao` = metadados; arquivos em _anexos_padrao/<owner_id>)
    para a lista `anexos` IN-PLACE (aditivo, dedupe por nome); copia os arquivos p/ a pasta
    de anexos do alvo e marca padrao_owner/kind. Retorna os nomes incluídos. NÃO grava o
    box — quem chama persiste."""
    dest = _anexos_dir(paj_norm, _chave_anexo(tipo, id_tramite))
    dest.mkdir(parents=True, exist_ok=True)
    existentes = {(a.get("nome") or "").strip().lower() for a in anexos}
    incluidos: list[str] = []
    for pa in padrao:
        nome = (pa.get("nome") or "").strip()
        if not nome or nome.lower() in existentes:
            continue
        aid = uuid.uuid4().hex[:12]
        arquivo_salvo = f"{aid}__{_nome_seguro(nome)}"
        with contextlib.suppress(Exception):
            src = caminho_anexo_padrao(owner_id, pa.get("arquivo", ""))
            if src.exists():
                shutil.copy2(src, dest / arquivo_salvo)
        anexos.append({
            "id": aid, "nome": pa.get("nome"), "arquivo": arquivo_salvo,
            "tamanho": pa.get("tamanho", 0), "tipo": pa.get("tipo", ""),
            "incluido_em": _dt.datetime.now().isoformat(timespec="seconds"),
            "padrao_owner": owner_id, "padrao_kind": kind,
        })
        existentes.add(nome.lower())
        incluidos.append(pa.get("nome"))
    return incluidos


def _comunicado_por_tipo(comunicados: list, tipo: str) -> dict | None:
    """Acha, na lista de comunicados da caixa, o que tem EXATAMENTE esse `tipo` E possui
    anexos padrão vinculados. None se não houver."""
    alvo = (tipo or "").strip()
    if not alvo:
        return None
    return next((c for c in (comunicados or [])
                 if (c.get("tipo") or "").strip() == alvo and c.get("anexos")), None)


def _auto_incluir_anexos_padrao_por_tipo(paj_norm: str, sol: dict, comunicados: list) -> list[str]:
    """Quando o TIPO (classificação da IA ou manual) corresponde a um comunicado COM
    anexo padrão, inclui esse anexo automaticamente na tramitação — espelhando o que o
    'aplicar modelo' faz no modal. Opera no `sol` em memória (o box é gravado por quem
    chama). Idempotente (dedupe por nome)."""
    tipo = (sol.get("sugestao_tipo_editada") or sol.get("sugestao_tipo") or "").strip()
    com = _comunicado_por_tipo(comunicados, tipo)
    if not com:
        return []
    anexos = sol.setdefault("anexos", [])
    return _copiar_anexos_padrao_para(anexos, paj_norm, "tramite", sol.get("id_tramite", ""),
                                      "comunicado", com.get("id", ""), com.get("anexos") or [])


def anexar_padrao_em_tramite(slug: str, paj_norm: str, tipo: str, id_tramite: str,
                             kind: str, owner_id: str, owner_slug: str | None) -> dict:
    """Copia os anexos PADRÃO de um comunicado/parágrafo para os anexos da tramitação
    (ou conjunto). Aditivo, dedupe por nome; marca padrao_owner p/ remoção seletiva."""
    padrao = config_service.anexos_padrao(kind, owner_id, owner_slug)
    # O comunicado/parágrafo pode ser GERAL (slug=None) mesmo aplicado numa caixa:
    # se não achou no slug informado, tenta os gerais.
    if not padrao and config_service.owner_padrao(kind, owner_id, owner_slug) is None:
        padrao = config_service.anexos_padrao(kind, owner_id, None)
    if not padrao:
        return {"ok": True, "incluidos": [], "anexos": []}
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    alvo, anexos = _anexos_do_alvo(box, tipo, id_tramite)
    if alvo is None:
        return {"ok": False, "erro": "tramitação/conjunto não encontrado"}
    incluidos = _copiar_anexos_padrao_para(anexos, paj_norm, tipo, id_tramite, kind, owner_id, padrao)
    _gravar_box(slug, box)
    return {"ok": True, "incluidos": incluidos, "anexos": anexos}


def remover_anexos_padrao_do_alvo(slug: str, paj_norm: str, tipo: str,
                                  id_tramite: str, owner_id: str) -> dict:
    """Remove da tramitação os anexos que foram incluídos por um dado texto/parágrafo
    padrão (marcados com padrao_owner). Usado quando o usuário desfaz a aplicação."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    alvo, anexos = _anexos_do_alvo(box, tipo, id_tramite)
    if alvo is None:
        return {"ok": False, "erro": "tramitação/conjunto não encontrado"}
    dest = _anexos_dir(paj_norm, _chave_anexo(tipo, id_tramite))
    restantes, removidos = [], []
    for a in anexos:
        if a.get("padrao_owner") == owner_id:
            with contextlib.suppress(Exception):
                (dest / a.get("arquivo", "")).unlink()
            removidos.append(a.get("nome"))
        else:
            restantes.append(a)
    alvo["anexos"] = restantes
    _gravar_box(slug, box)
    return {"ok": True, "removidos": removidos, "anexos": restantes}


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


# Submenus de "Prontos para Envio" (1 por MEIO): (slug_url, chave_do_meio, rótulo).
# App/E-mail = envio em lote; WhatsApp = assistido por linha; Telefone/Carta = a definir.
PRONTOS_MEIOS = [
    ("app", "app_sisdpu", "Aplicativo SISDPU"),
    ("whatsapp", "whatsapp", "WhatsApp"),
    ("email", "email", "E-mail"),
    ("telefone", "telefone", "Telefone"),
    ("carta", "carta", "Carta"),
]
_PRONTOS_KEY = {slug: key for slug, key, _l in PRONTOS_MEIOS}
_PRONTOS_LABEL = {slug: lab for slug, _k, lab in PRONTOS_MEIOS}


def prontos_por_meio() -> dict:
    """Contagens para os submenus de Prontos: {slug_meio: n, 'total': n_distinto}.
    UMA passada em listar_prontos_envio (item com 2 meios conta nos 2 submenus)."""
    prontos = listar_prontos_envio()
    d = {slug: sum(1 for p in prontos if key in (p.get("envio_meios") or []))
         for slug, key, _l in PRONTOS_MEIOS}
    d["total"] = len(prontos)
    return d


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
    # Endereço postável? Exige ao menos um endereço com CEP (chave do fluxo dos Correios).
    # Sem endereço, a CARTA fica indisponível (gravar por carta / expedir correspondência).
    d["tem_endereco"] = any((e.get("cep") or "").strip() for e in d["enderecos"])
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
    # colapsa parágrafos vazios (sem linha em branco entre as linhas) — vale também
    # p/ análises antigas em cache que ainda tinham \n\n gravado.
    d["resumo_ia"] = colapsar_paragrafos(sol.get("resumo_ia", ""))
    # A classificação MANUAL (via dropdown de texto padrão) prevalece sobre a da IA.
    d["sugestao_tipo"] = sol.get("sugestao_tipo_editada") or sol.get("sugestao_tipo", "")
    d["sugestao_editada"] = sol.get("sugestao_editada", "")
    d["sugestao_atual"] = colapsar_paragrafos(sol.get("sugestao_editada") or sol.get("sugestao_ia") or "")
    # aviso de dados padrão não identificados → destacar em vermelho (conferência manual)
    d["sugestao_alerta"] = analise_service.precisa_conferencia_manual(d["sugestao_atual"])
    # fila de envio por tramitação
    envio = sol.get("envio") or {}
    d["envio_status"] = envio.get("status", "")
    d["envio_mensagem"] = envio.get("mensagem", "")
    d["envio_meios"] = envio.get("meios", []) or []
    d["envio_somente_leitura"] = bool(envio.get("somente_leitura"))
    d["envio_tramitar_grupo"] = envio.get("tramitar_grupo", "") or ""
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
    d["reenvio"] = sol.get("reenvio") or None  # selo de reenvio (origem no histórico)
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
    d["alerta_fora_caixa"] = sol.get("alerta_fora_caixa")  # selo "conferência manual (possível remoção)"
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
    # Gravado para envio, já enviado, OU dentro do fluxo de ligações/correspondência
    # → não é mais "pendente" na caixa (passou a viver na central correspondente).
    if (s.get("envio") or {}).get("status") == "gravado" or s.get("enviados"):
        return False
    if (s.get("ligacao") or {}).get("status"):
        return False
    if (s.get("correspondencia") or {}).get("status"):
        return False
    if s.get("concluido_sem_contato"):  # concluído da caixa sem nenhum contato
        return False
    return True


def _sols_pendentes(box: dict) -> list[dict]:
    return [s for s in box.get("solicitacoes", []) if _sol_pendente(s)]


def _marcar_membros_conjunto(box: dict, ids: list[str], entry: dict) -> None:
    """Carimba CADA tramitação-membro de um conjunto com um registro de `enviados`,
    além do próprio objeto do conjunto. Sem isso, o estado "enviado" vive só no
    conjunto; se ele for desfeito depois (ex.: chegou tramitação nova → a triagem
    fica stale), as membros perderiam a marca e reviveriam na triagem mesmo já tendo
    sido contatadas. Com a marca por tramitação, `_sol_pendente` as exclui sempre."""
    ids = set(ids or [])
    for s in box.get("solicitacoes", []):
        if s.get("id_tramite") in ids:
            s.setdefault("enviados", []).append({**entry, "via_conjunto": True})


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
    d["reenvio"] = conj.get("reenvio") or None  # selo de reenvio (origem no histórico)
    d["id"] = prim.get("id", "")          # id interno do processo (p/ o Playwright abrir a tela)
    d["oficio"] = prim.get("oficio_origem", "")
    d["url_detalhe"] = prim.get("url_detalhe", "")
    d["remetente"] = prim.get("remetente", "")
    d["data_envio"] = prim.get("data_envio", "")
    d["data_envio_key"] = _data_key(prim.get("data_envio", ""))
    d["prazo"] = next((s.get("prazo") for s in sols if s.get("prazo")), "")
    d["datas_tramites"] = [s.get("data_envio", "") for s in sols]
    d["remetentes_tramites"] = [s.get("remetente", "") for s in sols]
    d["descricoes_tramites"] = [s.get("descricao_tramite", "") for s in sols]
    d["prazos_tramites"] = [s.get("prazo", "") for s in sols]  # prazo por tramitação (selo ao lado do remetente)
    d["resumo_ia"] = colapsar_paragrafos(conj.get("resumo_ia", ""))
    d["sugestao_tipo"] = conj.get("sugestao_tipo_editada", "")  # conjunto só tem classificação MANUAL
    d["sugestao_editada"] = conj.get("sugestao_editada", "")
    d["sugestao_atual"] = colapsar_paragrafos(conj.get("sugestao_editada") or conj.get("sugestao_ia") or "")
    # aviso de dados padrão não identificados → destacar em vermelho (conferência manual)
    d["sugestao_alerta"] = analise_service.precisa_conferencia_manual(d["sugestao_atual"])
    envio = conj.get("envio") or {}
    d["envio_status"] = envio.get("status", "")
    d["envio_mensagem"] = envio.get("mensagem", "")
    d["envio_meios"] = envio.get("meios", []) or []
    d["envio_somente_leitura"] = bool(envio.get("somente_leitura"))
    d["envio_tramitar_grupo"] = envio.get("tramitar_grupo", "") or ""
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
    d["alerta_fora_caixa"] = box.get("conjunto", {}).get("alerta_fora_caixa")  # selo conferência manual
    return d


def _tramites_info(t: dict, conjunto: bool) -> list[dict]:
    """Dados da(s) TRAMITAÇÃO(ÕES) ATENDIDA(S) no contato — guardados no registro do
    histórico para exibir na linha retrátil (card vermelho): data, remetente, prazo e
    descrição. Sempre uma LISTA (1 item na tramitação única; N no conjunto)."""
    if conjunto:
        datas = t.get("datas_tramites") or []
        rems = t.get("remetentes_tramites") or []
        praz = t.get("prazos_tramites") or []
        descs = t.get("descricoes_tramites") or []
        n = max(len(datas), len(rems), len(praz), len(descs), 0)
        return [{
            "data": datas[i] if i < len(datas) else "",
            "remetente": rems[i] if i < len(rems) else "",
            "prazo": praz[i] if i < len(praz) else "",
            "descricao": descs[i] if i < len(descs) else "",
        } for i in range(n)]
    return [{
        "data": t.get("data_envio", ""),
        "remetente": t.get("remetente", ""),
        "prazo": t.get("prazo", ""),
        "descricao": t.get("descricao_tramite", ""),
    }]


def montar_dashboard(slug: str) -> dict:
    """Classifica os itens da caixa em 3 grupos: triagem, pendentes e fila (gravados).
    Itens têm `tipo` = 'tramite' | 'conjunto' | 'triagem'. Itens JÁ ENVIADOS (e que
    ainda não saíram da caixa) não são exibidos — ficam só no Histórico de contatos."""
    triagem, pendentes, fila = [], [], []
    bdir = _box_dir(slug)
    if bdir.is_dir():
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                em_caixa = box.get("em_caixa", True)
                cont = ler_contato(box.get("paj_norm", ""))
                sols = box.get("solicitacoes", [])
                # tramitações gravadas para envio entram SEMPRE na fila — inclusive
                # quando o PAJ já saiu da caixa (em_caixa=False): é o caso do "Reenviar"
                # de um trâmite já concluído. (As já enviadas/não-pendentes ficam de fora.)
                for s in sols:
                    if (s.get("envio") or {}).get("status") == "gravado":
                        fila.append({**_enriquecer_tramite(box, s, cont), "tipo": "tramite"})
                # PAJ FORA da caixa: honramos só os itens gravados para envio (incl. o
                # conjunto, no reenvio de conjunto); nada de pendentes/triagem (esses só
                # valem enquanto o PAJ está na caixa). Com o PAJ ainda na caixa, o conjunto
                # é tratado no bloco de triagem abaixo — por isso o guard evita duplicar.
                if not em_caixa:
                    conj = box.get("conjunto") or {}
                    if conj and (conj.get("envio") or {}).get("status") == "gravado":
                        fila.append(_enriquecer_conjunto(box, cont))
                    continue
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
                    # conjunto encaminhado p/ ligação/correspondência (sem envio de mensagem)
                    # vive nas centrais — não reaparece em pendentes.
                    _conj_em_central = (bool((conj.get("ligacao") or {}).get("status")
                                             and not _lig_terminal((conj.get("ligacao") or {}).get("status", "")))
                                        or (conj.get("correspondencia") or {}).get("status") == "pendente")
                    if item["envio_status"] == "gravado":
                        fila.append(item)
                    elif not item["contato_enviado"] and not _conj_em_central:
                        pendentes.append(item)
                    # else: conjunto já enviado/encaminhado → não exibe (fica só no Histórico/centrais)
                elif decisao == "separado":
                    for s in pend:
                        pendentes.append({**_enriquecer_tramite(box, s, cont),
                                          "tipo": "tramite", "de_triagem_separado": True,
                                          "n_separados": len(pend)})
                else:
                    triagem.append(_item_triagem(box, pend, cont))
    triagem.sort(key=lambda d: d.get("paj", ""))
    pendentes.sort(key=lambda d: d.get("data_envio_key") or "99999999")
    # Urgentes (prazo 0) primeiro na fila; depois por ordem de gravação.
    fila.sort(key=lambda d: (0 if d.get("envio_urgente") else 1, d.get("envio_gravado_em", "")))
    return {"triagem": triagem, "pendentes": pendentes, "fila": fila}


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
    ia_on = analise_service.disponivel()  # Modo Simples: sem IA → decisão imediata, sem re-ler o SISDPU
    async with _triagem_ctx():  # roda em paralelo com outras triagens (até _TRIAGEM_MAX)
        if ia_on and id_proc:  # só re-lê o detalhamento quando a IA vai analisar
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
                    # anexo padrão do comunicado identificado → incluído automaticamente
                    _auto_incluir_anexos_padrao_por_tipo(paj_norm, s, comunicados)
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
                    somente_leitura: bool = False, prazos: dict | None = None,
                    tramitar_grupo: str | None = None) -> dict:
    """Grava o CONJUNTO de um PAJ na fila de envio. `meios` é cumulativo: além dos meios de
    mensagem (app/whatsapp), pode incluir 'telefone'/'carta', que encaminham o CONJUNTO para
    as centrais de Ligações / Correspondências (uma ligação/carta cobre todos os trâmites)."""
    from services import envio_service
    todos = meios or []
    # Encaminhamentos cumulativos do CONJUNTO.
    if "telefone" in todos:
        enfileirar_ligacao(slug, paj_norm, "", mensagem=mensagem, tipo="conjunto")
    if "carta" in todos and _paj_tem_endereco(paj_norm):
        encaminhar_correspondencia(slug, paj_norm, "", mensagem=mensagem, tipo="conjunto")
    msg_meios = [m for m in todos if m in envio_service.MEIOS]
    box = _ler_box(slug, paj_norm)  # re-lê após encaminhamentos
    if not box or not box.get("conjunto"):
        return box or {}
    if msg_meios:
        box["conjunto"]["envio"] = {
            "status": "gravado", "mensagem": colapsar_paragrafos(remover_emojis(mensagem)),  # só texto — sem emojis
            "meios": msg_meios, "somente_leitura": bool(somente_leitura),
            "tramitar_grupo": (tramitar_grupo or "").strip(),
            **_prazos_envio(prazos),
            "gravado_em": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        _gravar_box(slug, box)
    return box


def remover_conjunto_da_fila(slug: str, paj_norm: str, meio: str | None = None) -> dict:
    """Igual a remover_da_fila, mas para o CONJUNTO: remove só `meio` se houver outros
    pendentes; senão volta o conjunto a "pendente" (triagem)."""
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    box["conjunto"]["envio"] = _remover_meio_ou_zerar(box["conjunto"].get("envio"), meio)
    _gravar_box(slug, box)
    return box


def devolver_para_pendentes(slug: str, paj_norm: str,
                            id_tramites: list[str]) -> dict:
    """Devolve as tramitações GRAVADAS indicadas para 'pendentes' (limpa o envio).
    Como o PAJ passa a ter 2+ pendentes, elas caem naturalmente na TRIAGEM da
    caixa, onde o usuário decide tratar em conjunto ou separado. NÃO roda a IA
    agora (para não atrasar o trabalho). Usada pela remediação do conflito de
    App SISDPU (>=2 mensagens ao mesmo PAJ)."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado"}
    ids = [i for i in (id_tramites or []) if i]
    n = 0
    for s in box.get("solicitacoes", []):
        if s.get("id_tramite") in ids:
            s["envio"] = {}
            n += 1
    # zera a decisão de triagem anterior → o PAJ volta à triagem (sem IA).
    box.pop("triagem", None)
    box.pop("conjunto", None)
    _gravar_box(slug, box)
    return {"ok": True, "slug": slug, "paj_norm": paj_norm, "devolvidas": n}


def definir_sugestao_conjunto(slug: str, paj_norm: str, texto: str,
                              tipo: str | None = None) -> dict:
    box = _ler_box(slug, paj_norm)
    if not box or not box.get("conjunto"):
        return {}
    box["conjunto"]["sugestao_editada"] = colapsar_paragrafos(remover_emojis(texto))  # só texto — sem emojis
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


@contextlib.asynccontextmanager
async def _entrar_exclusivo_fila(timeout: float = 600.0):
    """Como o grupo exclusivo (_sync_lock), mas ENFILEIRA (aguarda a vez) em vez de
    recusar. `asyncio.Lock` acorda os que esperam em ordem FIFO → vira uma fila justa.
    Usado no envio INDIVIDUAL de WhatsApp: se já há um envio/sync em andamento, o novo
    espera e roda em seguida, em vez de falhar com 'operação em andamento'. Após pegar
    o lock, drena triagens em voo (novas não iniciam enquanto o lock está preso)."""
    await _sync_lock.acquire()
    try:
        inicio = time.monotonic()
        while _triagem_ativas > 0 and (time.monotonic() - inicio) < timeout:
            await asyncio.sleep(0.2)
        yield
    finally:
        _sync_lock.release()


# ---------- triagem como TAREFA DE FUNDO (sobrevive à navegação do usuário) ----------
# A análise da IA leva dezenas de segundos. Se rodasse dentro da requisição, sair da
# página abortaria a resposta e a tela não saberia do resultado. Por isso a triagem é
# disparada como task e o progresso fica num REGISTRO consultável (por caixa). O front
# mostra o spinner enquanto o job está "running" e recarrega quando ele some (concluído).
# Chave = "slug::paj_norm". Estado em memória (some no restart — mas o resultado já fica
# gravado no box; um job "running" órfão não sobrevive ao restart).
_triagem_jobs: dict[str, dict] = {}


def _job_key(slug: str, paj_norm: str) -> str:
    return f"{slug}::{paj_norm}"


async def _rodar_triagem_job(key: str, coro):
    """Executa a coroutine de triagem e atualiza o registro: remove ao concluir com
    sucesso (a tela recarrega e lê o box já atualizado) ou marca 'erro'."""
    try:
        res = await coro
        if res.get("ok"):
            _triagem_jobs.pop(key, None)
        else:
            j = _triagem_jobs.get(key, {})
            j.update({"status": "erro", "erro": res.get("erro", "falha")})
            _triagem_jobs[key] = j
    except Exception as e:  # noqa: BLE001
        j = _triagem_jobs.get(key, {})
        j.update({"status": "erro", "erro": str(e)})
        _triagem_jobs[key] = j


def iniciar_triagem(slug: str, paj_norm: str, kind: str,
                    mapeamento: dict | None = None) -> dict:
    """Dispara a triagem (conjunto/separado) em segundo plano e registra o job.
    Retorna imediatamente {ok, status}. Se já houver um job em andamento para o PAJ,
    não inicia outro."""
    key = _job_key(slug, paj_norm)
    atual = _triagem_jobs.get(key)
    if atual and atual.get("status") == "running":
        return {"ok": True, "status": "running"}
    if kind == "conjunto":
        coro = tratar_conjunto(slug, paj_norm)
    elif kind == "separado":
        coro = tratar_separado(slug, paj_norm, mapeamento or {})
    else:
        return {"ok": False, "erro": "tipo de triagem inválido"}
    _triagem_jobs[key] = {"status": "running", "kind": kind}
    # guarda a referência da task para não ser coletada pelo GC
    _triagem_jobs[key]["task"] = asyncio.create_task(_rodar_triagem_job(key, coro))
    return {"ok": True, "status": "running"}


def status_triagem(slug: str) -> dict:
    """Jobs de triagem de uma caixa: {paj_norm: {status, kind, erro}}. Jobs concluídos
    já não aparecem (foram removidos); o cliente interpreta a ausência como concluído."""
    prefixo = f"{slug}::"
    out: dict = {}
    for k, v in list(_triagem_jobs.items()):
        if k.startswith(prefixo):
            out[k[len(prefixo):]] = {"status": v.get("status", "running"),
                                     "kind": v.get("kind", ""), "erro": v.get("erro", "")}
    return out


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


def _evento_modo_ia() -> dict:
    """Linha inicial do popup de sincronização informando o modo em vigor:
    Modo IA (envia ao Claude CLI) × Modo Simples (não envia). Reflete o efetivo
    (`analise_service.disponivel()`) — se a IA está ligada mas o Claude não foi
    detectado, roda como Simples."""
    if analise_service.disponivel():
        return {"level": "info", "msg": "Modo IA (com envio ao Claude CLI)"}
    return {"level": "info", "msg": "Modo Simples (sem envio ao Claude CLI)"}


def _sep() -> dict:
    """Linha em branco (separador visual) no popup de sincronização."""
    return {"level": "sep", "msg": ""}


async def sincronizar_stream(slug: str, anunciar_modo: bool = True):
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
        if anunciar_modo:
            yield _evento_modo_ia()
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

        pajs_atuais: set[str] = set()
        trabalhos_ia: list[dict] = []  # análises coletadas na Fase 1, rodadas em paralelo na Fase 2
        if por_paj:
            yield _sep()  # separa o cabeçalho da lista de PAJs
        i = 0
        for paj_norm, itens_paj in por_paj.items():
            i += 1
            paj = itens_paj[0].get("paj", "")
            upsert_paj(slug, paj, itens_paj, em_caixa=True)
            pajs_atuais.add(paj_norm)
            novos_neste = [it for it in itens_paj if it.get("id_tramite") in novos_ids]

            assistido, det = "", {}
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

            etiqueta = "NOVO" if novos_neste else "atualizado"
            yield {"level": ("novo" if novos_neste else "muted"),
                   "msg": f"[{i}/{total_pajs}] {etiqueta} • PAJ {paj} — {assistido or 's/ nome'}"}

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
                    pass  # 2+ pendentes → análise vai para a triagem (não agenda IA aqui)
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
            yield _sep()  # separa a lista de PAJs da fase de análise
            conc = config_service.get_analise_concorrencia()
            paralelo = f"até {conc} análises por lote" if conc > 1 else "uma de cada vez (sequencial)"
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
                        if ana:
                            sol["resumo_ia"] = ana.get("resumo", "")
                            sol["sugestao_ia"] = ana.get("sugestao", "")
                            sol["sugestao_tipo"] = ana.get("tipo", "")
                            sol["resumo_ia_chave"] = chave
                            # anexo padrão do comunicado identificado → incluído automaticamente
                            _auto_incluir_anexos_padrao_por_tipo(job["paj_norm"], sol, comunicados)
                            mudou = True
                            logs.append({"level": "ok",
                                         "msg": f"     ✓ PAJ {job['paj']} · análise IA recebida"})
                        else:
                            logs.append({"level": "erro",
                                         "msg": f"     ✗ PAJ {job['paj']} · análise IA não recebida (ver manualmente)"})
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
            yield _sep()
            yield {"level": "info", "msg": f"{len(saidos)} PAJ(s) saíram da caixa: {', '.join(saidos)}"}

        dur = time.monotonic() - t0
        resumo = {"ok": True, "slug": slug, "grupo": nome_grupo,
                  "qtd_pajs": len(pajs_atuais), "qtd_solicitacoes": total_envios,
                  "novos": n_novos, "saidos": len(saidos), "duracao": round(dur, 1)}
        yield _sep()
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
    yield _evento_modo_ia()
    tot_pajs = tot_novos = tot_saidos = 0
    falhas = 0
    for idx, cx in enumerate(caixas, 1):
        if idx > 1:  # separa visualmente o bloco desta caixa do da caixa anterior
            yield _sep(); yield _sep()
        yield {"level": "novo", "msg": f"══ [{idx}/{len(caixas)}] {cx['nome']} ══"}
        async for ev in sincronizar_stream(cx["slug"], anunciar_modo=False):
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
            # anexo padrão do comunicado identificado → incluído automaticamente
            _auto_incluir_anexos_padrao_por_tipo(box.get("paj_norm", ""), sol, comunicados)
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
    # Reanálise tem o MESMO perfil das triagens (só IA, sem navegador, escreve apenas
    # o box do próprio PAJ) → roda em PARALELO com triagens/outras reanálises (até
    # _TRIAGEM_MAX), recusando somente enquanto uma op EXCLUSIVA (sync/envio) roda.
    if _ocupado_exclusivo():
        return {"ok": False, "erro": "Há uma sincronização/envio em andamento. Aguarde."}
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

    async with _triagem_ctx():  # paralelo com triagens/reanálises (até _TRIAGEM_MAX)
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


async def _tramites_fora_da_caixa(ids: list[str]) -> list[str]:
    """Verifica (leve, no SISDPU) quais dos `id_tramite` originários NÃO estão mais na
    caixa de grupo — provável conclusão/remoção por outro usuário. Best-effort: em erro
    de checagem retorna [] (não bloqueia o envio)."""
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        return []
    try:
        pres = await sisdpu_client.checar_tramites_na_caixa(ids)
    except Exception:  # noqa: BLE001
        return []
    return [i for i in ids if not pres.get(i, True)]


_ALERTA_FORA_CAIXA_DET = (
    "A tramitação saiu da caixa — provavelmente já foi tratada/concluída por outro "
    "usuário. Envio cancelado; confira manualmente (possível remoção)."
)

# Aviso (NÃO bloqueia) quando o trâmite saiu da caixa por AÇÃO EXTERNA (possível
# conclusão por outro usuário) e NÃO há registro NOSSO de conclusão para ele. O envio
# por meio(s) adicional(is) segue mesmo assim (decisão do usuário: multi-meio livre).
_ALERTA_FORA_CAIXA_EXTERNO = (
    "⚠ Atenção: o trâmite não está mais na caixa de grupo (possível conclusão por outro "
    "usuário). Enviando mesmo assim — confira manualmente se necessário."
)


def _marcar_alerta_fora_caixa(slug: str, paj_norm: str, tipo: str, id_tramite: str) -> None:
    """Marca a tramitação/conjunto com 'alerta_fora_caixa' (selo de conferência manual /
    possível remoção). NÃO é preservado por _solic_de_item → some sozinho na próxima
    sincronização (que também remove o item se ele realmente saiu)."""
    box = _ler_box(slug, paj_norm)
    alvo = box.get("conjunto") if tipo == "conjunto" else _achar_solic(box, id_tramite)
    if alvo is None:
        return
    alvo["alerta_fora_caixa"] = {"em": _dt.datetime.now().isoformat(timespec="seconds"),
                                 "detalhe": _ALERTA_FORA_CAIXA_DET}
    _gravar_box(slug, box)


def _salvar_sessao_log(meio: str, log: list, falha: bool) -> None:
    """Salva (best-effort) UMA sessão de envio no log de disparos, a partir de um
    `log` interno — usado pelos meios SEM modal ao vivo (Ligações/Correspondências;
    WhatsApp/App são capturados no stream da rota). NUNCA propaga erro: o envio não
    pode quebrar por causa do log."""
    import contextlib
    with contextlib.suppress(Exception):
        from services import disparos_service
        linhas = disparos_service.linhas_de_log(log)
        if linhas:
            disparos_service.registrar_sessao(meio, linhas, bool(falha))


async def _enviar_item_stream(slug, caixa, t, modo, rotulo="", meio_unico=None):
    """Dispara UM item da fila (tramitação ou conjunto); registra no histórico global +
    no box e remove da fila os meios enviados. Se `meio_unico` for dado, envia SÓ aquele
    meio (os demais permanecem na fila). Yields eventos de log e, por último, o sentinela
    {"_status": <status>}. Deve rodar DENTRO do _sync_lock."""
    from services import envio_service, historico_service
    paj_norm = t["paj_norm"]
    conjunto = t.get("tipo") == "conjunto"
    ids = t.get("tramite_ids", []) if conjunto else [t.get("id_tramite", "")]
    # WhatsApp é ASSISTIDO (enviado 1-a-1 na aba WhatsApp), NÃO entra no lote autônomo.
    meios_todos = t.get("envio_meios", []) or []
    wa_presente = "whatsapp" in meios_todos
    if meio_unico:
        meios = [meio_unico] if (meio_unico in meios_todos and meio_unico != "whatsapp") else []
    else:
        meios = [m for m in meios_todos if m != "whatsapp"]
    mensagem = t.get("envio_mensagem", "")
    nome = t.get("assistido") or "s/ nome"
    etiqueta = (f"conjunto de {len(ids)} tramitações" if conjunto
                else f"tramitação {t.get('data_envio', '')}")
    if not meios:
        if wa_presente:
            yield {"level": "muted",
                   "msg": f"{rotulo}PAJ {t['paj']} ({nome}): só WhatsApp — enviar na aba WhatsApp. Pulado no lote."}
            yield {"_status": "pulado"}
        else:
            yield {"level": "erro",
                   "msg": f"{rotulo}PAJ {t['paj']} ({nome}): sem meio selecionado — mantido na fila."}
            yield {"_status": "falha"}
        return

    n_anx = len(t.get("anexos", []) or [])
    anx_txt = f" · 📎 {n_anx} anexo(s)" if n_anx else " · sem anexos"
    yield {"level": "novo",
           "msg": f"{rotulo}PAJ {t['paj']} — {etiqueta} — {nome} · "
                  f"{', '.join(envio_service.label_meio(m) for m in meios)}{anx_txt}"}

    # Verificação anti-duplicidade (só em modo REAL): a(s) tramitação(ões) originária(s)
    # ainda está(ão) na caixa? Se TODAS saíram (outro usuário já concluiu), NÃO envia —
    # A verificação NÃO bloqueia (multi-meio livre): se o trâmite saiu da caixa,
    # apenas informamos. Se fomos NÓS que concluímos (envio anterior por outro meio),
    # seguimos sem alarde; se saiu por AÇÃO EXTERNA (possível outro usuário), avisamos
    # e seguimos mesmo assim. EXCEÇÃO — REENVIO: dispensada (já concluído por definição).
    ids_orig = [i for i in ids if str(i).strip()]
    algum_real = any(config_service.get_envio_modo_meio(m) == "real" for m in meios)
    if ids_orig and algum_real and t.get("reenvio"):
        yield {"level": "muted",
               "msg": "     Verificação na caixa dispensada (reenvio — trâmite originário já concluído)."}
    elif ids_orig and algum_real:
        fora = await _tramites_fora_da_caixa(ids_orig)
        if fora and len(fora) == len(ids_orig):
            previa = _conclusao_previa(ids_orig)
            if previa:
                yield {"level": "aviso",
                       "msg": "     Verificação na caixa: trâmite já concluído por este painel ("
                              + _detalhe_ja_concluido(previa) + ") — seguindo com o envio."}
            else:
                yield {"level": "aviso", "msg": "     " + _ALERTA_FORA_CAIXA_EXTERNO}

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
        "tramites_info": _tramites_info(t, conjunto),
        "tipo": t.get("sugestao_tipo", ""),  # classificação do texto padrão
        "caixa_slug": slug, "caixa_nome": caixa["nome"], "grupo": caixa["grupo"],
        "mensagem": mensagem, "anexos": anexos_meta,
        "anexo_tipo": tipo_alvo, "anexo_ref": ("" if conjunto else ids[0]),
        "url_detalhe": t.get("url_detalhe", ""),  # p/ o PAJ virar link no Histórico
    }

    # UMA linha de histórico POR MEIO (status/prazo próprios + campos de baixa reservados).
    ok_meios, fail_meios, falhas, ok_ids = [], [], {}, []
    algum_real = False
    ultimo_ts = ""
    id_proc = t.get("id", "")
    tramitar_grupo = t.get("envio_tramitar_grupo", "") or ""
    app_mov_seq = None       # nº da auto-movimentação do App (p/ o registro do box)
    app_tram_caixa = ""      # caixa efetivamente tramitada (p/ o registro do box)
    for meio in meios:
        r = await envio_service.enviar(meio, ctx)
        st = r.get("status", "falha")
        detalhe = r.get("detalhe", st)
        # O App devolve "... · Mov N" no fim do detalhe; a mov. vira ETAPA dedicada abaixo.
        if meio == "app_sisdpu":
            detalhe = re.sub(r"\s*·\s*Mov\s+\S+\s*$", "", detalhe)
        # Linha de ENVIO enxuta para o App (sem nome/somente_leitura); demais meios: genérica.
        if meio == "app_sisdpu":
            p_app = prazo_por_meio.get("app_sisdpu", 5)
            prazo_lbl = "urgente" if p_app == 0 else f"prazo {p_app}d"
            if r.get("ok"):
                env_txt = (f"Notificação enviada com sucesso no aplicativo · {prazo_lbl}" if st == "enviado"
                           else f"Notificação enviada no aplicativo (simulado) · {prazo_lbl}")
                yield {"level": "ok", "msg": f"     ✓ {env_txt}"}
            else:
                yield {"level": "erro", "msg": f"     ✗ Notificação pelo aplicativo falhou: {detalhe}"}
        else:
            yield {"level": ("ok" if r.get("ok") else "erro"),
                   "msg": f"     {envio_service.label_meio(meio)}: {detalhe}"}
        if r.get("ok"):
            # Registra no Histórico SÓ o que foi efetivamente enviado (ou simulado).
            # Falhas NÃO são registradas — a falha já fica visível/acionável em Prontos.
            mov_seq = r.get("mov_seq") if meio == "app_sisdpu" else None
            # PIPELINE (só o App, e só em envio REAL): o App AUTO-MOVIMENTA o PAJ; se
            # foi escolhida uma caixa no "Gravar para envio", TRAMITAMOS logo em seguida,
            # referenciando essa movimentação. Tudo registrado no log passo-a-passo.
            log, pipe = [], {}
            _log_inicial(log, t, caixa.get("grupo", ""))  # sincronização + identificação
            if meio == "app_sisdpu":
                _log_passo(log, "Notificação no aplicativo", True, env_txt)
                if st == "enviado":
                    ok_mov = mov_seq is not None
                    mov_det = (f'criada automaticamente após notificação · "mov. {mov_seq}"' if ok_mov
                               else "o App não retornou o número da movimentação")
                    _log_passo(log, "Movimentação", ok_mov, mov_det)
                    yield _evento_passo("Movimentação", ok_mov, mov_det)
                    pipe["mov_status"] = "movimentado" if ok_mov else "desconhecido"
                    if ok_mov and id_proc:
                        pipe["mov_url"] = f"{sisdpu_client.SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_proc}"
                        app_mov_seq = mov_seq
                    pode_concluir = False
                    if tramitar_grupo and ok_mov and id_proc:
                        _dt_data, _pd, prazo_dias = _prazo_tramitacao(p_app)
                        tramr = await _tramitar_referenciando_mov(
                            id_proc, t["paj"], tramitar_grupo, mov_seq, p_app, log)
                        pipe.update({
                            "tram_grupo": tramitar_grupo,
                            "tram_caixa": tramr["tram_caixa"], "tram_status": tramr["tram_status"],
                            "tram_detalhe": tramr["tram_detalhe"], "tram_url": tramr["tram_url"],
                            "tram_descricao": tramr["tram_descricao"],
                        })
                        if tramr["tram_status"] == "tramitado":
                            app_tram_caixa = tramr["tram_caixa"]
                            pode_concluir = True
                            yield _evento_passo("Tramitação do PAJ", True,
                                                f'realizada para a caixa "{tramitar_grupo}" · prazo {prazo_dias}d')
                        else:
                            yield _evento_passo("Tramitação do PAJ", False,
                                                f'falhou para a caixa "{tramitar_grupo}": {tramr["tram_detalhe"]}')
                    elif tramitar_grupo and not ok_mov:
                        pipe.update({"tram_grupo": tramitar_grupo, "tram_status": "nao_realizada",
                                     "tram_detalhe": "Tramitação cancelada: movimentação automática não confirmada."})
                        _log_passo(log, "Tramitação do PAJ", False, "cancelada (movimentação não confirmada)")
                        yield _evento_passo("Tramitação do PAJ", False, "cancelada (movimentação não confirmada)")
                    else:
                        pipe["tram_status"] = "nao_solicitada"
                        _log_passo(log, "Tramitação do PAJ", None, "não solicitada")
                        yield _evento_passo("Tramitação do PAJ", None, "não solicitada")
                        pode_concluir = ok_mov
                    # CONCLUSÃO do(s) trâmite(s) ORIGINÁRIO(S) sincronizado(s) — só quando a
                    # movimentação teve êxito E (tramitação OK ou não solicitada).
                    ids_orig = [x for x in ids if x]
                    previa = _conclusao_previa(ids_orig) if (pode_concluir and ids_orig) else None
                    if previa:
                        det = _detalhe_ja_concluido(previa)
                        pipe["concluido_status"] = "ja_concluido"
                        pipe["concluido_detalhe"] = det
                        _log_passo(log, "Conclusão do trâmite originário", True, det + " — etapa pulada")
                        yield _evento_passo("Conclusão do trâmite originário", True, det + " — etapa pulada")
                    elif pode_concluir and ids_orig:
                        yield {"level": "muted",
                               "msg": f"     admitindo e concluindo {len(ids_orig)} trâmite(s) originário(s)…"}
                        try:
                            conc = await sisdpu_client.concluir_paj(ids_orig)
                        except Exception as e:  # noqa: BLE001
                            conc = {"ok": False, "detalhe": f"exceção: {e}"}
                        adm_ok, adm_det, conc_ok, conc_det = _resumo_conclusao(conc)
                        _log_passo(log, "Admissão na caixa de grupo", adm_ok, adm_det)
                        yield _evento_passo("Admissão na caixa de grupo", adm_ok, adm_det)
                        _log_passo(log, "Conclusão na caixa de entrada", conc_ok, conc_det)
                        yield _evento_passo("Conclusão na caixa de entrada", conc_ok, conc_det)
                        pipe["concluido_status"] = "concluido" if conc_ok else "falha"
                        pipe["concluido_detalhe"] = conc_det
                    else:
                        motivo = ("não realizada (tramitação não teve êxito)" if not pode_concluir
                                  else "sem trâmite originário identificado")
                        pipe["concluido_status"] = "nao_realizada"
                        pipe["concluido_detalhe"] = motivo
                        _log_passo(log, "Conclusão do trâmite originário", None, motivo)
                        yield _evento_passo("Conclusão do trâmite originário", None, motivo)
                else:  # simulado
                    pipe["mov_status"] = "nao_aplicavel"
                    pipe["tram_status"] = "nao_solicitada"
                    pipe["concluido_status"] = "nao_aplicavel"
                    _log_passo(log, "Movimentação/Tramitação/Conclusão", None, "modo simulado — não executadas")
                    yield _evento_passo("Movimentação/Tramitação/Conclusão", None, "modo simulado — não executadas")
            reg = historico_service.registrar({
                **base_reg,
                "meio": meio, "meios": [meio],
                "prazo": prazo_por_meio.get(meio, 5),
                "somente_leitura": bool(t.get("envio_somente_leitura")) if meio == "app_sisdpu" else False,
                # nº da movimentação criada no PAJ pelo App (auto-movimenta) — vira "Mov N"
                # na coluna Comprovante do histórico. Só o App devolve mov_seq.
                "mov_seq": mov_seq,
                "id_processo": id_proc,         # p/ "refazer" tramitação
                "log": log,
                # status "reenviado" (laranja) quando o envio veio do botão Reenviar
                "status": ("reenviado" if (t.get("reenvio") and st in ("enviado", "simulado")) else st),
                "detalhe": detalhe, "reenvio": t.get("reenvio") or None,
                # reservados p/ controle futuro de resposta/baixa (por meio):
                "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
                **pipe,
            })
            ultimo_ts = reg["ts"]
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
                         "mov_seq": app_mov_seq, "tram_caixa": app_tram_caixa,
                         "somente_leitura": bool(t.get("envio_somente_leitura")) if "app_sisdpu" in ok_meios else False}
                if conjunto:
                    entry["tramite_ids"] = ids
                alvo.setdefault("enviados", []).append(entry)
                # Carimba também cada tramitação-membro (sobrevive ao desfazer do
                # conjunto — impede reviver tramitação já contatada em conjunto).
                if conjunto:
                    _marcar_membros_conjunto(box, ids, entry)
            # Meios NÃO processados agora (envio por meio único) permanecem na fila,
            # junto com os que FALHARAM e o WhatsApp (sempre assistido).
            nao_proc = [m for m in meios_todos if m != "whatsapp" and m not in meios]
            restantes = nao_proc + fail_meios + (["whatsapp"] if wa_presente else [])
            if restantes:
                envio = alvo.get("envio") or {}
                envio["meios"] = restantes
                # preserva falhas dos meios não processados + registra as novas falhas
                prev_falhas = {m: dd for m, dd in (envio.get("falhas") or {}).items() if m in nao_proc}
                prev_falhas.update(falhas)
                if prev_falhas:
                    envio["falhas"] = prev_falhas   # {meio: detalhe} — marcação da falha
                    envio["falha_em"] = ultimo_ts or envio.get("falha_em", "")
                else:
                    envio.pop("falhas", None)
                alvo["envio"] = envio
            else:
                alvo["envio"] = {}                # tudo OK → sai da fila
        _gravar_box(slug, box)
    yield {"_status": status}


def _usuario_sisdpu() -> str:
    """Usuário SISDPU (login) sob o qual o painel executa as etapas — registrado ao
    lado de cada passo do log. Todas as ações do painel correm por este usuário."""
    import os
    return os.environ.get("SISDPU_USERNAME", "") or "—"


def _log_passo(log: list, etapa: str, ok: bool | None, detalhe: str, ts: str | None = None) -> None:
    """Anexa um passo ao log individual do envio (usado na coluna LOG do histórico).
    Grava também o USUÁRIO SISDPU que executou o passo. `ts` permite datar um passo
    reconstruído (ex.: sincronização) com o horário real do evento."""
    log.append({
        "ts": ts or _dt.datetime.now().isoformat(timespec="seconds"),
        "usuario": _usuario_sisdpu(),
        "etapa": etapa,
        "ok": (None if ok is None else bool(ok)),
        "detalhe": str(detalhe or "")[:600],
    })


def _evento_passo(etapa: str, ok: bool | None, detalhe: str, prefixo: str = "     ",
                  nivel: str | None = None) -> dict:
    """Formata um passo como evento SSE (linha do popup de progresso). `nivel` força a
    cor da linha (ex.: 'aviso' = âmbar); por padrão deriva de `ok`."""
    icone = "✓" if ok else ("✗" if ok is False else "•")
    niv = nivel or ("ok" if ok else ("erro" if ok is False else "muted"))
    txt = f"{prefixo}{icone} {etapa}" + (f": {detalhe}" if detalhe else "")
    return {"level": niv, "msg": txt}


def _passo(log: list, emit, etapa: str, ok: bool | None, detalhe: str, nivel: str | None = None) -> None:
    """Registra o passo no log do histórico E, se houver `emit`, transmite ao popup
    em tempo real. `emit(evento_dict)` é fornecido pelos fluxos em streaming. `nivel`
    força a cor da linha no popup (ex.: 'aviso' = âmbar)."""
    _log_passo(log, etapa, ok, detalhe)
    if emit is not None:
        emit(_evento_passo(etapa, ok, detalhe, nivel=nivel))


async def _drenar_com_passos(coro_factory):
    """Executa uma coroutine que emite passos via `emit(evento)` numa fila interna e
    YIELDA os eventos em TEMPO REAL (para SSE). `coro_factory(emit)` deve devolver a
    coroutine a rodar. Ao final, yielda {"_result": <retorno da coroutine>}."""
    fila_ev: asyncio.Queue = asyncio.Queue()
    _FIM = object()

    def emit(evento: dict) -> None:
        fila_ev.put_nowait(evento)

    async def _run():
        try:
            return await coro_factory(emit)
        finally:
            fila_ev.put_nowait(_FIM)

    tarefa = asyncio.create_task(_run())
    while True:
        ev = await fila_ev.get()
        if ev is _FIM:
            break
        yield ev
    yield {"_result": await tarefa}


def _montar_descricao_mov(meio_label: str, mensagem: str, numeros,
                          prazo: int | None, nomes: list[str], tramitar_grupo: str) -> str:
    """Monta a descrição HTML PADRÃO da movimentação (título/negrito, conteúdo em
    itálico, urgente em vermelho, anexos em bullets, comprovante + linha de tramitação).
    Usada no envio E no 'refazer' — garante que a movimentação refeita seja IDÊNTICA.
    `numeros` pode ser um número único (str) ou uma LISTA de números (WhatsApp para
    vários telefones do mesmo PAJ) — nesse caso todos são listados na descrição."""
    import html as _html
    e = _html.escape
    nums = [n for n in (numeros if isinstance(numeros, (list, tuple)) else [numeros]) if n]
    alvo = ", ".join(e(n) for n in nums) if nums else "—"
    rotulo_alvo = "enviada para os números" if len(nums) > 1 else "enviada para"
    n_comp = len(nums)
    n_anx = len(nomes or [])
    urgente = (prazo is None) or (int(prazo) <= 0)
    prazo_txt = f"{int(prazo)} dias" if (prazo is not None and int(prazo) > 0) else "—"
    urg_html = '<span style="color:#c00000;"><strong>Sim</strong></span>' if urgente else "Não"
    cabecalho = "<br>".join([
        "<strong>[Painel de Comunicações]</strong>",
        f"Mensagem {e(meio_label)} {rotulo_alvo}: <strong>{alvo}</strong>",
        f"Conteúdo: <em>{e(mensagem)}</em>",
        f"Prazo indicado: {e(prazo_txt)}",
        f"Urgente: {urg_html}",
        f"Anexos: {n_anx} {'item' if n_anx == 1 else 'itens'}",
    ])
    bullets = ("<ul>" + "".join(f"<li>{e(n)}</li>" for n in (nomes or [])) + "</ul>") if nomes else ""
    tram_linha = (f"PAJ tramitado para <strong>{e(tramitar_grupo)}</strong>"
                  if tramitar_grupo else "PAJ não tramitado")
    comp_linha = ("Comprovantes de envio em anexo (um por número)" if n_comp > 1
                  else "Comprovante de envio em anexo")
    return (f"<p>{cabecalho}</p>{bullets}"
            f"<p>{comp_linha}<br><br>{tram_linha}</p>")


def _descricao_tramitacao(mov_seq) -> str:
    """Descrição PADRÃO da tramitação feita pelo painel após a movimentação — referencia
    a movimentação gerada (mov. "X"). Usada no envio e no 'refazer' (mesmo texto)."""
    seq = str(mov_seq).strip() if mov_seq not in (None, "") else ""
    ref = f' (mov. "{seq}")' if seq else ""
    return f"Mensagem enviada pelo Painel de Comunicações{ref}"


def _dia_util(d, feriados: set) -> bool:
    """True se `d` (datetime.date) é dia útil: não é sábado/domingo (automático) nem um
    feriado/dia de recesso cadastrado em Configurações."""
    return d.weekday() < 5 and d not in feriados


def _proximo_dia_util(d, feriados: set):
    """Avança `d` (datetime.date) até o próximo dia útil, pulando fins de semana e os
    feriados cadastrados. Pedido da usuária Larissa (07/08/2026): a tramitação estava
    caindo em sábado/domingo; agora a data é sempre adiada para o 1º dia útil seguinte."""
    while not _dia_util(d, feriados):
        d += _dt.timedelta(days=1)
    return d


def _prazo_tramitacao(prazo_envio) -> tuple[str, str, int]:
    """Calcula a data de 'Nova conclusão em' da tramitação a partir do prazo (em
    dias) informado no envio. Decisão do usuário:
      - prazo > 0        -> hoje + prazo dias
      - urgente(0)       -> hoje + 1 dia
      - 'sem'/'sem_prazo'-> tramita SEM prazo de conclusão (campo em branco no SISDPU)
      - None/inválido    -> hoje + padrão configurável (config_service, 15 dias)
    Retorna (data 'DD/MM/AAAA' ou '' p/ sem prazo, descrição do prazo, dias|None). A data é
    relativa ao momento da tramitação (recalculada também no 'refazer')."""
    if str(prazo_envio).strip().lower() in ("sem", "sem_prazo"):
        return "", "Tramitado sem prazo de conclusão.", None
    try:
        p = int(prazo_envio)
    except (TypeError, ValueError):
        p = None
    if p is None:
        dias = config_service.get_prazo_tramitacao_padrao()
        desc = f"Aguardar retorno do assistido — prazo padrão de {dias} dias."
    elif p <= 0:
        dias = 1
        desc = "Aguardar retorno do assistido — contato urgente."
    else:
        dias = p
        desc = f"Aguardar retorno do assistido — prazo de {p} dia{'s' if p != 1 else ''}."
    calc = _dt.date.today() + _dt.timedelta(days=dias)
    feriados = config_service.get_feriados_datas()
    if not _dia_util(calc, feriados):
        calc = _proximo_dia_util(calc, feriados)
        desc += " (adiado para o próximo dia útil — cairia em fim de semana/feriado)"
    return calc.strftime("%d/%m/%Y"), desc, dias


async def _tramitar_referenciando_mov(id_proc: str, paj: str, tramitar_grupo: str,
                                      mov_seq, prazo, log: list, rotulo: str = "Tramitação do PAJ",
                                      emit=None, descricao: str | None = None) -> dict:
    """Tramita o PAJ para `tramitar_grupo`, referenciando a movimentação `mov_seq` e
    preenchendo o prazo de conclusão (deriva de `prazo`). Registra o passo em `log` e,
    se houver `emit`, transmite ao popup em tempo real.
    `descricao` (opcional): substitui a descrição PADRÃO (`_descricao_tramitacao`, que
    fala de "mensagem enviada" — correta para o envio original, mas não para outros
    usos, ex.: encaminhamento feito ao registrar uma resposta do assistido).
    Retorna dict com tram_status/tram_caixa/tram_detalhe/tram_url/tram_descricao/
    tram_id_tramite (id do trâmite recém-criado na caixa destino — capturado aqui,
    com 1 leitura leve da caixa, única vez; "" se não deu pra capturar, nesse caso
    quem for localizar depois cai de volta no batimento por PAJ).
    Usado tanto pela movimentação manual (WhatsApp) quanto pela auto-movimentação (App)."""
    res = {"tram_caixa": "", "tram_status": "pendente", "tram_detalhe": "",
           "tram_url": "", "tram_descricao": "", "tram_id_tramite": ""}
    tram_desc = descricao if descricao is not None else _descricao_tramitacao(mov_seq)
    res["tram_descricao"] = tram_desc
    prazo_data, prazo_desc, prazo_dias = _prazo_tramitacao(prazo)
    if emit is not None:
        emit({"level": "muted", "msg": f"     tramitando o PAJ → {tramitar_grupo}…"})
    try:
        tram = await sisdpu_client.tramitar_paj(
            [{"id": id_proc, "numero": paj}], grupo=tramitar_grupo,
            descricao=tram_desc, prazo_data=prazo_data, prazo_desc=prazo_desc)
    except Exception as e:  # noqa: BLE001
        tram = {"ok": False, "status": "falha", "detalhe": f"exceção: {e}"}
    res["tram_status"] = "tramitado" if tram.get("ok") else "falha"
    res["tram_caixa"] = tramitar_grupo if tram.get("ok") else ""
    res["tram_detalhe"] = tram.get("detalhe", "")
    if tram.get("ok"):
        res["tram_url"] = f"{sisdpu_client.SISDPU_URL}/pages/tramite/historicoTramitacao.xhtml?id={id_proc}&caixaEntrada=false"
        _prazo_txt = "sem prazo" if prazo_dias is None else f"prazo {prazo_dias}d"
        _passo(log, emit, rotulo, True,
               f'realizada para a caixa "{tramitar_grupo}" · {_prazo_txt}')
        # Captura o id_tramite EXATO do trâmite recém-criado, agora (leitura leve, 1x) —
        # evita, mais tarde, ter que "adivinhar" por PAJ se houver 2 trâmites do mesmo
        # PAJ na mesma caixa (ex.: reenvio antes da confirmação do primeiro).
        with contextlib.suppress(Exception):
            info_novo = await sisdpu_client.listar_caixa_grupo(tramitar_grupo, enriquecer=False)
            paj_norm_alvo = normalizar_paj(paj)
            achado_novo = next((it for it in (info_novo.get("itens") or [])
                                 if normalizar_paj(it.get("paj", "")) == paj_norm_alvo), None)
            if achado_novo and achado_novo.get("id_tramite"):
                res["tram_id_tramite"] = achado_novo["id_tramite"]
    else:
        _passo(log, emit, rotulo, False,
               f'falhou para a caixa "{tramitar_grupo}": {tram.get("detalhe", "")}')
    return res


def _ids_originarios(fonte: dict) -> list[str]:
    """Ids dos trâmites ORIGINÁRIOS (sincronizados na caixa de grupo, que deram origem
    ao envio) de um item da fila OU de um registro do histórico: `tramite_ids` (conjunto)
    ou, na falta, o `id_tramite` único."""
    ids = [str(i).strip() for i in (fonte.get("tramite_ids") or []) if str(i).strip()]
    if not ids:
        one = str(fonte.get("id_tramite") or "").strip()
        if one:
            ids = [one]
    return ids


def _log_inicial(log: list, t: dict, grupo: str) -> None:
    """Prepara o log com as etapas ANTERIORES ao envio, reconstruídas do item:
    (1) SINCRONIZAÇÃO — captação do(s) trâmite(s) na caixa de grupo;
    (2) IDENTIFICAÇÃO — ids, descrição(ões) e forma de tratamento (separado/conjunto).
    Assim o log da coluna LOG cobre TODO o fluxo, desde a sincronização."""
    ids = _ids_originarios(t)
    conjunto = bool(t.get("conjunto")) or len(ids) > 1
    ren = t.get("reenvio") or {}
    if ren:
        # REENVIO: o log abre pela ESCOLHA de reenviar (não pela sincronização).
        meio_orig = _MEIO_LABEL.get(ren.get("meio_orig", ""), ren.get("meio_orig") or "—")
        det = "Reenvio solicitado a partir do histórico"
        if ren.get("evento_data"):
            det += f" (envio original de {ren['evento_data']}, meio {meio_orig})"
        _log_passo(log, "Reenvio solicitado", None, det, ts=ren.get("em"))
    else:
        # timestamp real da sincronização, se o item guardou (senão, fica sem override)
        ts_sync = t.get("incluido_em") or t.get("sincronizado_em") or None
        _log_passo(log, "Sincronização", True,
                   f"Trâmite(s) captado(s) na caixa de grupo '{grupo}' durante a sincronização.",
                   ts=ts_sync)
    descs = t.get("descricoes_tramites") or ([t.get("descricao_tramite")] if t.get("descricao_tramite") else [])
    descs = [d for d in descs if d]
    desc_txt = " | ".join(descs) if descs else "(sem descrição)"
    _log_passo(log, "Identificação do(s) trâmite(s)", True,
               f"{len(ids)} trâmite(s) [{', '.join(ids) or '—'}] · tratamento "
               f"{'conjunto' if conjunto else 'separado'} · descrição: {desc_txt}")


def _resumo_conclusao(r: dict) -> tuple:
    """A partir do retorno de `concluir_paj`, resume as DUAS etapas: admissão (retirada
    da caixa de grupo) e conclusão (baixa na caixa do usuário).
    Retorna (adm_ok, adm_det, conc_ok, conc_det)."""
    resultados = r.get("resultados", {}) or {}
    n = len(resultados)
    n_adm = sum(1 for v in resultados.values() if v.get("admitido") is True)
    ja_fora = sum(1 for v in resultados.values() if v.get("admitido") is None)
    adm_ok = n > 0 and all(v.get("admitido") in (True, None) for v in resultados.values())
    conc_ok = bool(r.get("ok"))
    adm_det = f"{n_adm}/{n} admitido(s) (retirado(s) da caixa de grupo)"
    if ja_fora:
        adm_det += f"; {ja_fora} já fora do grupo"
    conc_det = r.get("detalhe", "")
    return adm_ok, adm_det, conc_ok, conc_det


_MEIO_LABEL = {"whatsapp": "WhatsApp", "app_sisdpu": "App SISDPU", "email": "e-mail"}


def _conclusao_previa(ids: list[str], excluir_rec_id: str = "") -> dict | None:
    """Procura no histórico um registro (≠ excluir_rec_id) que JÁ concluiu (status
    'concluido' ou 'ja_concluido') ao menos um dos trâmites ORIGINÁRIOS `ids`. Serve
    para NÃO tentar concluir de novo quando o MESMO trâmite já foi baixado por outro
    meio. Retorna {meio, ts, rec_id} do mais recente, ou None."""
    from services import historico_service
    ids_set = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if not ids_set:
        return None
    achado = None
    for rec in historico_service.listar():
        if excluir_rec_id and rec.get("id") == excluir_rec_id:
            continue
        if rec.get("concluido_status") not in ("concluido", "ja_concluido"):
            continue
        if ids_set & set(_ids_originarios(rec)):
            if achado is None or (rec.get("ts", "") > achado.get("ts", "")):
                achado = {"meio": rec.get("meio", ""), "ts": rec.get("ts", ""), "rec_id": rec.get("id", "")}
    return achado


def _detalhe_ja_concluido(previa: dict) -> str:
    meio = _MEIO_LABEL.get(previa.get("meio", ""), previa.get("meio") or "outro meio")
    quando = (previa.get("ts", "") or "")[:16].replace("T", " ")
    return f"já concluído pelo meio {meio}" + (f" em {quando}" if quando else "")


async def _concluir_originarios(ids: list[str], log: list, emit=None) -> dict:
    """ADMITE (retira da caixa de grupo) + CONCLUI (baixa na caixa do usuário) o(s)
    trâmite(s) ORIGINÁRIO(S) — os sincronizados que geraram o envio (NÃO a tramitação
    que o painel cria). Registra DUAS etapas (admissão + conclusão) no log/popup.
    Se o trâmite JÁ foi concluído por OUTRO meio, PULA e registra isso.
    Retorna {concluido_status, concluido_detalhe}."""
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        _passo(log, emit, "Conclusão do trâmite originário", None,
               "nenhum trâmite originário identificado (registro sem id_tramite)")
        return {"concluido_status": "nao_realizada", "concluido_detalhe": "sem id de trâmite originário"}
    previa = _conclusao_previa(ids)
    if previa:
        det = _detalhe_ja_concluido(previa)
        _passo(log, emit, "Conclusão do trâmite originário", True, det + " — etapa pulada")
        return {"concluido_status": "ja_concluido", "concluido_detalhe": det}
    if emit is not None:
        emit({"level": "muted", "msg": f"     admitindo e concluindo {len(ids)} trâmite(s) originário(s)…"})
    try:
        r = await sisdpu_client.concluir_paj(ids)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "status": "falha", "detalhe": f"exceção: {e}"}
    adm_ok, adm_det, conc_ok, conc_det = _resumo_conclusao(r)
    _passo(log, emit, "Admissão na caixa de grupo", adm_ok, adm_det)
    _passo(log, emit, "Conclusão na caixa de entrada", conc_ok, conc_det)
    return {"concluido_status": ("concluido" if conc_ok else "falha"),
            "concluido_detalhe": conc_det}


async def _movimentar_e_tramitar(t: dict, meio_label: str, mensagem: str,
                                  anexos_meta: list[dict], comprovantes: list[dict] | None,
                                  tramitar_grupo: str, log: list,
                                  numeros: list[str] | None = None, prazo: int | None = None,
                                  anexos_arquivos: list[dict] | None = None, emit=None) -> dict:
    """Após um envio bem-sucedido: (1) MOVIMENTA o PAJ documentando o envio (fase
    'Mensagem ao assistido', descrição por template) anexando à movimentação OS MESMOS
    ANEXOS da mensagem (`anexos_arquivos`, com caminho) MAIS os comprovantes (UM por
    número contatado — `comprovantes`=[{nome, caminho}]), todos como 'Correspondência';
    e (2) se `tramitar_grupo` foi escolhido E a movimentação teve êxito, TRAMITA o PAJ
    para essa caixa. `numeros` = os números efetivamente contatados (listados na
    descrição). Nunca levanta — captura tudo no `log`.
    Retorna {mov_seq, mov_status, mov_detalhe, tram_caixa, tram_status, tram_detalhe}."""
    from ingestao import sisdpu_client
    from ingestao.sisdpu_client import SISDPU_URL
    res = {"mov_seq": None, "mov_status": "falha", "mov_detalhe": "", "mov_url": "",
           "tram_caixa": "", "tram_status": ("nao_solicitada" if not tramitar_grupo else "pendente"),
           "tram_detalhe": "", "tram_url": "", "tram_descricao": "", "tram_id_tramite": "",
           "concluido_status": "nao_realizada", "concluido_detalhe": ""}
    id_proc = t.get("id") or ""
    paj = t.get("paj", "")
    if not id_proc:
        res["mov_detalhe"] = "PAJ sem id de processo — movimentação não realizada."
        _passo(log, emit, "Movimentação", False, res["mov_detalhe"])
        return res
    # Descrição PADRÃO da movimentação (mesma no envio e no "refazer").
    nomes = [a.get("nome", "") for a in (anexos_meta or []) if a.get("nome")]
    descricao = _montar_descricao_mov(meio_label, mensagem, numeros or [], prazo, nomes, tramitar_grupo)
    res["mov_descricao"] = descricao  # guardado no histórico p/ permitir "refazer" idêntico
    # Anexos da MOVIMENTAÇÃO = os MESMOS anexos da mensagem (documentos enviados ao
    # assistido) + o(s) comprovante(s) do envio (um por número contatado), todos como
    # "Correspondência".
    mov_anexos = []
    for a in (anexos_arquivos or []):
        cam = a.get("caminho") or ""
        if cam and Path(cam).exists():
            mov_anexos.append({"nome": a.get("nome") or Path(cam).name,
                               "caminho": str(cam), "tipo": a.get("tipo") or ""})
    for comp in (comprovantes or []):
        cam = comp.get("caminho") or ""
        if cam and Path(cam).exists():
            mov_anexos.append({"nome": comp.get("nome") or Path(cam).name,
                               "caminho": str(cam), "tipo": "pdf"})
    if emit is not None:
        emit({"level": "muted", "msg": "     movimentando o PAJ (documentando o envio)…"})
    try:
        mov = await sisdpu_client.movimentar_paj(
            [{"id": id_proc, "numero": paj}], descricao, anexos=mov_anexos, descricao_html=True)
    except Exception as e:  # noqa: BLE001
        mov = {"ok": False, "status": "falha", "detalhe": f"exceção: {e}", "mov_seq": None}
    res["mov_seq"] = mov.get("mov_seq")
    res["mov_status"] = "movimentado" if mov.get("ok") else "falha"
    res["mov_detalhe"] = mov.get("detalhe", "")
    if mov.get("ok"):
        res["mov_url"] = f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_proc}"
    _passo(log, emit, "Movimentação", mov.get("ok"),
           (f'registrada · "mov. {res["mov_seq"]}"' if mov.get("ok") else mov.get("detalhe", "")))
    if mov_anexos:
        _passo(log, emit, "Juntada de anexos à movimentação", mov.get("ok"),
               (f"{len(mov_anexos)} anexo(s) como Correspondência" if mov.get("ok")
                else "não anexado (movimentação falhou)"))
    if not mov.get("ok"):
        if tramitar_grupo:
            res["tram_status"] = "nao_realizada"
            res["tram_detalhe"] = "Tramitação cancelada: movimentação não teve êxito."
            _passo(log, emit, "Tramitação do PAJ", False, "cancelada (movimentação não teve êxito)")
        res["concluido_detalhe"] = "não realizada (movimentação não teve êxito)"
        _passo(log, emit, "Conclusão do trâmite originário", None, res["concluido_detalhe"])
        return res
    if tramitar_grupo:
        tramr = await _tramitar_referenciando_mov(
            id_proc, paj, tramitar_grupo, res["mov_seq"], prazo, log, emit=emit)
        res.update({k: tramr[k] for k in
                    ("tram_caixa", "tram_status", "tram_detalhe", "tram_url", "tram_descricao",
                     "tram_id_tramite")})
        pode_concluir = (tramr["tram_status"] == "tramitado")
    else:
        _passo(log, emit, "Tramitação do PAJ", None, "não solicitada")
        pode_concluir = True
    # CONCLUSÃO do(s) trâmite(s) ORIGINÁRIO(S): só quando a movimentação teve êxito E
    # (a tramitação teve êxito OU não foi solicitada).
    if pode_concluir:
        res.update(await _concluir_originarios(_ids_originarios(t), log, emit))
    else:
        res["concluido_detalhe"] = "não realizada (tramitação não teve êxito)"
        _passo(log, emit, "Conclusão do trâmite originário", None, res["concluido_detalhe"])
    return res


# ==================== LIGAÇÕES TELEFÔNICAS ====================
# Fluxo paralelo ao envio de mensagem: o servidor liga para o assistido (até 3
# tentativas). Cada tentativa gera uma movimentação no SISDPU:
#   - contato frutífero    -> fase "Atendimento de retorno" (descrição = texto do
#                             servidor + número), depois pergunta se tramita/conclui;
#   - tentativa infrutífera -> fase "Tentativa infrutífera de contato com o assistido".
# Todos os contatos registram o número ligado; cada ligação vira 1 registro no
# histórico (meio "telefone"). Após 3 infrutíferas, o PAJ pode ser encaminhado para
# CORRESPONDÊNCIA (expedição pelos Correios — fluxo futuro).
_FASE_ATENDIMENTO_RETORNO = "Atendimento de retorno"
_FASE_TENTATIVA_INFRUTIFERA = "Tentativa infrutífera de contato com o assistido"
# Fase da movimentação ao expedir uma carta pelos Correios (nome exato no SISDPU).
_FASE_CARTA_EXPEDIDA = "Ofício/Carta/Comunicação expedida"
LIGACOES_MAX_TENTATIVAS = 3


def _lig_terminal(status: str) -> bool:
    """Status que RETIRAM o trâmite da central de ligações (resolvido/encaminhado/cancelado)."""
    return status in ("resolvido_frutifero", "encaminhado_correspondencia", "cancelado")


def _tentativas_infrutiferas(lig: dict) -> int:
    return sum(1 for t in (lig.get("tentativas") or []) if t.get("resultado") == "infrutifero")


def _caixas_com_dados() -> list[dict]:
    """Todas as caixas que TÊM pasta de dados (habilitadas OU não). O fluxo de
    ligações/correspondências persiste mesmo se a caixa sair da sincronização.
    Meta (nome/grupo) via get_caixa; fallback = o próprio slug."""
    out: list[dict] = []
    if not DADOS_DIR.is_dir():
        return out
    for d in sorted(DADOS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cx = config_service.get_caixa(d.name) or {}
        out.append({"slug": d.name, "nome": cx.get("nome") or d.name,
                    "grupo": cx.get("grupo") or d.name})
    return out


def _alvo_contato(box: dict, id_tramite: str, tipo: str = "tramite"):
    """Resolve o ALVO de ligação/correspondência — a solicitação individual OU o conjunto —
    de forma unificada. Retorna (alvo|None, id_proc, originarios[list], paj_disp, sugestao)."""
    if tipo == "conjunto":
        conj = box.get("conjunto")
        if not conj:
            return None, "", [], "", ""
        ids = [str(i).strip() for i in (conj.get("tramite_ids") or []) if str(i).strip()]
        sols = [s for s in box.get("solicitacoes", []) if s.get("id_tramite") in set(ids)]
        prim = sols[0] if sols else {}
        return (conj, prim.get("id", ""), ids, box.get("paj", ""),
                conj.get("sugestao_editada") or conj.get("sugestao_ia") or "")
    s = _achar_solic(box, id_tramite)
    if not s:
        return None, "", [], "", ""
    return (s, s.get("id", ""), [id_tramite], box.get("paj", ""),
            s.get("sugestao_editada") or s.get("sugestao_ia") or "")


def enfileirar_ligacao(slug: str, paj_norm: str, id_tramite: str, mensagem: str = "",
                       tipo: str = "tramite") -> dict:
    """Marca um trâmite (ou CONJUNTO) para CONTATO TELEFÔNICO: entra em 'Ligações Pendentes'.
    `mensagem` = base do contato (registro pré-preenchido da movimentação). Idempotente —
    se já ativo, só atualiza a mensagem-base. NÃO mexe em correspondência (cumulativos)."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado na caixa."}
    alvo, _idp, _ids, _paj, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    msg = colapsar_paragrafos(remover_emojis(mensagem or ""))
    lig = alvo.get("ligacao") or {}
    if lig.get("status") and not _lig_terminal(lig.get("status")):
        if msg:
            lig["mensagem_base"] = msg
            alvo["ligacao"] = lig
            _gravar_box(slug, box)
        return {"ok": True, "ja": True}
    alvo["ligacao"] = {
        "status": "pendente",
        "enfileirado_em": _dt.datetime.now().isoformat(timespec="seconds"),
        "tramitar_grupo": "",
        "mensagem_base": msg,
        "tipo": tipo,
        "tentativas": [],
    }
    _gravar_box(slug, box)
    return {"ok": True}


def cancelar_ligacao(slug: str, paj_norm: str, id_tramite: str, tipo: str = "tramite") -> dict:
    """Remove a marcação de ligação — o trâmite/conjunto volta a ser 'pendente' na caixa."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    alvo, _idp, _ids, _paj, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    alvo["ligacao"] = {}
    _gravar_box(slug, box)
    return {"ok": True}


def _enriquecer_ligacao(box: dict, alvo: dict, cont: dict, caixa_meta: dict, tipo: str = "tramite") -> dict:
    d = _enriquecer_conjunto(box, cont) if tipo == "conjunto" else _enriquecer_tramite(box, alvo, cont)
    d["lig_tipo"] = tipo
    lig = alvo.get("ligacao") or {}
    d["caixa_slug"] = caixa_meta["slug"]
    d["caixa_nome"] = caixa_meta["nome"]
    d["grupo"] = caixa_meta["grupo"]
    d["lig_status"] = lig.get("status", "")
    tent = lig.get("tentativas") or []
    d["lig_tentativas"] = tent
    d["lig_n"] = len(tent)
    d["lig_n_infrutiferas"] = _tentativas_infrutiferas(lig)
    d["lig_tramitar_grupo"] = lig.get("tramitar_grupo", "")
    d["lig_mensagem_base"] = lig.get("mensagem_base", "")
    d["lig_enfileirado_em"] = lig.get("enfileirado_em", "")
    ult = tent[-1] if tent else {}
    d["lig_ultima_em"] = ult.get("em", "")
    d["lig_ultimo_numero"] = ult.get("numero", "")
    d["lig_ultimo_resultado"] = ult.get("resultado", "")
    d["lig_max"] = LIGACOES_MAX_TENTATIVAS
    d["lig_esgotado"] = d["lig_n_infrutiferas"] >= LIGACOES_MAX_TENTATIVAS
    return d


def listar_ligacoes() -> dict:
    """Central de Ligações (todas as caixas): {"pendentes":[...], "feitas":[...]}.
    Pendentes = sem nenhuma tentativa; Feitas = >=1 tentativa, ainda em aberto."""
    pend, feitas = [], []
    for c in _caixas_com_dados():
        bdir = _box_dir(c["slug"])
        if not bdir.is_dir():
            continue
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                cont = ler_contato(box.get("paj_norm", ""))
                for s in box.get("solicitacoes", []):
                    st = (s.get("ligacao") or {}).get("status", "")
                    if not st or _lig_terminal(st):
                        continue
                    item = _enriquecer_ligacao(box, s, cont, c, "tramite")
                    (pend if item["lig_n"] == 0 else feitas).append(item)
                conj = box.get("conjunto") or {}
                st_c = (conj.get("ligacao") or {}).get("status", "")
                if st_c and not _lig_terminal(st_c):
                    item = _enriquecer_ligacao(box, conj, cont, c, "conjunto")
                    (pend if item["lig_n"] == 0 else feitas).append(item)
    pend.sort(key=lambda d: d.get("lig_enfileirado_em", ""))
    feitas.sort(key=lambda d: d.get("lig_ultima_em", ""), reverse=True)
    return {"pendentes": pend, "feitas": feitas}


def contar_ligacoes() -> dict:
    """Contadores da central de Ligações + modo de envio do meio 'telefone'.
    `aguardando` = ligações que ainda precisam de um contato: pendentes (0 tentativas)
    + feitas em andamento (com tentativas, mas ainda não esgotadas). Não conta as
    esgotadas (3 tentativas — já aguardam decisão de correspondência)."""
    db = listar_ligacoes()
    em_andamento = sum(1 for it in db["feitas"] if not it.get("lig_esgotado"))
    return {"pendentes": len(db["pendentes"]), "feitas": len(db["feitas"]),
            "aguardando": len(db["pendentes"]) + em_andamento,
            "total": len(db["pendentes"]) + len(db["feitas"]),
            "modo": config_service.get_envio_modo_meio("telefone")}


async def registrar_ligacao(slug: str, paj_norm: str, id_tramite: str, numeros,
                            resultado: str, registro: str = "", tramitar_grupo: str = "",
                            concluir: bool = False, prazo=None, tipo: str = "tramite", emit=None) -> dict:
    """Registra UMA tentativa de ligação (trâmite ou CONJUNTO) e roda o pipeline no SISDPU:
      - frutífero:   movimenta (fase 'Atendimento de retorno', descrição = texto do
                     servidor + número) e, se pedido, tramita e/ou conclui;
      - infrutífero: movimenta (fase 'Tentativa infrutífera de contato com o assistido').
    `numeros` = lista dos números ligados nesta tentativa (aceita 1 ou vários). No conjunto,
    a conclusão abrange TODOS os trâmites originários. Grava a tentativa no alvo e 1 registro
    no histórico (meio 'telefone')."""
    from services import historico_service
    if isinstance(numeros, str):
        numeros = [numeros]
    numeros = [str(n).strip() for n in (numeros or []) if str(n).strip()]
    num_txt = ", ".join(numeros)
    resultado = (resultado or "").strip().lower()
    if resultado not in ("frutifero", "infrutifero"):
        return {"ok": False, "erro": "Resultado inválido."}
    if not numeros:
        return {"ok": False, "erro": "Informe ao menos um número para o qual foi ligado."}
    caixa = config_service.get_caixa(slug)
    box = _ler_box(slug, paj_norm)
    if not box or not caixa:
        return {"ok": False, "erro": "PAJ/caixa não encontrado."}
    alvo, id_proc, originarios, paj_disp, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    lig = alvo.get("ligacao") or {}
    if not lig.get("status"):
        return {"ok": False, "erro": "Este item não está na fila de ligações."}
    cont = ler_contato(paj_norm)
    d = _enriquecer_conjunto(box, cont) if tipo == "conjunto" else _enriquecer_tramite(box, alvo, cont)
    n_atual = len(lig.get("tentativas") or []) + 1
    frutifero = resultado == "frutifero"
    log: list = []
    _rot_num = "número" if len(numeros) == 1 else "números"
    _log_passo(log, "Ligação telefônica", True,
               f"Tentativa {n_atual} · {_rot_num} {num_txt} · "
               + ("contato frutífero" if frutifero else "tentativa infrutífera"))
    if frutifero:
        fase = _FASE_ATENDIMENTO_RETORNO
        texto = (registro or "").strip() or "Contato telefônico frutífero com o assistido."
        descricao = f"Contato telefônico realizado para o(s) {_rot_num}: {num_txt}.\n\n{texto}"
    else:
        fase = _FASE_TENTATIVA_INFRUTIFERA
        descricao = (f"[Painel de Comunicações] Tentativa {n_atual} de {LIGACOES_MAX_TENTATIVAS} "
                     f"de contato telefônico sem êxito.\n{_rot_num.capitalize()} ligado(s): {num_txt}.")
    # Modo de envio do meio "telefone" (Configurações). Em SIMULADO nada é feito no
    # SISDPU — a ligação é registrada só para testar o fluxo (transições/histórico).
    simulado = config_service.get_envio_modo_meio("telefone") == "simulado"
    tram_res = {"tram_caixa": "", "tram_status": "nao_solicitada", "tram_detalhe": "",
                "tram_url": "", "tram_descricao": ""}
    conc_res = {"concluido_status": "nao_realizada", "concluido_detalhe": ""}
    mov_seq = None
    mov_url = ""
    if simulado:
        # 1s) MOVIMENTAÇÃO simulada — não toca no SISDPU.
        _passo(log, emit, f"Movimentação (fase '{fase}')", None,
               "modo simulado — nenhuma movimentação real no SISDPU")
        mov_status_val = "simulado"
        hist_status = "simulado"
        if frutifero:
            if tramitar_grupo:
                tram_res.update({"tram_caixa": tramitar_grupo, "tram_status": "simulado"})
                _passo(log, emit, "Tramitação do PAJ", None,
                       f"modo simulado — não tramitado (destino: {tramitar_grupo})")
            else:
                _passo(log, emit, "Tramitação do PAJ", None, "não solicitada")
            if concluir:
                conc_res = {"concluido_status": "simulado", "concluido_detalhe": "modo simulado — não concluído"}
                _passo(log, emit, "Conclusão do trâmite originário", None, "modo simulado — não concluído")
            else:
                _passo(log, emit, "Conclusão do trâmite originário", None, "não solicitada")
    else:
        # 1) MOVIMENTAÇÃO real (fase específica; texto do servidor como descrição no frutífero)
        if emit is not None:
            emit({"level": "muted", "msg": "     movimentando o PAJ…"})
        mov = {"ok": False, "detalhe": "PAJ sem id de processo.", "mov_seq": None}
        if id_proc:
            try:
                mov = await sisdpu_client.movimentar_paj(
                    [{"id": id_proc, "numero": paj_disp}], descricao, fase=fase)
            except Exception as e:  # noqa: BLE001
                mov = {"ok": False, "detalhe": f"exceção: {e}", "mov_seq": None}
        mov_ok = bool(mov.get("ok"))
        mov_seq = mov.get("mov_seq")
        _passo(log, emit, f"Movimentação (fase '{fase}')", mov_ok,
               (f'registrada · "mov. {mov_seq}"' if mov_ok else mov.get("detalhe", "")))
        if not mov_ok:
            # Movimentação real falhou → NÃO conta a tentativa (o usuário corrige e refaz).
            _salvar_sessao_log("telefone", log, True)
            return {"ok": False, "erro": mov.get("detalhe", "Falha na movimentação.")}
        mov_url = f"{sisdpu_client.SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_proc}" if id_proc else ""
        mov_status_val = "movimentado"
        hist_status = "enviado"
        # 2) frutífero: tramitação/conclusão opcionais
        if frutifero:
            if tramitar_grupo:
                tram_res = await _tramitar_referenciando_mov(
                    id_proc, paj_disp, tramitar_grupo, mov_seq, prazo, log, emit=emit)
            else:
                _passo(log, emit, "Tramitação do PAJ", None, "não solicitada")
            tram_ok = (not tramitar_grupo) or (tram_res["tram_status"] == "tramitado")
            if concluir and tram_ok:
                conc_res = await _concluir_originarios(originarios, log, emit)
            elif concluir:
                conc_res = {"concluido_status": "nao_realizada",
                            "concluido_detalhe": "não concluída (tramitação não teve êxito)"}
                _passo(log, emit, "Conclusão do trâmite originário", None, conc_res["concluido_detalhe"])
            else:
                _passo(log, emit, "Conclusão do trâmite originário", None, "não solicitada")
    # 3) grava a tentativa no trâmite + atualiza o status da ligação
    tentativa = {
        "em": _dt.datetime.now().isoformat(timespec="seconds"),
        "numero": num_txt, "numeros": numeros, "resultado": resultado, "registro": (registro or "").strip(),
        "mov_seq": mov_seq, "mov_status": mov_status_val, "mov_url": mov_url,
        "tram_caixa": tram_res["tram_caixa"], "tram_status": tram_res["tram_status"],
        "concluido_status": conc_res["concluido_status"], "usuario": _usuario_sisdpu(),
        "simulado": simulado,
    }
    lig.setdefault("tentativas", []).append(tentativa)
    if tramitar_grupo:
        lig["tramitar_grupo"] = tramitar_grupo
    if frutifero:
        lig["status"] = "resolvido_frutifero"
    elif _tentativas_infrutiferas(lig) >= LIGACOES_MAX_TENTATIVAS:
        lig["status"] = "esgotado"
    else:
        lig["status"] = "em_andamento"
    alvo["ligacao"] = lig
    _gravar_box(slug, box)
    # 4) registro no HISTÓRICO (meio "telefone")
    historico_service.registrar({
        "paj_norm": paj_norm, "paj": paj_disp,
        "assistido": d.get("assistido", ""), "oficio": d.get("oficio", ""),
        "id_tramite": (originarios[0] if len(originarios) == 1 else ""),
        "tramite_ids": originarios, "conjunto": (tipo == "conjunto"),
        "tramite_data": d.get("data_envio", ""),
        "tramites_info": _tramites_info(d, tipo == "conjunto"),
        "caixa_slug": slug, "caixa_nome": caixa.get("nome", ""), "grupo": caixa.get("grupo", ""),
        "mensagem": (tentativa["registro"] if frutifero else
                     f"Tentativa {n_atual}/{LIGACOES_MAX_TENTATIVAS} de contato telefônico — sem êxito."),
        "meio": "telefone", "meios": ["telefone"],
        "url_detalhe": d.get("url_detalhe", ""), "id_processo": id_proc,
        "ligacao_numero": num_txt, "ligacao_numeros": numeros, "ligacao_resultado": resultado, "ligacao_tentativa": n_atual,
        "mov_seq": mov_seq, "mov_status": mov_status_val, "mov_url": mov_url,
        "mov_descricao": descricao,
        "tram_grupo": tramitar_grupo or "", "tram_caixa": tram_res["tram_caixa"],
        "tram_status": tram_res["tram_status"], "tram_detalhe": tram_res["tram_detalhe"],
        "tram_url": tram_res["tram_url"], "tram_descricao": tram_res["tram_descricao"],
        "concluido_status": conc_res["concluido_status"], "concluido_detalhe": conc_res["concluido_detalhe"],
        "status": hist_status,
        "detalhe": ("Ligação registrada (modo simulado)." if simulado else "Ligação registrada."),
        "log": log,
        "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
    })
    _salvar_sessao_log("telefone", log, hist_status not in ("enviado", "simulado"))
    return {"ok": True, "resultado": resultado, "tentativa": n_atual, "simulado": simulado,
            "status_ligacao": lig["status"], "mov_seq": mov_seq,
            "esgotado": lig["status"] == "esgotado", "detalhe": ""}


async def concluir_paj_sem_contato(slug: str, paj_norm: str, id_tramite: str,
                                   tipo: str = "tramite", tramitar_grupo: str = "",
                                   prazo=None, tram_descricao: str = "", emit=None) -> dict:
    """Conclui o(s) trâmite(s) ORIGINÁRIO(S) de um PAJ na caixa SEM nenhum contato com o
    assistido (PAJ já tratado / sem medida de contato necessária). Opcionalmente TRAMITA o
    PAJ para outra caixa (grupo/prazo/descrição escolhidos no modal). Marca o item como
    concluído (sai de "Pendentes", inclusive o conjunto) e grava 1 evento no Histórico de
    contatos (meio 'sem_contato'). Sempre real no SISDPU (admite + conclui o originário)."""
    from services import historico_service
    caixa = config_service.get_caixa(slug)
    box = _ler_box(slug, paj_norm)
    if not box or not caixa:
        return {"ok": False, "erro": "PAJ/caixa não encontrado."}
    alvo, id_proc, originarios, paj_disp, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    if not id_proc:
        return {"ok": False, "erro": "PAJ sem id de processo — não é possível concluir."}
    cont = ler_contato(paj_norm)
    d = _enriquecer_conjunto(box, cont) if tipo == "conjunto" else _enriquecer_tramite(box, alvo, cont)
    log: list = []
    _log_passo(log, "Conclusão sem contato", True,
               "PAJ concluído da caixa sem nenhum contato com o assistido pelo Painel de Comunicações.")
    tram_res = {"tram_caixa": "", "tram_status": "nao_solicitada", "tram_detalhe": "",
                "tram_url": "", "tram_descricao": ""}
    # 1) Tramitação OPCIONAL (grupo/prazo/descrição escolhidos no modal).
    if tramitar_grupo:
        prazo_data, prazo_desc, prazo_dias = _prazo_tramitacao(prazo)
        desc = (tram_descricao or "").strip() or \
            "PAJ concluído pelo Painel de Comunicações sem necessidade de contato com o assistido."
        tram_res["tram_descricao"] = desc
        if emit is not None:
            emit({"level": "muted", "msg": f"     tramitando o PAJ → {tramitar_grupo}…"})
        try:
            tram = await sisdpu_client.tramitar_paj(
                [{"id": id_proc, "numero": paj_disp}], grupo=tramitar_grupo,
                descricao=desc, prazo_data=prazo_data, prazo_desc=prazo_desc)
        except Exception as e:  # noqa: BLE001
            tram = {"ok": False, "detalhe": f"exceção: {e}"}
        if tram.get("ok"):
            _prazo_txt = "sem prazo" if prazo_dias is None else f"prazo {prazo_dias}d"
            tram_res.update({"tram_caixa": tramitar_grupo, "tram_status": "tramitado",
                             "tram_detalhe": tram.get("detalhe", ""),
                             "tram_url": f"{sisdpu_client.SISDPU_URL}/pages/tramite/historicoTramitacao.xhtml?id={id_proc}&caixaEntrada=false"})
            _passo(log, emit, "Tramitação do PAJ", True,
                   f'realizada para a caixa "{tramitar_grupo}" · {_prazo_txt}')
        else:
            tram_res.update({"tram_status": "falha", "tram_detalhe": tram.get("detalhe", "")})
            _passo(log, emit, "Tramitação do PAJ", False,
                   f'falhou para a caixa "{tramitar_grupo}": {tram.get("detalhe", "")}')
            _salvar_sessao_log("sem_contato", log, True)
            return {"ok": False, "erro": "Falha ao tramitar: " + tram.get("detalhe", "")}
    else:
        _passo(log, emit, "Tramitação do PAJ", None, "não solicitada")
    # 2) Conclusão do(s) trâmite(s) originário(s): admite (sai do grupo) + conclui (baixa).
    conc_res = await _concluir_originarios(originarios, log, emit)
    if conc_res.get("concluido_status") not in ("concluido", "ja_concluido"):
        _salvar_sessao_log("sem_contato", log, True)
        return {"ok": False, "erro": "Não foi possível concluir o trâmite: "
                + conc_res.get("concluido_detalhe", "")}
    # 3) Marca como concluído-sem-contato → sai de "Pendentes" (trâmite e/ou conjunto).
    marca = {"em": _dt.datetime.now().isoformat(timespec="seconds"),
             "usuario": _usuario_sisdpu(), "tram_grupo": tramitar_grupo or ""}
    if tipo == "conjunto":
        conj = box.get("conjunto") or {}
        conj["concluido_sem_contato"] = marca
        box["conjunto"] = conj
        ids_set = set(originarios)
        for s in box.get("solicitacoes", []):
            if s.get("id_tramite") in ids_set:
                s["concluido_sem_contato"] = marca
    else:
        alvo["concluido_sem_contato"] = marca
    _gravar_box(slug, box)
    # 4) Registro no Histórico de contatos (meio 'sem_contato').
    historico_service.registrar({
        "paj_norm": paj_norm, "paj": paj_disp,
        "assistido": d.get("assistido", ""), "oficio": d.get("oficio", ""),
        "id_tramite": (originarios[0] if len(originarios) == 1 else ""),
        "tramite_ids": originarios, "conjunto": (tipo == "conjunto"),
        "tramite_data": d.get("data_envio", ""),
        "tramites_info": _tramites_info(d, tipo == "conjunto"),
        "caixa_slug": slug, "caixa_nome": caixa.get("nome", ""), "grupo": caixa.get("grupo", ""),
        "mensagem": "PAJ concluído sem necessidade de contato com o assistido pelo Painel de Comunicações.",
        "meio": "sem_contato", "meios": ["sem_contato"],
        "url_detalhe": d.get("url_detalhe", ""), "id_processo": id_proc,
        "mov_seq": None, "mov_status": "nao_aplicavel", "mov_url": "",
        "tram_grupo": tramitar_grupo or "", "tram_caixa": tram_res["tram_caixa"],
        "tram_status": tram_res["tram_status"], "tram_detalhe": tram_res["tram_detalhe"],
        "tram_url": tram_res["tram_url"], "tram_descricao": tram_res["tram_descricao"],
        "concluido_status": conc_res["concluido_status"], "concluido_detalhe": conc_res["concluido_detalhe"],
        "status": "concluido",
        "detalhe": "Concluído da caixa sem nenhum contato.",
        "log": log,
        "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
    })
    _salvar_sessao_log("sem_contato", log, False)
    return {"ok": True, "concluido_status": conc_res["concluido_status"],
            "tram_status": tram_res["tram_status"]}


# ==================== CONFIRMAÇÕES DE LEITURA (WhatsApp) ====================
# Pedido da equipe (2026-10-28): mensagens por WhatsApp pedem "responda OK" para
# confirmar o recebimento; o PAJ é tramitado para uma caixa de aguardo (ex.: "01 -
# PREV_AGENDA/AGUARDAR"). Até aqui, a checagem das respostas e a baixa no SISDPU eram
# 100% manuais, todo dia. Esta central automatiza a LEITURA (WhatsApp) e deixa a
# DECISÃO final com o usuário (1 clique por item) — nunca movimenta/conclui sozinha.
def _persistir_verificacao(historico_id: str, item: dict) -> None:
    """Grava o resultado da última verificação (Situação/Resposta) no próprio registro
    do histórico — assim a central mostra o último estado conhecido mesmo depois de
    sair e voltar à página, sem precisar rodar "Verificar respostas" de novo (pedido
    do usuário, 2026-10-28). Sobrescrito no próximo "Verificar respostas"/"Atualizar
    agora"; não é persistência de uma ação real, só de leitura."""
    from services import historico_service
    historico_service.atualizar(historico_id, {
        "verif_em": _dt.datetime.now().isoformat(timespec="seconds"),
        "verif_pronto_para_concluir": bool(item.get("pronto_para_concluir")),
        "verif_tem_resposta": bool(item.get("tem_resposta")),
        "verif_texto_resposta": item.get("texto_resposta", ""),
        "verif_data_resposta": item.get("data_resposta", ""),
        "verif_confirmou": bool(item.get("confirmou")),
        "verif_status_leitura": item.get("status_leitura", ""),
        "verif_erro": item.get("erro", ""),
    })


def _dias_desde(ts: str) -> int | None:
    """Dias corridos entre `ts` (ISO, ex.: registro do envio) e hoje. None se inválido."""
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(ts)[:19])
    except ValueError:
        return None
    return (_dt.datetime.now().date() - dt.date()).days


def _numeros_candidatos(reg: dict) -> list[str]:
    """Números candidatos p/ reabrir a conversa do WhatsApp deste registro — prioriza
    os números CONFIRMADOS pelo próprio WhatsApp no momento do envio (`numeros_confirmados`
    — ground truth lido do painel "Dados do contato", disponível em envios novos). Para
    registros ANTIGOS (sem esse campo), reconstrói o mesmo formato a partir do número
    cadastrado: DDI 55 + DDD + número (`normalizar_numero` — a mesma conta já usada por
    `enviar_mensagem` para montar o link de envio, e cujo resultado já fica registrado
    no próprio log de cada item, ex. "WhatsApp p/ 5541999082652"). Cobre ~99% dos casos
    (o usuário confirmou, 2026-10-28, que o único diferencial do "número simplificado"
    é a ausência do DDI — DDD e número continuam os mesmos). Inclui também o número
    cadastrado cru por último, sem duplicar, como última rede de segurança."""
    from ingestao.whatsapp_client import normalizar_numero
    confirmados = [n for n in (reg.get("numeros_confirmados") or []) if n]
    registrados = [n for n in (reg.get("numeros") or []) if n]
    reconstruidos = [normalizar_numero(n) for n in registrados]
    vistos: set[str] = set()
    candidatos: list[str] = []
    for n in confirmados + reconstruidos + registrados:
        if n and n not in vistos:
            vistos.add(n)
            candidatos.append(n)
    return candidatos


def _candidatos_confirmacao():
    """Registros ainda pendentes de confirmação (WhatsApp, tramitado p/ caixa de
    aguardo, não confirmado/removido) — SEM aplicar corte de antiguidade. Base
    compartilhada por `listar_aguardando_confirmacao`/`contar_ocultos_por_cutoff`/
    `buscar_pendencia_por_paj`."""
    from services import historico_service
    for r in historico_service.listar():
        meios = r.get("meios") or ([r["meio"]] if r.get("meio") else [])
        if "whatsapp" not in meios:
            continue
        if r.get("tram_status") != "tramitado" or not r.get("tram_caixa"):
            continue
        if r.get("confirmacao_status") in ("confirmado", "removido"):
            continue
        yield r


def contar_ocultos_por_cutoff() -> int:
    """Quantos PAJs seriam candidatos a confirmação, mas estão ocultos pelo corte de
    antiguidade — só para avisar na tela, evitando a sensação de "sumiu sem explicação"."""
    from services import config_service
    cutoff = config_service.get_confirmacoes_cutoff_dias()
    if not cutoff:
        return 0
    n = 0
    for r in _candidatos_confirmacao():
        dias = _dias_desde(r.get("ts", ""))
        if dias is not None and dias > cutoff:
            n += 1
    return n


def listar_aguardando_confirmacao() -> list[dict]:
    """Registros do Histórico com WhatsApp enviado e TRAMITADO para uma caixa de aguardo
    — candidatos à confirmação de recebimento. ⚠️ `concluido_status` do registro NÃO serve
    de filtro aqui: ele descreve a conclusão do TRÂMITE ORIGINÁRIO (que acontece sempre,
    automaticamente, logo após qualquer envio bem-sucedido) — é um passo DIFERENTE da
    tramitação criada na caixa de aguardo, cujo estado não é rastreado localmente (só
    existe de fato na caixa de grupo do SISDPU; `verificar_confirmacoes_stream` confere
    ao vivo). Aqui só filtramos pelo campo próprio `confirmacao_status` (marcado por
    `confirmar_recebimento` quando o usuário já confirmou por esta central, ou por
    `remover_pendencia_confirmacao` quando o usuário tirou o item da lista sem confirmar).
    ⚠️ Corte de antiguidade (`config_service.get_confirmacoes_cutoff_dias`, padrão 10
    dias): PAJs cujo ÚLTIMO envio é mais antigo que o corte NÃO aparecem — residual
    pequeno, tratado manualmente como era antes desta central existir (evita que uma
    instalação em máquina com histórico acumulado despeje centenas de PAJs antigos já
    resolvidos por fora, com risco de duplicar ação no SISDPU). 0 = sem corte. A busca
    por PAJ (`buscar_pendencia_por_paj`) tem PRIORIDADE sobre esse corte (pedido do
    usuário, 2026-10-28) — é a forma de alcançar um PAJ antigo sem mexer na configuração."""
    from services import config_service
    cutoff = config_service.get_confirmacoes_cutoff_dias()
    out = []
    for r in _candidatos_confirmacao():
        # Selo "N dias" na Situação: antiguidade do envio (ts) e se o prazo (dias)
        # já decorreu — calculado a cada leitura, não é persistido no registro.
        dias = _dias_desde(r.get("ts", ""))
        if cutoff and dias is not None and dias > cutoff:
            continue
        try:
            prazo_dias = int(r.get("prazo") or 0)
        except (TypeError, ValueError):
            prazo_dias = 0
        r["dias_decorridos"] = dias
        r["prazo_expirado"] = bool(prazo_dias) and dias is not None and dias > prazo_dias
        out.append(r)
    return out


def _chave_busca_paj(s: str) -> str:
    """Normaliza para busca livre por PAJ: só dígitos/letras, maiúsculo — assim
    "2023/029-00839", "029-00839" e "00839" casam igual (substring)."""
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def buscar_pendencia_por_paj(query: str) -> list[dict]:
    """Busca candidatos a confirmação por PAJ (substring tolerante a barra/hífen/
    espaço) — IGNORA o corte de antiguidade: a busca tem PRIORIDADE sobre o corte
    (pedido do usuário, 2026-10-28), para achar um PAJ antigo sem mexer na
    configuração. Mais recentes primeiro."""
    q = _chave_busca_paj(query)
    if not q:
        return []
    out = []
    for r in _candidatos_confirmacao():
        if q not in _chave_busca_paj(r.get("paj", "")):
            continue
        dias = _dias_desde(r.get("ts", ""))
        try:
            prazo_dias = int(r.get("prazo") or 0)
        except (TypeError, ValueError):
            prazo_dias = 0
        r["dias_decorridos"] = dias
        r["prazo_expirado"] = bool(prazo_dias) and dias is not None and dias > prazo_dias
        out.append(r)
    out.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out


def listar_confirmadas(limite: int = 50) -> list[dict]:
    """Registros já CONFIRMADOS por esta central (Confirmar recebimento, individual ou
    em bloco) — mais recentes primeiro. Mostrados na seção "Confirmações registradas"."""
    from services import historico_service
    out = [r for r in historico_service.listar()
           if r.get("confirmacao_status") == "confirmado" and "whatsapp" in (r.get("meios") or [])]
    out.sort(key=lambda r: r.get("confirmacao_em") or "", reverse=True)
    return out[:limite]


def contar_confirmacoes() -> dict:
    """Contador para o selo do menu lateral."""
    return {"total": len(listar_aguardando_confirmacao())}


async def verificar_confirmacoes_stream(historico_ids: list[str] | None = None):
    """Varre as pendências de confirmação (ver `listar_aguardando_confirmacao`): resolve
    o id_tramite ATUAL de cada uma na caixa de grupo para onde foi tramitada (pode ter
    mudado desde o envio) e lê o WhatsApp de cada número contatado em busca de uma
    resposta — SOMENTE LEITURA, nenhuma ação é tomada aqui. GERA eventos SSE (log ao
    vivo) e, por fim, {"done": True, "resultado": {"itens": [...]}} para a central
    montar a lista com o botão "Confirmar recebimento" por item.

    `historico_ids` (opcional): restringe a varredura a ESSES ids só — usado quando o
    usuário tem uma busca por PAJ ativa na tela e clica em "Verificar respostas"
    (pedido do usuário, 2026-10-28: o botão deve agir só sobre o que está filtrado, não
    sobre todas as pendências). Quando informado, IGNORA o corte de antiguidade (mesma
    regra já aplicada à busca por PAJ — o usuário escolheu esses itens explicitamente)."""
    from ingestao import sisdpu_client, whatsapp_client
    from services import historico_service
    if historico_ids:
        ids_set = set(historico_ids)
        pendentes = [r for r in _candidatos_confirmacao() if r["id"] in ids_set]
    else:
        pendentes = listar_aguardando_confirmacao()
    if not pendentes:
        msg = ("Nenhum dos itens filtrados está mais pendente de confirmação."
               if historico_ids else "Nenhum envio por WhatsApp aguardando confirmação no momento.")
        yield {"level": "info", "msg": msg}
        yield {"done": True, "resultado": {"itens": []}}
        return
    yield {"level": "info", "msg": f"{len(pendentes)} envio(s) aguardando confirmação. Localizando as tramitações…"}
    # Resolve o id_tramite ATUAL por caixa de destino (agrupa p/ não reler a mesma
    # caixa de grupo várias vezes — ela pode ter vários PAJs pendentes).
    por_caixa: dict[str, list[dict]] = {}
    for r in pendentes:
        por_caixa.setdefault(r["tram_caixa"], []).append(r)
    mapa_id_tramite: dict[str, str] = {}  # historico_id -> id_tramite atual na caixa (ou "")
    # ⚠️ Só marcamos "não está mais na caixa" (autolimpeza) quando a caixa FOI LIDA COM
    # SUCESSO (sem exceção e "encontrado"=True) — senão qualquer falha de leitura (ex.:
    # cofre do SISDPU trancado) faria TODO MUNDO daquela caixa parecer "já concluído" e
    # sumir indevidamente da lista. Bug real encontrado e corrigido em 2026-10-28.
    caixas_ok: set[str] = set()
    for caixa, regs in por_caixa.items():
        yield {"level": "muted", "msg": f"     lendo a caixa de grupo \"{caixa}\"…"}
        try:
            info = await sisdpu_client.listar_caixa_grupo(caixa, enriquecer=False)
        except Exception as e:  # noqa: BLE001
            yield {"level": "erro", "msg": f"     falha ao ler \"{caixa}\": {e}"}
            info = {}
        if info.get("encontrado"):
            caixas_ok.add(caixa)
        else:
            yield {"level": "aviso", "msg": f"     caixa \"{caixa}\" não encontrada no SISDPU no momento "
                                            "— pendências dela não serão verificadas agora."}
        por_paj: dict[str, dict] = {}
        por_idt: dict[str, dict] = {}
        for it in (info.get("itens") or []):
            por_paj.setdefault(normalizar_paj(it.get("paj", "")), it)
            if it.get("id_tramite"):
                por_idt.setdefault(it["id_tramite"], it)
        for r in regs:
            # Prefere o id_tramite exato capturado no envio (sem ambiguidade); cai para
            # o batimento por PAJ só em registros antigos que não têm esse campo, ou se
            # o id capturado não aparecer mais na caixa (ex.: nome de campo mudou).
            idt_conhecido = r.get("tram_id_tramite") or ""
            achado = por_idt.get(idt_conhecido) if idt_conhecido else None
            if achado is None:
                achado = por_paj.get(r.get("paj_norm", ""))
            mapa_id_tramite[r["id"]] = (achado or {}).get("id_tramite", "")
    # Lê o WhatsApp de cada pendência (uma conversa por vez — perfil é de 1 processo só).
    itens: list[dict] = []
    for r in pendentes:
        numeros = _numeros_candidatos(r)
        numero = numeros[0] if numeros else ""
        id_tram = mapa_id_tramite.get(r["id"], "")
        caixa_lida_ok = r["tram_caixa"] in caixas_ok
        item = {"historico_id": r["id"], "paj": r.get("paj", ""), "paj_norm": r.get("paj_norm", ""),
                "assistido": r.get("assistido", ""), "numero": numero, "mov_seq": r.get("mov_seq"),
                "tram_caixa": r.get("tram_caixa", ""), "pronto_para_concluir": bool(id_tram),
                "tem_resposta": False, "texto_resposta": "", "data_resposta": "",
                "confirmou": False, "status_leitura": "", "erro": ""}
        if not id_tram and not caixa_lida_ok:
            # A leitura da CAIXA falhou (ex.: cofre trancado) — não sabemos o estado real.
            # NÃO autolimpa (não mexe no registro); o usuário tenta de novo depois.
            item["erro"] = f'Não foi possível verificar a caixa "{r.get("tram_caixa", "")}" agora — tente novamente.'
            yield {"level": "erro", "msg": f"     PAJ {item['paj']}: {item['erro']}"}
            _persistir_verificacao(r["id"], item)
            itens.append(item)
            continue
        if not id_tram:
            # A caixa FOI lida com sucesso e o PAJ genuinamente não está mais nela: já foi
            # concluído manualmente (fora desta central). Autolimpa (marca confirmado)
            # para não ficar reaparecendo nas próximas verificações — não é um erro.
            item["erro"] = "Já não está na caixa de aguardo — provavelmente já foi concluído manualmente."
            historico_service.atualizar(r["id"], {
                "confirmacao_status": "confirmado",
                "confirmacao_detalhe": "autolimpeza: trâmite não encontrado na caixa de aguardo (caixa lida com sucesso)"})
            yield {"level": "muted", "msg": f"     PAJ {item['paj']}: {item['erro']}"}
            _persistir_verificacao(r["id"], item)
            itens.append(item)
            continue
        if not numero and not r.get("paj", ""):
            item["erro"] = "Sem número de WhatsApp registrado neste envio."
            yield {"level": "aviso", "msg": f"     PAJ {item['paj']}: {item['erro']}"}
            _persistir_verificacao(r["id"], item)
            itens.append(item)
            continue
        # Sem número (ou número desatualizado): a leitura ainda tenta achar a conversa
        # pelo PAJ no texto da mensagem (Plano B — ver _abrir_conversa).
        yield {"level": "muted", "msg": f"     PAJ {item['paj']} — lendo o WhatsApp ({numero or 'sem número, buscando pelo PAJ'})…"}
        try:
            leitura = await whatsapp_client.ler_respostas_apos_envio(
                numero, paj_ref=r.get("paj", ""), numeros_registrados=numeros[1:])
        except Exception as e:  # noqa: BLE001
            leitura = {"ok": False, "detalhe": f"exceção: {e}"}
        if not leitura.get("ok"):
            item["erro"] = leitura.get("detalhe") or "falha ao ler o WhatsApp"
            yield {"level": "erro", "msg": f"     PAJ {item['paj']}: {item['erro']}"}
        else:
            item.update(tem_resposta=leitura.get("tem_resposta", False),
                       texto_resposta=leitura.get("texto_resposta", ""),
                       data_resposta=leitura.get("data_resposta", ""),
                       confirmou=leitura.get("confirmou", False),
                       status_leitura=leitura.get("status_leitura", ""))
            # Cacheia a conversa INTEIRA lida agora (mesma sessão do WhatsApp) — assim
            # "Abrir conversa" mostra na hora, sem precisar abrir outra sessão sempre
            # que o usuário só quer CONFERIR (pedido do usuário, 2026-10-28).
            historico_service.atualizar(r["id"], {
                "conversa_cache": leitura.get("mensagens") or [],
                "conversa_cache_em": _dt.datetime.now().isoformat(timespec="seconds"),
            })
            if leitura.get("metodo_abertura") == "busca_texto":
                yield {"level": "aviso", "msg": f"     PAJ {item['paj']}: o número cadastrado não abriu a "
                                                "conversa certa — achei pelo texto da mensagem (busca pelo "
                                                "PAJ). Confira/atualize o número cadastrado."}
            if leitura.get("confirmou"):
                yield {"level": "ok", "msg": f"     PAJ {item['paj']}: resposta com possível confirmação."}
            elif leitura.get("tem_resposta"):
                yield {"level": "muted", "msg": f"     PAJ {item['paj']}: há resposta, mas sem confirmação clara — confira."}
            else:
                yield {"level": "muted", "msg": f"     PAJ {item['paj']}: ainda sem resposta."}
        _persistir_verificacao(r["id"], item)
        itens.append(item)
    n_conf = sum(1 for it in itens if it.get("confirmou"))
    yield {"level": "ok", "msg": f"Verificação concluída: {len(itens)} item(ns) · {n_conf} com possível confirmação."}
    yield {"done": True, "resultado": {"itens": itens}}


async def confirmar_recebimento(historico_id: str, texto_resposta: str = "",
                                data_resposta: str = "", encaminhar_grupo: str = "",
                                encaminhar_prazo=None) -> dict:
    """Confirma o recebimento de UM item da central (ação REAL, sempre disparada por um
    clique do usuário — nunca automática): movimenta o PAJ na fase 'Juntada de
    documento' citando a mov do envio E a resposta do assistido (quando informada pelo
    chamador — vem da última "Verificar respostas"), e dá baixa (admite + conclui) na
    tramitação parada na caixa de aguardo. Reaproveita o mesmo pipeline mov+conclui já
    validado em `_concluir_originarios`.
    `data_resposta` no formato 'DD/MM/AAAA HH:MM' (só a hora entra na movimentação).
    `encaminhar_grupo` (opcional, coluna "Encaminhamento" da central, pedido do
    usuário 2026-10-28): se informado, depois da baixa na caixa de aguardo dar certo,
    faz uma NOVA tramitação do PAJ para essa caixa, com `encaminhar_prazo` (dias,
    mesma convenção de `_prazo_tramitacao`). Reaproveita `_tramitar_referenciando_mov`
    (mesmo pipeline já usado no envio original). Falha no encaminhamento NÃO desfaz
    nem marca como falha a confirmação em si (que já tinha dado certo) — vem reportada
    em `aviso` no retorno, para o usuário conferir manualmente.
    Se houver resposta, gera um PDF da conversa (mesmo formato do comprovante de envio)
    e anexa à movimentação — pedido do usuário, 2026-10-28."""
    from ingestao import sisdpu_client, whatsapp_client
    from services import historico_service
    reg = historico_service.obter(historico_id)
    if not reg:
        return {"ok": False, "erro": "Registro do histórico não encontrado."}
    if reg.get("confirmacao_status") == "confirmado":
        # ⚠️ Usa confirmacao_status (desta central), NÃO concluido_status: este último
        # descreve a conclusão do trâmite ORIGINÁRIO, que já acontece automaticamente em
        # todo envio por WhatsApp bem-sucedido — quase sempre "concluido" e não tem
        # relação com a tramitação da caixa de aguardo que esta função trata.
        return {"ok": False, "erro": "Este item já está concluído."}
    id_processo = reg.get("id_processo", "")
    paj_disp = reg.get("paj", "")
    tram_caixa = reg.get("tram_caixa", "")
    if not id_processo or not tram_caixa:
        return {"ok": False, "erro": "Registro sem id de processo ou caixa de destino — não é possível agir."}
    # Resolve o id_tramite ATUAL na caixa de aguardo (pode ter mudado desde a verificação).
    try:
        info = await sisdpu_client.listar_caixa_grupo(tram_caixa, enriquecer=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Falha ao ler a caixa \"{tram_caixa}\": {e}"}
    itens_caixa = info.get("itens") or []
    idt_conhecido = reg.get("tram_id_tramite") or ""
    achado = next((it for it in itens_caixa if it.get("id_tramite") == idt_conhecido), None) if idt_conhecido else None
    if achado is None:
        pn = reg.get("paj_norm", "")
        achado = next((it for it in itens_caixa if normalizar_paj(it.get("paj", "")) == pn), None)
    if not achado or not achado.get("id_tramite"):
        return {"ok": False, "erro": f'Trâmite não localizado na caixa "{tram_caixa}" — pode já ter '
                                     "sido concluído manualmente, ou a caixa mudou de nome."}
    id_tramite = achado["id_tramite"]
    log: list = list(reg.get("log") or [])
    mov_seq = reg.get("mov_seq")
    base = (f"Destinatário da mensagem da mov {mov_seq} deu ciente do recebimento"
            if mov_seq else "Destinatário deu ciente do recebimento da mensagem")
    # `texto_resposta` já vem com a hora ANTES de cada linha (ver whatsapp_client.
    # ler_respostas_apos_envio) — não prefixa de novo aqui.
    texto_resposta = (texto_resposta or "").strip()
    if texto_resposta:
        desc = f"{base}:\n{texto_resposta}"
    elif reg.get("verif_status_leitura") == "lida":
        # Sem resposta por TEXTO, mas o próprio WhatsApp confirma que a mensagem foi
        # lida (✓✓ azul) — efeito equivalente a "respondida" para fins de registro no
        # PAJ (pedido do usuário, 2026-10-28). Texto exato pedido pelo usuário.
        desc = "Confirmada leitura no WhatsApp, sem resposta expressa."
    else:
        desc = f"{base}."
    # Print da resposta em PDF (mesmo formato do comprovante de envio) — anexado à
    # movimentação, igual já fazemos com o comprovante quando a mensagem é enviada.
    # Best-effort: se não conseguir gerar, a movimentação segue sem o anexo.
    anexos_mov: list[dict] = []
    numeros_reg = _numeros_candidatos(reg)
    numero_resp = numeros_reg[0] if numeros_reg else ""
    if texto_resposta and (numero_resp or reg.get("paj", "")):
        try:
            pdf_resposta = await whatsapp_client.capturar_pdf_resposta(
                numero_resp, str(COMPROVANTES_WA_DIR), paj_ref=reg.get("paj", ""),
                numeros_registrados=numeros_reg[1:])
        except Exception:  # noqa: BLE001
            pdf_resposta = None
        if pdf_resposta:
            anexos_mov.append({"nome": Path(pdf_resposta).name, "caminho": pdf_resposta, "tipo": "pdf"})
            _log_passo(log, "Print da resposta (PDF)", True, Path(pdf_resposta).name)
        else:
            _log_passo(log, "Print da resposta (PDF)", False,
                      "não foi possível gerar — movimentação segue sem o anexo")
    try:
        mov = await sisdpu_client.movimentar_paj(
            [{"id": id_processo, "numero": paj_disp}], descricao=desc, fase="Juntada de documento",
            anexos=anexos_mov or None)
    except Exception as e:  # noqa: BLE001
        mov = {"ok": False, "detalhe": f"exceção: {e}"}
    mov_ok = bool(mov.get("ok"))
    _log_passo(log, "Movimentação (ciente do recebimento)", mov_ok,
               (f'"mov. {mov.get("mov_seq")}"' if mov_ok else mov.get("detalhe", "")))
    if not mov_ok:
        historico_service.atualizar(historico_id, {"log": log})
        return {"ok": False, "erro": "Falha ao movimentar: " + mov.get("detalhe", "")}
    # ⚠️ NÃO mexe em concluido_status/concluido_detalhe: esses campos descrevem a
    # conclusão do TRÂMITE ORIGINÁRIO (já aconteceu no envio) — um passo DIFERENTE
    # desta baixa da tramitação da caixa de aguardo, que usa campos próprios.
    conc_res = await _concluir_originarios([id_tramite], log)
    # Encaminhamento (nova tramitação, opcional): só depois da baixa dar certo — é
    # explicitamente um passo "a mais", nunca substitui nem bloqueia o fluxo normal.
    encaminhar_grupo = (encaminhar_grupo or "").strip()
    aviso_encaminhamento = ""
    if encaminhar_grupo and conc_res.get("concluido_status") in ("concluido", "ja_concluido"):
        enc_desc = (f'Encaminhado para nova análise após resposta do assistido na mov. '
                    f'"{mov.get("mov_seq")}".' if mov.get("mov_seq") else
                    "Encaminhado para nova análise após resposta do assistido.")
        try:
            enc_res = await _tramitar_referenciando_mov(
                id_processo, paj_disp, encaminhar_grupo, mov.get("mov_seq"), encaminhar_prazo,
                log, rotulo="Encaminhamento do PAJ", descricao=enc_desc)
        except Exception as e:  # noqa: BLE001
            enc_res = {"tram_status": "falha", "tram_detalhe": f"exceção: {e}"}
            _log_passo(log, "Encaminhamento do PAJ", False, enc_res["tram_detalhe"])
        if enc_res.get("tram_status") != "tramitado":
            aviso_encaminhamento = (f'Resposta registrada normalmente, mas o encaminhamento para '
                                    f'"{encaminhar_grupo}" falhou: {enc_res.get("tram_detalhe", "")}')
    campos = {"log": log, "confirmacao_mov_seq": mov.get("mov_seq"),
              "confirmacao_em": _dt.datetime.now().isoformat(timespec="seconds"),
              "confirmacao_usuario": _usuario_sisdpu(),
              "confirmacao_resposta": texto_resposta, "confirmacao_resposta_data": data_resposta}
    if conc_res.get("concluido_status") in ("concluido", "ja_concluido"):
        campos["confirmacao_status"] = "confirmado"
        campos["confirmacao_detalhe"] = conc_res.get("concluido_detalhe", "")
        historico_service.atualizar(historico_id, campos)
        return {"ok": True, "aviso": aviso_encaminhamento} if aviso_encaminhamento else {"ok": True}
    campos["confirmacao_detalhe"] = conc_res.get("concluido_detalhe", "")
    historico_service.atualizar(historico_id, campos)
    return {"ok": False, "erro": "Movimentação feita, mas falhou a conclusão: "
                                 + conc_res.get("concluido_detalhe", "")}


def remover_pendencia_confirmacao(historico_id: str) -> dict:
    """Tira um item da lista de pendentes de verificação SEM confirmar nada no SISDPU —
    ação local/instantânea (ex.: PAJ que o usuário já tratou por fora, ou que não se
    aplica mais). Usa o mesmo campo `confirmacao_status`, com valor próprio "removido"
    (nunca aparece em `listar_confirmadas`, que exige "confirmado")."""
    from services import historico_service
    reg = historico_service.obter(historico_id)
    if not reg:
        return {"ok": False, "erro": "Registro do histórico não encontrado."}
    if reg.get("confirmacao_status") in ("confirmado", "removido"):
        return {"ok": False, "erro": "Este item já não está mais pendente."}
    historico_service.atualizar(historico_id, {
        "confirmacao_status": "removido",
        "confirmacao_detalhe": "Removido manualmente da lista de pendentes (sem ação no SISDPU).",
        "confirmacao_em": _dt.datetime.now().isoformat(timespec="seconds"),
        "confirmacao_usuario": _usuario_sisdpu(),
    })
    return {"ok": True}


async def confirmar_recebimento_lote_stream(itens: list[dict]):
    """Confirma em bloco (1 clique) vários itens já verificados: cada `item` é
    {historico_id, texto_resposta, data_resposta} — a mesma informação que a tabela já
    mostra para cada um. Chama `confirmar_recebimento` em sequência, um de cada vez
    (cada confirmação é uma ação REAL própria no SISDPU), emitindo progresso via SSE."""
    from services import historico_service
    itens = [it for it in (itens or []) if it.get("historico_id")]
    if not itens:
        yield {"level": "aviso", "msg": "Nenhum item selecionado para confirmar em bloco."}
        yield {"done": True, "resultado": {"itens": []}}
        return
    yield {"level": "info", "msg": f"Confirmando {len(itens)} item(ns) em bloco…"}
    resultados: list[dict] = []
    for it in itens:
        hid = it["historico_id"]
        reg = historico_service.obter(hid) or {}
        paj = reg.get("paj", hid)
        yield {"level": "muted", "msg": f"     PAJ {paj}: confirmando…"}
        try:
            r = await confirmar_recebimento(
                hid, it.get("texto_resposta", ""), it.get("data_resposta", ""),
                it.get("encaminhar_grupo", ""), it.get("encaminhar_prazo"))
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "erro": f"exceção: {e}"}
        if r.get("ok"):
            yield {"level": "ok", "msg": f"     PAJ {paj}: confirmado."}
            if r.get("aviso"):
                yield {"level": "aviso", "msg": f"     PAJ {paj}: {r['aviso']}"}
        else:
            yield {"level": "erro", "msg": f"     PAJ {paj}: {r.get('erro', 'falha')}"}
        resultados.append({"historico_id": hid, "paj": paj, "ok": bool(r.get("ok")),
                            "erro": r.get("erro", ""), "aviso": r.get("aviso", "")})
    n_ok = sum(1 for r in resultados if r["ok"])
    yield {"level": "ok", "msg": f"Confirmação em bloco concluída: {n_ok}/{len(resultados)} com êxito."}
    yield {"done": True, "resultado": {"itens": resultados}}


async def verificar_uma_pendencia(historico_id: str) -> dict:
    """Reverifica UM item específico — mesma lógica/campos do item que
    `verificar_confirmacoes_stream` monta em lote (resolve o id_tramite atual na caixa
    de aguardo + lê o WhatsApp), mas para um só. Usado pelo "Atualizar agora" do
    celular simulado: assim o usuário não precisa rodar "Verificar respostas" em lote
    só para atualizar o PAJ que já está olhando. Retorna {ok, item, mensagens}."""
    from ingestao import sisdpu_client, whatsapp_client
    from services import historico_service
    reg = historico_service.obter(historico_id)
    if not reg:
        return {"ok": False, "erro": "Registro do histórico não encontrado."}
    numeros_reg = _numeros_candidatos(reg)
    numero = numeros_reg[0] if numeros_reg else ""
    tram_caixa = reg.get("tram_caixa", "")
    item = {"historico_id": historico_id, "paj": reg.get("paj", ""), "paj_norm": reg.get("paj_norm", ""),
            "assistido": reg.get("assistido", ""), "numero": numero, "mov_seq": reg.get("mov_seq"),
            "tram_caixa": tram_caixa, "pronto_para_concluir": False,
            "tem_resposta": False, "texto_resposta": "", "data_resposta": "",
            "confirmou": False, "status_leitura": "", "erro": ""}
    if not tram_caixa:
        item["erro"] = "Registro sem caixa de destino."
        _persistir_verificacao(historico_id, item)
        return {"ok": True, "item": item, "mensagens": reg.get("conversa_cache") or []}
    try:
        info = await sisdpu_client.listar_caixa_grupo(tram_caixa, enriquecer=False)
    except Exception:  # noqa: BLE001
        info = {}
    if not info.get("encontrado"):
        item["erro"] = f'Não foi possível verificar a caixa "{tram_caixa}" agora — tente novamente.'
        _persistir_verificacao(historico_id, item)
        return {"ok": True, "item": item, "mensagens": reg.get("conversa_cache") or []}
    itens_caixa = info.get("itens") or []
    idt_conhecido = reg.get("tram_id_tramite") or ""
    achado = next((it for it in itens_caixa if it.get("id_tramite") == idt_conhecido), None) if idt_conhecido else None
    if achado is None:
        achado = next((it for it in itens_caixa if normalizar_paj(it.get("paj", "")) == reg.get("paj_norm", "")), None)
    id_tram = (achado or {}).get("id_tramite", "")
    item["pronto_para_concluir"] = bool(id_tram)
    if not id_tram:
        item["erro"] = "Já não está na caixa de aguardo — provavelmente já foi concluído manualmente."
        historico_service.atualizar(historico_id, {
            "confirmacao_status": "confirmado",
            "confirmacao_detalhe": "autolimpeza: trâmite não encontrado na caixa de aguardo (caixa lida com sucesso)"})
        _persistir_verificacao(historico_id, item)
        return {"ok": True, "item": item, "mensagens": reg.get("conversa_cache") or []}
    if not numero and not reg.get("paj", ""):
        item["erro"] = "Sem número de WhatsApp registrado neste envio."
        _persistir_verificacao(historico_id, item)
        return {"ok": True, "item": item, "mensagens": reg.get("conversa_cache") or []}
    try:
        leitura = await whatsapp_client.ler_respostas_apos_envio(
            numero, paj_ref=reg.get("paj", ""), numeros_registrados=numeros_reg[1:])
    except Exception as e:  # noqa: BLE001
        leitura = {"ok": False, "detalhe": f"exceção: {e}"}
    if not leitura.get("ok"):
        item["erro"] = leitura.get("detalhe") or "falha ao ler o WhatsApp"
        _persistir_verificacao(historico_id, item)
        return {"ok": True, "item": item, "mensagens": reg.get("conversa_cache") or []}
    item.update(tem_resposta=leitura.get("tem_resposta", False),
               texto_resposta=leitura.get("texto_resposta", ""),
               data_resposta=leitura.get("data_resposta", ""),
               confirmou=leitura.get("confirmou", False),
               status_leitura=leitura.get("status_leitura", ""))
    mensagens = leitura.get("mensagens") or []
    historico_service.atualizar(historico_id, {
        "conversa_cache": mensagens,
        "conversa_cache_em": _dt.datetime.now().isoformat(timespec="seconds"),
    })
    _persistir_verificacao(historico_id, item)
    return {"ok": True, "item": item, "mensagens": mensagens}


async def obter_conversa_whatsapp(historico_id: str, forcar: bool = False) -> dict:
    """Mostra a conversa do WhatsApp do envio `historico_id` para o botão "Abrir
    conversa" (celular simulado). SOMENTE LEITURA (não movimenta/conclui nada).

    Por padrão usa o CACHE gravado a cada "Verificar respostas" (`conversa_cache` no
    histórico) — abre na hora, sem Playwright. `forcar=True` (botão "Atualizar agora"
    no modal): se o item ainda está pendente de confirmação, roda a reverificação
    completa (`verificar_uma_pendencia`) — assim a Situação/botões da linha na tabela
    de pendentes já saem atualizados, sem precisar de "Verificar respostas" em lote.
    Para um item já concluído/removido, só relê a conversa (não há mais o que verificar)."""
    from services import historico_service
    reg = historico_service.obter(historico_id)
    if not reg:
        return {"ok": False, "erro": "Registro do histórico não encontrado."}
    numeros = _numeros_candidatos(reg)
    numero = numeros[0] if numeros else ""
    if not forcar and reg.get("conversa_cache") is not None:
        return {"ok": True, "numero": numero, "assistido": reg.get("assistido", ""),
                "mensagens": reg.get("conversa_cache") or [],
                "cache": True, "cache_em": reg.get("conversa_cache_em", "")}
    ainda_pendente = reg.get("confirmacao_status") not in ("confirmado", "removido")
    if forcar and ainda_pendente:
        v = await verificar_uma_pendencia(historico_id)
        if not v.get("ok"):
            return {"ok": False, "erro": v.get("erro") or "falha ao reverificar"}
        agora = _dt.datetime.now().isoformat(timespec="seconds")
        return {"ok": True, "numero": numero, "assistido": reg.get("assistido", ""),
                "mensagens": v.get("mensagens") or [], "cache": False, "cache_em": agora,
                "verificacao": v.get("item")}
    if not numero and not reg.get("paj", ""):
        return {"ok": False, "erro": "Sem número de WhatsApp registrado neste envio."}
    from ingestao import whatsapp_client
    try:
        r = await whatsapp_client.obter_conversa(numero, paj_ref=reg.get("paj", ""), numeros_registrados=numeros[1:])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"exceção: {e}"}
    if not r.get("ok"):
        return {"ok": False, "erro": r.get("detalhe") or "falha ao ler o WhatsApp"}
    mensagens = r.get("mensagens") or []
    agora = _dt.datetime.now().isoformat(timespec="seconds")
    historico_service.atualizar(historico_id, {"conversa_cache": mensagens, "conversa_cache_em": agora})
    return {"ok": True, "numero": numero, "assistido": reg.get("assistido", ""),
            "mensagens": mensagens, "cache": False, "cache_em": agora}


# ==================== CORRESPONDÊNCIAS ====================
def _gerar_carta_corpo(msg: str) -> str:
    """CORPO da carta a partir da mensagem-base (Sugestão). Com IA disponível, adapta
    para linguagem simples/concisa (leitor humilde); sem IA (Modo Simples) ou em
    falha/cota, usa a própria mensagem-base. Chamado ao gravar por carta / expedir."""
    base = (msg or "").strip()
    if not base:
        return ""
    if analise_service.disponivel():
        with contextlib.suppress(Exception):
            corpo = analise_service.gerar_corpo_carta(base)
            if corpo:
                return corpo
    return base


def encaminhar_correspondencia(slug: str, paj_norm: str, id_tramite: str, motivo: str = "",
                               mensagem: str = "", encerrar_ligacao: bool = False,
                               tipo: str = "tramite", gerar_corpo: bool = False) -> dict:
    """Encaminha um trâmite (ou CONJUNTO) para a fila de CORRESPONDÊNCIAS. `mensagem` = base do
    texto da carta. `encerrar_ligacao` = True apenas quando vem do botão 'Enviar correspondência'
    da central de Ligações; do modal 'Gravar para envio' é False (cumulativo). Idempotente — se já
    pendente, atualiza a mensagem-base. `gerar_corpo=False` (padrão): NÃO chama a IA aqui — para
    não atrasar a triagem da caixa; o corpo é gerado sob demanda por PAJ (botão 'Gerar carta')."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    # Sem endereço postável a carta é indisponível (não encaminha).
    if not _paj_tem_endereco(paj_norm):
        return {"ok": False, "erro": "PAJ sem endereço cadastrado — carta indisponível."}
    alvo, _idp, _ids, _paj, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    msg = colapsar_paragrafos(remover_emojis(mensagem or ""))
    lig = alvo.get("ligacao") or {}
    corr = alvo.get("correspondencia") or {}
    if corr.get("status") == "pendente":
        if msg:
            corr["mensagem_base"] = msg
            if gerar_corpo:
                corr["carta_corpo"] = _gerar_carta_corpo(msg)  # regenera o corpo pela IA (sob demanda)
            alvo["correspondencia"] = corr
            _gravar_box(slug, box)
        return {"ok": True, "ja": True}
    alvo["correspondencia"] = {
        "status": "pendente",
        "em": _dt.datetime.now().isoformat(timespec="seconds"),
        "de_ligacao": bool(lig.get("tentativas")),
        "tentativas_ligacao": len(lig.get("tentativas") or []),
        "motivo": (motivo or "").strip(),
        "mensagem_base": msg,
        "carta_corpo": (_gerar_carta_corpo(msg) if gerar_corpo else ""),  # IA só sob demanda ('Gerar carta')
        "tipo": tipo,
    }
    if encerrar_ligacao and lig.get("status"):
        lig["status"] = "encaminhado_correspondencia"
        alvo["ligacao"] = lig
    _gravar_box(slug, box)
    # Entrada NOVA na fila → começa SEM endereço de envio/validação (valem só enquanto na fila:
    # se o PAJ voltar depois de removido/expedido, exige nova validação e nova geração da carta).
    cont = ler_contato(paj_norm)
    if _limpar_endereco_envio(cont):
        _gravar_contato(cont)
    _det_corr = ("Encaminhado para expedição por carta"
                 + (" (após ligações infrutíferas)" if lig.get("tentativas") else "")
                 + ((" — motivo: " + motivo.strip()) if (motivo or "").strip() else "")
                 + ". Expedição pelos Correios: pendente.")
    _salvar_sessao_log("carta",
                       [{"ok": None, "etapa": "Encaminhamento para correspondência", "detalhe": _det_corr}],
                       False)
    return {"ok": True}


def editar_carta_corpo(slug: str, paj_norm: str, id_tramite: str, texto: str,
                       tipo: str = "tramite") -> dict:
    """Salva o CORPO da carta editado MANUALMENTE na conferência (central de
    Correspondências). NÃO regera pela IA — grava exatamente o texto conferido."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    alvo, *_ = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    corr = alvo.get("correspondencia") or {}
    if corr.get("status") != "pendente":
        return {"ok": False, "erro": "Correspondência não está pendente."}
    corr["carta_corpo"] = colapsar_paragrafos(remover_emojis(texto or ""))
    alvo["correspondencia"] = corr
    _gravar_box(slug, box)
    return {"ok": True}


def gerar_carta(slug: str, paj_norm: str, id_tramite: str, tipo: str = "tramite") -> dict:
    """Gera pela IA o CORPO da carta de UM PAJ da fila de correspondências, SOB DEMANDA
    (botão 'Gerar carta'). A geração é feita aqui — e não no 'Gravar para envio' — para não
    atrasar a triagem da caixa. Retorna {ok, carta_corpo, ia}."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    alvo, *_ = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    corr = alvo.get("correspondencia") or {}
    if corr.get("status") != "pendente":
        return {"ok": False, "erro": "Correspondência não está pendente."}
    base = (corr.get("mensagem_base") or "").strip()
    if not base:
        return {"ok": False, "erro": "Sem texto-base para gerar a carta."}
    corpo = _gerar_carta_corpo(base)
    corr["carta_corpo"] = corpo
    alvo["correspondencia"] = corr
    _gravar_box(slug, box)
    return {"ok": True, "carta_corpo": corpo, "ia": analise_service.disponivel()}


def _primeiro_endereco(cont: dict) -> dict | None:
    """1º endereço do PAJ com CEP (o que será usado na expedição)."""
    for e in (cont.get("enderecos") or []):
        if (e.get("cep") or "").strip():
            return e
    return None


_CEP_CACHE: dict = {}


CEP_INEXISTENTE = "INEXISTENTE"  # sentinela: CEP que NENHUM provedor encontrou


def _cep_normaliza(d: dict):
    """Normaliza a resposta de um provedor (formato ViaCEP/OpenCEP) para o dict interno.
    Retorna CEP_INEXISTENTE se a resposta indica não-encontrado."""
    if not isinstance(d, dict) or d.get("erro") or d.get("error"):
        return CEP_INEXISTENTE
    logr = (d.get("logradouro") or "").strip()
    cidade = (d.get("localidade") or d.get("cidade") or "").strip()
    # Sem logradouro E sem cidade → resposta vazia/ inválida.
    if not logr and not cidade:
        return CEP_INEXISTENTE
    return {"logradouro": logr, "bairro": (d.get("bairro") or "").strip(),
            "cidade": cidade, "uf": (d.get("uf") or "").strip()}


def _cep_consultar(url: str):
    """Consulta UM provedor de CEP. Retorna dict | CEP_INEXISTENTE | None (indisponível).
    404/400 = CEP não encontrado nesse provedor; timeout/offline/erro = indisponível (None)."""
    import json as _j
    import urllib.error as _ue
    import urllib.request as _u
    try:
        with _u.urlopen(url, timeout=3) as r:
            d = _j.loads(r.read().decode("utf-8"))
    except _ue.HTTPError as e:
        return CEP_INEXISTENTE if e.code in (400, 404) else None
    except Exception:  # noqa: BLE001 — offline/timeout/JSON inválido → indisponível
        return None
    return _cep_normaliza(d)


# Provedores de CEP, consultados EM ORDEM (nome → construtor da URL). ViaCEP é o primário;
# OpenCEP é a ALTERNATIVA (o ViaCEP às vezes responde 'erro' p/ CEPs VÁLIDOS — ex.: 81260-040).
_CEP_PROVEDORES = (
    ("viacep", lambda digs: f"https://viacep.com.br/ws/{digs}/json/"),
    ("opencep", lambda digs: f"https://opencep.com/v1/{digs}"),
)
_CEP_PROV_NOMES = {"viacep": "ViaCEP", "opencep": "OpenCEP"}


def resolver_cep_detalhado(cep: str) -> dict:
    """Consulta TODOS os provedores (SEM short-circuit) e devolve:
      {"resolvido": {logradouro,bairro,cidade,uf} | CEP_INEXISTENTE | None,
       "provedores": {"viacep": "ok"|"nao"|"off", "opencep": ...}}
    ok=achou · nao=respondeu inexistente · off=indisponível. `resolvido` segue a regra do
    resolver_cep (1º provedor que achou; senão None se houve offline; senão INEXISTENTE).
    Cacheia o resultado completo só quando NÃO houve provedor offline (permite nova tentativa)."""
    digs = "".join(c for c in str(cep or "") if c.isdigit())
    if len(digs) != 8:
        return {"resolvido": CEP_INEXISTENTE,
                "provedores": {nome: "nao" for nome, _ in _CEP_PROVEDORES}}
    if digs in _CEP_CACHE:
        return _CEP_CACHE[digs]
    prov: dict = {}
    achou = None
    houve_indisponivel = False
    for nome, urlfn in _CEP_PROVEDORES:
        r = _cep_consultar(urlfn(digs))
        if isinstance(r, dict):
            prov[nome] = "ok"
            if achou is None:
                achou = r
        elif r is None:
            prov[nome] = "off"
            houve_indisponivel = True
        else:
            prov[nome] = "nao"
    if achou is not None:
        resolvido = achou
    elif houve_indisponivel:
        resolvido = None
    else:
        resolvido = CEP_INEXISTENTE
    out = {"resolvido": resolvido, "provedores": prov}
    if not houve_indisponivel:   # definitivo → cacheia (senão permite retry depois)
        _CEP_CACHE[digs] = out
    return out


def resolver_cep(cep: str):
    """Só o endereço resolvido (dict | CEP_INEXISTENTE | None). Ver resolver_cep_detalhado."""
    return resolver_cep_detalhado(cep)["resolvido"]


def _prov_badges_list(prov: dict | None) -> list[dict]:
    """Converte {viacep:'ok'|'nao'|'off', ...} na lista de badges do template (nome/marca/status)."""
    if not prov:
        return []
    return [{"nome": _CEP_PROV_NOMES.get(k, k), "st": prov[k],
             "mark": "✓" if prov.get(k) == "ok" else "✗"}
            for k in ("viacep", "opencep") if prov.get(k)]


def _norm_logr(s: str) -> str:
    """Normaliza logradouro p/ comparar: minúsculas, sem acento, sem o tipo (rua/av...)."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                if not unicodedata.combining(c))
    s = re.sub(r"^(rua|r|av|avenida|travessa|tv|alameda|al|rodovia|rod|estrada|estr|praca|"
               r"largo|quadra|q|servidao|servidão)\.?\s+", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_txt(s: str) -> str:
    """Normaliza texto p/ comparar (município): minúsculas, sem acento, sem '(...)'
    (ex.: 'Curitiba (capital)' → 'curitiba')."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# UF pode vir como SIGLA (ViaCEP: "PR") ou NOME por extenso (cadastro SISDPU: "PARANA").
# Para comparar sem falso-positivo, canonizamos tudo para a SIGLA.
_UF_NOME2SIGLA = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
    "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI",
    "rio de janeiro": "RJ", "rio grande do norte": "RN", "rio grande do sul": "RS",
    "rondonia": "RO", "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP",
    "sergipe": "SE", "tocantins": "TO"}
_UF_SIGLAS = set(_UF_NOME2SIGLA.values())


def _uf_sigla(x: str) -> str:
    """Devolve a SIGLA da UF a partir de sigla OU nome por extenso (sem acento).
    Ex.: 'PR'→'PR', 'PARANA'→'PR', 'Paraná'→'PR'. Desconhecido → devolve normalizado."""
    s = _norm_txt(x).upper()
    if not s:
        return ""
    if len(s) == 2 and s in _UF_SIGLAS:
        return s
    return _UF_NOME2SIGLA.get(s.lower(), s)


def _conferir_endereco(end: dict | None) -> dict:
    """Compara o endereço do CADASTRO com o resolvido pelo CEP (ViaCEP). Retorna
    {resolvido, resolvido_txt, cadastro_txt, divergente, cep_localidade, verificado,
    cep_invalido}. `cep_invalido`=True quando o CEP não existe (→ modal de correção);
    `verificado`=False só quando o ViaCEP está indisponível (offline)."""
    out = {"resolvido": None, "resolvido_txt": "", "cadastro_txt": (_fmt_endereco(end) if end else ""),
           "divergente": False, "divergencias": [], "cep_localidade": False,
           "verificado": False, "cep_invalido": False, "provedores": {}}
    if not end:
        return out
    det = resolver_cep_detalhado(end.get("cep", ""))
    out["provedores"] = det.get("provedores") or {}  # status por serviço (ViaCEP/OpenCEP)
    r = det["resolvido"]
    if r is None:
        return out  # serviços de CEP indisponíveis (offline) → não verificado
    out["verificado"] = True
    if r == CEP_INEXISTENTE:
        out["cep_invalido"] = True   # CEP não existe → precisa corrigir
        return out
    out["resolvido"] = r
    out["resolvido_txt"] = ", ".join(p for p in [
        r["logradouro"], r["bairro"], (f'{r["cidade"]}/{r["uf"]}' if r["cidade"] or r["uf"] else "")] if p)
    # Município e UF: o CEP determina a cidade/UF — se o cadastro divergir, é erro
    # (vale inclusive para CEP de localidade, que não tem logradouro p/ comparar).
    divs = []
    mc, mr = _norm_txt(end.get("municipio", "")), _norm_txt(r["cidade"])
    uc, ur = _uf_sigla(end.get("uf", "")), _uf_sigla(r["uf"])  # canoniza p/ sigla (nome×sigla não é divergência)
    if mc and mr and mc != mr:
        divs.append("município")
    if uc and ur and uc != ur:
        divs.append("UF")
    if r["logradouro"]:
        lc, lr = _norm_logr(end.get("logradouro", "")), _norm_logr(r["logradouro"])
        if lc and lr and lc != lr and lc not in lr and lr not in lc:
            divs.append("logradouro")
    else:
        out["cep_localidade"] = True  # CEP de localidade (cidade inteira, sem logradouro)
    out["divergente"] = bool(divs)
    out["divergencias"] = divs
    return out


def _end_fingerprint(end: dict | None) -> str:
    """Assinatura do endereço p/ saber se uma validação salva ainda vale (invalida se
    CEP/logradouro/município/UF mudarem)."""
    if not end:
        return ""
    return "|".join(_norm_txt(str(end.get(k, ""))) for k in ("cep", "logradouro", "municipio", "uf"))


def _persistir_validacao(cont: dict, end: dict | None, conf: dict) -> None:
    """Salva no CONTATO o resultado da conferência de endereço, com a assinatura do
    endereço conferido — para re-exibir o badge e reabilitar o Expedir ao voltar à aba."""
    cont["_validacao_endereco"] = {
        "fp": _end_fingerprint(end), "em": _dt.datetime.now().isoformat(timespec="seconds"),
        "verificado": conf["verificado"], "divergente": conf["divergente"],
        "divergencias": conf.get("divergencias", []),
        "cep_localidade": conf["cep_localidade"], "cep_invalido": conf["cep_invalido"],
        "resolvido": conf.get("resolvido"),  # dados do CEP (p/ montar o endereço de envio)
        "provedores": conf.get("provedores") or {}}  # status por serviço (badges ViaCEP/OpenCEP)
    _gravar_contato(cont)


def _limpar_endereco_envio(cont: dict) -> bool:
    """Remove o endereço de ENVIO e a validação (valem só enquanto o PAJ está na fila).
    Retorna True se algo foi removido (o chamador grava o contato)."""
    removido = False
    for k in ("_validacao_endereco", "_endereco_envio"):
        if cont.pop(k, None) is not None:
            removido = True
    return removido


def _endereco_envio(cont: dict) -> str:
    """Endereço EXATO para onde a carta será enviada. É um dado À PARTE do cadastro (chave
    `_endereco_envio`): SÓ existe depois que o endereço foi VALIDADO e SALVO (validação casando,
    sem CEP inválido nem divergência) — senão retorna "" (vazio). Monta: número/complemento/CEP
    do endereço de envio + logradouro/bairro/cidade/UF conforme o CEP (o site dos Correios
    preenche o logradouro pelo CEP); em CEP de localidade / serviços de CEP indisponíveis, usa o do envio."""
    end = cont.get("_endereco_envio")
    v = cont.get("_validacao_endereco") or {}
    if not end or not v or v.get("fp") != _end_fingerprint(end):
        return ""                                   # sem endereço de envio validado
    if v.get("cep_invalido") or v.get("divergente"):
        return ""                                   # ainda não é um destino válido
    envio = dict(end)
    r = v.get("resolvido") or {}
    if v.get("verificado") and not v.get("cep_localidade") and r.get("logradouro"):
        envio["logradouro"] = r.get("logradouro") or envio.get("logradouro")
        if r.get("bairro"):
            envio["bairro"] = r["bairro"]
        if r.get("cidade"):
            envio["municipio"] = r["cidade"]
        if r.get("uf"):
            envio["uf"] = r["uf"]
    return _fmt_endereco(envio)


def _validacao_para_render(cont: dict) -> dict:
    """A partir da validação SALVA (se ainda casar com o ENDEREÇO DE ENVIO), devolve o que a
    lista de correspondências precisa: {badge:{cls,txt}|None, liberado:bool, divergente:0/1}.
    Espelha os badges/estados do botão 'Validar endereço' do front."""
    v = cont.get("_validacao_endereco") or {}
    end = cont.get("_endereco_envio")
    if not v or not end or v.get("fp") != _end_fingerprint(end):
        return {"badge": None, "liberado": False, "divergente": 0}  # nunca validado / desatualizado
    if v.get("cep_invalido"):
        return {"badge": {"cls": "cep-div", "txt": "⚠ CEP não encontrado — revalide"}, "liberado": False, "divergente": 0}
    if not v.get("verificado"):
        return {"badge": {"cls": "cep-erro", "txt": "⚠ CEP não verificado (serviços de CEP indisponíveis) — confira manualmente"},
                "liberado": True, "divergente": 1}
    if v.get("divergente"):
        campos = ", ".join(v.get("divergencias") or []) or "endereço"
        return {"badge": {"cls": "cep-div", "txt": f"⚠ Diverge do CEP em: {campos} — corrija"}, "liberado": False, "divergente": 0}
    if v.get("cep_localidade"):
        return {"badge": {"cls": "cep-loc", "txt": "ⓘ CEP de localidade — usará o logradouro do cadastro"}, "liberado": True, "divergente": 0}
    return {"badge": {"cls": "cep-ok", "txt": "✓ Endereço validado"}, "liberado": True, "divergente": 0}


def _parse_data_br(s: str):
    """Extrai uma date de 'DD/MM/AAAA' (ou None)."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    if not m:
        return None
    with contextlib.suppress(Exception):
        return _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def _montar_descricao_carta(assistido: str, end: dict, lote: str, previsao: str,
                            inteiro_teor: str) -> str:
    """Descrição HTML da movimentação 'Ofício/Carta/Comunicação expedida'."""
    import html as _html
    e = _html.escape
    endereco = _fmt_endereco(end) if end else "—"
    linhas = [
        "<strong>[Painel de Comunicações] Carta expedida pelos Correios (e-Carta)</strong>",
        f"Destinatário: <strong>{e(assistido or '—')}</strong>",
        f"Endereço: {e(endereco)}",
        f"Lote/Mensagem SMT: <strong>{e(lote or '—')}</strong>",
        f"Previsão de entrega: {e(previsao or 'não informada')}",
    ]
    corpo = e(inteiro_teor or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return (f"<p>{'<br>'.join(linhas)}</p>"
            f"<p>Carta e comprovante da postagem em anexo.</p>"
            f"<p><strong>Inteiro teor da carta:</strong><br><em>{corpo}</em></p>")


def validar_endereco_carta(paj_norm: str) -> dict:
    """Valida o endereço p/ expedição e DEFINE o endereço de ENVIO (dado à parte do cadastro):
    valida o envio já salvo ou, se ainda não houver, o do cadastro. Resolve o CEP (ViaCEP) e
    compara. Chamado pelo botão 'Validar endereço' antes de habilitar o Expedir. NÃO altera o
    cadastro do PAJ (`enderecos`)."""
    cont = ler_contato(paj_norm)
    base = cont.get("_endereco_envio") or _primeiro_endereco(cont)
    if not base:
        return {"ok": False, "erro": "PAJ sem endereço com CEP — carta indisponível."}
    campos = {k: (str(base.get(k) or "")).strip() for k in _CAMPOS_ENDERECO}
    cont["_endereco_envio"] = campos             # validar define/confirma o endereço de envio
    conf = _conferir_endereco(campos)
    _persistir_validacao(cont, campos, conf)     # grava _endereco_envio + validação (1 _gravar_contato)
    return _resp_conf(conf, campos, cont)


_CAMPOS_ENDERECO = ("cep", "logradouro", "numero", "complemento", "bairro", "municipio", "uf")


def _resp_conf(conf: dict, campos: dict, cont: dict | None = None) -> dict:
    """Payload padrão das rotas de validação/salvamento a partir da conferência + endereço.
    Se `cont` for dado, inclui `endereco_envio_txt` = o Endereço de Envio já resolvido (""
    quando ainda não é um destino válido) — o front atualiza o bloco 'Endereço de Envio'
    na hora, sem precisar recarregar a página."""
    return {"ok": True, "verificado": conf["verificado"], "divergente": conf["divergente"],
            "divergencias": conf["divergencias"], "cep_localidade": conf["cep_localidade"],
            "cep_invalido": conf["cep_invalido"], "cadastro_txt": conf["cadastro_txt"],
            "resolvido_txt": conf["resolvido_txt"],
            "cadastro": {k: (campos.get(k) or "") for k in _CAMPOS_ENDERECO},
            "resolvido": conf["resolvido"], "provedores": conf.get("provedores") or {},
            "endereco_envio_txt": (_endereco_envio(cont) if cont is not None else "")}


def _set_endereco_cadastro(cont: dict, campos: dict) -> None:
    """Atualiza o 1º endereço com CEP do CADASTRO local (`enderecos`) — usar SÓ quando o SISDPU
    confirmar a gravação, para o 'Endereço de Cadastro' refletir o que está de fato no SISDPU."""
    ends = cont.get("enderecos") or []
    idx = next((i for i, e in enumerate(ends) if (e.get("cep") or "").strip()), None)
    if idx is None:
        ends.insert(0, {})
        idx = 0
    ends[idx].update(campos)
    cont["enderecos"] = ends


def validar_endereco_editado(endereco: dict) -> dict:
    """Valida um endereço EDITADO (SEM salvar): resolve o CEP informado e compara com o
    logradouro digitado. Usado pelo botão 'Validar' do modal de conferência."""
    end = {k: (str(endereco.get(k) or "")).strip() for k in _CAMPOS_ENDERECO}
    if not end["cep"]:
        return {"ok": False, "erro": "Informe o CEP."}
    conf = _conferir_endereco(end)
    return {"ok": True, "verificado": conf["verificado"], "divergente": conf["divergente"],
            "divergencias": conf["divergencias"],
            "cep_localidade": conf["cep_localidade"], "cep_invalido": conf["cep_invalido"],
            "cadastro_txt": conf["cadastro_txt"],
            "resolvido_txt": conf["resolvido_txt"], "resolvido": conf["resolvido"],
            "provedores": conf.get("provedores") or {}}


def atualizar_endereco_local(paj_norm: str, endereco: dict) -> dict:
    """'Salvar endereço para envio': grava o endereço de ENVIO (dado à parte) e revalida.
    NÃO altera o 'Endereço de Cadastro' (`enderecos`) — este só muda com gravação confirmada no
    SISDPU (ou por sincronização). Retorna a nova conferência."""
    cont = ler_contato(paj_norm)
    campos = {k: (str(endereco.get(k) or "")).strip() for k in _CAMPOS_ENDERECO}
    if not campos["cep"]:
        return {"ok": False, "erro": "Informe o CEP."}
    cont["_endereco_envio"] = campos             # só o endereço de envio
    conf = _conferir_endereco(campos)
    _persistir_validacao(cont, campos, conf)     # grava _endereco_envio + validação
    return _resp_conf(conf, campos, cont)


async def atualizar_endereco(slug: str, paj_norm: str, id_tramite: str, tipo: str,
                             endereco: dict) -> dict:
    """Grava o endereço corrigido no CADASTRO DO ASSISTIDO no SISDPU E no cache local,
    e revalida. Retorna a conferência (como atualizar_endereco_local) + sisdpu_ok/detalhe."""
    from ingestao import sisdpu_client
    from services import credenciais_service
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    alvo, id_proc, _ids, _paj, _sug = _alvo_contato(box, id_tramite, tipo)
    if not id_proc:
        sis = {"ok": False, "detalhe": "PAJ sem id de processo — atualizado só localmente."}
    elif not (credenciais_service.destrancado() or credenciais_service.env_tem_credenciais()):
        sis = {"ok": False, "detalhe": "SISDPU desconectado"}   # não tenta abrir o navegador
    else:
        try:
            sis = await sisdpu_client.atualizar_endereco_assistido(id_proc, endereco)
        except Exception as e:  # noqa: BLE001
            sis = {"ok": False, "detalhe": f"falha ao gravar no SISDPU: {e}"}
    # Sempre grava o endereço de ENVIO + validação; o 'Endereço de Cadastro' (`enderecos`) só é
    # atualizado se o SISDPU CONFIRMOU a gravação (senão continua espelhando o SISDPU real).
    cont = ler_contato(paj_norm)
    campos = {k: (str(endereco.get(k) or "")).strip() for k in _CAMPOS_ENDERECO}
    if not campos["cep"]:
        return {"ok": False, "erro": "Informe o CEP."}
    cont["_endereco_envio"] = campos
    if sis.get("ok"):
        _set_endereco_cadastro(cont, campos)
    conf = _conferir_endereco(campos)
    _persistir_validacao(cont, campos, conf)   # 1 _gravar_contato (envio + validação [+ cadastro])
    local = _resp_conf(conf, campos)
    local["sisdpu_ok"] = bool(sis.get("ok"))
    local["sisdpu_detalhe"] = sis.get("detalhe", "")
    return local


async def atualizar_endereco_por_cep(slug: str, paj_norm: str, id_tramite: str, tipo: str,
                                     cep: str) -> dict:
    """Atualiza o endereço do assistido no SISDPU trocando o CEP (caso de CEP desmembrado, do
    modal da expedição). Parte do endereço de ENVIO/cadastro atual, aplica o CEP novo e — se
    algum serviço resolver o CEP — atualiza logradouro/bairro/município/UF; número e complemento
    são MANTIDOS. Reusa `atualizar_endereco` (grava no SISDPU + cache local + revalida)."""
    digs = "".join(c for c in str(cep or "") if c.isdigit())
    if len(digs) != 8:
        return {"ok": False, "erro": "Informe um CEP válido (8 dígitos)."}
    cont = ler_contato(paj_norm)
    base = cont.get("_endereco_envio") or _primeiro_endereco(cont) or {}
    end = {k: (str(base.get(k) or "")).strip() for k in _CAMPOS_ENDERECO}
    end["cep"] = digs
    r = resolver_cep(digs)
    if isinstance(r, dict):
        if r.get("logradouro"):
            end["logradouro"] = r["logradouro"]
        if r.get("bairro"):
            end["bairro"] = r["bairro"]
        if r.get("cidade"):
            end["municipio"] = r["cidade"]
        if r.get("uf"):
            end["uf"] = r["uf"]
    return await atualizar_endereco(slug, paj_norm, id_tramite, tipo, end)


async def expedir_correspondencia(slug: str, paj_norm: str, id_tramite: str,
                                  tipo: str = "tramite", forcar: bool = False,
                                  cep_override: str = "") -> dict:
    """Wrapper NÃO-streaming: consome `expedir_correspondencia_stream` e devolve só o
    resultado final (compat p/ chamadas sem modal ao vivo)."""
    resultado = {"ok": False, "erro": "sem retorno"}
    async for ev in expedir_correspondencia_stream(slug, paj_norm, id_tramite, tipo, forcar, cep_override):
        if ev.get("done"):
            resultado = ev.get("resultado", resultado)
    return resultado


async def expedir_correspondencia_stream(slug: str, paj_norm: str, id_tramite: str,
                                         tipo: str = "tramite", forcar: bool = False,
                                         cep_override: str = ""):
    """Entrada pública: serializa expedições de carta (e contra qualquer outra operação
    exclusiva — sync/refazer/etc.) num ÚNICO grupo, via o MESMO `_sync_lock` usado no resto
    do painel. ⚠️ CORREÇÃO 2026-08-20: antes desta trava, duas expedições disparadas quase
    ao mesmo tempo (ou uma expedição + uma sincronização) disputavam o MESMO navegador dos
    Correios/SISDPU (singletons) concorrentemente — risco real de misturar dados entre
    cartas. Agora ENFILEIRA (`_entrar_exclusivo_fila`, fila justa FIFO) em vez de recusar:
    uma 2ª expedição clicada durante a 1ª espera a vez e roda em seguida, sozinha."""
    if not _pode_exclusivo():
        yield {"level": "muted", "msg": "Aguardando outra operação em andamento (fila)…"}
    async with _entrar_exclusivo_fila():
        async for ev in _expedir_carta_sem_trava(slug, paj_norm, id_tramite, tipo, forcar, cep_override):
            yield ev


async def _expedir_carta_sem_trava(slug: str, paj_norm: str, id_tramite: str,
                                   tipo: str = "tramite", forcar: bool = False,
                                   cep_override: str = ""):
    """Implementação de fato — SEMPRE chamar via `expedir_correspondencia_stream` (que
    aplica a trava exclusiva). Expede a carta pelos Correios (SMT) e, em caso de POSTAGEM
    (modo real), movimenta o PAJ na fase 'Ofício/Carta/Comunicação expedida' + junta os DOIS
    PDFs (carta + comprovante da postagem) e TRAMITA para a caixa de aguardo (prazo = previsão
    de entrega + N dias). Governado pelo modo do meio 'correspondencia': SIMULADO percorre o
    assistente e para no Resumo (não posta). GERA eventos SSE do log AO VIVO — sub-passos no
    site dos Correios e depois movimentação/tramitação no SISDPU — e, por último,
    {"done": True, "resultado": {...}}."""
    from ingestao import correios_client, sisdpu_client
    from services import historico_service
    log: list = []

    def passo(etapa, ok, detalhe, nivel=None):
        """Registra no log do histórico E devolve o evento SSE para yield."""
        _log_passo(log, etapa, ok, detalhe)
        return _evento_passo(etapa, ok, detalhe, nivel=nivel)

    def _falha(msg):
        return {"done": True, "resultado": {"ok": False, "erro": msg}}

    box = _ler_box(slug, paj_norm)
    caixa = config_service.get_caixa(slug)
    if not box or not caixa:
        yield {"level": "erro", "msg": "PAJ/caixa não encontrado."}
        yield _falha("PAJ/caixa não encontrado.")
        return
    if not _paj_tem_endereco(paj_norm):
        yield {"level": "erro", "msg": "PAJ sem endereço cadastrado — carta indisponível."}
        yield _falha("PAJ sem endereço cadastrado — carta indisponível.")
        return
    alvo, id_proc, originarios, paj_disp, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        yield {"level": "erro", "msg": "Trâmite/conjunto não encontrado."}
        yield _falha("Trâmite/conjunto não encontrado.")
        return
    corr = alvo.get("correspondencia") or {}
    if corr.get("status") != "pendente":
        yield {"level": "erro", "msg": "Correspondência não está pendente."}
        yield _falha("Correspondência não está pendente.")
        return
    cont = ler_contato(paj_norm)
    # A carta vai para o ENDEREÇO DE ENVIO (validado/salvo); só cai no cadastro se, por algum
    # motivo, não houver envio definido (fluxo antigo).
    end = cont.get("_endereco_envio") or _primeiro_endereco(cont)
    if not end:
        yield {"level": "erro", "msg": "PAJ sem endereço com CEP."}
        yield _falha("PAJ sem endereço com CEP.")
        return
    # Conferência do endereço: se o CEP resolver um logradouro divergente do cadastro,
    # NÃO expede automaticamente (exige conferência manual → forcar=True).
    conf = _conferir_endereco(end)
    # CEP corrigido pelo usuário (desmembrado, informado no modal de CEP não identificado):
    # substitui o CEP do destinatário e dispensa a conferência (o usuário já decidiu prosseguir).
    _cep_ovr = "".join(c for c in str(cep_override or "") if c.isdigit())
    if conf["divergente"] and not forcar and not _cep_ovr:
        yield {"level": "aviso", "msg": "Endereço do CEP diverge do cadastro — conferência necessária."}
        yield {"done": True, "resultado": {
            "ok": False, "status": "conferencia",
            "cadastro": conf["cadastro_txt"], "resolvido": conf["resolvido_txt"],
            "erro": ("O endereço do CEP diverge do cadastro. Confira antes de expedir "
                     "(ou confirme para expedir mesmo assim).")}}
        return
    assistido = cont.get("assistido", "") or ""
    destinatario = {"nome": assistido, "cpf": cont.get("cpf", ""),
                    "cep": (_cep_ovr or end.get("cep", "")), "numero": end.get("numero", ""),
                    "complemento": end.get("complemento", ""),
                    # logradouro do CADASTRO: usado só se o CEP for de localidade (autofill vazio)
                    "logradouro": end.get("logradouro", ""),
                    "cep_localidade": conf["cep_localidade"]}
    cabecalho = config_service.get_carta_cabecalho()
    rodape = config_service.get_carta_rodape()
    corpo = corr.get("carta_corpo", "") or corr.get("mensagem_base", "")
    modo = config_service.get_envio_modo_meio("correspondencia")
    efetivar = (modo == "real")
    COMPROVANTES_CARTA_DIR.mkdir(parents=True, exist_ok=True)
    yield {"level": "novo",
           "msg": f"Expedição de carta — PAJ {paj_disp} · {assistido} · "
                  f"modo {'REAL (posta e cobra)' if efetivar else 'simulado (não posta)'}"}
    _log_passo(log, "Expedição de carta", None,
               f"Destinatário {assistido} · CEP {end.get('cep', '')} · modo {'real' if efetivar else 'simulado'}")
    # --- Correios: sub-passos do site AO VIVO (login → destinatário → carta → resumo → efetivar) ---
    r = {"ok": False, "detalhe": "sem retorno"}
    try:
        async for ev in _drenar_com_passos(lambda emit: correios_client.expedir_carta(
                destinatario, cabecalho, corpo, rodape, efetivar=efetivar,
                comprovante_dir=str(COMPROVANTES_CARTA_DIR), emit=emit)):
            if "_result" in ev:
                r = ev["_result"]
            else:
                yield ev
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "detalhe": f"exceção: {e}"}
    if not r.get("ok"):
        yield passo("Expedição de carta", False, r.get("detalhe", ""))
        if r.get("status") == "cep_nao_identificado":
            # CEP não reconhecido pelos Correios → status próprio p/ o badge de falha na tela.
            yield {"done": True, "resultado": {
                "ok": False, "status": "cep_nao_identificado",
                "erro": r.get("detalhe", ""), "detalhe": r.get("detalhe", "")}}
        else:
            yield _falha(r.get("detalhe", "Falha na expedição da carta."))
        return
    if not efetivar:
        _log_passo(log, "Expedição de carta", None, "Modo simulado — chegou ao Resumo, sem postar.")
        yield {"level": "ok", "msg": "Modo simulado: carta montada e conferida no Resumo (não postada)."}
        yield {"done": True, "resultado": {
            "ok": True, "status": "simulado",
            "detalhe": "Modo simulado: a carta foi montada e chegou ao Resumo, mas NÃO foi postada."}}
        return
    # ---- POSTAGEM efetivada → Fase 3 (movimentação + juntada + tramitação) ----
    lote = r.get("lote", "")
    previsao = r.get("previsao_entrega", "")
    comprovante = r.get("comprovante_caminho", "")
    comp_nome = r.get("comprovante_nome", "")
    carta_pdf = r.get("carta_pdf_caminho", "")    # PDF do CONTEÚDO da carta (capturado no "Visualizar")
    carta_nome = r.get("carta_pdf_nome", "")
    yield passo("Postagem nos Correios", True, f"Lote {lote or '—'} · previsão {previsao or 'não informada'}")
    inteiro_teor = "\n\n".join(p for p in [cabecalho, corpo, rodape] if p).strip()
    dias = config_service.get_carta_prazo_pos_entrega()
    base = _parse_data_br(previsao) or _dt.date.today()
    calc_aguardar = _proximo_dia_util(base + _dt.timedelta(days=dias), config_service.get_feriados_datas())
    data_aguardar = calc_aguardar.strftime("%d/%m/%Y")
    desc_mov = _montar_descricao_carta(assistido, end, lote, previsao, inteiro_teor)
    # Dois anexos, com nomes UNIFORMES no SISDPU (o `nome` vira o nome do arquivo + a
    # descrição do documento): "Carta" e "Comprovante da postagem".
    anexos = []
    if carta_pdf:
        anexos.append({"nome": "Carta.pdf", "caminho": carta_pdf})
    if comprovante:
        anexos.append({"nome": "Comprovante da postagem.pdf", "caminho": comprovante})
    anexos = anexos or None
    n_anx = len(anexos or [])
    yield {"level": "info",
           "msg": f"     Movimentando o PAJ no SISDPU (fase '{_FASE_CARTA_EXPEDIDA}') · "
                  f"{n_anx} anexo(s): carta + comprovante da postagem…"}
    try:
        mov = await sisdpu_client.movimentar_paj(
            [{"id": id_proc, "numero": paj_disp}], desc_mov,
            fase=_FASE_CARTA_EXPEDIDA, anexos=anexos,
            tipo_arquivo="Correspondência", descricao_html=True)
    except Exception as e:  # noqa: BLE001
        mov = {"ok": False, "detalhe": f"exceção: {e}", "mov_seq": None}
    mov_ok = bool(mov.get("ok"))
    mov_seq = mov.get("mov_seq")
    yield passo(f"Movimentação (fase '{_FASE_CARTA_EXPEDIDA}')", mov_ok,
                (f'"mov. {mov_seq}"' if mov_ok else mov.get("detalhe", "")))
    tram_caixa, tram_status = "", "nao_realizada"
    if mov_ok:
        caixa_tram = config_service.get_carta_caixa_tramitacao()
        desc_tram = f"Carta expedida. Aguardar retorno até {data_aguardar}."
        yield {"level": "info",
               "msg": f"     Tramitando para {caixa_tram} (aguardar retorno até {data_aguardar})…"}
        try:
            tram = await sisdpu_client.tramitar_paj(
                [{"id": id_proc, "numero": paj_disp}], grupo=caixa_tram,
                descricao=desc_tram, prazo_data=data_aguardar, prazo_desc=desc_tram)
        except Exception as e:  # noqa: BLE001
            tram = {"ok": False, "detalhe": f"exceção: {e}"}
        tram_ok = bool(tram.get("ok"))
        tram_caixa = caixa_tram if tram_ok else ""
        tram_status = "tramitado" if tram_ok else "falha"
        yield passo(f"Tramitação → {caixa_tram}", tram_ok,
                    f"aguardar até {data_aguardar}" if tram_ok else tram.get("detalhe", ""))
    # CONCLUSÃO do(s) trâmite(s) ORIGINÁRIO(S): admite na caixa de grupo + dá baixa na caixa de
    # entrada (btnConcluir). Sem isso, o trâmite de origem fica PENDENTE na caixa. Só quando a
    # movimentação teve êxito E a tramitação foi OK (senão mantém na caixa p/ nova tentativa).
    concluido_status = "nao_realizada"
    ids_orig = [x for x in (originarios or []) if x]
    if mov_ok and tram_status == "tramitado" and ids_orig:
        yield {"level": "muted",
               "msg": f"     admitindo e concluindo {len(ids_orig)} trâmite(s) originário(s)…"}
        try:
            conc = await sisdpu_client.concluir_paj(ids_orig)
        except Exception as e:  # noqa: BLE001
            conc = {"ok": False, "detalhe": f"exceção: {e}"}
        adm_ok, adm_det, conc_ok, conc_det = _resumo_conclusao(conc)
        yield passo("Admissão na caixa de grupo", adm_ok, adm_det)
        yield passo("Conclusão na caixa de entrada", conc_ok, conc_det)
        concluido_status = "concluido" if conc_ok else "falha"
    # persistência: marca a correspondência como EXPEDIDA (sai dos pendentes)
    corr.update(status="expedida", expedida_em=_dt.datetime.now().isoformat(timespec="seconds"),
                lote=lote, previsao_entrega=previsao, data_aguardar=data_aguardar,
                inteiro_teor=inteiro_teor, comprovante=comp_nome, carta_pdf=carta_nome,
                mov_seq=mov_seq, tram_caixa=tram_caixa)
    alvo["correspondencia"] = corr
    _gravar_box(slug, box)
    with contextlib.suppress(Exception):
        historico_service.registrar({
            "paj_norm": paj_norm, "paj": paj_disp, "assistido": assistido,
            "oficio": alvo.get("oficio_origem", ""),
            "id_tramite": (originarios[0] if len(originarios) == 1 else ""),
            "tramite_ids": originarios, "conjunto": (tipo == "conjunto"),
            "tramites_info": _tramites_info(_enriquecer_conjunto(box, cont) if tipo == "conjunto"
                                            else _enriquecer_tramite(box, alvo, cont), tipo == "conjunto"),
            "caixa_slug": slug, "caixa_nome": caixa.get("nome", ""), "grupo": caixa.get("grupo", ""),
            "meio": "carta", "meios": ["carta"], "mensagem": corpo,
            "mov_descricao": desc_mov, "url_detalhe": alvo.get("url_detalhe", ""),
            "id_processo": id_proc, "mov_seq": mov_seq,
            "mov_status": "movimentado" if mov_ok else "falha",
            "tram_caixa": tram_caixa, "tram_status": tram_status, "tram_grupo": tram_caixa,
            "concluido_status": concluido_status,
            "status": "enviado" if mov_ok else "parcial",
            "detalhe": f"Carta expedida (Lote {lote or '—'}; previsão {previsao or '—'}).",
            "comprovante": comp_nome, "carta_pdf": carta_nome, "lote": lote,
            "previsao_entrega": previsao, "log": log,
            "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
        })
    # NÃO chamar _salvar_sessao_log aqui: a sessão do disparo (log ao vivo) é persistida pelo
    # `disparos_service.stream_e_salvar` que envolve este gerador na rota /stream.
    yield {"level": "ok",
           "msg": f"Carta expedida. Lote {lote or '—'}; aguardar retorno até {data_aguardar}."}
    yield {"done": True, "resultado": {
        "ok": True, "status": "expedido", "lote": lote, "previsao": previsao,
        "data_aguardar": data_aguardar, "mov_seq": mov_seq, "tram_caixa": tram_caixa,
        "concluido_status": concluido_status,
        "detalhe": f"Carta expedida. Lote {lote or '—'}; aguardar retorno até {data_aguardar}."}}


async def rejuntar_comprovante(slug: str, paj_norm: str, id_tramite: str, comprovante_nome: str,
                               tipo: str = "tramite", previsao: str = "") -> dict:
    """CORREÇÃO: junta o COMPROVANTE DA POSTAGEM CORRETO a um PAJ (quando um comprovante trocado
    foi anexado). Faz uma movimentação fase 'Juntada de documento' com o PDF e atualiza o registro
    da correspondência (campo `comprovante`). NÃO refaz a tramitação."""
    from ingestao import sisdpu_client
    from services import historico_service
    # 1) tenta o box local; 2) se o PAJ já saiu da caixa (tramitado), recupera id_processo/paj do HISTÓRICO.
    box = _ler_box(slug, paj_norm)
    alvo, id_proc, paj_disp = None, "", ""
    if box:
        alvo, id_proc, _originarios, paj_disp, _sug = _alvo_contato(box, id_tramite, tipo)
    if not id_proc:
        with contextlib.suppress(Exception):
            for r in historico_service.listar():
                if r.get("paj_norm") == paj_norm and r.get("meio") == "carta" and r.get("id_processo"):
                    id_proc = str(r.get("id_processo"))
                    paj_disp = r.get("paj") or paj_disp
                    break
    if not id_proc:
        return {"ok": False, "erro": "PAJ/id_processo não encontrado (nem no box, nem no histórico)."}
    caminho = COMPROVANTES_CARTA_DIR / Path(comprovante_nome).name
    if not caminho.is_file():
        return {"ok": False, "erro": f"Comprovante '{comprovante_nome}' não encontrado no disco."}
    prev_txt = f"<br>Previsão de entrega: {previsao}." if previsao else ""
    desc = ("<p><strong>Comprovante de postagem correto. Desconsiderar o anterior.</strong>"
            + prev_txt + "</p>")
    try:
        mov = await sisdpu_client.movimentar_paj(
            [{"id": id_proc, "numero": paj_disp}], desc, fase="Juntada de documento",
            anexos=[{"nome": "Comprovante da postagem.pdf", "caminho": str(caminho)}],
            tipo_arquivo="Correspondência", descricao_html=True)
    except Exception as e:  # noqa: BLE001
        mov = {"ok": False, "detalhe": f"exceção: {e}"}
    if not mov.get("ok"):
        return {"ok": False, "erro": mov.get("detalhe", "falha na juntada")}
    # atualiza o registro da correspondência p/ apontar o comprovante correto (só se o box existir)
    if box and alvo:
        with contextlib.suppress(Exception):
            corr = alvo.get("correspondencia") or {}
            if corr:
                corr["comprovante"] = Path(comprovante_nome).name
                if previsao:
                    corr["previsao_entrega"] = previsao
                alvo["correspondencia"] = corr
                _gravar_box(slug, box)
    return {"ok": True, "mov_seq": mov.get("mov_seq"),
            "detalhe": f"Comprovante correto juntado (mov. {mov.get('mov_seq') or '—'})."}


def cancelar_correspondencia(slug: str, paj_norm: str, id_tramite: str, tipo: str = "tramite") -> dict:
    """Remove o trâmite/conjunto da fila de correspondências (não revive a ligação encerrada).
    Zera a carta gerada e a mensagem-base (que ficam em `correspondencia`) e, se o PAJ não tiver
    mais nenhuma correspondência pendente, zera também a validação/endereço de envio — esses dados
    valem SÓ enquanto o PAJ está na fila (ao voltar, exige nova validação e nova geração da carta)."""
    box = _ler_box(slug, paj_norm)
    if not box:
        return {"ok": False, "erro": "PAJ não encontrado."}
    alvo, _idp, _ids, _paj, _sug = _alvo_contato(box, id_tramite, tipo)
    if alvo is None:
        return {"ok": False, "erro": "Trâmite/conjunto não encontrado."}
    alvo["correspondencia"] = {}  # zera status + mensagem-base + carta_corpo
    _gravar_box(slug, box)
    conj = box.get("conjunto") or {}
    resta = (any((s.get("correspondencia") or {}).get("status") == "pendente"
                 for s in box.get("solicitacoes", []))
             or (conj.get("correspondencia") or {}).get("status") == "pendente")
    if not resta:
        cont = ler_contato(paj_norm)
        if _limpar_endereco_envio(cont):
            _gravar_contato(cont)
    return {"ok": True}


def _enriquecer_correspondencia(box: dict, alvo: dict, cont: dict, caixa_meta: dict, tipo: str = "tramite") -> dict:
    d = _enriquecer_conjunto(box, cont) if tipo == "conjunto" else _enriquecer_tramite(box, alvo, cont)
    d["corr_tipo"] = tipo
    corr = alvo.get("correspondencia") or {}
    d["caixa_slug"] = caixa_meta["slug"]
    d["caixa_nome"] = caixa_meta["nome"]
    d["grupo"] = caixa_meta["grupo"]
    d["corr_status"] = corr.get("status", "")
    d["corr_em"] = corr.get("em", "")
    d["corr_de_ligacao"] = corr.get("de_ligacao", False)
    d["corr_tentativas_ligacao"] = corr.get("tentativas_ligacao", 0)
    d["corr_motivo"] = corr.get("motivo", "")
    d["corr_mensagem_base"] = corr.get("mensagem_base", "")
    # Corpo da carta (IA ou editado); fallback p/ a base em registros antigos.
    d["corr_carta_corpo"] = corr.get("carta_corpo", "") or corr.get("mensagem_base", "")
    # Validação de endereço PERSISTIDA (re-exibe badge + reabilita Expedir ao voltar à aba).
    # A conferência em si continua sob demanda ("Validar endereço"); aqui só relemos o que ficou salvo.
    val = _validacao_para_render(cont)
    d["corr_val_badge"] = val["badge"]
    d["corr_validado"] = val["liberado"]
    d["corr_val_divergente"] = val["divergente"]
    # Badges por serviço de CEP (✓/✗ ViaCEP · ✓/✗ OpenCEP) — só quando a validação está vigente.
    d["corr_val_provedores"] = (_prov_badges_list((cont.get("_validacao_endereco") or {}).get("provedores"))
                                if val["badge"] else [])
    _pe = _primeiro_endereco(cont)
    d["corr_endereco_cadastro"] = _fmt_endereco(_pe) if _pe else ""  # endereço tal como no cadastro
    d["corr_endereco_envio"] = _endereco_envio(cont)  # endereço EXATO de destino da carta (CEP)
    return d


def listar_correspondencias() -> dict:
    """Central de Correspondências (todas as caixas): {"pendentes":[...]}."""
    pend = []
    for c in _caixas_com_dados():
        bdir = _box_dir(c["slug"])
        if not bdir.is_dir():
            continue
        for f in bdir.glob("PAJ-*.json"):
            with contextlib.suppress(Exception):
                box = json.loads(f.read_text(encoding="utf-8"))
                cont = ler_contato(box.get("paj_norm", ""))
                for s in box.get("solicitacoes", []):
                    if (s.get("correspondencia") or {}).get("status") == "pendente":
                        pend.append(_enriquecer_correspondencia(box, s, cont, c, "tramite"))
                conj = box.get("conjunto") or {}
                if (conj.get("correspondencia") or {}).get("status") == "pendente":
                    pend.append(_enriquecer_correspondencia(box, conj, cont, c, "conjunto"))
    pend.sort(key=lambda d: d.get("corr_em", ""))
    return {"pendentes": pend}


def contar_correspondencias() -> dict:
    db = listar_correspondencias()
    return {"pendentes": len(db["pendentes"]), "total": len(db["pendentes"]),
            "modo": config_service.get_envio_modo_meio("correspondencia")}


async def enviar_whatsapp_tramite(slug: str, paj_norm: str, id_tramite: str,
                                  tipo: str = "tramite", headless: bool | None = None,
                                  emit=None) -> dict:
    """Envio ASSISTIDO por WhatsApp de UMA tramitação OU conjunto (fora do lote).

    Modelo: 1 clique no painel → o Playwright abre a conversa (Chrome dedicado, na
    máquina do servidor), envia TEXTO e depois os ANEXOS, gera o comprovante PDF
    (2 páginas: dados do contato + conversa) e registra no histórico + em `enviados`
    do alvo (tramitação ou conjunto), limpando a fila. Respeita o modo do meio
    (simulado × real). Se `emit` for dado, transmite cada passo (envio → comprovante →
    movimentação → tramitação) ao popup em tempo real.
    Retorna {ok, status, detalhe, comprovante, comprovante_nome}."""
    from ingestao import whatsapp_client
    from services import historico_service

    caixa = config_service.get_caixa(slug)
    if not caixa:
        return {"ok": False, "status": "falha", "detalhe": "Caixa desconhecida."}

    tipo = "conjunto" if tipo == "conjunto" else "tramite"
    conjunto = tipo == "conjunto"
    # Item ENRIQUECIDO da fila (resolve mensagem/anexos/prazo/ids p/ tramitação E conjunto).
    t = _achar_item_fila(listar_fila(slug), paj_norm, tipo, id_tramite or "")
    if not t:
        return {"ok": False, "status": "falha",
                "detalhe": "Item não está na fila (tramitação/conjunto não encontrado)."}
    if "whatsapp" not in (t.get("envio_meios") or []):
        return {"ok": False, "status": "falha",
                "detalhe": "WhatsApp não está pendente neste item."}

    ids = t.get("tramite_ids") or ([id_tramite] if id_tramite else [])
    ref_anexo = "" if conjunto else (ids[0] if ids else "")
    mensagem = (t.get("envio_mensagem") or t.get("sugestao_atual") or "").strip()

    cont = ler_contato(paj_norm)
    numeros = cont.get("whatsapp_numeros") or []
    if not numeros:
        return {"ok": False, "status": "falha",
                "detalhe": "PAJ sem número de WhatsApp cadastrado."}
    assistido = t.get("assistido") or cont.get("assistido") or ""
    paj = t.get("paj", "")

    anexos_meta = t.get("anexos", []) or []
    anexos_ctx = [{**a, "caminho": str(caminho_anexo(paj_norm, tipo, ref_anexo,
                   a.get("arquivo", "")))} for a in anexos_meta]

    modo = config_service.get_envio_modo_meio("whatsapp")
    tramitar_grupo = t.get("envio_tramitar_grupo", "") or ""

    # 3ª mensagem (prazo): sempre há prazo (0 = urgente). Enviada por último.
    prazo_wa = _prazo_ok(t.get("envio_prazo_whatsapp", 5))
    if prazo_wa == 0:
        msg_prazo = "Favor atender com urgência."
    else:
        msg_prazo = f"Prazo de {prazo_wa} dia{'s' if prazo_wa != 1 else ''}."

    def _achar_alvo(box):
        if not box:
            return None
        return box.get("conjunto") if conjunto else _achar_solic(box, id_tramite)

    # ENFILEIRA (aguarda a vez) em vez de recusar: se já há um envio/sync em andamento,
    # este espera na fila (FIFO) e roda em seguida.
    log: list = []
    _log_inicial(log, t, caixa.get("grupo", ""))  # sincronização + identificação
    async with _entrar_exclusivo_fila():
        # CHECAGEM PRÉVIA de conflito de sessão do SISDPU (modo real): evita ENVIAR a
        # mensagem e só depois falhar na movimentação/tramitação. Se houver conflito,
        # ABORTA sem enviar e avisa o usuário para resolver.
        if modo != "simulado":
            conf = await sisdpu_client.checar_conflito_sessao(t.get("id") or None)
            if conf.get("conflito"):
                _passo(log, emit, "Checagem de sessão do SISDPU", False,
                       "conflito detectado — envio abortado antes de enviar a mensagem")
                box2 = _ler_box(slug, paj_norm)
                alvo = _achar_alvo(box2)
                if alvo is not None:
                    e2 = alvo.get("envio") or {}
                    e2["falhas"] = {"whatsapp": "Conflito de sessão do SISDPU — resolva antes de enviar."}
                    e2["falha_em"] = _dt.datetime.now().isoformat(timespec="seconds")
                    alvo["envio"] = e2
                    _gravar_box(slug, box2)
                return {"ok": False, "status": "falha",
                        "detalhe": ("Conflito de sessão do SISDPU detectado — feche a OUTRA sessão do seu "
                                    "usuário no SISDPU e tente novamente. A mensagem NÃO foi enviada.")}
            # Verificação na caixa — NÃO bloqueia (multi-meio livre, decisão do usuário):
            # se o trâmite saiu da caixa, apenas informamos. Se fomos NÓS que concluímos
            # (envio anterior por outro meio), seguimos sem alarde; se saiu por AÇÃO EXTERNA
            # (possível outro usuário), avisamos e seguimos mesmo assim (confira manualmente).
            # EXCEÇÃO — REENVIO: dispensada (trâmite originário já concluído por definição).
            if t.get("reenvio"):
                _passo(log, emit, "Verificação na caixa", None,
                       "reenvio — dispensada (trâmite originário já concluído)")
            else:
                ids_norm = [i for i in ids if str(i).strip()]
                fora = await _tramites_fora_da_caixa(ids_norm)
                if ids_norm and fora and len(fora) == len(ids_norm):
                    previa = _conclusao_previa(ids_norm)
                    if previa:
                        _passo(log, emit, "Verificação na caixa", None,
                               "trâmite já concluído por este painel (" + _detalhe_ja_concluido(previa)
                               + ") — seguindo com o envio por este meio", nivel="aviso")
                    else:
                        _passo(log, emit, "Verificação na caixa", None,
                               _ALERTA_FORA_CAIXA_EXTERNO, nivel="aviso")
        # ENVIO por NÚMERO: dispara para CADA número cadastrado (pode haver vários).
        # Falha em um número NÃO impede os demais — a falha fica registrada só no LOG.
        # Entre um número e outro do mesmo PAJ, espera ~70% do intervalo configurado
        # (varia o timing para parecer mais humano). `envios` = 1 dict por número.
        envios: list[dict] = []
        if modo == "simulado":
            await asyncio.sleep(0.4)
            for numero in numeros:
                envios.append({"numero": numero, "ok": True, "status": "simulado",
                               "detalhe": f"[simulado] WhatsApp para {numero} · {msg_prazo}",
                               "comprovante": None, "comprovante_nome": ""})
                _passo(log, emit, f"Envio da mensagem (WhatsApp) → {numero}", True,
                       "[simulado] não enviado de verdade")
        else:
            if headless is None:
                headless = config_service.get_whatsapp_headless()
            intervalo = int(config_service.get_whatsapp_intervalo_seg() * 0.7)
            for i, numero in enumerate(numeros):
                if i > 0 and intervalo > 0:
                    if emit is not None:
                        emit({"level": "muted", "msg": f"     aguardando {intervalo}s antes do próximo número…"})
                    await asyncio.sleep(intervalo)
                if emit is not None:
                    emit({"level": "muted", "msg": f"     abrindo a conversa e enviando a mensagem a {numero}…"})
                r1 = await whatsapp_client.enviar_mensagem(
                    numero=numero, mensagem=mensagem, anexos=anexos_ctx,
                    headless=headless, comprovante_dir=str(COMPROVANTES_WA_DIR), paj_ref=paj_norm,
                    mensagem_prazo=msg_prazo,
                    mensagem_referencia=(f"Contato referente ao PAJ {paj}" if paj else ""))
                comp = r1.get("comprovante")
                envios.append({"numero": numero, "numero_confirmado": r1.get("numero_confirmado") or "",
                               "ok": r1.get("ok"),
                               "status": r1.get("status", "falha"), "detalhe": r1.get("detalhe", ""),
                               "comprovante": comp, "comprovante_nome": Path(comp).name if comp else ""})
                _passo(log, emit, f"Envio da mensagem (WhatsApp) → {numero}",
                       r1.get("ok"), r1.get("detalhe", ""))

        oks = [e for e in envios if e.get("ok")]
        fails = [e for e in envios if not e.get("ok")]

        # NENHUM número deu certo → mantém o item na fila como falha (rede de segurança).
        if not oks:
            det = "; ".join(f"{e['numero']}: {e['detalhe']}" for e in fails) or "falha no envio"
            box2 = _ler_box(slug, paj_norm)
            alvo = _achar_alvo(box2)
            if alvo is not None:
                e = alvo.get("envio") or {}
                e["falhas"] = {"whatsapp": det}
                e["falha_em"] = _dt.datetime.now().isoformat(timespec="seconds")
                alvo["envio"] = e
                _gravar_box(slug, box2)
            return {"ok": False, "status": "falha", "detalhe": det}

        # PELO MENOS um número deu certo → conclui normalmente com os bem-sucedidos.
        # Os que falharam ficam SÓ no log (não voltam à fila, decisão do usuário).
        for e in fails:
            _passo(log, emit, f"Falha no envio → {e['numero']} (registrada apenas no log)",
                   False, e["detalhe"])

        numeros_ok = [e["numero"] for e in oks]
        # Número no formato RECONHECIDO PELO WHATSAPP (ground truth, lido do painel
        # "Dados do contato" no momento do envio — ver whatsapp_client._capturar_contato_jpeg)
        # — mais confiável que o número cadastrado para reabrir a conversa depois
        # (pedido do usuário, 2026-10-28). Cai para o cadastrado quando não extraiu.
        numeros_confirmados_ok = [e.get("numero_confirmado") or e["numero"] for e in oks]
        comprovantes = [{"nome": e["comprovante_nome"], "caminho": e["comprovante"]}
                        for e in oks if e.get("comprovante")]
        # p/ histórico/UI: nome do PDF associado a cada número contatado
        comprovantes_reg = [{"nome": e["comprovante_nome"], "numero": e["numero"]}
                            for e in oks if e.get("comprovante_nome")]
        comps_nomes = [c["nome"] for c in comprovantes_reg]
        status_geral = "enviado" if (not fails and modo != "simulado") else (
            "simulado" if modo == "simulado" else "parcial")
        detalhe_geral = f"WhatsApp para {', '.join(numeros_ok)}" + (
            f" · falha em {', '.join(e['numero'] for e in fails)}" if fails else "")
        res = {"ok": True, "status": status_geral, "detalhe": detalhe_geral}

        if modo != "simulado":
            _passo(log, emit, "Comprovantes de envio (PDF)", bool(comprovantes),
                   (f"{len(comprovantes)} comprovante(s) — um por número"
                    if comprovantes else "não foi possível gerar comprovante"))

        # PIPELINE: movimenta (documenta o envio) e, se escolhido, tramita — só em modo
        # REAL (o SISDPU é sempre real; em simulado não mexemos no PAJ). Movimentação
        # ÚNICA, listando todos os números contatados + um comprovante para cada.
        if modo != "simulado":
            mov_tram = await _movimentar_e_tramitar(
                t, "WhatsApp", mensagem, anexos_meta, comprovantes, tramitar_grupo, log,
                numeros=numeros_ok, prazo=prazo_wa, anexos_arquivos=anexos_ctx, emit=emit)
        else:
            mov_tram = {"mov_seq": None, "mov_status": "nao_aplicavel", "mov_detalhe": "",
                        "tram_caixa": "", "tram_status": "nao_solicitada", "tram_detalhe": "",
                        "concluido_status": "nao_aplicavel", "concluido_detalhe": ""}
            _passo(log, emit, "Movimentação/Tramitação/Conclusão", None, "modo simulado — não executadas")

        envio_id = uuid.uuid4().hex[:12]
        reg = historico_service.registrar({
            "envio_id": envio_id,
            "paj_norm": paj_norm, "paj": paj, "assistido": assistido,
            "oficio": t.get("oficio", ""),
            "id_tramite": (ids[0] if len(ids) == 1 else ""), "tramite_ids": ids,
            "conjunto": conjunto, "tramite_data": t.get("data_envio", ""),
            "tramites_info": _tramites_info(t, conjunto),
            "tipo": t.get("sugestao_tipo", ""),
            "caixa_slug": slug, "caixa_nome": caixa["nome"], "grupo": caixa["grupo"],
            "meio": "whatsapp", "meios": ["whatsapp"],
            "prazo": prazo_wa,
            "mensagem": mensagem, "anexos": anexos_meta,
            "anexo_tipo": tipo, "anexo_ref": ref_anexo,
            "url_detalhe": t.get("url_detalhe", ""),
            "id_processo": t.get("id", ""),          # p/ "refazer" mov/tram
            "tram_grupo": tramitar_grupo,            # caixa PRETENDIDA (mesmo se falhar)
            "mov_descricao": mov_tram.get("mov_descricao", ""),  # p/ "refazer" a movimentação
            # status "reenviado" (laranja) quando o envio veio do botão Reenviar
            "status": ("reenviado" if (t.get("reenvio") and res.get("status") in ("enviado", "simulado"))
                       else res.get("status", "enviado")),
            "detalhe": res.get("detalhe", ""), "reenvio": t.get("reenvio") or None,
            # comprovante(s) — 1 PDF por número contatado (em dados/_comprovantes_whatsapp/).
            # `comprovante` = 1º (retrocompat); `comprovantes` = lista [{nome, numero}].
            "comprovante": (comps_nomes[0] if comps_nomes else ""),
            "comprovantes": comprovantes_reg,
            "numeros": numeros_ok,
            "numeros_confirmados": numeros_confirmados_ok,
            # documentação no PAJ (movimentação + tramitação opcional) + log individual
            "mov_seq": mov_tram["mov_seq"], "mov_status": mov_tram["mov_status"],
            "mov_detalhe": mov_tram["mov_detalhe"], "mov_url": mov_tram.get("mov_url", ""),
            "tram_caixa": mov_tram["tram_caixa"], "tram_status": mov_tram["tram_status"],
            "tram_detalhe": mov_tram["tram_detalhe"], "tram_url": mov_tram.get("tram_url", ""),
            "tram_descricao": mov_tram.get("tram_descricao", ""),  # tooltip da coluna "Tramitado"
            # id do trâmite criado na caixa destino, capturado no próprio envio — usado
            # pela Central de Confirmações de leitura p/ localizá-lo sem ambiguidade de
            # PAJ (ex.: reenvio antes da confirmação do 1º). "" em registros antigos.
            "tram_id_tramite": mov_tram.get("tram_id_tramite", ""),
            # conclusão (admissão + baixa) do(s) trâmite(s) ORIGINÁRIO(S)
            "concluido_status": mov_tram.get("concluido_status", "nao_realizada"),
            "concluido_detalhe": mov_tram.get("concluido_detalhe", ""),
            "log": log,
            "respondido": False, "respondido_em": "", "baixado": False, "baixado_em": "", "obs": "",
        })
        box2 = _ler_box(slug, paj_norm)
        alvo = _achar_alvo(box2)
        if alvo is not None:
            entry = {"em": reg["ts"], "meios": ["whatsapp"], "mensagem": mensagem,
                     "status": res.get("status", "enviado"), "envio_id": envio_id,
                     "historico_ids": [reg["id"]], "anexos": anexos_meta,
                     "comprovante": (comps_nomes[0] if comps_nomes else ""),
                     "comprovantes": comprovantes_reg, "numeros": numeros_ok,
                     "mov_seq": mov_tram["mov_seq"], "tram_caixa": mov_tram["tram_caixa"]}
            if conjunto:
                entry["tramite_ids"] = ids
                # carimba também cada tramitação-membro (sobrevive ao desfazer do
                # conjunto — impede reviver tramitação já contatada em conjunto).
                _marcar_membros_conjunto(box2, ids, entry)
            alvo.setdefault("enviados", []).append(entry)
            # remove SÓ o WhatsApp da fila; preserva outros meios (app/email) pendentes.
            e2 = alvo.get("envio") or {}
            restantes = [m for m in (e2.get("meios") or []) if m != "whatsapp"]
            if restantes:
                e2["meios"] = restantes
                alvo["envio"] = e2
            else:
                alvo["envio"] = {}
            _gravar_box(slug, box2)
        return {**res, "comprovante_nome": (comps_nomes[0] if comps_nomes else ""),
                "comprovantes": comprovantes_reg, "historico_id": reg["id"]}


# ==================== REFAZER movimentação/tramitação (recuperação de falha) ====================
def _id_processo_do_registro(rec: dict) -> str:
    """id de processo do registro do histórico (campo direto ou extraído da url_detalhe)."""
    idp = rec.get("id_processo") or ""
    if idp:
        return str(idp)
    m = re.search(r"[?&]id=(\d+)", rec.get("url_detalhe", "") or "")
    return m.group(1) if m else ""


def _mov_anexos_do_registro(rec: dict) -> list[dict]:
    """Reconstrói os anexos da movimentação (documentos da mensagem + comprovante) a
    partir do registro do histórico, para o 'refazer'."""
    if rec.get("meio") == "carta":
        # Carta usa esquema PRÓPRIO (nomes fixos, pasta de Correios) — NÃO o de
        # anexos/comprovantes do WhatsApp/App tratado abaixo.
        itens: list[dict] = []
        if rec.get("carta_pdf"):
            p = COMPROVANTES_CARTA_DIR / rec["carta_pdf"]
            if p.exists():
                itens.append({"nome": "Carta.pdf", "caminho": str(p), "tipo": "pdf"})
        if rec.get("comprovante"):
            p = COMPROVANTES_CARTA_DIR / rec["comprovante"]
            if p.exists():
                itens.append({"nome": "Comprovante da postagem.pdf", "caminho": str(p), "tipo": "pdf"})
        return itens
    itens: list[dict] = []
    for a in (rec.get("anexos") or []):
        cam = caminho_anexo(rec.get("paj_norm", ""), rec.get("anexo_tipo", "tramite"),
                            rec.get("anexo_ref", ""), a.get("arquivo", ""))
        if cam and Path(cam).exists():
            itens.append({"nome": a.get("nome") or a.get("arquivo") or Path(cam).name,
                          "caminho": str(cam), "tipo": a.get("tipo") or ""})
    # Comprovante(s): 1 por número contatado. Registros novos têm `comprovantes`
    # [{nome, numero}]; os antigos têm só `comprovante` (string única).
    comps = [c.get("nome") for c in (rec.get("comprovantes") or []) if c.get("nome")]
    if not comps and rec.get("comprovante"):
        comps = [rec["comprovante"]]
    for comp in comps:
        p = COMPROVANTES_WA_DIR / comp
        if p.exists():
            itens.append({"nome": comp, "caminho": str(p), "tipo": "pdf"})
    return itens


async def refazer_movimentacao(historico_id: str) -> dict:
    """Refaz a MOVIMENTAÇÃO de um registro do histórico cuja movimentação falhou.
    Checa conflito de sessão antes; em sucesso, atualiza o registro (mov_status/seq/url)."""
    from services import historico_service
    from ingestao.sisdpu_client import SISDPU_URL
    rec = historico_service.obter(historico_id)
    if not rec:
        return {"ok": False, "erro": "Registro não encontrado."}
    log_novo = list(rec.get("log") or [])
    _log_passo(log_novo, "Refazer movimentação — solicitado", None, "reprocessamento manual da movimentação")
    id_proc = _id_processo_do_registro(rec)
    paj = rec.get("paj", "")
    if not id_proc:
        _log_passo(log_novo, "Refazer movimentação", False, "PAJ sem id de processo no registro")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "PAJ sem id de processo no registro."}
    conf = await sisdpu_client.checar_conflito_sessao(id_proc)
    if conf.get("conflito"):
        _log_passo(log_novo, "Refazer — checagem de sessão", False, "conflito detectado — refazimento abortado")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Conflito de sessão do SISDPU — feche a outra sessão e tente de novo."}
    _log_passo(log_novo, "Refazer — checagem de sessão", True, "sem conflito de sessão")
    # Usa a descrição EXATA da movimentação original (guardada no registro). Registros
    # antigos sem `mov_descricao`: reconstrói idêntica com o mesmo template + dados do
    # registro (nº de WhatsApp vem do cadastro do PAJ).
    descricao = rec.get("mov_descricao") or ""
    if not descricao:
        _meios = {"whatsapp": "WhatsApp", "app_sisdpu": "App SISDPU", "email": "E-mail"}
        numeros = rec.get("numeros") or ler_contato(rec.get("paj_norm", "")).get("whatsapp_numeros") or [""]
        nomes = [a.get("nome", "") for a in (rec.get("anexos") or []) if a.get("nome")]
        descricao = _montar_descricao_mov(
            _meios.get(rec.get("meio", ""), "WhatsApp"), rec.get("mensagem", ""), numeros,
            rec.get("prazo"), nomes, rec.get("tram_grupo") or "")
    elif rec.get("meio") == "carta" and rec.get("lote") and rec.get("previsao_entrega"):
        # A movimentação original pode ter sido registrada ANTES do comprovante ser
        # capturado (Lote/Previsão em branco no texto guardado) — se já sabemos os
        # valores agora (ex.: recuperados depois via /recuperar-comprovantes), atualiza
        # o texto antes de refazer, para o registro do PAJ sair correto.
        import html as _html
        e = _html.escape
        descricao = re.sub(r"(Lote/Mensagem SMT:\s*<strong>)[^<]*(</strong>)",
                           rf"\g<1>{e(str(rec['lote']))}\g<2>", descricao)
        descricao = re.sub(r"(Previsão de entrega:\s*)[^<]*(<)",
                           rf"\g<1>{e(str(rec['previsao_entrega']))}\g<2>", descricao)
    # Carta usa a fase específica "Ofício/Carta/Comunicação expedida"; demais meios
    # usam o padrão (_FASE_MENSAGEM_ASSISTIDO) já assumido por movimentar_paj.
    fase_kw = {"fase": _FASE_CARTA_EXPEDIDA} if rec.get("meio") == "carta" else {}
    mov_anexos = _mov_anexos_do_registro(rec)
    if not _pode_exclusivo():
        _log_passo(log_novo, "Refazer movimentação", None, "operação em andamento — tente de novo")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Há uma operação em andamento. Aguarde e tente de novo."}
    async with _entrar_exclusivo_fila():
        try:
            mov = await sisdpu_client.movimentar_paj(
                [{"id": id_proc, "numero": paj}], descricao, anexos=mov_anexos,
                descricao_html=True, **fase_kw)
        except Exception as e:  # noqa: BLE001
            mov = {"ok": False, "detalhe": f"exceção: {e}", "mov_seq": None}
    _log_passo(log_novo, "Refazer movimentação do PAJ", mov.get("ok"), mov.get("detalhe", ""))
    if mov.get("ok"):
        historico_service.atualizar(historico_id, {
            "mov_status": "movimentado", "mov_seq": mov.get("mov_seq"),
            "mov_detalhe": mov.get("detalhe", ""),
            "mov_url": f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_proc}",
            "log": log_novo,
        })
        return {"ok": True, "mov_seq": mov.get("mov_seq")}
    historico_service.atualizar(historico_id, {"mov_status": "falha",
                                               "mov_detalhe": mov.get("detalhe", ""), "log": log_novo})
    return {"ok": False, "erro": mov.get("detalhe", "Falha na movimentação.")}


async def refazer_tramitacao(historico_id: str) -> dict:
    """Refaz a TRAMITAÇÃO de um registro cujo trâmite falhou/não foi realizado. Exige
    movimentação bem-sucedida antes (regra). Checa conflito; em sucesso, atualiza o registro."""
    from services import historico_service
    from ingestao.sisdpu_client import SISDPU_URL
    rec = historico_service.obter(historico_id)
    if not rec:
        return {"ok": False, "erro": "Registro não encontrado."}
    log_novo = list(rec.get("log") or [])
    _log_passo(log_novo, "Refazer tramitação — solicitado", None, "reprocessamento manual da tramitação")
    id_proc = _id_processo_do_registro(rec)
    paj = rec.get("paj", "")
    # Carta: a caixa de destino é a CONFIGURADA (não escolhida por envio) — não fica
    # gravada em tram_grupo/tram_caixa enquanto a tramitação não tiver sido feita ainda.
    grupo = (config_service.get_carta_caixa_tramitacao() if rec.get("meio") == "carta"
             else (rec.get("tram_grupo") or rec.get("tram_caixa") or ""))
    if not id_proc:
        _log_passo(log_novo, "Refazer tramitação", False, "PAJ sem id de processo no registro")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "PAJ sem id de processo no registro."}
    if not grupo:
        _log_passo(log_novo, "Refazer tramitação", False, "nenhuma caixa de tramitação escolhida neste envio")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Nenhuma caixa de tramitação foi escolhida neste envio."}
    if rec.get("mov_status") != "movimentado" and not rec.get("mov_seq"):
        _log_passo(log_novo, "Refazer tramitação", False, "movimentação ainda não concluída — refaça-a antes")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Faça a movimentação (refazer) antes de tramitar."}
    conf = await sisdpu_client.checar_conflito_sessao(id_proc)
    if conf.get("conflito"):
        _log_passo(log_novo, "Refazer — checagem de sessão", False, "conflito detectado — refazimento abortado")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Conflito de sessão do SISDPU — feche a outra sessão e tente de novo."}
    _log_passo(log_novo, "Refazer — checagem de sessão", True, "sem conflito de sessão")
    if not _pode_exclusivo():
        _log_passo(log_novo, "Refazer tramitação", None, "operação em andamento — tente de novo")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Há uma operação em andamento. Aguarde e tente de novo."}
    if rec.get("meio") == "carta":
        # Prazo da carta = PREVISÃO DE ENTREGA (Correios) + N dias (config) — base
        # diferente da tramitação genérica (que conta a partir de HOJE).
        dias = config_service.get_carta_prazo_pos_entrega()
        base = _parse_data_br(rec.get("previsao_entrega") or "") or _dt.date.today()
        prazo_data = (base + _dt.timedelta(days=dias)).strftime("%d/%m/%Y")
        tram_desc = f"Carta expedida. Aguardar retorno até {prazo_data}."
        prazo_desc = tram_desc
        prazo_dias = dias
    else:
        tram_desc = _descricao_tramitacao(rec.get("mov_seq"))
        prazo_data, prazo_desc, prazo_dias = _prazo_tramitacao(rec.get("prazo"))
    async with _entrar_exclusivo_fila():
        try:
            tram = await sisdpu_client.tramitar_paj(
                [{"id": id_proc, "numero": paj}], grupo=grupo,
                descricao=tram_desc, prazo_data=prazo_data, prazo_desc=prazo_desc)
        except Exception as e:  # noqa: BLE001
            tram = {"ok": False, "detalhe": f"exceção: {e}"}
    _log_passo(log_novo, "Refazer tramitação do PAJ", tram.get("ok"),
               (f'realizada para a caixa "{grupo}" · prazo {prazo_dias}d' if tram.get("ok")
                else f'falhou para a caixa "{grupo}": {tram.get("detalhe", "")}'))
    if tram.get("ok"):
        historico_service.atualizar(historico_id, {
            "tram_status": "tramitado", "tram_caixa": grupo, "tram_detalhe": tram.get("detalhe", ""),
            "tram_url": f"{SISDPU_URL}/pages/tramite/historicoTramitacao.xhtml?id={id_proc}&caixaEntrada=false",
            "tram_descricao": tram_desc,
            "log": log_novo,
        })
        return {"ok": True, "tram_caixa": grupo}
    historico_service.atualizar(historico_id, {"tram_status": "falha",
                                               "tram_detalhe": tram.get("detalhe", ""), "log": log_novo})
    return {"ok": False, "erro": tram.get("detalhe", "Falha na tramitação.")}


async def refazer_conclusao(historico_id: str) -> dict:
    """Refaz a CONCLUSÃO (admissão + baixa) do(s) trâmite(s) ORIGINÁRIO(S) — os
    sincronizados que geraram o envio — de um registro cuja conclusão falhou/não foi
    realizada. Checa conflito; atualiza o registro (concluido_status/detalhe + log)."""
    from services import historico_service
    rec = historico_service.obter(historico_id)
    if not rec:
        return {"ok": False, "erro": "Registro não encontrado."}
    log_novo = list(rec.get("log") or [])
    _log_passo(log_novo, "Refazer conclusão — solicitado", None, "reprocessamento manual da conclusão")
    ids = _ids_originarios(rec)
    id_proc = _id_processo_do_registro(rec)
    if not ids:
        _log_passo(log_novo, "Refazer conclusão", False, "registro sem id de trâmite originário")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Registro sem id de trâmite originário para concluir."}
    conf = await sisdpu_client.checar_conflito_sessao(id_proc or None)
    if conf.get("conflito"):
        _log_passo(log_novo, "Refazer — checagem de sessão", False, "conflito detectado — refazimento abortado")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Conflito de sessão do SISDPU — feche a outra sessão e tente de novo."}
    _log_passo(log_novo, "Refazer — checagem de sessão", True, "sem conflito de sessão")
    if not _pode_exclusivo():
        _log_passo(log_novo, "Refazer conclusão", None, "operação em andamento — tente de novo")
        historico_service.atualizar(historico_id, {"log": log_novo})
        return {"ok": False, "erro": "Há uma operação em andamento. Aguarde e tente de novo."}
    async with _entrar_exclusivo_fila():
        try:
            conc = await sisdpu_client.concluir_paj(ids)
        except Exception as e:  # noqa: BLE001
            conc = {"ok": False, "detalhe": f"exceção: {e}"}
    ok = bool(conc.get("ok"))
    _log_passo(log_novo, "Refazer conclusão do trâmite originário", ok, conc.get("detalhe", ""))
    historico_service.atualizar(historico_id, {
        "concluido_status": "concluido" if ok else "falha",
        "concluido_detalhe": conc.get("detalhe", ""),
        "log": log_novo,
    })
    return {"ok": True} if ok else {"ok": False, "erro": conc.get("detalhe", "Falha na conclusão.")}


def _reconstruir_solic_do_registro(rec: dict) -> dict:
    """Monta uma solicitação a partir de um REGISTRO do histórico, para reenviar o
    EVENTO ORIGINAL (MESMO trâmite/mensagem) mesmo que o trâmite já tenha sido concluído
    e saído do box. Preserva o `id_tramite` ORIGINAL do registro — o reenvio NÃO se ancora
    em trâmites novos que tenham chegado depois (esses são outros assuntos, não tratados)."""
    return {
        "id_tramite": rec.get("id_tramite", ""),
        "id": rec.get("id_processo", "") or "",
        "oficio_origem": rec.get("oficio", ""),
        "remetente": "",
        "data_envio": rec.get("tramite_data", ""),
        "prazo": "",
        "url_detalhe": rec.get("url_detalhe", ""),
        "descricao_tramite": rec.get("descricao_tramite", ""),
        "atribuicao": "",
        "resumo_ia": "", "resumo_ia_chave": "", "sugestao_ia": "",
        "sugestao_tipo": rec.get("tipo", ""), "sugestao_tipo_editada": "",
        "sugestao_editada": "",
        "mov_seq": "",
        "anexos": rec.get("anexos", []) or [],
        "envio": {},
        "enviados": [],
    }


def reenviar_do_historico(historico_id: str, meios: list[str]) -> dict:
    """Recoloca na fila de envio (Prontos) o EVENTO do histórico, para contatar por um
    meio ADICIONAL. Ater-se ao TRÂMITE ORIGINÁRIO do registro (mesmo id/mensagem) — NÃO
    usa trâmites novos que tenham chegado depois. Se o trâmite original já saiu do box
    (concluído/sincronizado), a solicitação é RECONSTRUÍDA a partir do registro e
    reinjetada, preservando o id original. A conclusão, se já feita, é pulada no envio.
    (Conjunto: exige que o conjunto ainda esteja no box.)"""
    from services import historico_service, envio_service
    rec = historico_service.obter(historico_id)
    if not rec:
        return {"ok": False, "erro": "Registro não encontrado."}
    meios = [m for m in (meios or []) if m in envio_service.MEIOS]
    if not meios:
        return {"ok": False, "erro": "Selecione ao menos um meio para reenviar."}
    slug = rec.get("caixa_slug", "")
    paj_norm = rec.get("paj_norm", "")
    if not (slug and paj_norm):
        return {"ok": False, "erro": "Registro sem caixa/PAJ — não é possível reenviar."}
    mensagem = rec.get("mensagem", "")
    prazo = rec.get("prazo", 5)
    prazos = {m: prazo for m in ("app_sisdpu", "whatsapp", "email")}
    tramitar_grupo = rec.get("tram_grupo", "") or ""
    somente_leitura = bool(rec.get("somente_leitura"))

    # selo de REENVIO: marca a origem (registro, quando, meio original) para o envio
    # gravar status "reenviado" e o log abrir com "Reenvio solicitado".
    marca = {"de": historico_id, "meio_orig": rec.get("meio", ""),
             "em": _dt.datetime.now().isoformat(timespec="seconds"),
             "evento_data": (rec.get("ts", "") or "")[:16].replace("T", " ")}
    if bool(rec.get("conjunto")):
        box = _ler_box(slug, paj_norm)
        if not (box and box.get("conjunto")):
            return {"ok": False, "erro": ("O conjunto deste envio não está mais na caixa — não é "
                                          "possível reenviar automaticamente. Trate pela caixa de origem.")}
        box["conjunto"]["reenvio"] = marca
        _gravar_box(slug, box)
        gravar_conjunto(slug, paj_norm, mensagem, meios, somente_leitura, prazos, tramitar_grupo)
    else:
        idt = rec.get("id_tramite", "")
        if not idt:
            return {"ok": False, "erro": "Registro sem id de trâmite originário — não é possível reenviar."}
        box = _ler_box(slug, paj_norm) or {"paj_norm": paj_norm, "paj": rec.get("paj", ""),
                                           "em_caixa": False, "solicitacoes": []}
        box.setdefault("solicitacoes", [])
        # reinjeta a solicitação do trâmite ORIGINAL (se não estiver mais no box), sem
        # mexer nos demais trâmites do PAJ (ex.: trâmites novos ainda não tratados)
        if not _achar_solic(box, idt):
            box["solicitacoes"].append(_reconstruir_solic_do_registro(rec))
        _achar_solic(box, idt)["reenvio"] = marca  # marca o selo de reenvio na solicitação
        _gravar_box(slug, box)
        gravar_para_envio(slug, paj_norm, idt, mensagem, meios, somente_leitura, prazos, tramitar_grupo)
    labels = ", ".join(_MEIO_LABEL.get(m, m) for m in meios)
    return {"ok": True, "meios": meios, "slug": slug,
            "detalhe": f"Recolocado na fila (trâmite originário) para: {labels}."}


async def enviar_whatsapp_lote_stream(selecao: list[dict]):
    """Envio em LOTE por WhatsApp (assistido, 1 clique → dispara vários), com
    INTERVALO entre mensagens (ritmo humano, reduz risco de ban). `selecao`: lista
    de {slug, paj_norm, id_tramite, paj?}. Emite eventos SSE; reaproveita
    `enviar_whatsapp_tramite` (que trava/valida/gera comprovante+histórico por item).
    Erros não param o lote (o item fica na fila com a falha)."""
    itens = [s for s in (selecao or []) if s.get("slug") and s.get("paj_norm")
             and (s.get("id_tramite") or s.get("tipo") == "conjunto")]
    if not itens:
        yield {"level": "info", "msg": "Nada a enviar (seleção vazia)."}
        yield {"done": True, "resumo": {"ok": True, "enviados": 0, "falhas": 0}}
        return
    intervalo = config_service.get_whatsapp_intervalo_seg()
    t0 = time.monotonic()
    yield {"level": "info",
           "msg": f"Enviando {len(itens)} WhatsApp — intervalo de {intervalo}s entre mensagens."}
    enviados = falhas = 0
    for i, it in enumerate(itens, 1):
        if i > 1:
            yield _sep()
            yield {"level": "info", "msg": f"aguardando {intervalo}s antes da próxima…"}
            yield _sep()
            await asyncio.sleep(intervalo)
        paj_lbl = it.get("paj") or it.get("paj_norm")
        yield {"level": "novo", "msg": f"[{i}/{len(itens)}] {paj_lbl} — abrindo conversa…"}
        res = {"ok": False, "detalhe": "falha"}
        n_passos = 0
        try:
            async for ev in _drenar_com_passos(lambda emit: enviar_whatsapp_tramite(
                    it["slug"], it["paj_norm"], it.get("id_tramite", ""),
                    it.get("tipo", "tramite"), emit=emit)):
                if "_result" in ev:
                    res = ev["_result"] or {"ok": False, "detalhe": "sem retorno"}
                else:
                    n_passos += 1
                    yield ev
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "detalhe": str(e)}
        if res.get("ok"):
            enviados += 1
            # sucesso: as etapas (envio → comprovante → movimentação → tramitação) já
            # foram transmitidas uma a uma; não repetir um "✓ resumo" redundante aqui.
        else:
            falhas += 1
            # só sinaliza ✗ se NENHUMA etapa foi emitida (falhas que retornam cedo, ex.:
            # "PAJ sem número de WhatsApp"); se houve etapa, ela já mostrou o erro.
            if not n_passos:
                yield {"level": "erro", "msg": f"     ✗ {res.get('detalhe') or res.get('erro') or 'falha'}"}
    dur = time.monotonic() - t0
    yield _sep()
    yield {"level": "ok",
           "msg": f"Concluído em {dur:.0f}s — {enviados} enviado(s), {falhas} com falha."}
    yield {"done": True, "resumo": {"ok": True, "enviados": enviados, "falhas": falhas,
                                    "duracao": round(dur, 1)}}


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
                    elif ev["_status"] == "pulado":
                        pass  # WhatsApp fora do lote — não conta como enviado nem falha
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
                    elif ev["_status"] == "pulado":
                        pass  # WhatsApp fora do lote — não conta como enviado nem falha
                    else:
                        enviados += 1
                else:
                    yield ev
        dur = time.monotonic() - t0
        yield {"level": "ok",
               "msg": f"Concluído em {dur:.0f}s — {enviados} enviado(s), {falhas} com falha."}
        yield {"done": True, "resumo": {"ok": True, "enviados": enviados, "falhas": falhas,
                                        "modo": modo, "duracao": round(dur, 1)}}


async def enviar_item_meio_stream(slug: str, paj_norm: str, tipo: str, id_tramite: str, meio: str):
    """Versão STREAMING do envio individual por UM meio (App/E-mail): yielda os passos
    (envio → movimentação → tramitação) em TEMPO REAL para o popup e, por último,
    {"done": True, "resultado": {ok,status,detalhe}}."""
    def _fim(detalhe, status="falha", ok=False):
        return {"done": True, "resultado": {"ok": ok, "status": status, "detalhe": detalhe}}

    caixa = config_service.get_caixa(slug)
    if not caixa:
        yield _fim("Caixa desconhecida."); return
    it = _achar_item_fila(listar_fila(slug), paj_norm, tipo or "tramite", id_tramite or "")
    if not it:
        yield _fim("Item não está na fila."); return
    if meio not in (it.get("envio_meios") or []):
        yield _fim("Meio não pendente neste item."); return
    if not _pode_exclusivo():
        yield _fim("Há uma operação em andamento (sincronização/envio). Aguarde."); return
    modo = config_service.get_envio_modo()
    status, detalhe = "falha", ""
    async with _sync_lock:
        async for ev in _enviar_item_stream(slug, caixa, it, modo, meio_unico=meio):
            if "_status" in ev:
                status = ev["_status"]
            else:
                if ev.get("level") in ("ok", "erro"):
                    detalhe = ev.get("msg", "").strip()
                yield ev
    yield {"done": True, "resultado": {"ok": status in ("enviado", "simulado", "parcial"),
                                       "status": status, "detalhe": detalhe or status}}


async def enviar_item_meio(slug: str, paj_norm: str, tipo: str, id_tramite: str, meio: str) -> dict:
    """Envio INDIVIDUAL de UM item por UM meio (App/E-mail). Bloqueia até concluir.
    Wrapper NÃO-streaming sobre `enviar_item_meio_stream` (mantém a API antiga)."""
    resultado = {"ok": False, "status": "falha", "detalhe": "falha"}
    async for ev in enviar_item_meio_stream(slug, paj_norm, tipo, id_tramite, meio):
        if ev.get("done"):
            resultado = ev["resultado"]
    return resultado


async def enviar_whatsapp_tramite_stream(slug: str, paj_norm: str, id_tramite: str,
                                         tipo: str = "tramite"):
    """Versão STREAMING do envio individual por WhatsApp: yielda os passos (envio →
    comprovante → movimentação → tramitação) em TEMPO REAL e, por último,
    {"done": True, "resultado": <dict de enviar_whatsapp_tramite>}."""
    async for ev in _drenar_com_passos(lambda emit: enviar_whatsapp_tramite(
            slug, paj_norm, id_tramite, tipo, emit=emit)):
        if "_result" in ev:
            yield {"done": True, "resultado": ev["_result"] or {"ok": False, "detalhe": "sem retorno"}}
        else:
            yield ev


# ==================== reset do painel ====================
def resetar_painel(total: bool) -> dict:
    """Reseta o painel. Move os dados OPERACIONAIS (PAJs/tramitações por caixa,
    contatos e histórico) para uma lixeira (reversível). Se `total`, também
    restaura o config ao padrão de fábrica (config_painel_comunicacoes.json.bak é salvo antes).
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
_migrar_para_tramite()
