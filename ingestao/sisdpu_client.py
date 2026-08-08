"""Cliente SISDPU via Playwright headless (async).

Mantido como funcoes modulo-level com estado global de browser/page para
simplificar uso; concorrencia de sincronizacao e' serializada por um
asyncio.Lock em services/sync_service.py.

Credenciais vem das env vars SISDPU_USERNAME e SISDPU_PASSWORD (carregadas do
.env pelo config.py).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
import contextlib


SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"

# Logger dedicado — os logs `[busca-paj]` vao para logs/app.log (handler
# configurado em app.py com rotacao diaria). Tambem aparecem no console
# quando o painel e' iniciado num terminal visivel.
_log = logging.getLogger("sisdpu.busca")


class CredenciaisInvalidas(Exception):
    """SISDPU rejeitou usuario/senha no login."""

    pass


_playwright: Any = None
_browser: Browser | None = None
_page: Page | None = None


# JS que espera o PrimeFaces terminar todos os AJAX pendentes.
_WAIT_PF_AJAX = """
() => new Promise((resolve) => {
    const check = () => {
        if (typeof PrimeFaces === 'undefined'
            || !PrimeFaces.ajax
            || !PrimeFaces.ajax.Queue
            || PrimeFaces.ajax.Queue.isEmpty()) {
            resolve(true);
        } else {
            setTimeout(check, 100);
        }
    };
    check();
})
"""


def _sel(element_id: str) -> str:
    """Gera seletor CSS para IDs JSF (que contem ':')."""
    return f'[id="{element_id}"]'


async def _get_page() -> Page:
    """Retorna a page Playwright, criando browser se necessario.

    O Chromium e' iniciado com flags que FORCAM download de PDFs em vez de
    abrir o preview interno (captura o arquivo bruto via evento `download`, em
    vez de previsualizar).
    """
    global _playwright, _browser, _page
    if _page is not None:
        try:
            # Acessa atributo so para verificar que a page ainda esta viva;
            # se foi fechada, levanta exception e caimos no reset abaixo.
            _ = _page.url
            return _page
        except Exception:
            _page = None
            _browser = None

    if _playwright is None:
        _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            # Desabilita o PDF viewer interno do Chromium: PDFs viram download
            "--disable-features=PDFViewerUpdate,PdfUnseasoned",
        ],
    )
    context = await _browser.new_context(
        viewport={"width": 1280, "height": 1024},
        accept_downloads=True,
    )
    _page = await context.new_page()
    return _page


async def _wait_pf_ajax(page: Page, timeout: int = 10000) -> None:
    """Espera o PrimeFaces terminar AJAX pendente, com timeout de seguranca."""
    try:
        await page.evaluate(_WAIT_PF_AJAX, timeout=timeout)
    except Exception:
        await page.wait_for_timeout(1000)


async def _login() -> None:
    """Faz login no SISDPU.

    Credenciais: primeiro o COFRE protegido por senha-mestra (se destrancado);
    senão, o .env (SISDPU_USERNAME/PASSWORD) — modo legado/compatibilidade.
    """
    from services import credenciais_service  # import tardio evita ciclo
    cred = credenciais_service.get_credenciais()
    if cred:
        user, senha = cred
    else:
        user = os.environ.get("SISDPU_USERNAME", "")
        senha = os.environ.get("SISDPU_PASSWORD", "")
    if not user or not senha:
        raise RuntimeError(
            "Credenciais do SISDPU indisponíveis: desbloqueie o painel com a senha-mestra "
            "(ou defina SISDPU_USERNAME/PASSWORD no .env).")
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/login.xhtml", wait_until="domcontentloaded")
    await page.wait_for_selector(_sel("frmLogin:epaj_input_usuario"), timeout=15000)
    await page.fill(_sel("frmLogin:epaj_input_usuario"), user)
    await page.fill(_sel("frmLogin:epaj_input_senha"), senha)
    await page.evaluate('PrimeFaces.ab({s:"frmLogin:loginButton",f:"frmLogin"})')
    try:
        await page.wait_for_url("**/caixaEntrada**", timeout=15000)
    except PWTimeoutError:
        # Nao chegou na caixa — checar se SISDPU recusou as credenciais.
        # PrimeFaces exibe erros em .ui-messages-error, .mensagem-erro, ou
        # um <div class="ui-growl-message"> com texto de usuario/senha.
        msg_recusa = await page.evaluate(
            """() => {
                const seletores = [
                    '.ui-messages-error-detail',
                    '.ui-messages-error',
                    '.mensagem-erro',
                    '.ui-growl-message',
                    '#frmLogin .ui-message-error',
                ];
                for (const s of seletores) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                }
                // fallback: varrer body por frase caracteristica
                const body = (document.body.innerText || '').toLowerCase();
                const pistas = [
                    'usu\u00e1rio ou senha', 'usuario ou senha',
                    'senha incorreta', 'senha inv\u00e1lida', 'senha invalida',
                    'credenciais inv\u00e1lidas', 'credenciais invalidas',
                    'login inv\u00e1lido', 'login invalido',
                ];
                for (const p of pistas) {
                    const i = body.indexOf(p);
                    if (i >= 0) return body.substr(Math.max(0, i - 20), 120);
                }
                return null;
            }"""
        )
        if msg_recusa:
            raise CredenciaisInvalidas(msg_recusa[:200]) from None
        # Nao foi login fail explicito — repropaga como timeout.
        raise
    # Login OK (chegou na caixaEntrada): sessão saudável → limpa eventual conflito.
    from services import credenciais_service as _cred
    _cred.marcar_conectado()


async def _ensure_logged_in() -> None:
    """Garante que estamos logados. Refaz login se sessao expirou."""
    global _browser, _page
    page = await _get_page()
    try:
        url = page.url
        if "login" in url or not url.startswith("http"):
            await _login()
            return
        await page.goto(
            f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await page.wait_for_timeout(500)
        if "login" in page.url:
            await _login()
    except Exception:
        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        _browser = None
        _page = None
        await _login()


async def testar_login() -> dict:
    """Testa o acesso ATUAL ao SISDPU (headless). Usa `_ensure_logged_in`, que
    APROVEITA uma sessão já ativa (não força a tela de login) e só faz um login
    novo se não houver sessão — validando as credenciais nesse caso.
    Retorna {ok, status, detalhe}:
      - 'ok'           -> acesso confirmado (sessão ativa ou login bem-sucedido);
      - 'credenciais'  -> o SISDPU recusou usuário/senha (conferir);
      - 'indisponivel' -> não há credenciais carregadas (painel trancado);
      - 'erro'         -> falha de conexão/SISDPU (tentar de novo).
    """
    try:
        await _ensure_logged_in()
        page = await _get_page()
        if "login" in (page.url or "").lower():
            return {"ok": False, "status": "credenciais",
                    "detalhe": "O SISDPU recusou o usuário/senha. Confira as credenciais."}
    except CredenciaisInvalidas:
        return {"ok": False, "status": "credenciais",
                "detalhe": "O SISDPU recusou o usuário/senha. Confira as credenciais."}
    except RuntimeError as e:
        return {"ok": False, "status": "indisponivel", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001 — timeout/rede/SISDPU fora do ar
        return {"ok": False, "status": "erro",
                "detalhe": f"Não foi possível testar agora ({e}). "
                           "Verifique a conexão/o SISDPU e tente de novo."}
    return {"ok": True, "status": "ok",
            "detalhe": "Acesso confirmado — usuário e senha corretos."}


async def reconectar() -> dict:
    """Força uma RECONEXÃO no SISDPU (recupera de conflito de sessão / sessão derrubada
    por outro login). Fecha o navegador atual (descarta a sessão possivelmente inválida)
    e refaz o login com as credenciais ATIVAS — criando uma sessão nova (que assume a
    vez, derrubando eventuais sessões concorrentes do mesmo usuário).
    Retorna {ok, status, detalhe}."""
    global _browser, _page
    with contextlib.suppress(Exception):
        if _browser:
            await _browser.close()
    _browser = None
    _page = None
    try:
        await _login()  # login fresco com as credenciais ativas
        page = await _get_page()
        if "login" in (page.url or "").lower():
            return {"ok": False, "status": "credenciais",
                    "detalhe": "O SISDPU recusou o usuário/senha ao reconectar. Confira as credenciais."}
    except CredenciaisInvalidas:
        return {"ok": False, "status": "credenciais",
                "detalhe": "O SISDPU recusou o usuário/senha ao reconectar. Confira as credenciais."}
    except RuntimeError as e:
        return {"ok": False, "status": "indisponivel", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "erro",
                "detalhe": f"Não foi possível reconectar ({e}). Verifique a conexão/o SISDPU e tente de novo."}
    return {"ok": True, "status": "ok",
            "detalhe": "Reconectado ao SISDPU — sessão do painel reativada."}


async def caixa_de_entrada() -> dict:
    """Navega para a caixa de entrada e extrai os itens listados."""
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    body = await page.inner_text("body")

    table_data = await page.evaluate(
        """
        () => {
            const tables = document.querySelectorAll(
                '.ui-datatable-data tr, .ui-datalist-content .ui-datalist-item'
            );
            if (tables.length > 0) {
                return Array.from(tables).map(tr => tr.innerText.trim())
                    .filter(t => t.length > 0);
            }
            const allTr = document.querySelectorAll('table tbody tr');
            return Array.from(allTr).map(tr => tr.innerText.trim())
                .filter(t => t.length > 0);
        }
        """
    )

    return {
        "url": page.url,
        "itens_tabela": table_data or [],
        "texto_pagina": body[:5000],
    }


# Valor-semente da DESCOBERTA de caixas: serve só para ABRIR a página de caixas de
# entrada de grupo e LISTAR todas as caixas (grupos_disponiveis reúne TODOS os
# cabeçalhos da página, independentemente deste valor). É de propósito um texto que
# NÃO casa com nenhuma caixa real — assim a descoberta não extrai/enriquece PAJs de
# nenhuma caixa à toa. Agnóstico à unidade: cada unidade tem suas próprias caixas.
CAIXA_GRUPO_SEED = "(descoberta de caixas)"

# JS que extrai as linhas de UMA caixa de grupo (a que casa com `nomeGrupo`).
# Isolado como constante para poder ser testado offline contra um HTML salvo.
# Usa `:scope > td` para pegar SO as celulas de nivel da linha (a coluna
# "Detalhes" tem uma tabela aninhada de icones de acao cujos <td> internos, de
# outro modo, bagunçariam os indices das colunas).
_JS_EXTRAIR_CAIXA_GRUPO = r"""
(nomeGrupo) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const alvo = norm(nomeGrupo);

    // Cada caixa de grupo tem um header com <strong>NOME</strong>.
    const headers = Array.from(document.querySelectorAll('.ui-datatable-header'));
    const disponiveis = [];
    let tabelaAlvo = null;
    let matchExato = false;
    for (const h of headers) {
        const strong = h.querySelector('strong');
        const nome = strong ? strong.textContent.trim() : '';
        if (!nome) continue;
        disponiveis.push(nome);
        if (matchExato) continue;
        if (norm(nome) === alvo) {
            tabelaAlvo = h.closest('.ui-datatable');
            matchExato = true;
        } else if (tabelaAlvo === null && norm(nome).includes(alvo)) {
            tabelaAlvo = h.closest('.ui-datatable');
        }
    }

    const itens = [];
    if (tabelaAlvo) {
        const linhas = tabelaAlvo.querySelectorAll('tbody tr[data-rk]');
        for (const tr of linhas) {
            const idTramite = tr.getAttribute('data-rk') || '';
            const tds = Array.from(tr.querySelectorAll(':scope > td'));

            // Celula "Processo" = a que contem o link de detalhamento.
            let pIdx = -1, linkDet = null;
            for (let i = 0; i < tds.length; i++) {
                const a = tds[i].querySelector('a[href*="detalhamentoProcesso"]');
                if (a) { pIdx = i; linkDet = a; break; }
            }
            if (pIdx < 0) continue;

            const textoProc = (linkDet.textContent || '').replace(/\s+/g, ' ').trim();
            const mPaj = textoProc.match(/(\d{4}\/\d{3}-\d+)\s*(.*)$/);
            const paj = mPaj ? mPaj[1] : '';
            let oficio = mPaj ? mPaj[2].trim() : '';
            oficio = oficio.replace(/^\(\s*/, '').replace(/\s*\)$/, '').trim();

            const href = linkDet.getAttribute('href') || '';
            const mId = href.match(/[?&]id=(\d+)/);
            const idProc = mId ? mId[1] : '';
            const urlDet = href
                ? (href.startsWith('http') ? href : (location.origin + href))
                : '';

            const cellText = (i) =>
                (tds[i] ? (tds[i].textContent || '').replace(/\s+/g, ' ').trim() : '');
            // Data de envio: prefere um valor COM hora (às vezes o horário
            // completo está só no tooltip/title da célula, não no texto visível).
            const cellRich = (i) => {
                const td = tds[i];
                if (!td) return '';
                const txt = (td.textContent || '').replace(/\s+/g, ' ').trim();
                const ttl = (td.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
                return /\d{2}:\d{2}/.test(ttl) ? ttl : txt;
            };

            itens.push({
                id_tramite: idTramite,
                id: idProc,
                paj: paj,
                oficio_origem: oficio,
                remetente: cellText(pIdx + 1),
                data_envio: cellRich(pIdx + 2),
                prazo: cellText(pIdx + 3),
                url_detalhe: urlDet,
            });
        }
    }

    return {
        encontrado: tabelaAlvo !== null,
        grupos_disponiveis: disponiveis,
        itens: itens,
    };
}
"""


# Lê o diálogo "Detalhes do trâmite" (formC:tramiteDetalhamentoDlg), já aberto.
_JS_LER_POPUP_TRAMITE = r"""() => {
    const d = document.querySelector('[id="formC:tramiteDetalhamentoDlg"]');
    if (!d) return {};
    const t = (d.innerText || '').replace(/\r/g, '');
    // 1ª data-hora do diálogo = "Data do trâmite" (o Prazo vem depois).
    const dt = t.match(/(\d{2}\/\d{2}\/\d{4}\s+\d{2}:\d{2}:\d{2})/);
    // Se a "descrição" capturada for, na verdade, o RÓTULO do próximo campo (ex.:
    // "Assistido(s):"), a descrição está VAZIA — devolve '' em vez de vazar o campo
    // seguinte. (Um rótulo = palavra-chave + "(s)" opcional + ":", tudo no início.)
    const ehRotulo = (v) => /^(Assistido|Atribui[çc][ãa]o|Data do tr[âa]mite|Situa[çc][ãa]o|Órg[ãa]o)\s*(\(s\))?\s*:/i.test(v);
    const pega = (rot) => {
        const m = t.match(new RegExp(rot + '\\s*:?\\s*\\n\\s*([^\\n]+)', 'i'));
        if (!m) return '';
        const v = m[1].trim();
        return ehRotulo(v) ? '' : v;
    };
    return {
        data_envio: dt ? dt[1] : '',
        descricao_tramite: pega('Descri[çc][ãa]o do tr[âa]mite'),
        atribuicao: pega('Atribui[çc][ãa]o'),
    };
}"""


async def _ler_popup_tramite(page: Page, id_tramite: str) -> dict:
    """Abre o popup "Detalhes do trâmite" de UMA tramitação (clique no ícone da coluna
    Detalhes), lê data-hora/descrição/atribuição e FECHA o diálogo (é modal, senão
    bloqueia o próximo clique). Best-effort: erro → {}."""
    clicou = await page.evaluate(
        """(idt) => {
            const tr = document.querySelector('tr[data-rk="' + idt + '"]');
            if (!tr) return false;
            const tds = [...tr.querySelectorAll(':scope > td')];
            const a = tds[1] && tds[1].querySelector('a');
            if (!a) return false;
            a.click();
            return true;
        }""",
        id_tramite,
    )
    if not clicou:
        return {}
    await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(500)
    dados = await page.evaluate(_JS_LER_POPUP_TRAMITE)
    # Fecha o diálogo modal para liberar o clique da próxima tramitação.
    await page.evaluate(
        """() => {
            const d = document.querySelector('[id="formC:tramiteDetalhamentoDlg"]');
            const x = d && d.querySelector('.ui-dialog-titlebar-close');
            if (x) x.click();
        }"""
    )
    await page.wait_for_timeout(300)
    return dados or {}


async def listar_caixa_grupo(nome_grupo: str = CAIXA_GRUPO_SEED) -> dict:
    """Lista os PAJs de UMA caixa de entrada de grupo (ex.: "01 - PREV_AJUIZADOS").

    A caixa de entrada do SISDPU tem uma aba "Caixa de Entrada do Grupo" que
    empilha varias caixas de grupo, cada uma renderizada como um datatable
    proprio com um cabecalho <strong>NOME DO GRUPO</strong>. Esta funcao ativa
    essa aba, localiza o datatable cujo cabecalho casa com `nome_grupo` e extrai
    as linhas.

    Cada linha tem as colunas: Detalhes (acoes), Processo (nº PAJ + oficio de
    origem, com link p/ o detalhamento), Remetente, Data de Envio, Prazo e
    Admitir. A providencia de contato NAO esta aqui — fica nas movimentacoes do
    detalhamento (obtida depois via detalhar_paj usando o `id` retornado).

    Retorna dict com: grupo, encontrado (bool), grupos_disponiveis (lista de
    nomes de todas as caixas de grupo vistas), url e itens (lista de PAJs com
    id_tramite, id, paj, oficio_origem, remetente, data_envio, prazo e
    url_detalhe).
    """
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(1500)
    await _wait_pf_ajax(page, timeout=10000)

    # Ativa a aba "Caixa de Entrada do Grupo" (o conteudo pode carregar via AJAX
    # ao clicar — por isso esperamos o PrimeFaces terminar depois). Se a aba ja
    # estiver ativa (ou nao existir), o clique e' no-op.
    await page.evaluate(
        """() => {
            const links = document.querySelectorAll('.ui-tabs-nav a, a[href*="tabView:tab"]');
            for (const a of links) {
                if ((a.textContent || '').toLowerCase().includes('caixa de entrada do grupo')) {
                    a.click();
                    return true;
                }
            }
            return false;
        }"""
    )
    await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(1200)

    resultado = await page.evaluate(_JS_EXTRAIR_CAIXA_GRUPO, nome_grupo)

    resultado = resultado or {}
    resultado["grupo"] = nome_grupo
    resultado["url"] = page.url
    _log.info(
        "listar_caixa_grupo(%s): encontrado=%s itens=%d grupos_disponiveis=%s",
        nome_grupo,
        resultado.get("encontrado"),
        len(resultado.get("itens", []) or []),
        resultado.get("grupos_disponiveis"),
    )

    # Enriquecimento via popup "Detalhes do trâmite" — para TODAS as tramitações.
    # A "Descrição do trâmite" é essencial para a análise: em PAJs sem movimentação
    # explicativa, o motivo do contato pode estar SÓ nela. Best-effort: captura
    # data-hora exata, descrição da tramitação e atribuição por tramitação.
    # (Antes só enriquecíamos PAJs com 2+ tramitações — candidatos a triagem —
    # para poupar tempo; agora priorizamos qualidade de análise.)
    itens = resultado.get("itens", []) or []
    alvos = [it for it in itens if it.get("id_tramite")]
    if alvos:
        _log.info("listar_caixa_grupo(%s): enriquecendo %d tramitação(ões) via popup (todas).",
                  nome_grupo, len(alvos))
        for it in alvos:
            try:
                extra = await _ler_popup_tramite(page, it["id_tramite"])
            except Exception as e:  # noqa: BLE001
                _log.warning("  popup tramitação %s falhou: %s", it.get("id_tramite"), e)
                extra = {}
            if extra.get("data_envio"):
                it["data_envio"] = extra["data_envio"]  # agora com hora
            if extra.get("descricao_tramite"):
                it["descricao_tramite"] = extra["descricao_tramite"]
            if extra.get("atribuicao"):
                it["atribuicao"] = extra["atribuicao"]
    return resultado


# JS extraido do DETALHAMENTO: app_sisdpu, nome do assistido e a lista de
# assistidos PF (com idPessoa) para depois abrir o cadastro de cada um.
#
# app_sisdpu: o cabecalho tem o <span id="detalhamentoForm:lnkNotificarAssistido">
# que fica VAZIO quando o assistido nao aderiu ao app, e contem um <a> (icone
# phone.png, title "Este PAJ possui acompanhamento pelo App") quando aderiu.
# Reforcamos com a frase de adesao que aparece nas movimentacoes.
_JS_DETALHAR_PAJ = r"""
(nMovs) => {
    const r = {
        assistido: "",
        contatos: { app_sisdpu: false, telefone: false, whatsapp: false, email: false, endereco: false },
        assistidos_pf: []
    };
    const bodyTxt = document.body.innerText || "";
    const span = document.getElementById("detalhamentoForm:lnkNotificarAssistido");

    // --- App SISDPU ---
    const temLinkApp = !!(span && span.querySelector("a"));
    const temFraseApp = /Ades[aã]o ao Termo de Responsabilidade do Aplicativo do SISDPU/i.test(bodyTxt);
    r.contatos.app_sisdpu = temLinkApp || temFraseApp;

    // --- Nome do assistido (span logo apos o lnkNotificarAssistido) ---
    if (span && span.nextElementSibling) {
        r.assistido = (span.nextElementSibling.textContent || "").replace(/\s+/g, " ").trim();
    }

    // --- Assistidos PF (idPessoa) — links p/ o Cadastro de Pessoa Fisica ---
    const tbl = document.getElementById("detalhamentoForm:dataTableListaAssistidosPF");
    if (tbl) {
        for (const a of tbl.querySelectorAll('a[href*="cadastroAssistidoPessoaFisica"]')) {
            const href = a.getAttribute("href") || "";
            const mp = href.match(/idPessoa=(\d+)/);
            if (mp) {
                const nome = (a.textContent || "").replace(/\(\s*\d+\s*PAJ.*$/i, "").replace(/\s+/g, " ").trim();
                r.assistidos_pf.push({ idPessoa: mp[1], nome: nome });
            }
        }
    }

    // --- Ultimas movimentacoes (para a analise da solicitacao) ---
    // A tabela de movimentacoes tem colunas: seq(0) anexo(1) data/hora(2)
    // movimentacao/tipo(3) fases(4) descricao(5) ... usuario. Filtramos por seq
    // numerico + celula de data (DD/MM/AAAA) para isolar essa tabela.
    const movs = [];
    // indice da coluna "Usuario" descoberto pelo CABECALHO de cada tabela (a posicao
    // pode variar); cache por tabela. Fallback = ultima celula (usuario e' a ultima coluna).
    const _uCache = new Map();
    const _idxUsuario = (table) => {
        if (!table) return -1;
        if (_uCache.has(table)) return _uCache.get(table);
        let idx = -1;
        table.querySelectorAll("thead th").forEach((th, i) => {
            if (idx === -1 && /usu[aá]rio/i.test(th.textContent || "")) idx = i;
        });
        _uCache.set(table, idx);
        return idx;
    };
    for (const tr of document.querySelectorAll("table tbody tr")) {
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length < 6) continue;
        const seq = (cells[0].textContent || "").trim();
        const data = (cells[2].textContent || "").trim();
        if (!/^\d{1,4}$/.test(seq)) continue;
        if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
        const uIdx = _idxUsuario(tr.closest("table"));
        const uCell = (uIdx >= 0 && cells[uIdx]) ? cells[uIdx] : cells[cells.length - 1];
        movs.push({
            seq: parseInt(seq, 10),
            data: data,
            tipo: (cells[3].textContent || "").replace(/\s+/g, " ").trim(),
            fases: (cells[4].textContent || "").replace(/\s+/g, " ").trim(),
            descricao: (cells[5].textContent || "").replace(/\s+/g, " ").trim(),
            usuario: (uCell ? uCell.textContent : "").replace(/\s+/g, " ").trim(),
        });
    }
    movs.sort((a, b) => b.seq - a.seq);   // mais recentes primeiro
    r.movimentacoes = movs.slice(0, nMovs || 5);

    return r;
}
"""

# JS extraido do CADASTRO DE PESSOA FISICA: telefones (com observacao), emails e
# enderecos. Os valores ficam em <input>/<select> (formulario), entao lemos
# .value / opcao selecionada — nao textContent.
#  - Contatos: em #tabelaContatos. Cada contato tem um input `pessoaContatoDescricao`
#    (o numero/email) cujo label ("Telefone:"/"Email:"/"Fax:") indica o tipo; a
#    observacao e' o input irmao `...pessoaContatoDescricaoTelefone` de mesmo indice.
#  - Enderecos: inputs/selects `pessoaEnderecoTipo/Logradouro/Numero/Complemento/
#    Bairro/Cep` + selects `enderecoUF`/`enderecoMunicipio`, agrupados pelo indice `:N:`.
_JS_CADASTRO_PF = r"""
() => {
    const val = (el) => el.tagName === "SELECT"
        ? (((el.options[el.selectedIndex] || {}).text) || "").replace(/\s+/g, " ").trim()
        : (el.value || "").replace(/\s+/g, " ").trim();

    const telefones = [], emails = [], enderecos = [];

    // ---------- Contatos ----------
    const tc = document.getElementById("tabelaContatos");
    if (tc) {
        const inputs = Array.from(tc.querySelectorAll("input"));
        for (const el of inputs) {
            const name = el.name || el.id || "";
            const m = name.match(/:(\d+):pessoaContatoDescricao$/);
            if (!m) continue;                       // ignora observacao e o resto
            const valor = val(el);
            if (!valor) continue;
            let tipo = "";
            if (el.id) {
                const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (l) tipo = l.textContent.trim();
            }
            const idx = m[1];
            let obs = "";
            const o = inputs.find(x =>
                new RegExp(":" + idx + ":pessoaContatoDescricaoTelefone$").test(x.name || x.id || ""));
            if (o) obs = val(o);
            if (/email/i.test(tipo)) emails.push(valor);
            else if (/telefone/i.test(tipo)) telefones.push({ numero: valor, obs: obs });
            // Fax e outros tipos sao ignorados
        }
    }

    // ---------- Enderecos ----------
    const chaves = ["pessoaEnderecoTipo","pessoaEnderecoLogradouro","pessoaEnderecoNumero",
                    "pessoaEnderecoComplemento","pessoaEnderecoBairro","enderecoMunicipio",
                    "enderecoUF","pessoaEnderecoCep"];
    const porIdx = {};
    for (const el of document.querySelectorAll("input,select")) {
        const name = el.name || el.id || "";
        for (const k of chaves) {
            const mm = name.match(new RegExp(":(\\d+):" + k + "$"));
            if (mm) { (porIdx[mm[1]] = porIdx[mm[1]] || {})[k] = val(el); break; }
        }
    }
    for (const idx of Object.keys(porIdx)) {
        const e = porIdx[idx];
        if (!(e.pessoaEnderecoLogradouro || e.pessoaEnderecoBairro || e.pessoaEnderecoCep)) continue;
        enderecos.push({
            tipo: e.pessoaEnderecoTipo || "", logradouro: e.pessoaEnderecoLogradouro || "",
            numero: e.pessoaEnderecoNumero || "", complemento: e.pessoaEnderecoComplemento || "",
            bairro: e.pessoaEnderecoBairro || "", municipio: e.enderecoMunicipio || "",
            uf: e.enderecoUF || "", cep: e.pessoaEnderecoCep || ""
        });
    }

    return { telefones, emails, enderecos };
}
"""


async def detalhar_paj(id_processo: str, n_movs: int = 5) -> dict:
    """Abre o detalhamento de um PAJ e o cadastro de cada assistido PF, e extrai
    todos os dados de contato disponiveis.

    Retorna:
        {
          "assistido": "NOME",
          "contatos": {app_sisdpu, telefone, whatsapp, email, endereco: bool},
          "telefones": [{"numero": "(41)...", "obs": "MAE"}, ...],
          "emails": ["x@y.com", ...],
          "enderecos": [{tipo, logradouro, numero, complemento, bairro, municipio, uf, cep}, ...],
        }
    whatsapp ainda retorna False (a mapear com o usuario).
    """
    await _ensure_logged_in()
    page = await _get_page()

    # 1) Detalhamento — app_sisdpu, nome, e idPessoa dos assistidos PF.
    await page.goto(
        f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}",
        wait_until="domcontentloaded",
    )
    await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(800)
    base = await page.evaluate(_JS_DETALHAR_PAJ, n_movs)
    base = base or {}

    contatos = base.get("contatos") or {
        "app_sisdpu": False, "telefone": False, "whatsapp": False, "email": False, "endereco": False
    }
    telefones: list[dict] = []
    emails: list[str] = []
    enderecos: list[dict] = []

    # 2) Cadastro de Pessoa Fisica de cada assistido — telefones/emails/enderecos.
    for a in base.get("assistidos_pf", []) or []:
        idp = a.get("idPessoa")
        if not idp:
            continue
        try:
            await page.goto(
                f"{SISDPU_URL}/pages/atendimento/cadastroAssistidoPessoaFisicaCombotoes.xhtml"
                f"?idProcesso={id_processo}&idPessoa={idp}",
                wait_until="domcontentloaded",
            )
            await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(600)
            cad = await page.evaluate(_JS_CADASTRO_PF)
        except Exception as e:  # noqa: BLE001
            _log.warning("cadastro PF idPessoa=%s falhou: %s", idp, e)
            continue
        telefones.extend(cad.get("telefones", []) or [])
        emails.extend(cad.get("emails", []) or [])
        enderecos.extend(cad.get("enderecos", []) or [])

    contatos["telefone"] = len(telefones) > 0
    contatos["email"] = len(emails) > 0
    contatos["endereco"] = len(enderecos) > 0

    resultado = {
        "assistido": base.get("assistido", ""),
        "contatos": contatos,
        "telefones": telefones,
        "emails": emails,
        "enderecos": enderecos,
        "movimentacoes": base.get("movimentacoes", []),
    }
    _log.info(
        "detalhar_paj(id=%s): assistido=%r app=%s tel=%d email=%d end=%d",
        id_processo,
        resultado["assistido"],
        contatos["app_sisdpu"],
        len(telefones), len(emails), len(enderecos),
    )
    return resultado


# ---------------------------------------------------------------------------
# ENVIO de NOTIFICACAO pelo Aplicativo SISDPU (tela "Notificar Assistido").
# Seletores confirmados ao vivo (PAJ de teste 2023/029-00839, id=3498632):
#   form ........ formInclusaoNotificacao
#   assistidos .. formInclusaoNotificacao:dataTableListaAssistidos
#                 (1 checkbox .ui-chkbox-box por linha; NAO vem pre-marcado)
#   prazo ....... formInclusaoNotificacao:valorProcesso_input  (text, default 5)
#   so-leitura .. formInclusaoNotificacao:isSomenteLeituraSelect_input (checkbox)
#   texto ....... formInclusaoNotificacao:rteTextoNotificacao
#                 (editor QUILL: .ql-editor contenteditable + input escondido _input)
#   Enviar ...... <button> texto "Enviar" (id volatil ...:j_idtNNN -> achamos por texto)
# Modais que bloqueiam/avisam (tratados): sessao assumida em outra estacao,
# "notificacao pendente nao respondida", "PAJ sem assistido pelo App".
# ---------------------------------------------------------------------------
_NOTIF_FORM = "formInclusaoNotificacao"
# Tipos de anexo aceitos pelo SISDPU na tela Notificar Assistido.
_TIPOS_ANEXO_OK = {"doc", "docx", "odt", "png", "jpg", "jpeg", "gif", "pdf"}


def _norm_nome(s: str) -> str:
    """Normaliza nome p/ comparar: sem acentos, MAIUSCULO, sem '(N PAJ'S)', 1 espaco."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(\s*\d+\s*paj.*?\)", "", s, flags=re.I)  # remove sufixo "(1 PAJ'S)"
    return re.sub(r"\s+", " ", s).strip().upper()


