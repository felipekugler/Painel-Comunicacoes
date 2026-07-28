"""Credenciais do SISDPU protegidas por SENHA-MESTRA (modalidade 3).

Modelo (ver decisão do usuário):
- O usuário+senha do SISDPU ficam CIFRADOS em disco (dados/_credenciais.enc).
- A chave de decifração é derivada de uma SENHA-MESTRA que o servidor digita ao
  abrir o painel — a senha-mestra NÃO é gravada em lugar nenhum.
- Após o desbloqueio, as credenciais ficam só na MEMÓRIA do processo (esta sessão);
  ao reiniciar o painel, é preciso desbloquear de novo.

Criptografia: Fernet (AES-128-CBC + HMAC-SHA256, autenticado) com chave derivada
por PBKDF2-HMAC-SHA256 (salt aleatório por arquivo). Biblioteca `cryptography`.

Fallback: se NÃO houver arquivo cifrado, o login usa SISDPU_USERNAME/PASSWORD do
.env (compatibilidade). Depois de configurar aqui, o .env deixa de ser necessário.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL COMUNICAÇÕES/Projeto
DADOS_DIR = BASE_DIR.parent / "dados"
CRED_FILE = DADOS_DIR / "_credenciais.enc"

_ITERACOES = 240_000  # custo do PBKDF2 (dificulta força bruta da senha-mestra)

# Estado em MEMÓRIA da sessão atual (nunca persistido).
# Guarda as credenciais do SISDPU e, opcionalmente, as do Correios/CAS (mesmo cofre,
# mesma senha-mestra) — usadas pelo correios_client no login headless da expedição.
_estado: dict = {"destrancado": False, "usuario": "", "senha": "",
                 "correios_usuario": "", "correios_senha": ""}

# Estado da CONEXÃO com o SISDPU (saúde da sessão do painel), atualizado pelas
# operações do sisdpu_client: 'conflito' fica True quando é detectado conflito de
# sessão (login em outra estação); volta a False após um login/reconexão bem-sucedido.
# Só é conhecido quando o painel age (não há detecção passiva).
_conexao: dict = {"conflito": False, "detalhe": ""}


def marcar_conflito(detalhe: str = "") -> None:
    """Registra que há CONFLITO de sessão no SISDPU (para o selo de status ficar âmbar)."""
    _conexao["conflito"] = True
    _conexao["detalhe"] = detalhe or "Conflito de sessão do SISDPU (login em outra estação)."


def marcar_conectado() -> None:
    """Registra que a sessão do SISDPU está saudável (login/reconexão OK) — limpa o conflito."""
    _conexao["conflito"] = False
    _conexao["detalhe"] = ""


def conexao_conflito() -> bool:
    """True se o último contato com o SISDPU acusou conflito de sessão (ainda não resolvido)."""
    return bool(_conexao["conflito"])


def conexao_detalhe() -> str:
    return _conexao["detalhe"]


def _derivar_chave(senha_mestra: str, salt: bytes) -> bytes:
    """Deriva a chave Fernet (32 bytes url-safe base64) a partir da senha-mestra."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERACOES)
    return base64.urlsafe_b64encode(kdf.derive(senha_mestra.encode("utf-8")))


def configurado() -> bool:
    """True se já existe um cofre de credenciais cifrado em disco."""
    return CRED_FILE.exists()


def destrancado() -> bool:
    """True se as credenciais já foram decifradas nesta sessão (estão em memória)."""
    return bool(_estado["destrancado"])


def _gravar_vault(dados: dict, senha_mestra: str) -> None:
    """Cifra o dict de credenciais (SISDPU + Correios) com a senha-mestra e grava."""
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    token = Fernet(_derivar_chave(senha_mestra, salt)).encrypt(
        json.dumps(dados).encode("utf-8"))
    CRED_FILE.write_text(
        json.dumps({"v": 2, "salt": base64.b64encode(salt).decode(), "token": token.decode()}),
        encoding="utf-8")


def salvar(usuario: str, senha: str, senha_mestra: str) -> None:
    """Cifra usuario+senha do SISDPU com a senha-mestra e grava em disco (preservando
    as credenciais do Correios já em memória, se houver). Deixa a sessão DESTRANCADA."""
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not usuario or not senha or not (senha_mestra or ""):
        raise ValueError("Usuário, senha do SISDPU e senha-mestra são obrigatórios.")
    _gravar_vault({"usuario": usuario, "senha": senha,
                   "correios_usuario": _estado.get("correios_usuario", ""),
                   "correios_senha": _estado.get("correios_senha", "")}, senha_mestra)
    _estado.update(destrancado=True, usuario=usuario, senha=senha)


