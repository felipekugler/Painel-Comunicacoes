"""Rota de Configurações do Painel de Comunicações (multi-caixa).

- GET  /configuracoes                       -> página de configurações
- POST /api/config/movimentacoes            -> nº de movimentações analisadas
- POST /api/config/caixa                    -> habilita/desabilita uma caixa
- POST /api/config/caixas/atualizar         -> relista as caixas no SISDPU
- POST /api/config/comunicados              -> cria comunicado (geral ou de caixa)
- POST /api/config/comunicados/{cid}        -> edita comunicado
- POST /api/config/comunicados/{cid}/del    -> exclui comunicado
  (todos os de comunicado aceitam "slug" no corpo: vazio/omisso = geral)
"""

from fastapi import APIRouter, Body, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ingestao import sisdpu_client
from services import (analise_service, config_service, credenciais_service,
                      painel_comunicacoes_service, historico_service)

router = APIRouter()


@router.get("/configuracoes", response_class=HTMLResponse)
async def configuracoes(request: Request):
    habilitadas = config_service.listar_caixas_habilitadas()
    grupos_habilitados = {c["grupo"] for c in habilitadas}
    todas = config_service.listar_caixas_todas()
    disponiveis = config_service.listar_caixas_disponiveis()
    # Garante que TODA caixa com entrada no config (inclusive as desabilitadas)
    # apareça na tabela — senão não haveria como reabilitá-la.
    for c in todas:
        if c["grupo"] not in disponiveis:
            disponiveis = [*disponiveis, c["grupo"]]
    caixas_disp = [{"grupo": g, "slug": config_service.slugify(g),
                    "nome": config_service.nome_caixa(g), "habilitada": g in grupos_habilitados}
                   for g in disponiveis]
    # comunicados + parágrafos de CADA caixa (habilitada ou não) para as abas
    caixas_com = [{**c,
                   "comunicados": config_service.listar_comunicados(c["slug"]),
                   "paragrafos": config_service.listar_paragrafos(c["slug"])}
                  for c in todas]
    template = request.app.state.jinja.get_template("configuracoes.html")
    return HTMLResponse(template.render(
        request=request,
        n_movimentacoes=config_service.get_n_movimentacoes(),
        analise_concorrencia=config_service.get_analise_concorrencia(),
        analise_concorrencia_max=config_service.ANALISE_CONCORRENCIA_MAX,
        layout_pendentes=config_service.get_layout_pendentes(),
        prazos_predefinidos=config_service.get_prazos_predefinidos(),
        prazo_tramitacao_padrao=config_service.get_prazo_tramitacao_padrao(),
        confirmacoes_cutoff_dias=config_service.get_confirmacoes_cutoff_dias(),
        feriados=config_service.get_feriados(),
        carta_cabecalho=config_service.get_carta_cabecalho(),
        carta_rodape=config_service.get_carta_rodape(),
        carta_prazo_pos_entrega=config_service.get_carta_prazo_pos_entrega(),
        carta_caixa_tramitacao=config_service.get_carta_caixa_tramitacao(),
        grupos_favoritos=config_service.get_grupos_favoritos(),
        grupos_unidade=config_service.get_grupos_unidade(),
        grupos_favoritos_max=config_service.GRUPOS_FAVORITOS_MAX,
        envio_modos=config_service.get_envio_modos(),
        whatsapp_intervalo=config_service.get_whatsapp_intervalo_seg(),
        abrir_tela_cheia=config_service.get_abrir_tela_cheia(),
        ia_ativa=config_service.get_ia_ativa(),
        claude_instalado=analise_service.claude_instalado(),
        comunicados=config_service.listar_comunicados(),  # gerais
        paragrafos=config_service.listar_paragrafos(),    # gerais
        caixas_disponiveis=caixas_disp,
        caixas=caixas_com,
        backups=historico_service.listar_backups(),
        seg_configurado=credenciais_service.configurado(),
        seg_destrancado=credenciais_service.destrancado(),
        seg_env_legado=credenciais_service.env_tem_credenciais(),
        seg_usuario=credenciais_service.get_usuario(),
        seg_correios_configurado=credenciais_service.correios_configurado(),
        seg_correios_usuario=credenciais_service.get_correios_usuario(),
    ))