_JS_ESTADO_NOTIFICACAO = r"""
() => {
    const body = document.body.innerText || '';
    const growls = [...document.querySelectorAll('.ui-growl-item, .ui-growl-message, .ui-messages-info, .ui-messages')]
        .map(g => (g.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    const dialogVis = (frag) => [...document.querySelectorAll('.ui-dialog')].some(d =>
        d.offsetParent !== null && new RegExp(frag, 'i').test(d.textContent || ''));
    return {
        url: location.href,
        growls,
        sucesso: /Notifica[çc][ãa]o enviada com sucesso/i.test(body)
                 || growls.some(g => /enviada com sucesso/i.test(g)),
        pendente: dialogVis('notifica[çc][ãa]o nova n[ãa]o respondida')
                  || dialogVis('possuem notifica[çc][ãa]o'),
        takeover: dialogVis('outra esta[çc][ãa]o') || dialogVis('efetuado em outra'),
        semApp: dialogVis('n[ãa]o possui assistido sendo acompanhado'),
        // Conflito de sessão (1 sessão por usuário): diálogo 'confirmDialogLogin'
        // ('Atenção' com 'Desbloquear'/'Sair'). Só bloqueia quando VISÍVEL — a máscara
        // '..._modal' só existe no DOM quando o diálogo está aberto.
        conflitoLogin: !!document.querySelector('[id$="confirmDialogLogin_modal"]')
                       || [...document.querySelectorAll('[id$="confirmDialogLogin"]')]
                              .some(d => d.offsetParent !== null),
    };
}
"""


