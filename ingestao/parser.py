"""Parser do sisdpu.txt -> metadata.json.

Extraido de _migrar_pajs.py para ser reusado tanto na migracao one-shot quanto
no sincronizador automatico. Evita duplicacao de regex de cabecalho e movs.

Funcao publica: `montar_metadata(paj_norm, texto_sisdpu) -> dict`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import re


HEADER_KEYS = {
    "paj": re.compile(r"^PAJ\s*:\s*(.+)$", re.IGNORECASE),
    "assistido": re.compile(r"^Assistido\s*:\s*(.+)$", re.IGNORECASE),
    "pretensao": re.compile(r"^Pretens[aã]o\s*:\s*(.+)$", re.IGNORECASE),
    "oficio_responsavel": re.compile(r"^Of[ií]cio\s*:\s*(.+)$", re.IGNORECASE),
    "data_abertura": re.compile(r"^Data\s+de\s+Abertura\s*:\s*(.+)$", re.IGNORECASE),
    "decurso": re.compile(r"^Decurso(?:\s+agendado)?\s*:\s*(.+)$", re.IGNORECASE),
    "status_linha": re.compile(r"^Status\s*:\s*(.+)$", re.IGNORECASE),
}

RE_PROCESSO_JUDICIAL = re.compile(
    r"PROCESSO\s+JUDICIAL\s+VINCULADO\s*:\s*([0-9\-\.]+)(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)
RE_PRAZO_CRITICO = re.compile(
    r"PRAZO\s+CR[IÍ]TICO\s*:\s*(\d{2}/\d{2}/\d{4})\s*(?:—|-|:)?\s*(.+)?",
    re.IGNORECASE,
)
RE_MOV_INICIO = re.compile(r"^\[(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?)\]\s*(.*)$")
RE_MOV_INLINE = re.compile(
    r"^Movimenta[cç][aã]o(?:\s*\[(\d{2}/\d{2}/\d{4})\])?\s*:\s*(.+)$",
    re.IGNORECASE,
)
# Prefixo opcional "[seq=N] " no inicio de uma linha de movimentacao —
# carrega o numero real da Seq. do SIS-DPU. Quando ausente (sisdpu.txt
# antigos, gerados antes desta feature), o parser cai no fallback de
# numeracao local 1..N.
RE_MOV_SEQ_PREFIX = re.compile(r"^\[seq=(\d+)\]\s*")
RE_AREA = re.compile(r"^([A-Za-zÀ-ÿ]+)")

AREAS_CANONICAS = {
    "criminal": "Criminal",
    "civel": "Civel",
    "cível": "Civel",
    "previdenciario": "Previdenciario",
    "previdenciário": "Previdenciario",
    "saude": "Saude",
    "saúde": "Saude",
    "execucao": "Execucao",
    "execução": "Execucao",
    "administrativo": "Administrativo",
    "curadoria": "Curadoria",
}


def parse_data_br(texto: str) -> str:
    """DD/MM/YYYY -> YYYY-MM-DD. Retorna '' se invalido."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", texto or "")
    if not m:
        return ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def derivar_foro(pretensao: str) -> str:
    """Primeira palavra da pretensao normalizada — vira o 'foro' tematico."""
    if not pretensao:
        return "?"
    m = RE_AREA.match(pretensao.strip())
    if not m:
        return "?"
    token = m.group(1).lower()
    return AREAS_CANONICAS.get(token, m.group(1).title())


def extrair_pretensao_pura(pretensao: str) -> str:
    """Extrai apenas a parte após ' >> ' da pretensão, removendo a área temática."""
    if not pretensao:
        return ""
    partes = pretensao.split(">>")
    if len(partes) > 1:
        return partes[1].strip()
    return pretensao.strip()


