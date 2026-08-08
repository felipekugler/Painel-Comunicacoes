"""Log das SESSÕES de envio (o passo a passo mostrado ao vivo no modal de "Envio",
salvo para consulta posterior na tabela "Log dos últimos envios").

Cada registro = UMA operação de envio (um popup inteiro — individual OU lote), com:
  id, ts (ISO), meio (whatsapp|app_sisdpu|email|telefone|carta),
  linhas [{level, msg}]  -> exatamente as linhas exibidas no modal,
  falha (bool)           -> houve alguma falha na operação.

Registro append-only e ROTATIVO em dados/_disparos/disparos.jsonl: mantém só os
últimos MAX_REGISTROS. Serve só para consulta — não interfere no histórico nem no
fluxo de envio.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL COMUNICACOES/Projeto
DADOS_DIR = BASE_DIR.parent / "dados"
DISP_DIR = DADOS_DIR / "_disparos"
DISP_FILE = DISP_DIR / "disparos.jsonl"

MAX_REGISTROS = 300  # log rotativo: guarda só as últimas N sessões de envio


def _ler_linhas() -> list[dict]:
    if not DISP_FILE.exists():
        return []
    itens: list[dict] = []
    for linha in DISP_FILE.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        with contextlib.suppress(Exception):
            itens.append(json.loads(linha))
    return itens


def registrar_sessao(meio: str, linhas: list, falha: bool) -> dict:
    """Grava UMA sessão de envio (as linhas do log + se houve falha) e mantém só as
    últimas MAX_REGISTROS. Best-effort: o chamador deve envolver em try/except — o
    log NUNCA pode quebrar um envio."""
    DISP_DIR.mkdir(parents=True, exist_ok=True)
    linhas = list(linhas or [])
    # "aviso" = houve alguma linha de aviso (âmbar) — ex.: verificação na caixa
    # (trâmite já concluído / saiu da caixa por ação externa). Independe de falha.
    aviso = any((l or {}).get("level") == "aviso" for l in linhas)
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "meio": meio,
        "linhas": linhas,
        "falha": bool(falha),
        "aviso": aviso,
    }
    itens = _ler_linhas()
    itens.append(rec)
    if len(itens) > MAX_REGISTROS:
        itens = itens[-MAX_REGISTROS:]  # descarta as mais antigas
    with DISP_FILE.open("w", encoding="utf-8") as fh:
        for r in itens:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rec


def listar_disparos(meio: str | None = None, limite: int | None = None) -> list[dict]:
    """Sessões da mais recente para a mais antiga. Filtra por `meio` se dado e
    limita a `limite` itens (se dado)."""
    itens = _ler_linhas()
    if meio:
        itens = [r for r in itens if r.get("meio") == meio]
    itens.sort(key=lambda r: r.get("ts", ""), reverse=True)
    if limite:
        itens = itens[:limite]
    return itens


def linhas_de_log(log: list) -> list[dict]:
    """Converte os passos de um `log` interno ({ok, etapa, detalhe}) em linhas
    {level, msg} para exibição — usado pelos meios que não têm modal ao vivo
    (Ligações/Correspondências)."""
    out: list[dict] = []
    for p in (log or []):
        ok = p.get("ok")
        lvl = "ok" if ok is True else ("erro" if ok is False else "info")
        etapa, det = p.get("etapa", ""), p.get("detalhe", "")
        out.append({"level": lvl, "msg": (f"{etapa}: {det}" if det else etapa)})
    return out


async def stream_e_salvar(eventos, meio: str, erro_key: str = "resultado"):
    """Envolve um async-gen de eventos de envio: repassa cada evento JÁ FORMATADO
    como linha SSE (idêntico ao que o modal recebe), ACUMULA as linhas de log e, ao
    final, persiste UMA sessão com exatamente o que foi mostrado ao vivo. Detecta
    falha por linha de erro ou pelo `resultado/resumo` final. Best-effort — nunca
    interrompe o stream por causa do log."""
    linhas: list[dict] = []
    falha = False
    try:
        async for ev in eventos:
            if isinstance(ev, dict):
                msg = ev.get("msg")
                if msg is not None:
                    lvl = ev.get("level", "info")
                    linhas.append({"level": lvl, "msg": msg})
                    if lvl == "erro":
                        falha = True
                if ev.get("done"):
                    res = ev.get("resultado") or ev.get("resumo") or {}
                    if (res.get("ok") is False) or (str(res.get("status", "")).lower()
                            in ("falha", "parcial", "fora_da_caixa", "conflito")):
                        falha = True
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    except Exception as e:  # noqa: BLE001
        linhas.append({"level": "erro", "msg": str(e)})
        falha = True
        yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'done': True, erro_key: {'ok': False, 'detalhe': str(e)}}, ensure_ascii=False)}\n\n"
    finally:
        with contextlib.suppress(Exception):
            if linhas:
                registrar_sessao(meio, linhas, falha)