async def _eval_seguro(page: Page, js: str, tentativas: int = 5) -> dict:
    """page.evaluate tolerante a navegacao: a tela Notificar re-navega (postback
    JSF no load; e o clique em Enviar redireciona ao detalhamento) — quando isso
    ocorre no meio do evaluate o Chromium destroi o contexto. Aqui re-tentamos
    apos a navegacao assentar, em vez de estourar."""
    ultimo: Exception | None = None
    for _ in range(tentativas):
        try:
            return await page.evaluate(js)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "context was destroyed" in msg or "navigation" in msg or "execution context" in msg:
                ultimo = e
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                await page.wait_for_timeout(500)
                continue
            raise
    if ultimo:
        raise ultimo
    return await page.evaluate(js)


# Mensagem exibida no log/selo quando o conflito de sessão persiste mesmo após tentar
# assumir a sessão ("Desbloquear").
_MSG_CONFLITO_SESSAO = ("Conflito de sessão do SISDPU — feche a outra sessão e tente de novo.")


async def _resolver_conflito_login(page: Page) -> bool:
    """Se o diálogo de conflito de sessão (confirmDialogLogin) estiver VISÍVEL, clica
    'Desbloquear' (o painel ASSUME a sessão, derrubando a outra) e aguarda o diálogo
    sumir. Retorna True se não há conflito (ou foi resolvido); False se persiste."""
    from services import credenciais_service as _cred
    est = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
    if not est.get("conflitoLogin"):
        _cred.marcar_conectado()
        return True
    _log.info("notificar_assistido: conflito de sessão detectado — clicando 'Desbloquear'.")
    btn = page.locator('[id$="confirmTramitacao"]').first  # botão "Desbloquear"
    with contextlib.suppress(Exception):
        if await btn.count():
            await btn.click(timeout=5000)  # clique REAL (não JS)
    with contextlib.suppress(Exception):  # espera a máscara do diálogo sumir
        await page.wait_for_selector('[id$="confirmDialogLogin_modal"]', state="hidden", timeout=8000)
    await page.wait_for_timeout(300)
    est2 = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
    resolvido = not est2.get("conflitoLogin")
    _cred.marcar_conectado() if resolvido else _cred.marcar_conflito()
    return resolvido


