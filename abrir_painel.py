"""Abre o Painel de Comunicações no navegador, respeitando a preferência
`abrir_tela_cheia` das Configurações.

- Tela cheia LIGADA: abre o Chrome (ou Edge) com `--start-fullscreen --new-window`
  apontando para o painel — equivalente a já entrar com F11.
- Tela cheia DESLIGADA (padrão): abre como aba no navegador padrão do sistema.

Chamado pelo Painel-Comunicacoes.bat no lugar de `start "" URL`. Rodar com o Python do
venv, a partir da pasta Projeto (para importar config_service).

GOTCHA conhecido: se o Chrome JÁ estiver aberto, ele tende a reusar a instância e
o `--start-fullscreen` pode não valer para a nova janela. O `--new-window` reduz,
mas para 100% de garantia seria preciso um perfil dedicado (kiosk) — evitado aqui
para não perder as sessões/login do usuário.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser

URL = "http://127.0.0.1:8002/"

# Caminhos usuais do Chrome e do Edge no Windows (Edge está sempre presente no Win11).
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _achar_navegador() -> str | None:
    """Retorna o caminho do Chrome (preferido) ou Edge, ou None se não achar."""
    for p in _CHROME_PATHS:
        if p and os.path.isfile(p):
            return p
    achado = shutil.which("chrome") or shutil.which("chrome.exe")
    if achado:
        return achado
    for p in _EDGE_PATHS:
        if p and os.path.isfile(p):
            return p
    return shutil.which("msedge") or shutil.which("msedge.exe")


def main() -> None:
    tela_cheia = False
    try:
        from services import config_service
        tela_cheia = config_service.get_abrir_tela_cheia()
    except Exception:  # noqa: BLE001 — sem config acessível, cai no navegador padrão
        tela_cheia = False

    if tela_cheia:
        navegador = _achar_navegador()
        if navegador:
            try:
                subprocess.Popen([navegador, "--new-window", "--start-fullscreen", URL])
                return
            except Exception:  # noqa: BLE001 — falhou o launch direto, cai no padrão
                pass
    # padrão: aba no navegador do sistema
    webbrowser.open(URL)


if __name__ == "__main__":
    sys.exit(main())
