"""Migracao one-shot: PAJs/textos/<PAJ>.txt -> PAJs/<PAJ>/sisdpu.txt + metadata.json.

Uso:
    python _migrar_pajs.py            # migra (idempotente)
    python _migrar_pajs.py --dry-run  # simula
    python _migrar_pajs.py --force    # regera metadata.json mesmo se ja existe
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from config import PAJS_DIR
from ingestao.parser import montar_metadata


def migrar_um(arquivo_txt: Path, destino_base: Path, dry_run: bool, force: bool) -> tuple[str, str]:
    """Migra um .txt. Retorna (status, mensagem)."""
    paj_norm = arquivo_txt.stem
    pasta_destino = destino_base / paj_norm
    metadata_path = pasta_destino / "metadata.json"
    sisdpu_path = pasta_destino / "sisdpu.txt"

    if metadata_path.exists() and not force:
        return ("skip", f"{paj_norm}: metadata.json ja existe — pulando")

    try:
        texto = arquivo_txt.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ("erro", f"{paj_norm}: falha lendo .txt — {e}")

    try:
        meta = montar_metadata(paj_norm, texto)
    except Exception as e:
        return ("erro", f"{paj_norm}: falha montando metadata — {type(e).__name__}: {e}")

    if dry_run:
        return (
            "ok",
            f"[dry-run] {paj_norm}: criaria pasta + sisdpu.txt + metadata.json "
            f"({len(meta['detalhes_sisdpu']['movimentacoes'])} movs, "
            f"pretensao='{meta['pretensao'][:40]}')",
        )

    pasta_destino.mkdir(parents=True, exist_ok=True)
    if not sisdpu_path.exists():
        shutil.copy2(arquivo_txt, sisdpu_path)
    metadata_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("ok", f"{paj_norm}: migrado ({len(meta['detalhes_sisdpu']['movimentacoes'])} movs)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra PAJs/textos/*.txt para PAJs/<PAJ>/")
    ap.add_argument("--dry-run", action="store_true", help="Simula sem escrever")
    ap.add_argument("--force", action="store_true", help="Regera metadata.json mesmo se existir")
    args = ap.parse_args()

    origem = PAJS_DIR / "textos"
    if not origem.exists():
        print(f"[erro] pasta de origem nao encontrada: {origem}")
        return 1

    txts = sorted(origem.glob("PAJ-*.txt"))
    if not txts:
        print(f"[aviso] nenhum PAJ-*.txt encontrado em {origem}")
        return 0

    print(f"[migrar] encontrados {len(txts)} PAJs em {origem}")
    if args.dry_run:
        print("[migrar] modo --dry-run — nada sera escrito")

    contadores = {"ok": 0, "skip": 0, "erro": 0}
    for txt in txts:
        status, msg = migrar_um(txt, PAJS_DIR, args.dry_run, args.force)
        contadores[status] = contadores.get(status, 0) + 1
        prefixo = {"ok": "[ok]", "skip": "[--]", "erro": "[ERR]"}[status]
        print(f"{prefixo} {msg}")

    print(
        f"\n[migrar] resumo: {contadores['ok']} processados, "
        f"{contadores['skip']} pulados, {contadores['erro']} erros"
    )
    return 0 if contadores["erro"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