async def checar_conflito_sessao(id_processo: str | None = None) -> dict:
    """Checagem PRÉVIA de conflito de sessão do SISDPU (usuário logado em outra
    estação/navegador — o SISDPU só admite 1 sessão por usuário). Navega a uma página
    do SISDPU e detecta o diálogo 'confirmDialogLogin'. NÃO resolve (não assume a
    sessão) — apenas reporta, para o painel ABORTAR o envio e avisar o usuário.
    Retorna {conflito: bool, detalhe}."""
    try:
        await _ensure_logged_in()
        page = await _get_page()
        url = (f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml?id={id_processo}&caixaEntrada=false"
               if id_processo else f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page, timeout=8000)
        await page.wait_for_timeout(500)
        from services import credenciais_service as _cred
        est = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
        if est.get("conflitoLogin"):
            _log.info("checar_conflito_sessao: CONFLITO detectado (id=%s).", id_processo)
            _cred.marcar_conflito(_MSG_CONFLITO_SESSAO)
            return {"conflito": True, "detalhe": _MSG_CONFLITO_SESSAO}
        _cred.marcar_conectado()
        return {"conflito": False, "detalhe": ""}
    except Exception as e:  # noqa: BLE001
        # Falha na checagem NÃO deve bloquear o envio — retorna sem conflito (o fluxo
        # normal ainda tem _resolver_conflito_login como rede de segurança).
        _log.warning("checar_conflito_sessao falhou (%r) — seguindo sem bloquear.", e)
        return {"conflito": False, "detalhe": ""}


async def _anexar_documentos(page: Page, anexos: list[dict]) -> list[str]:
    """Anexa cada documento na tela Notificar Assistido (popup 'Novo documento').

    Preserva o NOME ORIGINAL do arquivo (upload por buffer — o disco guarda um nome
    'aid__seguro') e usa esse nome como Descrição. Retorna os nomes efetivamente
    anexados. Best-effort: um anexo que falhe é pulado (logado), sem abortar os demais.
    Tipos aceitos pelo SISDPU: DOC, DOCX, ODT, PNG, JPEG, GIF, PDF.
    """
    import mimetypes

    anexados: list[str] = []
    for a in (anexos or []):
        caminho = a.get("caminho") or ""
        nome = a.get("nome") or (Path(caminho).name if caminho else "documento")
        if not caminho or not Path(caminho).exists():
            _log.warning("anexo ausente no disco, pulado: %s (%s)", nome, caminho)
            continue
        ext = (a.get("tipo") or Path(nome).suffix).lstrip(".").lower()
        if ext not in _TIPOS_ANEXO_OK:
            # SISDPU só aceita DOC/DOCX/ODT/PNG/JPEG/GIF/PDF — tipo diferente trava
            # o popup; pulamos (best-effort) para não abortar os demais anexos.
            _log.warning("anexo de tipo não permitido pelo SISDPU, pulado: %s (.%s)", nome, ext)
            continue
        try:
            data = Path(caminho).read_bytes()
            mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
            # abre o popup "Novo documento"
            await page.locator(f'[id="{_NOTIF_FORM}"] button', has_text="Incluir novo anexo").first.click()
            await page.wait_for_selector('[id="dialogoInclusaoForm:arquivosAnexo_input"]', timeout=10000)
            # seleciona o arquivo preservando o nome original (buffer)
            await page.locator('[id="dialogoInclusaoForm:arquivosAnexo_input"]').set_input_files(
                [{"name": nome, "mimeType": mime, "buffer": data}])
            # aguarda a linha "Arquivos para salvar" e ajusta a Descrição = nome
            desc = page.locator(
                'input[type="text"][name^="dialogoInclusaoForm:tableArquivosSalvar"]').first
            await desc.wait_for(timeout=10000)
            with contextlib.suppress(Exception):
                await desc.fill(nome)
            # "Anexar" -> mensagem "Arquivo incluído ao processo"
            await page.locator(
                'button[name^="dialogoInclusaoForm:tableArquivosSalvar"]', has_text="Anexar").first.click()
            with contextlib.suppress(Exception):
                await page.wait_for_function(
                    r"""() => /inclu[íi]do ao processo/i.test(document.body.innerText || '')""",
                    timeout=8000)
            await page.wait_for_timeout(400)
            # fecha o popup (se ainda aberto) p/ voltar à tela Notificar
            with contextlib.suppress(Exception):
                fechar = page.locator('button[name^="dialogoInclusaoForm:"]', has_text="Fechar").first
                if await fechar.is_visible():
                    await fechar.click()
                    await page.wait_for_timeout(400)
            anexados.append(nome)
            _log.info("anexo incluído: %s", nome)
        except Exception as e:  # noqa: BLE001
            _log.warning("falha ao anexar %s: %r", nome, e)
    return anexados


# Lê o MAIOR nº de sequência da tabela de movimentações do detalhamento (mesmos
# critérios do _JS_DETALHAR_PAJ: seq numérico + célula de data DD/MM/AAAA). Após o
# envio pelo App (que auto-movimenta o PAJ), a movimentação recém-criada é a de
# maior seq.
_JS_MAX_SEQ_MOV = r"""
() => {
    let max = null;
    for (const tr of document.querySelectorAll("table tbody tr")) {
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length < 6) continue;
        const seq = (cells[0].textContent || "").trim();
        const data = (cells[2].textContent || "").trim();
        if (!/^\d{1,4}$/.test(seq)) continue;
        if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
        const n = parseInt(seq, 10);
        if (max === null || n > max) max = n;
    }
    return max;
}
"""


async def _ler_seq_notificacao(page, id_processo: str) -> int | None:
    """Após o envio pelo App (o PAJ é auto-movimentado), lê o nº de sequência da
    movimentação recém-criada = a de MAIOR seq no detalhamento. Best-effort: navega
    ao detalhamento (dados frescos) e lê; qualquer erro devolve None."""
    try:
        url = f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(300)
        seq = await _eval_seguro(page, _JS_MAX_SEQ_MOV)
        return int(seq) if seq is not None else None
    except Exception:  # noqa: BLE001
        return None