@router.post("/api/config/movimentacoes", response_class=JSONResponse)
async def set_movimentacoes(payload: dict = Body(...)):
    try:
        n = int(payload.get("n", config_service.N_MOVIMENTACOES_PADRAO))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "n": config_service.set_n_movimentacoes(n)}


@router.post("/api/config/analise-concorrencia", response_class=JSONResponse)
async def set_analise_concorrencia(payload: dict = Body(...)):
    try:
        n = int(payload.get("n", config_service.ANALISE_CONCORRENCIA_PADRAO))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "n": config_service.set_analise_concorrencia(n)}


@router.post("/api/config/layout-pendentes", response_class=JSONResponse)
async def set_layout_pendentes(payload: dict = Body(...)):
    """Define o layout da tabela 'Pendentes': 'simples' (retrátil) ou 'completo' (carrossel)."""
    v = (payload or {}).get("layout", config_service.LAYOUT_PENDENTES_PADRAO)
    return {"ok": True, "layout": config_service.set_layout_pendentes(v)}


@router.post("/api/config/envio-modo", response_class=JSONResponse)
async def set_envio_modo(payload: dict = Body(...)):
    """Define o modo de UM meio: 'simulado' (não dispara) ou 'real' (dispara)."""
    meio = (payload or {}).get("meio", "")
    modo = (payload or {}).get("modo", "simulado")
    return {"ok": True, "modos": config_service.set_envio_modo_meio(meio, modo)}


@router.post("/api/config/whatsapp-intervalo", response_class=JSONResponse)
async def set_whatsapp_intervalo(payload: dict = Body(...)):
    """Intervalo MÍNIMO (segundos) entre mensagens no envio em lote por WhatsApp."""
    try:
        n = int((payload or {}).get("seg", 25))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "seg": config_service.set_whatsapp_intervalo_seg(n)}


@router.post("/api/config/ia-modo", response_class=JSONResponse)
async def set_ia_modo(payload: dict = Body(...)):
    """Alterna entre 'Modo Simples' (sem IA) e 'Modo com IA'. Corpo: {ia: bool}."""
    on = bool((payload or {}).get("ia"))
    ativa = config_service.set_ia_ativa(on)
    return {"ok": True, "ia_ativa": ativa, "claude_instalado": analise_service.claude_instalado()}


@router.post("/api/whatsapp/login", response_class=JSONResponse)
async def api_whatsapp_login():
    """Abre, em PROCESSO SEPARADO, a janela do WhatsApp Web para login por QR Code —
    equivale ao atalho WhatsApp-Login.bat, mas acionado direto do painel. Não bloqueia
    o servidor (o login roda no seu próprio processo, com janela visível para o QR)."""
    import subprocess
    import sys
    from pathlib import Path
    base = Path(__file__).resolve().parents[1]  # .../Projeto
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_CONSOLE  # janela mostra o progresso do login
    try:
        subprocess.Popen([sys.executable, "-m", "ingestao.whatsapp_client", "login"],
                         cwd=str(base), creationflags=flags)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return {"ok": True}


@router.post("/api/ia/testar", response_class=JSONResponse)
async def api_testar_ia():
    """Faz uma chamada MÍNIMA real ao Claude para confirmar que a IA está utilizável
    (instalada, autenticada e com cota). Retorna {ok, status, detalhe}."""
    from anyio import to_thread
    try:
        res = await to_thread.run_sync(analise_service.testar_conexao)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "status": "erro", "detalhe": str(e)}
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/ia/status", response_class=JSONResponse)
async def api_ia_status():
    """Status LEVE do Claude/IA para o selo do cabeçalho (NÃO chama o Claude):
    reflete o modo efetivo (config + instalação). O teste REAL é POST /api/ia/testar."""
    return {"ok": True, "disponivel": analise_service.disponivel(),
            "instalado": analise_service.claude_instalado()}


