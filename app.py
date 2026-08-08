"""Painel de Comunicações — gestao das caixas de entrada de grupo do SISDPU.

Painel web para SERVIDORES da DPU: lista, para as caixas de grupo escolhidas, os
PAJs que aguardam contato com o assistido, a providencia necessaria em cada um e
o status do contato (por Aplicativo SISDPU e WhatsApp).

Derivado do PAINEL SISDPU (do defensor); roda na porta 8002 para coexistir.
Auto-gerencia processo: mata qualquer python na porta antes de iniciar,
grava PID file. Evita duplicatas.
"""

import contextlib
import datetime as _dt
import logging
import os
import re
import subprocess
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import UNIDADE_DESCRICAO
from routes.configuracoes import router as configuracoes_router
from routes.dashboard import router as dashboard_router
from routes.historico import router as historico_router
from routes.ligacoes import router as ligacoes_router
from routes.seguranca import router as seguranca_router

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".server.pid"
LOGS_DIR = BASE_DIR / "logs"
# PAINEL COMUNICAÇÕES roda na porta 8002 para coexistir com o PAINEL SISDPU (8001)
# na mesma maquina sem conflito de porta.
PORT = 8002


def _configurar_logging() -> None:
    """Configura logging com rotacao diaria em logs/app.log (mantem 7 dias)."""
    LOGS_DIR.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "app.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


_configurar_logging()

# Versao do painel — fonte unica. Exposta a todos os templates via Jinja globals.
APP_VERSION = "0.1.0"

app = FastAPI(title="painel-comunicacoes", version=APP_VERSION)
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)
app.state.jinja.globals["app_version"] = APP_VERSION
app.state.jinja.globals["unidade_descricao"] = UNIDADE_DESCRICAO
# Menu dinamico: caixas habilitadas + contagem de PAJs (usado pela sidebar).
from services import painel_comunicacoes_service as _ds  # noqa: E402

app.state.jinja.globals["menu_caixas"] = _ds.menu_caixas
app.state.jinja.globals["total_prontos"] = _ds.contar_prontos_envio
app.state.jinja.globals["prontos_por_meio"] = _ds.prontos_por_meio
app.state.jinja.globals["prontos_meios"] = _ds.PRONTOS_MEIOS
app.state.jinja.globals["cor_caixa"] = _ds.cor_caixa
# Contadores das centrais de Ligações telefônicas e Correspondências (badges do menu).
app.state.jinja.globals["contar_ligacoes"] = _ds.contar_ligacoes
app.state.jinja.globals["contar_correspondencias"] = _ds.contar_correspondencias
# Modo de IA (Simples × com IA) — globals CALLABLE (sempre atuais).
from services import analise_service as _analise  # noqa: E402
from services import config_service as _cs  # noqa: E402

app.state.jinja.globals["ia_ativa"] = _cs.get_ia_ativa
# Modo EFETIVO (toggle + ANALISE_ATIVA + claude instalado) — usado no selo do cabeçalho.
app.state.jinja.globals["ia_disponivel"] = _analise.disponivel
# Estado do acesso ao SISDPU — usado no selo de status do cabeçalho (conectado/usuário).
from services import credenciais_service as _cred  # noqa: E402

app.state.jinja.globals["sisdpu_destrancado"] = _cred.destrancado
app.state.jinja.globals["sisdpu_usuario"] = _cred.get_usuario
app.state.jinja.globals["sisdpu_env"] = _cred.env_tem_credenciais
app.state.jinja.globals["sisdpu_conflito"] = _cred.conexao_conflito


# Formatadores de data — datas exibidas ao usuario saem no padrao BR DD/MM/AAAA.
_RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_RE_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_RE_BR_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})")
_RE_BR_DT = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")


def _data_br(valor) -> str:
    if not valor:
        return "—"
    s = str(valor).strip()
    m = _RE_BR_DATE.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = _RE_ISO_DATE.match(s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def _data_hora_br(valor) -> str:
    if not valor:
        return "—"
    s = str(valor).strip()
    m = _RE_ISO_DT.match(s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)} {m.group(4)}:{m.group(5)}"
    m = _RE_BR_DT.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)} {m.group(4)}:{m.group(5)}"
    return _data_br(s)


app.state.jinja.filters["data_br"] = _data_br
app.state.jinja.filters["data_hora_br"] = _data_hora_br
# Data de HOJE em ISO (AAAA-MM-DD), CALLABLE p/ ser sempre atual. Usada para marcar
# "Hoje" no Log dos últimos envios (compara com o ts ISO do registro, gerado no servidor).
app.state.jinja.globals["hoje_iso"] = lambda: _dt.datetime.now().strftime("%Y-%m-%d")
# Telefone BR para exibição: "(41) 99908-2652" (usado nos selos de comprovante).
from services.texto_util import formatar_telefone_br as _tel_br  # noqa: E402

app.state.jinja.filters["tel_br"] = _tel_br
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(dashboard_router)
app.include_router(configuracoes_router)
app.include_router(historico_router)
app.include_router(ligacoes_router)
app.include_router(seguranca_router)
# Sem cadeado de navegação: o painel abre livremente. A senha-mestra só é pedida
# (via modal no front-end) quando o usuário aciona uma função que usa o SISDPU e a
# sessão ainda não está destrancada — ver garantirSisdpu() em base.html.


def _cleanup_port(port: int) -> None:
    """Mata TODOS processos LISTEN na porta informada (Windows-only)."""
    if os.name != "nt":
        return
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    except Exception:
        return
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4 or pid == os.getpid() or pid in seen:
            continue
        seen.add(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[app] killed stale process on port {port}: PID {pid}")


def _kill_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
            print(f"[app] killed previous server PID {old_pid}")
    except (ValueError, FileNotFoundError):
        pass
    with contextlib.suppress(Exception):
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    _kill_pid_file()
    _cleanup_port(PORT)
    import time

    time.sleep(1)  # aguarda SO liberar porta

    PID_FILE.write_text(str(os.getpid()))
    print(f"[app] Painel de Comunicações PID={os.getpid()} — http://127.0.0.1:{PORT}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)
    finally:
        with contextlib.suppress(Exception):
            PID_FILE.unlink(missing_ok=True)
