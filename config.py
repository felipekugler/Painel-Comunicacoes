"""Configuração central do Painel de Comunicações."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Texto exibido sob "Painel de Comunicações" na sidebar — identifica a unidade que
# está operando esta instância (ex.: "DPU Curitiba", "Núcleo X"). Default genérico
# para não expor unidade específica no código; cada instalação ajusta no .env.
UNIDADE_DESCRICAO = os.getenv("UNIDADE_DESCRICAO") or "Defensoria Pública da União"