@router.get("/api/whatsapp/status", response_class=JSONResponse)
async def api_whatsapp_status():
    """Status LEVE do WhatsApp para o selo (SEM abrir o navegador — o perfil é travado
    por 1 processo). Reflete se há sessão salva no perfil persistente."""
    from ingestao import whatsapp_client
    return {"ok": True, "sessao": whatsapp_client.sessao_salva()}


@router.post("/api/config/abrir-tela-cheia", response_class=JSONResponse)
async def set_abrir_tela_cheia(payload: dict = Body(...)):
    """Liga/desliga a abertura do painel em TELA CHEIA no lançador (Painel-Comunicacoes.bat).
    Vale a partir da PRÓXIMA vez que o painel for aberto pelo atalho."""
    on = bool((payload or {}).get("on"))
    return {"ok": True, "on": config_service.set_abrir_tela_cheia(on)}


@router.post("/api/config/prazos-predefinidos", response_class=JSONResponse)
async def add_prazo_predef(payload: dict = Body(...)):
    """Adiciona um prazo pré-definido (badge). valor inteiro >= 0 (0 = sem prazo)."""
    if "valor" not in (payload or {}):
        return JSONResponse({"ok": False, "erro": "valor ausente"}, status_code=400)
    return {"ok": True, "prazos": config_service.add_prazo_predef(payload.get("valor"))}


@router.post("/api/config/prazos-predefinidos/del", response_class=JSONResponse)
async def del_prazo_predef(payload: dict = Body(...)):
    return {"ok": True, "prazos": config_service.del_prazo_predef((payload or {}).get("valor"))}