def parsear_header(linhas: list[str]) -> dict:
    """Extrai cabecalho estruturado das primeiras linhas (antes de 'MOVIMENTAÇÕES')."""
    header: dict = {}
    for linha in linhas:
        for key, regex in HEADER_KEYS.items():
            m = regex.match(linha.strip())
            if m and key not in header:
                header[key] = m.group(1).strip()

        m_proc = RE_PROCESSO_JUDICIAL.search(linha)
        if m_proc and "processo_judicial" not in header:
            header["processo_judicial"] = m_proc.group(1).strip()
            if m_proc.group(2):
                header["foro_detalhado"] = m_proc.group(2).strip()

        m_prazo = RE_PRAZO_CRITICO.search(linha)
        if m_prazo and "prazo_critico" not in header:
            header["prazo_critico"] = parse_data_br(m_prazo.group(1))
            header["prazo_descricao"] = (m_prazo.group(2) or "").strip()

    return header


def normalizar_paj_id(linha_paj: str, paj_norm: str) -> str:
    """Extrai '2026/044-00311' de 'PAJ: 2026/044-00311 | ...' ou deriva de paj_norm."""
    if linha_paj:
        m = re.match(r"(\d{4}/\d{3}-\d{5})", linha_paj.strip())
        if m:
            return m.group(1)
    m2 = re.match(r"PAJ-(\d{4})-(\d{3})-(\d{5})", paj_norm)
    if m2:
        return f"{m2.group(1)}/{m2.group(2)}-{m2.group(3)}"
    return paj_norm


def extrair_assistido_da_linha_paj(linha_paj: str) -> str | None:
    """Para formato 'PAJ: 2018/044-00326 | NOME | ...' extrai o nome."""
    if not linha_paj or "|" not in linha_paj:
        return None
    partes = [p.strip() for p in linha_paj.split("|")]
    if len(partes) >= 2:
        return partes[1]
    return None


def parsear_movimentacoes_estruturadas(movs_brutos: list[dict]) -> list[dict]:
    """Converte movimentações estruturadas do sisdpu_client direto para metadata.

    Entrada: lista de dicts com campos extraídos da tabela HTML (seq, anexo, data,
    movimentacao, fases, descricao, links_decisao, usuario).

    Saída: lista estruturada compatível com metadata.json, preservando todos os campos
    originais sem perda de dados.
    """
    movs = []
    for mov_bruto in (movs_brutos or []):
        try:
            seq_str = str(mov_bruto.get("seq", "")).strip()
            seq = int(seq_str) if seq_str.isdigit() else len(movs) + 1

            data_orig = mov_bruto.get("data", "")
            data_iso = parse_data_br(data_orig.split()[0]) if data_orig else ""

            mov = {
                "seq": seq,
                "data": data_iso,
                "data_original": data_orig,
                "descricao": mov_bruto.get("descricao") or "",
                "movimentacao": mov_bruto.get("movimentacao", ""),
                "fases": mov_bruto.get("fases", ""),
                "usuario": mov_bruto.get("usuario", ""),
                "links_decisao": mov_bruto.get("links_decisao", []),
                "anexo": mov_bruto.get("anexo", ""),
                "descricao_retorno": mov_bruto.get("descricao_retorno", ""),
            }
            movs.append(mov)
        except Exception:
            continue
    return movs