def salvar_correios(usuario: str, senha: str, senha_mestra: str) -> None:
    """Grava as credenciais do Correios/CAS no MESMO cofre (recifra tudo com a
    senha-mestra). Exige o painel já DESTRANCADO (credenciais do SISDPU em memória,
    para não perdê-las) e a senha-mestra correta."""
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not usuario or not senha or not (senha_mestra or ""):
        raise ValueError("Usuário, senha do Correios e senha-mestra são obrigatórios.")
    # As credenciais do SISDPU precisam estar em memória (cofre único). Se o painel
    # estiver BLOQUEADO, a própria senha-mestra do formulário desbloqueia agora.
    if not destrancado():
        if not destrancar(senha_mestra):
            raise ValueError("Senha-mestra incorreta — ou o acesso ao SISDPU ainda não foi "
                             "configurado. Configure o SISDPU primeiro (aba 'Acesso ao SISDPU').")
    elif configurado() and not _senha_mestra_confere(senha_mestra):
        raise ValueError("Senha-mestra incorreta.")
    _gravar_vault({"usuario": _estado.get("usuario", ""), "senha": _estado.get("senha", ""),
                   "correios_usuario": usuario, "correios_senha": senha}, senha_mestra)
    _estado.update(correios_usuario=usuario, correios_senha=senha)


def _senha_mestra_confere(senha_mestra: str) -> bool:
    """True se a senha-mestra decifra o cofre atual (sem alterar o estado em memória)."""
    if not CRED_FILE.exists():
        return True  # ainda não há cofre — qualquer senha-mestra inicia um novo
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        salt = base64.b64decode(d["salt"])
        Fernet(_derivar_chave(senha_mestra or "", salt)).decrypt(d["token"].encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False


def destrancar(senha_mestra: str) -> bool:
    """Tenta decifrar o cofre com a senha-mestra. Carrega em memória e retorna True
    se acertou; False se a senha-mestra estiver errada (ou o arquivo inválido)."""
    if not CRED_FILE.exists():
        return False
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        salt = base64.b64decode(d["salt"])
        bruto = Fernet(_derivar_chave(senha_mestra or "", salt)).decrypt(d["token"].encode("utf-8"))
        dados = json.loads(bruto.decode("utf-8"))
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
        return False
    except Exception:  # noqa: BLE001 — qualquer falha = senha errada / arquivo corrompido
        return False
    _estado.update(destrancado=True, usuario=dados.get("usuario", ""), senha=dados.get("senha", ""),
                   correios_usuario=dados.get("correios_usuario", ""),
                   correios_senha=dados.get("correios_senha", ""))
    return True


def get_credenciais() -> tuple[str, str] | None:
    """(usuario, senha) do SISDPU se destrancado; None caso contrário."""
    if _estado["destrancado"] and _estado["usuario"] and _estado["senha"]:
        return _estado["usuario"], _estado["senha"]
    return None


def get_credenciais_correios() -> tuple[str, str] | None:
    """(usuario, senha) do Correios/CAS se destrancado e cadastrado; None caso contrário."""
    if _estado["destrancado"] and _estado["correios_usuario"] and _estado["correios_senha"]:
        return _estado["correios_usuario"], _estado["correios_senha"]
    return None


def correios_configurado() -> bool:
    """True se, nesta sessão destrancada, há credenciais do Correios cadastradas.
    (Só é conhecível quando destrancado — o cofre em disco é opaco.)"""
    return bool(_estado["destrancado"] and _estado["correios_usuario"])


def get_usuario() -> str:
    """Usuário do SISDPU carregado (só quando destrancado) — para exibição."""
    return _estado["usuario"] if _estado["destrancado"] else ""


def get_correios_usuario() -> str:
    """Usuário do Correios carregado (só quando destrancado) — para exibição."""
    return _estado["correios_usuario"] if _estado["destrancado"] else ""


def bloquear() -> None:
    """Esquece as credenciais da memória (tranca de novo). O cofre em disco fica."""
    _estado.update(destrancado=False, usuario="", senha="", correios_usuario="", correios_senha="")
    _conexao.update(conflito=False, detalhe="")  # trancado = sem sessão/conexão


def remover() -> None:
    """Bloqueia e APAGA o cofre em disco (volta a exigir configuração/.env)."""
    bloquear()
    try:
        CRED_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def env_tem_credenciais() -> bool:
    """True se o .env fornece SISDPU_USERNAME e SISDPU_PASSWORD (modo legado)."""
    return bool(os.environ.get("SISDPU_USERNAME") and os.environ.get("SISDPU_PASSWORD"))


def precisa_desbloqueio() -> bool:
    """Regra do 'cadeado': o painel exige desbloqueio quando NÃO está destrancado E
    (existe um cofre cifrado OU não há credenciais no .env para o modo legado).
    Ou seja: se ainda não há cofre e o .env tem credenciais, o painel opera normal."""
    if destrancado():
        return False
    if configurado():
        return True
    return not env_tem_credenciais()  # sem cofre e sem .env → precisa configurar