@router.post("/api/config/prazo-tramitacao-padrao", response_class=JSONResponse)
async def set_prazo_tramitacao_padrao(payload: dict = Body(...)):
    """Prazo (dias) padrão de 'Nova conclusão em' da tramitação, usado quando o
    envio não informou prazo. Inteiro entre 1 e 365."""
    try:
        n = int((payload or {}).get("dias"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "dias": config_service.set_prazo_tramitacao_padrao(n)}


@router.post("/api/config/confirmacoes-cutoff-dias", response_class=JSONResponse)
async def set_confirmacoes_cutoff_dias(payload: dict = Body(...)):
    """Corte de antiguidade (dias) da Central de Confirmações de leitura — PAJs cujo
    último envio é mais antigo que isso somem da lista de pendentes. 0 = sem corte.
    Inteiro entre 0 e 365."""
    try:
        n = int((payload or {}).get("dias"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "dias": config_service.set_confirmacoes_cutoff_dias(n)}


@router.post("/api/config/feriados", response_class=JSONResponse)
async def add_feriado(payload: dict = Body(...)):
    """Cadastra um feriado/dia de recesso — um único dia OU um PERÍODO (data_inicio +
    data_fim, ex.: recesso forense) — + descrição opcional. Datas de prazo de tramitação
    que caírem dentro são adiadas para o próximo dia útil."""
    p = payload or {}
    ini = str(p.get("data_inicio") or p.get("data") or "").strip()
    fim = str(p.get("data_fim") or "").strip()
    if not config_service.valida_data_br(ini):
        return JSONResponse({"ok": False, "erro": "Data de início inválida (use DD/MM/AAAA)."}, status_code=400)
    if fim and not config_service.valida_data_br(fim):
        return JSONResponse({"ok": False, "erro": "Data de fim inválida (use DD/MM/AAAA)."}, status_code=400)
    return {"ok": True, "feriados": config_service.add_feriado(ini, fim, p.get("descricao", ""))}


@router.post("/api/config/feriados/del", response_class=JSONResponse)
async def del_feriado(payload: dict = Body(...)):
    p = payload or {}
    return {"ok": True, "feriados": config_service.del_feriado(
        p.get("data_inicio") or p.get("data", ""), p.get("data_fim", ""))}


@router.post("/api/config/carta-cabecalho", response_class=JSONResponse)
async def set_carta_cabecalho(payload: dict = Body(...)):
    """Cabeçalho padrão da carta (e-Carta/Correios)."""
    return {"ok": True, "texto": config_service.set_carta_cabecalho((payload or {}).get("texto", ""))}


@router.post("/api/config/carta-rodape", response_class=JSONResponse)
async def set_carta_rodape(payload: dict = Body(...)):
    """Rodapé padrão da carta (e-Carta/Correios)."""
    return {"ok": True, "texto": config_service.set_carta_rodape((payload or {}).get("texto", ""))}


@router.post("/api/config/carta-prazo-pos-entrega", response_class=JSONResponse)
async def set_carta_prazo_pos_entrega(payload: dict = Body(...)):
    """Dias somados à previsão de entrega para o prazo da tramitação de aguardo (0–365)."""
    try:
        n = int((payload or {}).get("dias"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "erro": "valor inválido"}, status_code=400)
    return {"ok": True, "dias": config_service.set_carta_prazo_pos_entrega(n)}


@router.post("/api/config/carta-caixa-tramitacao", response_class=JSONResponse)
async def set_carta_caixa_tramitacao(payload: dict = Body(...)):
    """Caixa de grupo p/ tramitação após expedir a carta (aguardo de retorno)."""
    return {"ok": True, "caixa": config_service.set_carta_caixa_tramitacao((payload or {}).get("caixa", ""))}


@router.post("/api/config/grupos-favoritos", response_class=JSONResponse)
async def add_grupo_favorito(payload: dict = Body(...)):
    """Adiciona uma caixa de grupo favorita (selo de atalho no modal Gravar).
    Limite GRUPOS_FAVORITOS_MAX; se cheio, retorna a lista atual inalterada."""
    grupo = ((payload or {}).get("grupo") or "").strip()
    if not grupo:
        return JSONResponse({"ok": False, "erro": "grupo ausente"}, status_code=400)
    favoritos = config_service.add_grupo_favorito(grupo)
    cheio = len(favoritos) >= config_service.GRUPOS_FAVORITOS_MAX and grupo not in favoritos
    return {"ok": True, "favoritos": favoritos, "cheio": cheio,
            "max": config_service.GRUPOS_FAVORITOS_MAX}


@router.post("/api/config/grupos-favoritos/del", response_class=JSONResponse)
async def del_grupo_favorito(payload: dict = Body(...)):
    return {"ok": True, "favoritos": config_service.del_grupo_favorito((payload or {}).get("grupo"))}


@router.post("/api/config/caixa", response_class=JSONResponse)
async def habilitar_caixa(payload: dict = Body(...)):
    grupo = (payload.get("grupo") or "").strip()
    if not grupo:
        return JSONResponse({"ok": False, "erro": "grupo ausente"}, status_code=400)
    config_service.habilitar_caixa(grupo, bool(payload.get("on")))
    return {"ok": True}


@router.post("/api/config/caixa/{slug}/limpar", response_class=JSONResponse)
async def limpar_caixa(slug: str):
    """Zera comunicados e parágrafos padrão de uma caixa (mantém a caixa)."""
    ok = config_service.limpar_caixa(slug)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/config/caixa/{slug}/excluir", response_class=JSONResponse)
async def excluir_caixa(slug: str):
    """Remove a aba/entrada de uma caixa (só se estiver desabilitada)."""
    ok = config_service.excluir_caixa(slug)
    if not ok:
        return JSONResponse({"ok": False, "erro": "caixa inexistente ou ainda habilitada"},
                            status_code=400)
    return {"ok": True}


@router.post("/api/config/resetar", response_class=JSONResponse)
async def resetar_painel(payload: dict = Body(default={})):
    """Reseta o painel. total=True apaga tudo (padrão de fábrica); total=False
    mantém textos/parágrafos padrão e caixas, limpando só os dados operacionais."""
    total = bool((payload or {}).get("total"))
    try:
        return painel_comunicacoes_service.resetar_painel(total)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/config/caixas/atualizar", response_class=JSONResponse)
async def atualizar_caixas():
    """Faz um login rápido e relista as caixas de grupo disponíveis."""
    try:
        res = await sisdpu_client.listar_caixa_grupo(config_service.CAIXA_SEED_GRUPO)
        grupos = res.get("grupos_disponiveis", [])
        config_service.set_caixas_disponiveis(grupos)
        return {"ok": True, "caixas": grupos}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=502)


@router.post("/api/config/comunicados", response_class=JSONResponse)
async def add_comunicado(payload: dict = Body(...)):
    slug = payload.get("slug") or None
    novo = config_service.add_comunicado(payload.get("tipo", ""), payload.get("texto", ""), slug)
    if not novo:
        return JSONResponse({"ok": False, "erro": "tipo e texto são obrigatórios"}, status_code=400)
    return {"ok": True, "comunicado": novo}


@router.post("/api/config/comunicados/{cid}", response_class=JSONResponse)
async def update_comunicado(cid: str, payload: dict = Body(...)):
    slug = payload.get("slug") or None
    ok = config_service.update_comunicado(cid, payload.get("tipo", ""), payload.get("texto", ""), slug)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


@router.post("/api/config/comunicados/{cid}/del", response_class=JSONResponse)
async def delete_comunicado(cid: str, payload: dict = Body(default={})):
    slug = (payload or {}).get("slug") or None
    return {"ok": config_service.delete_comunicado(cid, slug)}


# ---------------- parágrafos padrão ----------------
@router.post("/api/config/paragrafos", response_class=JSONResponse)
async def add_paragrafo(payload: dict = Body(...)):
    slug = payload.get("slug") or None
    novo = config_service.add_paragrafo(payload.get("titulo", ""), payload.get("texto", ""), slug)
    if not novo:
        return JSONResponse({"ok": False, "erro": "título e texto são obrigatórios"}, status_code=400)
    return {"ok": True, "paragrafo": novo}


@router.post("/api/config/paragrafos/{pid}", response_class=JSONResponse)
async def update_paragrafo(pid: str, payload: dict = Body(...)):
    slug = payload.get("slug") or None
    ok = config_service.update_paragrafo(pid, payload.get("titulo", ""), payload.get("texto", ""), slug)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


@router.post("/api/config/paragrafos/{pid}/del", response_class=JSONResponse)
async def delete_paragrafo(pid: str, payload: dict = Body(default={})):
    slug = (payload or {}).get("slug") or None
    return {"ok": config_service.delete_paragrafo(pid, slug)}


# ---------------- anexos PADRÃO (por comunicado/parágrafo) ----------------
async def _ler_uploads(arquivos: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [(f.filename or "arquivo", await f.read()) for f in (arquivos or [])]


@router.post("/api/config/anexos-padrao/{kind}/{oid}", response_class=JSONResponse)
async def add_anexos_padrao(kind: str, oid: str, slug: str = "",
                            arquivos: list[UploadFile] = File(default=[])):
    if kind not in ("comunicado", "paragrafo"):
        return JSONResponse({"ok": False, "erro": "tipo inválido"}, status_code=400)
    res = painel_comunicacoes_service.adicionar_anexos_padrao(kind, oid, slug or None,
                                                 await _ler_uploads(arquivos))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/config/anexos-padrao/{kind}/{oid}/remover/{anexo_id}", response_class=JSONResponse)
async def del_anexo_padrao(kind: str, oid: str, anexo_id: str, payload: dict = Body(default={})):
    slug = (payload or {}).get("slug") or None
    res = painel_comunicacoes_service.remover_anexo_padrao(kind, oid, slug, anexo_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/config/anexos-padrao/{oid}/arquivo/{arquivo}")
async def baixar_anexo_padrao(oid: str, arquivo: str):
    p = painel_comunicacoes_service.caminho_anexo_padrao(oid, arquivo).resolve()
    base = painel_comunicacoes_service.ANEXOS_PADRAO_DIR.resolve()
    if base not in p.parents or not p.is_file():
        return JSONResponse({"ok": False, "erro": "arquivo não encontrado"}, status_code=404)
    nome = arquivo.split("__", 1)[1] if "__" in arquivo else arquivo
    return FileResponse(str(p), filename=nome)