def parsear_movimentacoes(texto: str) -> list[dict]:
    """Extrai blocos de movimentacao do corpo do .txt.

    Suporta:
      [seq=N] [DD/MM/YYYY HH:MM] AUTOR: descricao... (formato atual)
      [DD/MM/YYYY HH:MM] AUTOR: descricao...        (formato antigo)
      [DD/MM/YYYY] texto...
      Movimentacao [DD/MM/YYYY]: texto...
      Movimentacao: texto...

    Quando o prefixo "[seq=N]" estiver presente, usa N como `seq` (numero
    real da Seq. no SIS-DPU). Caso contrario, cai num contador local 1..N
    (compativel com sisdpu.txt antigos).
    """
    movs: list[dict] = []
    linhas = texto.splitlines()
    current: dict | None = None
    seq_local = 0

    for linha in linhas:
        stripped = linha.rstrip()

        # Captura "[seq=N]" opcional no inicio e remove do stripped — assim
        # os regexes seguintes continuam batendo no resto da linha.
        m_seq_pref = RE_MOV_SEQ_PREFIX.match(stripped)
        seq_sis: int | None = None
        if m_seq_pref:
            with contextlib.suppress(ValueError):
                seq_sis = int(m_seq_pref.group(1))
            stripped = stripped[m_seq_pref.end() :]

        m_ini = RE_MOV_INICIO.match(stripped)
        m_inl = RE_MOV_INLINE.match(stripped)

        if m_ini:
            if current:
                movs.append(current)
            seq_local += 1
            seq_final = seq_sis if seq_sis is not None else seq_local
            data_br = m_ini.group(1).split()[0]
            current = {
                "seq": seq_final,
                "data": parse_data_br(data_br),
                "data_original": m_ini.group(1),
                "descricao": m_ini.group(2).strip(),
                "movimentacao": "",
                "fases": "",
            }
        elif m_inl:
            if current:
                movs.append(current)
            seq_local += 1
            seq_final = seq_sis if seq_sis is not None else seq_local
            data_ref = m_inl.group(1) or ""
            current = {
                "seq": seq_final,
                "data": parse_data_br(data_ref) if data_ref else "",
                "data_original": data_ref,
                "descricao": m_inl.group(2).strip(),
                "movimentacao": "",
                "fases": "",
            }
        elif current is not None:
            if stripped == "" or stripped.startswith("---"):
                if current.get("descricao"):
                    movs.append(current)
                    current = None
                continue
            current["descricao"] = (current["descricao"] + " " + stripped.strip()).strip()

    if current and current.get("descricao"):
        movs.append(current)

    return movs


def montar_metadata(paj_norm: str, texto_sisdpu: str) -> dict:
    """Converte o sisdpu.txt em metadata.json compativel com o UI."""
    linhas = texto_sisdpu.splitlines()

    cabecalho_lines: list[str] = []
    for linha in linhas:
        if re.match(r"^\s*MOVIMENTA[CÇ][OÕ]ES", linha, re.IGNORECASE):
            break
        cabecalho_lines.append(linha)

    header = parsear_header(cabecalho_lines)
    linha_paj = header.get("paj", "")
    paj_id = normalizar_paj_id(linha_paj, paj_norm)

    assistido = header.get("assistido", "") or (extrair_assistido_da_linha_paj(linha_paj) or "")

    pretensao = header.get("pretensao", "")
    foro_area = derivar_foro(pretensao)

    status_linha = header.get("status_linha", "").upper()
    status_paj = "Arquivado" if "ARQUIV" in status_linha else "Ativo"

    data_abertura_br = header.get("data_abertura", "")
    data_abertura_iso = parse_data_br(data_abertura_br)

    movs = parsear_movimentacoes(texto_sisdpu)
    ultima_mov = max(movs, key=lambda m: m.get("data") or "", default={}) if movs else {}

    prazos_abertos: list[dict] = []
    if header.get("prazo_critico"):
        dias: int | str = ""
        try:
            d = _dt.date.fromisoformat(header["prazo_critico"])
            dias = (d - _dt.date.today()).days
        except Exception:
            pass
        prazos_abertos.append(
            {
                "data_final": header["prazo_critico"],
                "dias": dias if dias != "" else "?",
                "parte": "DPU",
                "seq": 0,
                "descricao": header.get("prazo_descricao", ""),
            }
        )

    meta = {
        "paj": paj_id,
        "paj_norm": paj_norm,
        "assistido_caixa": assistido,
        "oficio_caixa": header.get("oficio_responsavel", ""),
        "pretensao": pretensao,
        "pretensaopura": extrair_pretensao_pura(pretensao),
        "foro_detectado": foro_area,
        "foro_detalhado": header.get("foro_detalhado", ""),
        "classificacao": pretensao or "?",
        "processo_judicial": header.get("processo_judicial", ""),
        "data_mov_caixa": ultima_mov.get("data") or data_abertura_iso,
        "desc_mov_caixa": (ultima_mov.get("descricao") or "")[:500],
        "data_abertura": data_abertura_iso,
        "decurso": header.get("decurso", ""),
        "primeira_deteccao": _dt.datetime.now().isoformat(),
        "classe_evento": "",
        "prazos_abertos": prazos_abertos,
        "detalhes_sisdpu": {
            "status_paj": status_paj,
            "movimentacoes": movs,
        },
        "migrado_em": _dt.datetime.now().isoformat(),
    }
    return meta
