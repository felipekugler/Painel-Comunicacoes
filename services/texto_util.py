"""Utilidades de texto compartilhadas pelo painel."""
from __future__ import annotations

import re

# Blocos Unicode de EMOJIS / símbolos gráficos. Mantém letras (com acento),
# números, pontuação comum e quebras de linha — só TEXTO é admitido nas mensagens.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoticons, pictogramas, transporte, símbolos suplementares, bandeiras
    "\U00002300-\U000023FF"  # técnicos (⌚ ⏰ ⏳ …)
    "\U00002600-\U000027BF"  # símbolos diversos + dingbats (⚠ ✅ ❌ ☀ ✈ ❤ …)
    "\U00002B00-\U00002BFF"  # símbolos e setas diversos (⭐ ⬅ …)
    "\U0000FE00-\U0000FE0F"  # seletores de variação (emoji-presentation)
    "\U0000200D"             # zero-width joiner (sequências de emoji)
    "\U000020E3"             # combining enclosing keycap
    "\U00002190-\U000021FF"  # setas (← → ↔ …)
    "]+",
    flags=re.UNICODE,
)


def remover_emojis(texto: str | None) -> str:
    """Remove emojis/símbolos gráficos do texto, preservando letras (com acento),
    números, pontuação e quebras de linha. Também limpa os espaços órfãos que a
    remoção deixa (sem mexer nas quebras de linha)."""
    if not texto:
        return texto or ""
    t = _EMOJI_RE.sub("", texto)
    t = re.sub(r"[ \t]{2,}", " ", t)      # colapsa espaços/tabs duplicados
    t = re.sub(r"[ \t]+\n", "\n", t)      # tira espaço antes da quebra
    t = re.sub(r"\n[ \t]+", "\n", t)      # tira espaço depois da quebra
    return t.strip()


def tem_emoji(texto: str | None) -> bool:
    return bool(texto) and bool(_EMOJI_RE.search(texto))


def colapsar_paragrafos(texto: str | None) -> str:
    """Remove as LINHAS EM BRANCO entre parágrafos: qualquer sequência de 2+
    quebras de linha (mesmo com espaços/tabs no meio) vira UMA única quebra.
    As mensagens ficam com uma linha por parágrafo, sem parágrafo vazio de
    separação (aumenta a altura à toa). Preserva o conteúdo das linhas."""
    if not texto:
        return texto or ""
    # normaliza CRLF/CR -> LF e tira espaços em volta das quebras
    t = texto.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    # 2+ quebras (linha(s) em branco) -> 1 quebra
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()