async def notificar_assistido(
    id_processo: str,
    mensagem: str,
    prazo_dias: int = 5,
    somente_leitura: bool = False,
    anexos: list[dict] | None = None,
    dry_run: bool = False,
    screenshot_path: str | None = None,
) -> dict:
    """Envia uma notificacao ao assistido pelo Aplicativo SISDPU.

    Seleciona TODOS os assistidos disponíveis na tela (a tela não distingue quem
    aderiu ao App; quem aderiu recebe, os demais ficam pendentes). Retorna
    {ok, status, detalhe}. status: 'enviado' | 'dry_run' | 'falha'. Qualquer condicao
    que impeca o envio devolve ok=False — assim o item PERMANECE na fila.
    """
    # prazo 0 no painel = "urgente" (prioridade na fila), NAO um prazo real de 0
    # dias — para a notificacao do App usamos o default 5 nesse caso.
    try:
        prazo = int(prazo_dias)
    except (TypeError, ValueError):
        prazo = 5
    if prazo <= 0:
        prazo = 5

    await _ensure_logged_in()
    page = await _get_page()
    url = f"{SISDPU_URL}/pages/movimentacao/notificarAssistido.xhtml?id={id_processo}"
    _log.info("notificar_assistido(id=%s prazo=%s SL=%s dry=%s)",
              id_processo, prazo, somente_leitura, dry_run)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # A tela pode re-navegar (postback JSF) logo apos o load — deixa assentar.
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(400)

    est = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
    if est.get("takeover"):
        return {"ok": False, "status": "falha",
                "detalhe": "Sessão do SISDPU assumida em outra estação — envio não realizado."}
    if est.get("semApp"):
        return {"ok": False, "status": "falha",
                "detalhe": "PAJ sem assistido acompanhado pelo App — envio não realizado."}
    # Aviso que aparece ao abrir a tela quando TODOS os assistidos acompanhados já têm
    # notificação nova não respondida ("...é necessário encerrar a Notificação para
    # prosseguir"): captura de imediato, sem tentar clicar atrás do modal.
    if est.get("pendente"):
        return {"ok": False, "status": "falha",
                "detalhe": ("Impossível enviar nova mensagem pelo aplicativo neste momento, "
                            "pois há mensagem anterior pendente de visualização pelo assistido.")}
    # Conflito de sessão (confirmDialogLogin): tenta ASSUMIR a sessão ('Desbloquear');
    # se persistir, falha com aviso claro.
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    try:
        await page.wait_for_selector(f'[id="{_NOTIF_FORM}:rteTextoNotificacao"]', timeout=15000)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "falha",
                "detalhe": f"Tela Notificar Assistido não carregou: {e}"}

    # ---------- 1) selecionar assistido(s) ----------
    # A datatable de assistidos popula um tique DEPOIS do editor — espera as linhas
    # aparecerem antes de contar (senao da' "0 assistidos" por corrida de tempo).
    tbl_sel = f'[id="{_NOTIF_FORM}:dataTableListaAssistidos"]'
    with contextlib.suppress(Exception):
        await page.wait_for_selector(tbl_sel, timeout=8000)
    with contextlib.suppress(Exception):
        await page.wait_for_selector(f'{tbl_sel} tbody > tr[data-rk]', timeout=5000)
    await page.wait_for_timeout(300)
    linhas = page.locator(f'{tbl_sel} tbody > tr[data-rk]')
    n = await linhas.count()
    if n == 0:
        # A tela lista os assistidos do PAJ, MENOS os que já têm uma notificação
        # anterior pendente de visualização/resposta. Zero = todos estão nesse estado
        # -> não dá para enviar agora; mantém na fila (permanece em Prontos).
        return {"ok": False, "status": "falha",
                "detalhe": ("Impossível enviar nova mensagem pelo aplicativo neste momento, "
                            "pois há mensagem anterior pendente de visualização pelo assistido.")}
    # Nomes dos assistidos disponíveis na tela (sem notificação pendente).
    nomes_tela: list[str] = []
    for i in range(n):
        nomes_tela.append((await linhas.nth(i).locator("td").nth(1).inner_text()).strip())

    # Um único assistido: seleciona o único. Mais de um: SELECIONA TODOS — a tela não
    # distingue quem aderiu ao App; quem aderiu recebe, e para quem não aderiu a
    # notificação fica pendente (some das opções futuras até ser lida/encerrada).
    alvos = list(range(n))
    nomes_sel: list[str] = []
    for i in alvos:
        nomes_sel.append(nomes_tela[i])
        box = linhas.nth(i).locator(".ui-chkbox-box").first
        cls = await box.get_attribute("class") or ""
        if "ui-state-active" not in cls:
            # O diálogo de conflito de sessão pode surgir num postback e sua máscara
            # intercepta o clique. Clique com timeout curto; se falhar, tenta assumir
            # a sessão e repete — se ainda bloquear, falha com aviso claro.
            try:
                await box.click(timeout=8000)
            except Exception:  # noqa: BLE001
                if not await _resolver_conflito_login(page):
                    return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}
                await box.click(timeout=8000)
            await page.wait_for_timeout(150)

    # ---------- 2) prazo da notificacao ----------
    # O spinner PrimeFaces desfaz um `fill()` normal (reverte p/ o valor anterior);
    # setar value + disparar input/change diretamente e' o que "pega".
    prazo_sel = f'[id="{_NOTIF_FORM}:valorProcesso_input"]'
    if await page.locator(prazo_sel).count():
        await page.evaluate(
            """([sel, v]) => {
                const el = document.querySelector(sel);
                if (el) {
                    el.value = String(v);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""", [prazo_sel, prazo])

    # ---------- 3) somente leitura ----------
    sl_inp = page.locator(f'[id="{_NOTIF_FORM}:isSomenteLeituraSelect_input"]')
    if await sl_inp.count():
        atual = await sl_inp.is_checked()
        if bool(somente_leitura) != atual:
            await page.locator(
                f'[id="{_NOTIF_FORM}:isSomenteLeituraSelect"] .ui-chkbox-box').first.click()
            await page.wait_for_timeout(150)

    # ---------- 4) texto da notificacao (editor Quill) ----------
    ed = page.locator(f'[id="{_NOTIF_FORM}:rteTextoNotificacao"] .ql-editor').first
    await ed.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    linhas_txt = (mensagem or "").split("\n")
    for idx, ln in enumerate(linhas_txt):
        if idx:
            await page.keyboard.press("Enter")
        if ln:
            await page.keyboard.type(ln)
    # valida/sincroniza o input escondido do editor; fallback injeta o HTML
    val = await page.locator(f'[id="{_NOTIF_FORM}:rteTextoNotificacao_input"]').input_value()
    if not (val or "").strip():
        html_corpo = "".join(
            f"<p>{_html.escape(ln)}</p>" if ln.strip() else "<p><br></p>"
            for ln in linhas_txt) or "<p><br></p>"
        await page.evaluate(
            """([html]) => {
                const ed = document.querySelector('[id="formInclusaoNotificacao:rteTextoNotificacao"] .ql-editor');
                const hid = document.getElementById('formInclusaoNotificacao:rteTextoNotificacao_input');
                if (ed) ed.innerHTML = html;
                if (hid) { hid.value = html; hid.dispatchEvent(new Event('change', {bubbles: true})); }
            }""", [html_corpo])

    # ---------- 5) anexos (documentos) ----------
    # No REAL, anexa cada documento (o clique em "Anexar" já inclui o arquivo ao
    # processo); no dry_run NÃO anexa (evita efeito colateral) — só sinaliza.
    if anexos and not dry_run:
        anexados = await _anexar_documentos(page, anexos)
        nota_anx = f" · {len(anexados)}/{len(anexos)} anexo(s) incluído(s)"
    elif anexos:
        nota_anx = f" · {len(anexos)} anexo(s) (anexados só no envio real)"
    else:
        nota_anx = ""
    resumo = f"{', '.join(nomes_sel)} · prazo {prazo}d · somente_leitura={somente_leitura}{nota_anx}"

    # ---------- DRY RUN: preenche tudo mas NAO clica em Enviar ----------
    if dry_run:
        if screenshot_path:
            with contextlib.suppress(Exception):
                await page.screenshot(path=screenshot_path, full_page=True)
        return {"ok": True, "status": "dry_run", "detalhe": f"[dry_run] pronto p/ enviar → {resumo}"}

    # ---------- 5) Enviar ----------
    enviar_btn = page.locator(f'[id="{_NOTIF_FORM}"] button', has_text="Enviar").first
    try:
        await enviar_btn.click(timeout=8000)
    except Exception:  # noqa: BLE001 — conflito de sessão pode interceptar o clique
        if not await _resolver_conflito_login(page):
            return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}
        await enviar_btn.click(timeout=8000)

    for _ in range(40):  # aguarda ate ~20s pelo desfecho
        await page.wait_for_timeout(500)
        est = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
        if est.get("conflitoLogin"):
            if not await _resolver_conflito_login(page):
                return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}
        if est.get("takeover"):
            return {"ok": False, "status": "falha",
                    "detalhe": "Sessão assumida em outra estação durante o envio — não confirmado."}
        if est.get("pendente"):
            return {"ok": False, "status": "falha",
                    "detalhe": ("Impossível enviar nova mensagem pelo aplicativo neste momento, "
                                "pois há mensagem anterior pendente de visualização pelo assistido.")}
        if est.get("sucesso") or "detalhamentoProcesso.xhtml" in est.get("url", ""):
            mov_seq = await _ler_seq_notificacao(page, id_processo)
            detalhe = f"Notificação enviada com sucesso → {resumo}"
            if mov_seq is not None:
                detalhe += f" · Mov {mov_seq}"
            return {"ok": True, "status": "enviado", "detalhe": detalhe, "mov_seq": mov_seq}
    return {"ok": False, "status": "falha",
            "detalhe": "Não confirmei o envio (timeout aguardando 'enviada com sucesso')."}


# ===========================================================================
# MOVIMENTAR / TRAMITAR PAJ (individual e em bloco)
# ---------------------------------------------------------------------------
# Funções reutilizáveis — a mesma tela do SISDPU serve individual e bloco; a
# diferença é quantos PAJs entram na lista. `pajs` = lista de dicts
#   {"id": <id_processo interno>, "numero": "AAAA/UUU-NNNNN"}.
# O 1º PAJ é aberto pela URL (?id=<id>); os demais são adicionados por número.
# ⚠️ RESTRIÇÃO DO SISDPU: adicionar por número é travado na UNIDADE do operador
#    logado — PAJs de outra unidade NÃO entram no bloco (individual via ?id=
#    funciona p/ qualquer unidade). As funções validam a lista montada e
#    retornam erro se algum PAJ esperado não entrou.
# ===========================================================================

_MOV_FORM = "formHonorario"
_TRAM_FORM = "form_tramite"
_TIPO_ARQUIVO_MOV_PADRAO = "Correspondência"
_FASE_MENSAGEM_ASSISTIDO = "Mensagem ao assistido"

# Seleciona uma <option> por TEXTO num <select> nativo (.epaj_select_formata) e
# dispara o evento change (necessário p/ o AJAX do PrimeFaces reagir). args =
# [selectId, textoAlvo, exato]. Retorna {ok, val, txt} ou {ok:false, opts}.
_JS_SELECT_OPT = r"""
(args) => {
  const [selId, alvo, exato] = args;
  const s = document.getElementById(selId);
  if (!s) return {ok:false, err:'select-inexistente'};
  const n = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
  let val=null, txt=null;
  for (const o of s.options){ if (n(o.text)===n(alvo)){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null && !exato) for (const o of s.options){ if (n(o.text).includes(n(alvo))){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null) return {ok:false, opts:[...s.options].map(o=>o.text.trim())};
  s.value = val; s.dispatchEvent(new Event('change', {bubbles:true}));
  return {ok:true, val, txt};
}
"""

# Escreve o texto na descrição da MOVIMENTAÇÃO (editor CKEditor) + no input/textarea
# subjacentes (garantia p/ o submit JSF). args = [txt, inputHiddenId, textareaId].
_JS_SET_CKEDITOR = r"""
(args) => {
  const [txt, hidId, taId, raw] = args;
  // Editor é HTML. Se `raw` → usa o texto como HTML cru (ex.: listas/parágrafos já
  // montados). Senão escapa e converte \n em <br> (preserva as quebras).
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const html = raw ? (txt||'') : esc(txt).replace(/\r\n|\r|\n/g, '<br>');
  let done=false, keys=[];
  if (window.CKEDITOR && CKEDITOR.instances) {
    keys = Object.keys(CKEDITOR.instances);
    for (const nm of keys) {
      if (nm.includes('editorValue')) {
        CKEDITOR.instances[nm].setData(html);
        try { CKEDITOR.instances[nm].updateElement(); } catch(e) {}
        done = true;
      }
    }
  }
  const hid = document.getElementById(hidId); if (hid) hid.value = html;
  const ta  = document.getElementById(taId);  if (ta)  ta.value  = html;
  return {done, keys};
}
"""

# Marca o checkbox "Todas as fases" (revela a fase 'Mensagem ao assistido' no
# select de fases). Localiza pelo TEXTO do label (o id é volátil).
_JS_MARCAR_TODAS_FASES = r"""
() => {
  const l = [...document.querySelectorAll('label')].find(x => /todas as fases/i.test(x.innerText||''));
  if (!l) return false;
  const td = l.closest('td') || l.parentElement;
  const box = td && td.querySelector('.ui-chkbox-box');
  if (!box) return false;
  const icon = box.querySelector('.ui-chkbox-icon');
  const jaMarcado = icon && /ui-icon-check/.test(icon.className);
  if (!jaMarcado) box.click();
  return true;
}
"""

# Lista os textos (números de PAJ) do listbox de PAJs selecionados.
async def _ler_pajs_lista(page: Page, input_id: str) -> list[str]:
    return await page.evaluate(
        """(id)=>{const s=document.getElementById(id);return s?[...s.options].map(o=>o.text.trim()).filter(Boolean):[];}""",
        input_id,
    )


# Diálogos de confirmação VISÍVEIS (os confirm dialogs do SISDPU são position:fixed
# → offsetParent é null mesmo visíveis; testamos por getBoundingClientRect).
async def _dialogos_visiveis(page: Page) -> list[dict]:
    return await _eval_seguro(page, r"""
    () => {
      const vis = d => { const s=getComputedStyle(d); const r=d.getBoundingClientRect();
        return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
      return [...document.querySelectorAll('.ui-confirm-dialog,.ui-dialog')].filter(vis).map(d=>({
        id: d.id,
        msg: ((d.querySelector('.ui-confirm-dialog-message,.ui-dialog-content')||{}).innerText||'').trim().slice(0,180),
        btns: [...d.querySelectorAll('button, a.ui-button')].filter(vis).map(b=>(b.innerText||'').trim()).filter(Boolean)
      }));
    }
    """)


