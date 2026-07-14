"""Histórico GLOBAL de mensagens enviadas pelo Painel de Comunicações.

Registro append-only em dados/_historico/mensagens.jsonl (uma linha JSON por
mensagem enviada). Independe da caixa: reúne tudo o que foi disparado pelo
painel, para consulta/pesquisa na página "Histórico de mensagens".

Cada registro é POR MEIO (uma linha por meio disparado), para permitir filtro,
acompanhamento e baixa por canal:
  id, ts (ISO), envio_id (agrupa os meios do mesmo disparo), paj_norm, paj, oficio,
  assistido, caixa_slug, caixa_nome, grupo, tipo, conjunto, id_tramite/tramite_ids,
  mensagem, anexos [{...}], meio (str), prazo (int), somente_leitura (bool),
  status ("enviado"/"simulado"/"falha"/"nao_configurado"), detalhe,
  + campos reservados p/ baixa: respondido, respondido_em, baixado, baixado_em, obs.
Registros ANTIGOS (agregados: meios[] + resultado{}) são expandidos na leitura por
`listar_por_meio()` (o arquivo não é reescrito).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import re
import shutil
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL COMUNICAÇÕES/Projeto
DADOS_DIR = BASE_DIR.parent / "dados"
HIST_DIR = DADOS_DIR / "_historico"
HIST_FILE = HIST_DIR / "mensagens.jsonl"
BAK_FILE = HIST_DIR / "mensagens.jsonl.bak"  # backup de 1 nível (versão anterior)


def _backup() -> None:
    """Copia a versão atual do histórico para o .bak (1 nível), antes de gravar."""
    if HIST_FILE.exists():
        with contextlib.suppress(Exception):
            shutil.copy2(HIST_FILE, BAK_FILE)


def _snapshot_datado() -> None:
    """Salva uma cópia DATADA do histórico antes de uma limpeza (não é sobrescrita
    pelas gravações seguintes) — protege contra 'segunda escrita' após um erro."""
    if HIST_FILE.exists():
        with contextlib.suppress(Exception):
            ts = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
            shutil.copy2(HIST_FILE, HIST_DIR / f"mensagens-{ts}.bak.jsonl")


def registrar(record: dict) -> dict:
    """Acrescenta um registro ao histórico (gera id + ts se faltarem)."""
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    _backup()  # preserva a versão anterior antes de acrescentar
    rec = dict(record)
    rec.setdefault("id", uuid.uuid4().hex[:12])
    rec.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
    with HIST_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def obter(rec_id: str) -> dict | None:
    """Retorna o registro com o `id` dado (ou None)."""
    for r in listar():
        if r.get("id") == rec_id:
            return r
    return None


def atualizar(rec_id: str, campos: dict) -> dict | None:
    """Atualiza `campos` do registro de `id` = rec_id (reescreve o jsonl preservando a
    ordem). Retorna o registro atualizado ou None se não encontrar. Usado pelo 'refazer'
    da movimentação/tramitação."""
    if not HIST_FILE.exists():
        return None
    _backup()
    atualizado = None
    saida: list[str] = []
    for linha in HIST_FILE.read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            continue
        try:
            r = json.loads(linha)
        except Exception:  # noqa: BLE001
            saida.append(linha)
            continue
        if r.get("id") == rec_id:
            r.update(campos or {})
            atualizado = r
            saida.append(json.dumps(r, ensure_ascii=False))
        else:
            saida.append(linha)
    if atualizado is None:
        return None
    HIST_FILE.write_text("\n".join(saida) + "\n", encoding="utf-8")
    return atualizado


def listar() -> list[dict]:
    """Todos os registros, do mais recente para o mais antigo."""
    if not HIST_FILE.exists():
        return []
    itens: list[dict] = []
    for linha in HIST_FILE.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        with contextlib.suppress(Exception):
            itens.append(json.loads(linha))
    itens.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return itens


def listar_por_meio() -> list[dict]:
    """Histórico expandido: UMA linha por MEIO (para filtro/baixa por canal).

    Registros novos já são por meio (têm 'meio'). Registros ANTIGOS (agregados,
    com 'meios'[] + 'resultado'{}) são expandidos aqui SÓ para exibição — o arquivo
    não é reescrito."""
    out: list[dict] = []
    for r in listar():
        if r.get("meio"):  # já é por meio (formato novo)
            r = dict(r)
            r.setdefault("meios", [r["meio"]])
            r.setdefault("prazo", 5)
            out.append(r)
            continue
        meios = r.get("meios") or []
        resultado = r.get("resultado") or {}
        prazos = {"app_sisdpu": r.get("prazo_app") or r.get("prazo_dias") or 5,
                  "whatsapp": r.get("prazo_whatsapp") or 5,
                  "email": r.get("prazo_email") or 5}
        if not meios:
            out.append({**r, "meio": "", "meios": [], "prazo": 5})
            continue
        for m in meios:
            out.append({**r, "meio": m, "meios": [m],
                        "status": resultado.get(m, r.get("status", "")),
                        "prazo": prazos.get(m, 5),
                        "somente_leitura": r.get("somente_leitura") if m == "app_sisdpu" else False,
                        "envio_id": r.get("envio_id") or r.get("id")})
    return out


# nomes de backup aceitos: o .bak de 1 nível e os snapshots datados da limpeza
_RE_BAK = re.compile(r"^(mensagens\.jsonl\.bak|mensagens-\d{4}-\d{2}-\d{2}-\d{6}\.bak\.jsonl)$")


def _contar_arquivo(f: Path) -> int:
    with contextlib.suppress(Exception):
        return len([ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()])
    return 0


def listar_backups() -> list[dict]:
    """Lista os backups do histórico disponíveis (para restauração pelo usuário)."""
    if not HIST_DIR.exists():
        return []
    out: list[dict] = []
    for f in HIST_DIR.iterdir():
        if not f.is_file() or not _RE_BAK.match(f.name):
            continue
        m = re.match(r"mensagens-(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})\.bak\.jsonl$", f.name)
        if m:
            quando = f"{m.group(3)}/{m.group(2)}/{m.group(1)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
            rotulo = "Cópia automática (antes de limpeza ou restauração)"
            ordem = m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5) + m.group(6)
        else:
            quando = ""
            rotulo = "Última versão salva (antes da última alteração)"
            ordem = "99999999999999"  # o .bak de 1 nível fica no topo
        out.append({"nome": f.name, "quando": quando, "rotulo": rotulo,
                    "registros": _contar_arquivo(f), "_ordem": ordem})
    out.sort(key=lambda b: b["_ordem"], reverse=True)
    return out


def restaurar(nome: str) -> dict:
    """Restaura o histórico a partir de um backup. Antes de sobrescrever, salva
    um snapshot datado do estado atual (a própria restauração é reversível)."""
    if not _RE_BAK.match(nome or ""):
        return {"ok": False, "erro": "backup inválido"}
    src = HIST_DIR / nome
    if not src.exists():
        return {"ok": False, "erro": "backup não encontrado"}
    _snapshot_datado()  # preserva o que está agora, para poder desfazer
    with contextlib.suppress(Exception):
        shutil.copy2(src, HIST_FILE)
    return {"ok": True, "registros": _contar_arquivo(HIST_FILE)}


def contar_por_paj() -> dict:
    """Retorna {paj_norm: nº de MENSAGENS (disparos distintos) no histórico}.
    Com o histórico por meio, conta `envio_id` distintos (as linhas de um mesmo
    disparo, uma por meio, contam como 1); registros antigos caem no próprio id."""
    vistos: dict = {}
    for r in listar():
        k = r.get("paj_norm") or r.get("paj")
        if not k:
            continue
        vistos.setdefault(k, set()).add(r.get("envio_id") or r.get("id"))
    return {k: len(v) for k, v in vistos.items()}


_STATUS_ENVIADO = {"enviado", "simulado", "dry_run"}


def remover_falhas() -> int:
    """Remove do histórico os registros que NÃO representam envio efetivo — mantém só
    'enviado'/'simulado'/'dry_run'. Falhas ('falha'/'nao_configurado'/etc.) não têm
    utilidade no histórico (a falha fica visível/acionável em Prontos). Faz snapshot
    datado + .bak antes de reescrever. Retorna quantos foram removidos."""
    itens = listar()
    if not itens:
        return 0

    def _mantem(r: dict) -> bool:
        st = (r.get("status") or "").strip().lower()
        if st in _STATUS_ENVIADO:
            return True
        # registros antigos agregados: mantém se ALGUM meio foi efetivamente enviado
        res = r.get("resultado") or {}
        return any((str(v) or "").strip().lower() in _STATUS_ENVIADO for v in res.values())

    manter = [r for r in itens if _mantem(r)]
    removidos = len(itens) - len(manter)
    if not removidos:
        return 0
    _snapshot_datado()
    _backup()
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    if manter:
        manter.sort(key=lambda r: r.get("ts", ""))  # ordem cronológica
        with HIST_FILE.open("w", encoding="utf-8") as fh:
            for r in manter:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        with contextlib.suppress(Exception):
            HIST_FILE.unlink()
    return removidos


def limpar(modo: str = "tudo", data: str = "") -> int:
    """Apaga registros do histórico. Retorna quantos foram removidos.

    - modo="tudo": apaga tudo.
    - modo="ate": apaga os registros ANTERIORES a `data` (AAAA-MM-DD),
      mantendo os do dia informado em diante. (ts ISO permite comparação direta.)
    """
    itens = listar()
    if not itens:
        return 0
    # Antes de qualquer limpeza: snapshot datado (permanente) + .bak (1 nível).
    _snapshot_datado()
    _backup()
    if modo == "tudo":
        with contextlib.suppress(Exception):
            HIST_FILE.unlink()
        return len(itens)
    if modo == "ate":
        corte = (data or "").strip()
        if not corte:
            return 0
        manter = [r for r in itens if r.get("ts", "") >= corte]
        removidos = len(itens) - len(manter)
        if removidos:
            manter.sort(key=lambda r: r.get("ts", ""))  # volta à ordem cronológica
            HIST_DIR.mkdir(parents=True, exist_ok=True)
            with HIST_FILE.open("w", encoding="utf-8") as fh:
                for r in manter:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return removidos
    return 0