async def _adicionar_paj_por_numero(page: Page, form: str, add_btn_id: str, numero: str) -> None:
    """Adiciona um PAJ (AAAA/UUU-NNNNN) na lista via ano + número + botão Adicionar.
    ⚠️ A unidade é fixa (a do operador) — PAJ de outra unidade não entra corretamente."""
    ano, resto = numero.split("/")
    _unidade, num = resto.split("-")
    await page.evaluate(_JS_SELECT_OPT, [f"{form}:data", ano, True])
    await page.fill(_sel(f"{form}:numProcesso"), num)
    with contextlib.suppress(Exception):
        await page.locator(_sel(add_btn_id)).first.click(timeout=6000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(600)


async def _anexar_documentos_movimentacao(
    page: Page, anexos: list[dict], tipo_arquivo: str = _TIPO_ARQUIVO_MOV_PADRAO
) -> list[str]:
    """Anexa documentos à MOVIMENTAÇÃO (popup 'dlgModalArquivos' da fase). Para cada
    anexo: abre o popup (link 'Adicionar Anexo' da linha da fase), sobe o arquivo
    (preservando o nome original por buffer), preenche Descrição = nome, escolhe o
    Tipo de Arquivo (padrão 'Correspondência') e clica 'Anexar'. Fecha o popup ao fim.
    Best-effort por arquivo (tipos aceitos = _TIPOS_ANEXO_OK). Retorna os anexados."""
    import mimetypes

    anexos = [a for a in (anexos or []) if a]
    if not anexos:
        return []
    anexados: list[str] = []
    # abre o popup de anexos na 1ª (única) linha da tabela de fases
    with contextlib.suppress(Exception):
        await page.get_by_text("Adicionar Anexo", exact=True).first.click()
    with contextlib.suppress(Exception):
        await page.wait_for_selector(_sel(f"{_MOV_FORM}:arquivosFase_input"), timeout=10000)
    await page.wait_for_timeout(400)
    for a in anexos:
        caminho = a.get("caminho") or ""
        nome = a.get("nome") or (Path(caminho).name if caminho else "documento")
        if not caminho or not Path(caminho).exists():
            _log.warning("mov anexo ausente no disco, pulado: %s (%s)", nome, caminho)
            continue
        ext = (a.get("tipo") or Path(nome).suffix).lstrip(".").lower()
        if ext not in _TIPOS_ANEXO_OK:
            _log.warning("mov anexo de tipo não permitido, pulado: %s (.%s)", nome, ext)
            continue
        try:
            data = Path(caminho).read_bytes()
            mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
            await page.locator(_sel(f"{_MOV_FORM}:arquivosFase_input")).set_input_files(
                [{"name": nome, "mimeType": mime, "buffer": data}])
            # aguarda a linha em 'arquivosSalvar' (Descrição :desc + Tipo :tipoArquivo)
            desc_sel = _sel(f"{_MOV_FORM}:arquivosSalvar:0:desc")
            await page.wait_for_selector(desc_sel, timeout=10000)
            with contextlib.suppress(Exception):
                await page.fill(desc_sel, nome)
            tp = await page.evaluate(
                _JS_SELECT_OPT, [f"{_MOV_FORM}:arquivosSalvar:0:tipoArquivo", tipo_arquivo, False])
            if not tp.get("ok"):
                _log.warning("mov anexo: tipo %r não achado (opts=%s)", tipo_arquivo, tp.get("opts"))
            # botão "Anexar" da linha
            with contextlib.suppress(Exception):
                await page.locator(
                    'button[name^="%s:arquivosSalvar"]' % _MOV_FORM, has_text="Anexar").first.click()
            await _wait_pf_ajax(page)
            await page.wait_for_timeout(500)
            anexados.append(nome)
            _log.info("mov anexo incluído: %s (tipo=%s)", nome, tipo_arquivo)
        except Exception as e:  # noqa: BLE001
            _log.warning("mov falha ao anexar %s: %r", nome, e)
    # fecha o popup (Fechar; se persistir, força PF.hide/Escape — um anexo problemático
    # NÃO pode deixar o modal aberto e bloquear o clique em "Movimentar")
    with contextlib.suppress(Exception):
        fechar = page.locator('button[name^="%s:"]' % _MOV_FORM, has_text="Fechar").first
        if await fechar.is_visible():
            await fechar.click()
            await page.wait_for_timeout(400)
    with contextlib.suppress(Exception):
        aberto = await page.evaluate(
            """()=>{const d=document.getElementById('%s:dlgModalArquivos');
                if(!d)return false;const s=getComputedStyle(d);const r=d.getBoundingClientRect();
                return s.display!=='none'&&r.width>0;}""" % _MOV_FORM)
        if aberto:
            await page.evaluate(
                "()=>{try{PF('dlgModalArquivosWidget').hide();}catch(e){} "
                "for(const w in (PrimeFaces.widgets||{})){try{if(/dlgModalArquivos/i.test(w))PrimeFaces.widgets[w].hide();}catch(e){}}}")
            await page.wait_for_timeout(300)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    return anexados


async def movimentar_paj(
    pajs: list[dict],
    descricao: str,
    fase: str = _FASE_MENSAGEM_ASSISTIDO,
    anexos: list[dict] | None = None,
    tipo_arquivo: str = _TIPO_ARQUIVO_MOV_PADRAO,
    dry_run: bool = False,
    descricao_html: bool = False,
) -> dict:
    """Registra uma MOVIMENTAÇÃO (novo sequencial) em um ou vários PAJs.

    pajs: [{"id": <id_processo>, "numero": "AAAA/UUU-NNNNN"}, ...] (bloco = mesma unidade).
    fase: fase da movimentação (default 'Mensagem ao assistido'). descricao: texto livre.
    anexos: [{"nome","caminho","tipo"}] opcionais (tipo de arquivo no SISDPU = `tipo_arquivo`).
    Retorna {ok, status, detalhe, pajs}. status: 'movimentado' | 'dry_run' | 'falha'.
    A movimentação é gravada no clique em 'Movimentar' (o popup 'Deseja tramitar?' é
    posterior/opcional → respondemos 'Não')."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("movimentar_paj(%s) fase=%r dry=%s", numeros, fase, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = (f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml"
           f"?id={pajs[0]['id']}&caixaEntrada=false")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    # bloco: adiciona os demais PAJs por número
    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _MOV_FORM, f"{_MOV_FORM}:add", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_MOV_FORM}:processosSelecionados_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de movimentação: {faltando} "
                            f"(lista={lista}). Possível restrição de unidade no bloco.")}

    # fase: garante 'Todas as fases' e seleciona a fase alvo
    await page.evaluate(_JS_MARCAR_TODAS_FASES)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(700)
    fr = await page.evaluate(_JS_SELECT_OPT, [f"{_MOV_FORM}:faseId", fase, False])
    if not fr.get("ok"):
        return {"ok": False, "status": "falha",
                "detalhe": f"Fase {fase!r} não encontrada no SISDPU."}
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    nfases = await page.eval_on_selector_all(
        _sel(f"{_MOV_FORM}:dataTableFasesSelecionadas") + " tbody tr",
        "els=>els.filter(e=>!/Nenhum registro/i.test(e.innerText)).length")
    if not nfases:
        return {"ok": False, "status": "falha", "detalhe": "Fase não foi adicionada à movimentação."}

    # descrição (CKEditor); descricao_html=True usa HTML cru (listas/parágrafos)
    await page.evaluate(_JS_SET_CKEDITOR,
                        [descricao, f"{_MOV_FORM}:editorValue", f"{_MOV_FORM}_editorValue",
                         bool(descricao_html)])

    # anexos (opcional) — antes de Movimentar
    anexados: list[str] = []
    if anexos and not dry_run:
        anexados = await _anexar_documentos_movimentacao(page, anexos, tipo_arquivo)

    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Movimentaria {numeros} (fase {fase!r}).", "pajs": numeros}

    # clica "Movimentar" (grava o registro)
    with contextlib.suppress(Exception):
        await page.get_by_role("button", name="Movimentar", exact=True).first.click()
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    # Confirma sucesso PELO GROWL AGORA — ANTES de dispensar o popup 'Deseja tramitar?',
    # pois clicar 'Não' pode NAVEGAR para o detalhamento (onde o growl não existe mais).
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"gravad[oa] com sucesso", body, re.I))
    # popup pós-commit 'Deseja tramitar?' → Não (best-effort)
    for d in await _dialogos_visiveis(page):
        if "tramitar" in _norm_nome(d.get("msg", "")).lower():
            with contextlib.suppress(Exception):
                await page.locator(_sel(f"{_MOV_FORM}:confirmDialogTramitacao") + ' button', has_text="Não").first.click()
                await page.wait_for_timeout(600)
            break
    # Fallback: se não peguei o growl mas a página navegou ao detalhamento (o que só
    # ocorre APÓS gravar), considera sucesso.
    if not ok and "detalhamentoProcesso" in page.url:
        ok = True
    detalhe = "Movimentação registrada com sucesso." if ok else "Não confirmei a movimentação (sem growl de sucesso)."
    if anexados:
        detalhe += f" Anexos: {', '.join(anexados)}."
    if not ok:
        with contextlib.suppress(Exception):
            diag = await _dialogos_visiveis(page)
            _log.warning("movimentar_paj: sem growl; dialogs=%s url=%s", diag, page.url)
    # lê o nº de sequência da movimentação recém-criada (maior seq do detalhamento)
    mov_seq = await _ler_seq_notificacao(page, pajs[0]["id"]) if ok else None
    if ok and mov_seq is not None:
        detalhe += f" · Mov {mov_seq}"
    return {"ok": ok, "status": "movimentado" if ok else "falha",
            "detalhe": detalhe, "pajs": numeros, "anexos": anexados, "mov_seq": mov_seq}


async def tramitar_paj(
    pajs: list[dict],
    grupo: str | None = None,
    usuarios: list[str] | None = None,
    descricao: str | None = None,
    prazo_data: str | None = None,
    prazo_desc: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Registra uma TRAMITAÇÃO (Trâmite Interno) de um ou vários PAJs para um GRUPO
    da unidade e/ou USUÁRIO(s) da unidade.

    pajs: [{"id","numero"}, ...] (bloco = mesma unidade).
    grupo: nome (ou trecho) da caixa de grupo destino (ex.: '01 - PREV_TESTES').
    usuarios: lista de nomes (trecho) de usuários destino (picklist).
    descricao: descrição do trâmite (opcional, máx 1000). prazo_data: 'DD/MM/AAAA'
    (opcional). prazo_desc: descrição do prazo (opcional, máx 300).
    Retorna {ok, status, detalhe, pajs}. status: 'tramitado' | 'dry_run' | 'falha'.
    Confirmamos SEM movimentar (clique em 'Não' no popup 'Também deseja movimentar?')."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    if not grupo and not usuarios:
        return {"ok": False, "status": "falha", "detalhe": "Informe um grupo ou usuário(s) destino."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("tramitar_paj(%s) grupo=%r usuarios=%s dry=%s", numeros, grupo, usuarios, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = (f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml"
           f"?id={pajs[0]['id']}&idTramite=0")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _TRAM_FORM, f"{_TRAM_FORM}:Adicionar", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_TRAM_FORM}:pajs_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de tramitação: {faltando} "
                            f"(lista={lista}). Possível restrição de unidade no bloco.")}

    # Trâmite Interno (radio value=1) — normalmente já é o default
    with contextlib.suppress(Exception):
        await page.evaluate(r"""()=>{const r=document.querySelector('input[name="form_tramite:j_idt125"][value="1"]');
            if(r&&!r.checked){const box=r.closest('.ui-radiobutton').querySelector('.ui-radiobutton-box'); box&&box.click();}}""")
        await _wait_pf_ajax(page)

    destino = []
    # grupo
    if grupo:
        g = await page.evaluate(_JS_SELECT_OPT, [f"{_TRAM_FORM}:grupoDestinatarioLocal", grupo, False])
        if not g.get("ok"):
            return {"ok": False, "status": "falha",
                    "detalhe": f"Grupo {grupo!r} não encontrado (opções={g.get('opts')})."}
        destino.append(f"grupo {g['txt']!r}")
        await _wait_pf_ajax(page)
        await page.wait_for_timeout(500)
    # usuários (picklist: double-click no item da lista de origem)
    for nome in (usuarios or []):
        with contextlib.suppress(Exception):
            item = page.locator(
                _sel(f"{_TRAM_FORM}:pickList") + ' .ui-picklist-source li').filter(has_text=nome).first
            await item.dblclick(timeout=5000)
            await _wait_pf_ajax(page)
        destino.append(f"usuário {nome!r}")

    # descrição do trâmite
    if descricao:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:descTramite"), descricao)
    # prazo
    if prazo_data:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:dataPrazo_input"), prazo_data)
    if prazo_desc:
        with contextlib.suppress(Exception):
            # a descrição do prazo é a textarea de maxlength=300 (id volátil j_idtNNN)
            await page.evaluate(r"""(txt)=>{const ta=[...document.querySelectorAll('textarea')].find(t=>t.getAttribute('maxlength')==='300');
                if(ta){ta.value=txt; ta.dispatchEvent(new Event('input',{bubbles:true}));}}""", prazo_desc)

    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Tramitaria {numeros} → {', '.join(destino)}.", "pajs": numeros}

    # Confirma a tramitação: exibe o diálogo 'Também deseja movimentar?' e clica 'Não'.
    # O gatilho confiável é PF(...).show() (o onclick do botão 'Tramitar' faz exatamente
    # isso: window.onbeforeunload=null; PF(widget).show(); return false). Evita depender
    # do id volátil do botão.
    with contextlib.suppress(Exception):
        await page.evaluate(
            "()=>{window.onbeforeunload=null; try{PF('confirmationDialogTramitarProcessoJudicialWidget').show();}catch(e){}}")
    await page.wait_for_timeout(700)
    dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        # fallback: clica o botão 'Tramitar' por texto
        with contextlib.suppress(Exception):
            await page.get_by_role("button", name="Tramitar", exact=True).first.click(timeout=5000)
        await page.wait_for_timeout(700)
        dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        _log.warning("tramitar_paj: diálogo não apareceu; dialogs vistos=%s url=%s", dlgs, page.url)
        return {"ok": False, "status": "falha",
                "detalhe": "Diálogo de confirmação da tramitação não apareceu."}
    with contextlib.suppress(Exception):
        await page.locator(_sel(f"{_TRAM_FORM}:NaoTramitarProcessoJudicial")).click(timeout=5000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(1000)
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"tramitad[oa]s?.*sucesso", body, re.I)) or "historicoTramitacao" in page.url
    detalhe = (f"Tramitação registrada para {', '.join(destino)}." if ok
               else "Não confirmei a tramitação (sem growl de sucesso).")
    return {"ok": ok, "status": "tramitado" if ok else "falha", "detalhe": detalhe, "pajs": numeros}


# ---- Concluir PAJ: ADMITIR (caixa de grupo) + CONCLUIR (caixa do usuário) ----
# Ativa a aba "Caixa de Entrada do Grupo" (renderiza as tabelas de grupo).
_JS_ATIVA_ABA_GRUPO = r"""() => {
    const links = document.querySelectorAll('.ui-tabs-nav a, a[href*="tabView:tab"]');
    for (const a of links) if ((a.textContent||'').toLowerCase().includes('caixa de entrada do grupo')) { a.click(); return true; }
    return false;
}"""
# O trâmite (data-rk) está em ALGUMA tabela de grupo? (linha tem link "Admitir").
_JS_TRAMITE_NO_GRUPO = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') return true;
    }
    return false;
}"""
# Clica o link "Admitir" da linha data-rk (retira da caixa de grupo → caixa do usuário).
_JS_ADMITIR_TRAMITE = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') { a.click(); return true; }
    }
    return false;
}"""
# O trâmite está na tabela da CAIXA DO USUÁRIO?
_JS_TRAMITE_NA_CAIXA = r"""(idt) => {
    const t = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites"]');
    return !!(t && t.querySelector('tbody tr[data-rk="'+idt+'"]'));
}"""
# Marca o checkbox (id estável baseado no id_tramite), se ainda não marcado.
_JS_MARCA_CHECKBOX = r"""(idt) => {
    const box = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites_'+idt+'_checkbox"]');
    if (!box) return false;
    if (!/ui-state-active/.test(box.getAttribute('class')||'')) box.click();
    return true;
}"""
_JS_CLICA_BTN_CONCLUIR = r"""() => {
    const b = document.querySelector('[id="tabView:caixaEntradaTab1Form:btnConcluir"]');
    if (!b) return false; b.click(); return true;
}"""
# DIAGNÓSTICO: o que está VISÍVEL nas caixas agora (ids, contagem, paginador) — p/ detectar
# quando o trâmite existe mas está em OUTRA PÁGINA (paginação) ou a aba não renderizou.
_JS_DIAG_CAIXAS = r"""() => {
    const info = {caixa_rows:0, caixa_rks:[], grupo_rows:0, grupo_rks:[], paginadores:[]};
    const vis = e => { const r=e.getBoundingClientRect(); return r.width>0 && r.height>0; };
    const cx = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites"]');
    if (cx) {
        const rows = cx.querySelectorAll('tbody tr[data-rk]');
        info.caixa_rows = rows.length;
        info.caixa_rks = [...rows].map(r=>r.getAttribute('data-rk')).slice(0,60);
    }
    const grp = [...document.querySelectorAll('tr[data-rk]')].filter(tr =>
        [...tr.querySelectorAll('a,button')].some(a => (a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase()==='admitir'));
    info.grupo_rows = grp.length;
    info.grupo_rks = grp.map(r=>r.getAttribute('data-rk')).slice(0,60);
    info.paginadores = [...document.querySelectorAll('.ui-paginator')].filter(vis)
        .map(p => (p.textContent||'').replace(/\s+/g,' ').trim().slice(0,80));
    // TODOS os data-rk presentes na página (mesmo sem link "Admitir") — p/ detectar id divergente.
    info.todos_rks = [...document.querySelectorAll('tr[data-rk]')].map(r=>r.getAttribute('data-rk')).slice(0,80);
    return info;
}"""
# DIAGNÓSTICO DIRECIONADO: para cada id-alvo, diz se a linha existe, em que tabela e quais ações
# tem (revela id divergente x seletor do 'Admitir'/checkbox errado).
_JS_DIAG_ALVO = r"""(ids) => ids.map(idt => {
    const tr = document.querySelector('tr[data-rk="'+idt+'"]');
    if (!tr) return {id: idt, presente: false};
    const acoes = [...tr.querySelectorAll('a,button')]
        .map(a => (a.textContent||'').replace(/\s+/g,' ').trim()).filter(Boolean).slice(0,10);
    const dt = tr.closest('.ui-datatable');
    const hdr = dt ? ((dt.querySelector('.ui-datatable-header strong, .ui-datatable-header')||{}).textContent||'') : '';
    return {id: idt, presente: true, tabela: hdr.replace(/\s+/g,' ').trim().slice(0,60), acoes};
})"""


async def concluir_paj(id_tramites: list[str], dry_run: bool = False) -> dict:
    """ADMITE (retira da caixa de grupo) e CONCLUI (baixa na caixa do usuário) o(s)
    TRÂMITE(S) ORIGINÁRIO(S) — os sincronizados na caixa de grupo que deram origem ao
    envio das mensagens (NÃO a tramitação que o painel cria). Recebe a lista de
    `id_tramite` (o data-rk lido na sincronização; NÃO muda ao admitir).

    Fluxo: caixaEntrada → aba de grupo → clica "Admitir" de cada trâmite → volta à
    caixa do usuário → marca o checkbox de cada um (id estável
    `...:tramites_{id}_checkbox`) → clica "Concluir" (btnConcluir). Confirma relendo.
    Retorna {ok, status, detalhe, resultados:{id:{admitido,concluido}}}. status:
    'concluido' | 'parcial' | 'falha' | 'dry_run'."""
    ids = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum id de trâmite informado."}
    _log.info("concluir_paj(%s) dry=%s", ids, dry_run)
    res = {i: {"admitido": False, "concluido": False} for i in ids}
    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Admitiria+concluiria os trâmites {ids}.", "resultados": res}

    await _ensure_logged_in()
    page = await _get_page()
    cx_url = f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml"

    # ---------- FASE 1: ADMITIR na caixa de grupo ----------
    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO, "resultados": res}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(900)
    with contextlib.suppress(Exception):
        diag = await page.evaluate(_JS_DIAG_CAIXAS)
        _log.info("concluir_paj: caixa de GRUPO — %d linha(s) c/ Admitir; rks_grupo=%s; todos_rks=%s; paginador=%s",
                  diag.get("grupo_rows"), diag.get("grupo_rks"), diag.get("todos_rks"), diag.get("paginadores"))
        alvo = await page.evaluate(_JS_DIAG_ALVO, ids)
        _log.info("concluir_paj: ALVO(s) na tela do grupo: %s", alvo)
    for i in ids:
        try:
            if await page.evaluate(_JS_TRAMITE_NO_GRUPO, i):
                if await page.evaluate(_JS_ADMITIR_TRAMITE, i):
                    # Espera generosa após admitir (o admit é AJAX + a máquina pode estar lenta
                    # por causa do streaming do AnyDesk consumindo CPU).
                    await _wait_pf_ajax(page, timeout=20000); await page.wait_for_timeout(1500)
                    res[i]["admitido"] = True
            else:
                # não está no grupo (página visível) — talvez já admitido antes OU em outra página
                res[i]["admitido"] = None
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: admitir %s falhou: %s", i, e)

    # ---------- FASE 2: CONCLUIR na caixa do usuário ----------
    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(1000)
    # O trâmite admitido pode DEMORAR a aparecer na caixa do usuário (admit é AJAX; máquina lenta
    # /CPU carregada pelo AnyDesk atrasa). Poll GENEROSO com reload até ~20s antes de marcar.
    for _tent in range(8):
        faltam = [i for i in ids if not await page.evaluate(_JS_TRAMITE_NA_CAIXA, i)]
        if not faltam:
            break
        _log.info("concluir_paj: aguardando %s aparecer(em) na caixa do usuário (tentativa %d/8)…",
                  faltam, _tent + 1)
        await page.wait_for_timeout(1800)
        await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page); await page.wait_for_timeout(700)
    with contextlib.suppress(Exception):
        diag = await page.evaluate(_JS_DIAG_CAIXAS)
        _log.info("concluir_paj: caixa do USUÁRIO — %d linha(s); rks=%s; paginador=%s",
                  diag.get("caixa_rows"), diag.get("caixa_rks"), diag.get("paginadores"))
    marcados = 0
    for i in ids:
        try:
            na = await page.evaluate(_JS_TRAMITE_NA_CAIXA, i)
            mk = (await page.evaluate(_JS_MARCA_CHECKBOX, i)) if na else False
            if na and mk:
                marcados += 1
                res[i]["marcado"] = True         # marcamos DE FATO o checkbox deste trâmite
            else:
                _log.warning("concluir_paj: trâmite %s não marcado (na_caixa=%s, checkbox=%s)", i, na, mk)
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: marcar %s falhou: %s", i, e)
    _log.info("concluir_paj: %d/%d checkbox(es) marcado(s) → Concluir", marcados, len(ids))
    if marcados:
        await page.evaluate(_JS_CLICA_BTN_CONCLUIR)
        await page.wait_for_timeout(900)
        # ⚠️ O SISDPU costuma abrir um DIÁLOGO de confirmação ("Deseja concluir o(s) trâmite(s)?")
        # após o btnConcluir. Se não confirmarmos, a conclusão NÃO se efetiva (era a causa do
        # '0/1 concluído' intermitente). Confirmamos clicando o botão afirmativo do diálogo.
        with contextlib.suppress(Exception):
            dlgs = await _dialogos_visiveis(page)
            if dlgs:
                _log.info("concluir_paj: diálogo pós-Concluir: %s", dlgs)
                clicado = await page.evaluate(r"""() => {
                    const vis = d => { const s=getComputedStyle(d); const r=d.getBoundingClientRect();
                        return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                    const AFIRM = /^(sim|confirmar|concluir|ok|continuar)$/i;
                    for (const dlg of document.querySelectorAll('.ui-confirm-dialog,.ui-dialog')) {
                        if (!vis(dlg)) continue;
                        for (const b of dlg.querySelectorAll('button, a.ui-button')) {
                            const t=(b.innerText||'').replace(/\s+/g,' ').trim();
                            if (AFIRM.test(t)) { b.click(); return t; }
                        }
                    }
                    return '';
                }""")
                _log.info("concluir_paj: confirmação do diálogo clicada = %r", clicado)
        await _wait_pf_ajax(page, timeout=15000); await page.wait_for_timeout(1300)

    # ---------- VERIFICAÇÃO ----------
    # ⚠️ ANTES: `concluido = não está na caixa E não está no grupo` — dava FALSO POSITIVO quando o
    # trâmite não era LOCALIZADO em nenhuma das duas (ex.: está em OUTRA PÁGINA do paginador, ou a
    # aba não renderizou): o painel dizia "1/1 concluído" sem ter concluído nada. AGORA exigimos
    # EVIDÊNCIA POSITIVA: só é "concluído" se REALMENTE marcamos o checkbox do trâmite E ele saiu
    # da caixa do usuário. Se nunca foi marcado (não localizado), é FALHA (fica visível + "refazer").
    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    na_caixa = {i: await page.evaluate(_JS_TRAMITE_NA_CAIXA, i) for i in ids}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(800)
    no_grupo = {i: await page.evaluate(_JS_TRAMITE_NO_GRUPO, i) for i in ids}
    for i in ids:
        if res[i].get("marcado"):
            # marcamos e clicamos Concluir → concluído se saiu da caixa do usuário
            res[i]["concluido"] = not na_caixa[i]
        else:
            # não localizado p/ marcar → NÃO declarar concluído (evita o falso positivo)
            res[i]["concluido"] = False
            _log.warning("concluir_paj: trâmite %s NÃO concluído — não foi localizado p/ marcar "
                         "(na_caixa=%s, no_grupo=%s). Provável paginação/aba ou id divergente.",
                         i, na_caixa[i], no_grupo[i])

    concluidos = [i for i in ids if res[i]["concluido"]]
    if len(concluidos) == len(ids):
        status, ok = "concluido", True
    elif concluidos:
        status, ok = "parcial", False
    else:
        status, ok = "falha", False
    detalhe = f"{len(concluidos)}/{len(ids)} trâmite(s) concluído(s)."
    return {"ok": ok, "status": status, "detalhe": detalhe, "resultados": res}


async def checar_tramites_na_caixa(id_tramites: list[str]) -> dict:
    """Verificação LEVE (sem popups): quais dos `id_tramite` ainda estão na caixa de
    entrada de grupo (aguardando tratamento). Retorna {id_tramite: bool}. Usada antes
    de enviar, para detectar tramitações que outro usuário já concluiu/removeu.

    Um trâmite 'na caixa de grupo' é uma linha tr[data-rk=id] com link 'Admitir'. Se
    não estiver mais lá (concluído/admitido por outro), retorna False para aquele id.
    """
    ids = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids:
        return {}
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                    wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(500)
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(800)
    presenca = {}
    for i in ids:
        try:
            presenca[i] = bool(await page.evaluate(_JS_TRAMITE_NO_GRUPO, i))
        except Exception:  # noqa: BLE001
            presenca[i] = True  # em dúvida, assume presente (não bloqueia o envio)
    _log.info("checar_tramites_na_caixa(%s) -> %s", ids, presenca)
    return presenca


async def listar_grupos_unidade(id_processo: str | None = None) -> list[str]:
    """Lê a lista de caixas de GRUPO da unidade (opções do dropdown 'Tramitar para Grupo
    da Unidade' da tela Tramitar PAJ). Retorna os nomes (sem 'Selecione...'). Usa a tela
    de tramitação; se `id_processo` for informado abre com ele (mais estável), senão a
    tela em modo bloco."""
    await _ensure_logged_in()
    page = await _get_page()
    url = f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml"
    if id_processo:
        url += f"?id={id_processo}&idTramite=0"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(300)
    grupos = await page.evaluate(
        """()=>{const s=document.getElementById('form_tramite:grupoDestinatarioLocal');
            return s?[...s.options].map(o=>o.text.trim()).filter(t=>t && !/^selecione/i.test(t)):[];}""")
    _log.info("listar_grupos_unidade: %d grupos", len(grupos or []))
    return grupos or []


_UF_NOME = {"AC": "ACRE", "AL": "ALAGOAS", "AP": "AMAPA", "AM": "AMAZONAS", "BA": "BAHIA",
            "CE": "CEARA", "DF": "DISTRITO FEDERAL", "ES": "ESPIRITO SANTO", "GO": "GOIAS",
            "MA": "MARANHAO", "MT": "MATO GROSSO", "MS": "MATO GROSSO DO SUL", "MG": "MINAS GERAIS",
            "PA": "PARA", "PB": "PARAIBA", "PR": "PARANA", "PE": "PERNAMBUCO", "PI": "PIAUI",
            "RJ": "RIO DE JANEIRO", "RN": "RIO GRANDE DO NORTE", "RS": "RIO GRANDE DO SUL",
            "RO": "RONDONIA", "RR": "RORAIMA", "SC": "SANTA CATARINA", "SP": "SAO PAULO",
            "SE": "SERGIPE", "TO": "TOCANTINS"}

_JS_SELECT_MUNICIPIO = r"""
(nome) => {
  const s = document.querySelector('[name$="enderecoMunicipio"]'); if (!s) return false;
  const norm = x => (x||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().replace(/\(.*\)/,'').trim();
  const alvo = norm(nome);
  const opt = [...s.options].find(o => norm(o.text) === alvo) || [...s.options].find(o => norm(o.text).startsWith(alvo));
  if (opt) { s.value = opt.value; s.dispatchEvent(new Event('change', {bubbles:true})); return true; }
  return false;
}
"""

# Seleciona a UF por texto NORMALIZADO (sem acento) — robusto quer o SISDPU liste a UF com
# ou sem acento ("PARANA"/"PARANÁ"). Recebe o NOME por extenso (de _UF_NOME). Dispara 'change'
# (é o gatilho da cascata que recarrega os municípios).
_JS_SELECT_UF = r"""
(nome) => {
  const s = document.querySelector('[name$="enderecoUF"]'); if (!s) return false;
  const norm = x => (x||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
  const alvo = norm(nome);
  const opt = [...s.options].find(o => norm(o.text) === alvo);
  if (opt) { s.value = opt.value; s.dispatchEvent(new Event('change', {bubbles:true})); return true; }
  return false;
}
"""


async def atualizar_endereco_assistido(id_processo: str, endereco: dict, dry_run: bool = False) -> dict:
    """Atualiza o endereço do 1º assistido PF do PAJ no cadastro do SISDPU e salva
    (botão 'Alterar'). `endereco` = {cep,logradouro,numero,complemento,bairro,municipio,uf}.
    Edita o 1º endereço do assistido. Retorna {ok, status, detalhe, avisos}. status:
    'salvo' | 'dry_run' | 'falha'. Cobre TODOS os campos, inclusive a cascata UF→Município
    (setar a UF dispara o AJAX do JSF que recarrega a lista de municípios; aguardamos a lista
    repovoar antes de selecionar o município por texto normalizado). Se o município não for
    encontrado na lista da UF, a gravação prossegue e o fato é reportado em `avisos`."""
    await _ensure_logged_in()
    page = await _get_page()
    # 1) idPessoa do 1º assistido PF (via detalhamento).
    await page.goto(f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}",
                    wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(500)
    idp = await page.evaluate(r"""() => { const m=document.body.innerHTML.match(/idPessoa=(\d+)/); return m?m[1]:""; }""")
    if not idp:
        return {"ok": False, "status": "falha", "detalhe": "Assistido (idPessoa) não localizado no PAJ."}
    # 2) Cadastro do assistido (campos de endereço editáveis já no load).
    await page.goto(f"{SISDPU_URL}/pages/atendimento/cadastroAssistidoPessoaFisicaCombotoes.xhtml"
                    f"?idProcesso={id_processo}&idPessoa={idp}",
                    wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(600)
    if not await page.query_selector('[name$="pessoaEnderecoLogradouro"]'):
        return {"ok": False, "status": "falha", "detalhe": "Endereço não encontrado no cadastro do assistido."}

    async def _set(suf: str, val: str) -> None:
        el = await page.query_selector(f'[name$="{suf}"]')
        if el is not None:
            await el.fill(str(val or ""))

    await _set("pessoaEnderecoCep", endereco.get("cep", ""))
    await _set("pessoaEnderecoLogradouro", endereco.get("logradouro", ""))
    await _set("pessoaEnderecoNumero", endereco.get("numero", ""))
    await _set("pessoaEnderecoComplemento", endereco.get("complemento", ""))
    await _set("pessoaEnderecoBairro", endereco.get("bairro", ""))
    # UF/Município (SELECTs em cascata): setar a UF dispara AJAX que RECARREGA a lista de
    # municípios; só então o município pode ser selecionado. Cobre correção que muda a cidade.
    avisos: list[str] = []
    uf = (endereco.get("uf") or "").strip().upper()
    mun = (endereco.get("municipio") or "").strip()
    if uf:
        # ViaCEP entrega a UF em SIGLA (ex.: "PR"); o SISDPU exige o NOME por extenso
        # (ex.: "PARANA") — convertido por _UF_NOME. A seleção compara texto normalizado
        # (sem acento), então funciona mesmo se o SISDPU listar "PARANÁ" com acento.
        uf_nome = _UF_NOME.get(uf, uf)
        _n = lambda x: unicodedata.normalize("NFD", (x or "")).encode("ascii", "ignore").decode().strip().upper()  # noqa: E731
        try:
            atual = await page.eval_on_selector(
                '[name$="enderecoUF"]',
                "s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''")
            if _n(atual) != _n(uf_nome):   # só troca (e dispara a cascata) se for outra UF
                if not await page.evaluate(_JS_SELECT_UF, uf_nome):
                    avisos.append(f"UF '{uf}' não encontrada na lista")
                else:
                    await _wait_pf_ajax(page)
                    for _ in range(30):   # aguarda a cascata popular os municípios
                        n = await page.eval_on_selector('[name$="enderecoMunicipio"]', "s => s.options.length")
                        if (n or 0) > 2:
                            break
                        await page.wait_for_timeout(200)
        except Exception as e:  # noqa: BLE001
            avisos.append(f"UF não aplicada ({e})")
    if mun:
        try:
            if not await page.evaluate(_JS_SELECT_MUNICIPIO, mun):
                avisos.append(f"município '{mun}' não encontrado na lista da UF")
        except Exception as e:  # noqa: BLE001
            avisos.append(f"município não aplicado ({e})")
    if dry_run:
        return {"ok": True, "status": "dry_run", "detalhe": "Endereço preenchido (não salvo).",
                "avisos": avisos}
    # 3) Salvar (botão 'Alterar' submete o cadastro).
    await page.evaluate(
        r"""() => { const b=[...document.querySelectorAll('input[type=submit],input[type=button],button')]
            .find(e => (e.value||e.textContent||'').trim()==='Alterar' && e.getBoundingClientRect().width>0);
            if (b) b.click(); }""")
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(1000)
    ok = await page.evaluate(r"""() => /gravado com sucesso/i.test(document.body.innerText||'')""")
    det = "Endereço atualizado no SISDPU." if ok else "O SISDPU não confirmou a gravação."
    if avisos:
        det += " Atenção: " + "; ".join(avisos) + "."
    return {"ok": bool(ok), "status": "salvo" if ok else "falha", "detalhe": det, "avisos": avisos}


async def fechar() -> None:
    """Fecha o navegador headless."""
    global _playwright, _browser, _page
    if _browser:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
        _page = None
    if _playwright:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None


if __name__ == "__main__":
    import asyncio
    import json

    async def _smoke() -> None:
        # Carrega .env do projeto para expor SISDPU_USERNAME/PASSWORD
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")

        try:
            data = await caixa_de_entrada()
            itens = data.get("itens_tabela", [])[:5]
            print(json.dumps(itens, ensure_ascii=False, indent=2))
            print(f"(+{len(data.get('itens_tabela', [])) - len(itens)} itens)")
        finally:
            await fechar()

    asyncio.run(_smoke())
