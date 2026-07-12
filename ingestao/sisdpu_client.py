"""Cliente SISDPU via Playwright headless (async).

Portado de dpu-workspace-main/dpuscript/mcp_servers/sisdpu/client.py.
Mantido como funcoes modulo-level com estado global de browser/page para
simplificar uso; concorrencia de sincronizacao e' serializada por um
asyncio.Lock em services/sync_service.py.

Credenciais vem das env vars SISDPU_USERNAME e SISDPU_PASSWORD (carregadas do
.env pelo config.py).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
import contextlib


SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"

# Logger dedicado — os logs `[busca-paj]` vao para logs/app.log (handler
# configurado em app.py com rotacao diaria). Tambem aparecem no console
# quando o painel e' iniciado num terminal visivel.
_log = logging.getLogger("sisdpu.busca")

# Diretorio onde despejamos screenshot + HTML quando a busca via campo global
# falha — facilita o diagnostico (DOM real do SISDPU vs nossa heuristica).
_DEBUG_DIR = Path(__file__).resolve().parents[1] / "logs" / "sisdpu_debug"

# Retencao do diretorio de debug: pra nao acumular indefinidamente (cada dump
# tem screenshot+HTML+body, ~1MB), mantem soh os N mais recentes. Suficiente
# pra investigar a janela ativa de problemas sem inflar o repositorio.
_DEBUG_DIR_RETER = 20


def _purgar_debug_dir_antigos(reter: int = _DEBUG_DIR_RETER) -> None:
    """Remove subpastas mais antigas de _DEBUG_DIR, mantendo apenas as `reter`
    mais recentes (por mtime). Best-effort — qualquer erro e' suprimido pra
    nao quebrar o fluxo principal."""
    if not _DEBUG_DIR.exists():
        return
    with contextlib.suppress(Exception):
        import shutil

        subpastas = [p for p in _DEBUG_DIR.iterdir() if p.is_dir()]
        if len(subpastas) <= reter:
            return
        subpastas.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for antigo in subpastas[reter:]:
            with contextlib.suppress(Exception):
                shutil.rmtree(antigo)


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
    abrir o preview interno — importante pro `listar_arquivos_paj`, ja que
    queremos capturar o arquivo bruto via evento `download`, nao previsualizar.
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
    """Faz login no SISDPU."""
    user = os.environ.get("SISDPU_USERNAME", "")
    senha = os.environ.get("SISDPU_PASSWORD", "")
    if not user or not senha:
        raise RuntimeError("SISDPU_USERNAME/SISDPU_PASSWORD não definidos no ambiente (.env)")
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


# Nome canonico da caixa de entrada de grupo tratada por este painel.
CAIXA_GRUPO_DIVCOM = "01 - PREV_DIVCOM"

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


async def listar_caixa_grupo(nome_grupo: str = CAIXA_GRUPO_DIVCOM) -> dict:
    """Lista os PAJs de UMA caixa de entrada de grupo (ex: "01 - PREV_DIVCOM").

    A caixa de entrada do SISDPU tem uma aba "Caixa de Entrada do Grupo" que
    empilha varias caixas de grupo, cada uma renderizada como um datatable
    proprio com um cabecalho <strong>NOME DO GRUPO</strong>. Esta funcao ativa
    essa aba, localiza o datatable cujo cabecalho casa com `nome_grupo` e extrai
    as linhas.

    Cada linha tem as colunas: Detalhes (acoes), Processo (nº PAJ + oficio de
    origem, com link p/ o detalhamento), Remetente, Data de Envio, Prazo e
    Admitir. A providencia de contato NAO esta aqui — fica nas movimentacoes do
    detalhamento (obtida depois via movimentacoes_paj/buscar_paj_global usando o
    `id` retornado).

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
    for (const tr of document.querySelectorAll("table tbody tr")) {
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length < 6) continue;
        const seq = (cells[0].textContent || "").trim();
        const data = (cells[2].textContent || "").trim();
        if (!/^\d{1,4}$/.test(seq)) continue;
        if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
        movs.push({
            seq: parseInt(seq, 10),
            data: data,
            tipo: (cells[3].textContent || "").replace(/\s+/g, " ").trim(),
            fases: (cells[4].textContent || "").replace(/\s+/g, " ").trim(),
            descricao: (cells[5].textContent || "").replace(/\s+/g, " ").trim(),
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


def parse_item_caixa(item_raw: str) -> dict | None:
    """Extrai {paj, oficio, assistido, data, desc, etiqueta} de uma linha da caixa.

    Aceita etiquetas SISDPU (rotulos livres que o Defensor aplica na caixa, tipo
    "Aleg finais", "Urgente", etc.) presentes como prefixo antes do PAJ. O PAJ e'
    capturado com re.search (posicional), e o texto anterior vira 'etiqueta'.

    Portado de preparar_pajs.py._parse_item.
    """
    parts = [p.strip() for p in item_raw.split("\t")]
    header = parts[0] if parts else ""
    m = re.search(r"(\d{4}/\d{3}-\d+)\s*\n?\(?\s*(.*?)\s*\)?$", header, re.DOTALL)
    if not m:
        return None
    paj = m.group(1).strip()
    oficio = m.group(2).replace("E.", "").strip()
    # Etiqueta = texto ANTES do PAJ (se houver). Strip pra limpar espacos/newlines.
    etiqueta = header[: m.start(1)].strip()
    assistido = parts[1] if len(parts) > 1 else ""
    data = parts[2] if len(parts) > 2 else ""
    descricao = parts[-1] if len(parts) >= 5 else ""
    return {
        "paj": paj,
        "oficio": oficio[:120],
        "assistido": assistido[:120],
        "data": data,
        "desc": descricao,
        "etiqueta": etiqueta[:80],
    }


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
    est = await _eval_seguro(page, _JS_ESTADO_NOTIFICACAO)
    if not est.get("conflitoLogin"):
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
    return not est2.get("conflitoLogin")


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
    let best = null, bestRow = null;
    for (const tr of document.querySelectorAll("table tbody tr")) {
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length < 6) continue;
        const seq = (cells[0].textContent || "").trim();
        const data = (cells[2].textContent || "").trim();
        if (!/^\d{1,4}$/.test(seq)) continue;
        if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
        const n = parseInt(seq, 10);
        if (best === null || n > best) { best = n; bestRow = tr; }
    }
    if (best === null) return null;
    // Âncora p/ o link do histórico rolar até a movimentação: id do <tr> se houver,
    // senão o id da própria tabela (a mov mais recente é a 1ª linha dela).
    let anchor = "";
    if (bestRow) {
        anchor = bestRow.id || "";
        if (!anchor) { const t = bestRow.closest("table"); anchor = (t && t.id) || ""; }
    }
    return { seq: best, anchor: anchor };
}
"""


async def _ler_seq_notificacao(page, id_processo: str) -> dict:
    """Após o envio pelo App (o PAJ é auto-movimentado), lê o nº de sequência da
    movimentação recém-criada = a de MAIOR seq no detalhamento, e a ÂNCORA da linha
    (id do <tr>/tabela) p/ o link do histórico rolar até ela. Best-effort: navega ao
    detalhamento (dados frescos) e lê; qualquer erro devolve {'seq': None, 'anchor': ''}."""
    try:
        url = f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(300)
        r = await _eval_seguro(page, _JS_MAX_SEQ_MOV)
        if isinstance(r, dict) and r.get("seq") is not None:
            return {"seq": int(r["seq"]), "anchor": str(r.get("anchor") or "")}
    except Exception:  # noqa: BLE001
        pass
    return {"seq": None, "anchor": ""}


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
            info = await _ler_seq_notificacao(page, id_processo)
            mov_seq = info.get("seq")
            mov_anchor = info.get("anchor") or ""
            detalhe = f"Notificação enviada com sucesso → {resumo}"
            if mov_seq is not None:
                detalhe += f" · Mov {mov_seq}"
            return {"ok": True, "status": "enviado", "detalhe": detalhe,
                    "mov_seq": mov_seq, "mov_anchor": mov_anchor}
    return {"ok": False, "status": "falha",
            "detalhe": "Não confirmei o envio (timeout aguardando 'enviada com sucesso')."}


async def movimentacoes_paj(numero: str, ano: str, unidade: str = "44") -> dict:
    """Retorna dados completos do PAJ incluindo MOVIMENTACOES com URLs de peca.

    Fluxo: Caixa de Entrada -> clica no TEXTO DO PAJ -> Detalhamento do Processo.
    """
    await _ensure_logged_in()
    page = await _get_page()

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"

    clicked = await page.evaluate(
        """
        (pajTexto) => {
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes(pajTexto)) { a.click(); return true; }
            }
            return false;
        }
        """,
        paj_texto,
    )

    if not clicked:
        clicked = await page.evaluate(
            """
            (numero) => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes(numero)) { a.click(); return true; }
                }
                return false;
            }
            """,
            numero,
        )

    if not clicked:
        return {"erro": f"PAJ {paj_texto} não encontrado na caixa de entrada"}

    await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(3000)

    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            const assistidoMatch = body.match(
                /Assistido\\(s\\)[\\t\\s]+([^\\n]+)/
            );
            if (assistidoMatch) r.assistido = assistidoMatch[1].trim();

            // Extrai CPF da tabela "Assistido(s) Pessoa(s) Física:"
            // Seletor: primeira linha de dados, terceira célula (coluna CPF)
            const cpfCell = document.querySelector(
                '#detalhamentoForm\\\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)'
            );
            if (cpfCell) {
                const cpfText = cpfCell.textContent.trim().split('\\n')[0];
                if (cpfText && /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfText)) {
                    r.cpf_assistido = cpfText;
                }
            }

            const pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            }

            const oficioMatch = body.match(/Oficio[\\t\\s]+([^\\n]+)/) ||
                                body.match(/Ofício[\\t\\s]+([^\\n]+)/);
            if (oficioMatch) r.oficio = oficioMatch[1].trim();

            // Pretensao — formato "Pretensão  Area >> Subarea >> ..."
            const pretensaoMatch = body.match(
                /Pretens[ãa]o[\\t\\s]+([^\\n]+)/
            );
            if (pretensaoMatch) r.pretensao = pretensaoMatch[1].trim();

            // Data de abertura
            const aberturaMatch = body.match(
                /(?:Data\\s+de\\s+Abertura|Dt\\.?\\s*Abertura)[\\t\\s]+([^\\n]+)/i
            );
            if (aberturaMatch) r.data_abertura = aberturaMatch[1].trim();

            // Extração de Processo Judicial com Validação Cruzada
            // Busca dados na tabela de "Processo Judicial" e valida contra movimentações
            // IMPORTANTE: Todos os campos capturam DADOS COMPLETOS SEM LIMITE DE TAMANHO
            const extrairProcessoJudicial = () => {
              const resultado = {
                processo_judicial: null,     // Tamanho variável (número do processo)
                juizo: null,                 // Tamanho variável (nome/código do juízo)
                data_inclusao: null,         // Tamanho variável (data em DD/MM/YYYY)
                observacoes_processo: null,  // Tamanho variável SEM LIMITE (campo livre)
                validacao: { sucesso: false, motivo: '' }
              };

              // Buscar tabela de "Processo Judicial"
              const tabelas = document.querySelectorAll('table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro');
              let tabelaProcesso = null;

              for (const tabela of tabelas) {
                const th = tabela.querySelector('th');
                if (th && th.textContent.includes('Processo Judicial')) {
                  tabelaProcesso = tabela;
                  break;
                }
              }

              if (!tabelaProcesso) return resultado;

              const linhas = tabelaProcesso.querySelectorAll('tbody tr');

              // Processar linhas
              for (let i = 0; i < linhas.length; i++) {
                const linha = linhas[i];
                const celulas = linha.querySelectorAll('td');

                // Linha de DADOS (3+ colunas, sem "Observações")
                if (celulas.length >= 3 && !linha.textContent.includes('Observações:')) {
                  // Coluna 1: Processo (span com font-weight:bold)
                  const spanProc = celulas[0]?.querySelector('span[style*="font-weight"]');
                  resultado.processo_judicial = spanProc?.textContent.trim() || null;

                  // Coluna 2: Juízo (span com id contendo "juizo")
                  const spanJuizo = celulas[1]?.querySelector('span[id*="juizo"]');
                  resultado.juizo = spanJuizo?.textContent.trim() || null;

                  // Coluna 3: Data de Inclusão (primeiro span)
                  const spanData = celulas[2]?.querySelector('span');
                  resultado.data_inclusao = spanData?.textContent.trim() || null;
                }

                // Linha de OBSERVAÇÕES
                if (linha.textContent.includes('Observações:')) {
                  const celulasObs = linha.querySelectorAll('td');
                  if (celulasObs.length >= 2) {
                    resultado.observacoes_processo = celulasObs[1]?.textContent.trim() || null;
                  }
                }
              }

              return resultado;
            };

            // Tabela de movimentacoes do SISDPU (EXTRAIR ANTES da validação cruzada)
            // ESTRUTURA DA TABELA (indices das células <td>):
            // 0: Seq. (numero sequencial ex: "1", "2", "3")
            // 1: Anexo (icone de clipe se houver arquivo)
            // 2: Data/Hora (formato "DD/MM/YYYY HH:MM:SS")
            // 3: Movimentação (tipo de movimentacao ex: "MOVIMENTAÇÃO MANUAL")
            // 4: Fases (descricao da fase ex: "° Decurso de prazo;")
            // 5: Descrição (texto completo da movimentacao)
            // 6: (vazio/checkbox)
            // 7: Usuário (nome do usuario que fez a movimentacao)
            // 8+: (colunas adicionais)
            // CRÍTICO: NÃO INVERTER cells[3] e cells[4] — isso quebra o painel
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 8) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const anexo = (cells[1]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.innerHTML || '';
                    const usuario = (cells[7]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || fases)) {
                        const urls = textoCompleto.match(/https?:\\/\\/[^\\s]+/g) || [];
                        const links_decisao = urls.filter(u =>
                            u.includes('stj.jus.br') ||
                            u.includes('stf.jus.br') ||
                            u.includes('portal.stf') ||
                            u.includes('processo.stj') ||
                            u.includes('eproc') ||
                            u.includes('downloadPeca') ||
                            u.includes('documento') ||
                            u.includes('decisao') ||
                            u.endsWith('.pdf')
                        );

                        // descricao_retorno será extraído em segunda passagem (após cliques)
                        let descricao_retorno = '';

                        movs.push({
                            seq: seq,
                            anexo: anexo,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto,
                            links_decisao: links_decisao,
                            usuario: usuario,
                            descricao_retorno: descricao_retorno
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 500);

            // AGORA fazer a extração de Processo Judicial com validação cruzada
            // (após movimentações já estarem carregadas)
            const dadosProcesso = extrairProcessoJudicial();

            // Validação cruzada contra movimentações
            if (dadosProcesso.processo_judicial && dadosProcesso.data_inclusao) {
              const movCadastro = movs.find(mov =>
                mov.movimentacao?.includes('Cadastramento de processo judicial')
              );

              if (movCadastro) {
                // Validar 1: Número aparece na descrição
                const descricao = movCadastro.descricao || '';
                const numeroMatch = descricao.match(/Número do Processo Judicial[:\\s]+([\\S]+)/i);
                const numeroNaMov = numeroMatch ? numeroMatch[1] : '';

                // Validar 2: Data coincide (formato DD/MM/YYYY)
                const dataMov = movCadastro.data.substring(0, 10); // DD/MM/YYYY
                const dataInclusaoFormatada = dadosProcesso.data_inclusao;

                if (numeroNaMov === dadosProcesso.processo_judicial &&
                    dataMov === dataInclusaoFormatada) {
                  dadosProcesso.validacao.sucesso = true;
                  dadosProcesso.validacao.motivo = 'Validação cruzada bem-sucedida';
                } else {
                  dadosProcesso.validacao.motivo = 'Dados não coincidem com movimentação';
                }
              } else {
                dadosProcesso.validacao.motivo = 'Movimentação de cadastramento não encontrada';
              }
            } else {
              dadosProcesso.validacao.motivo = 'Dados incompletos (sem número ou data)';
            }

            // Armazenar dados extraídos
            r.processo_judicial = dadosProcesso.processo_judicial;
            r.juizo = dadosProcesso.juizo;
            r.data_inclusao = dadosProcesso.data_inclusao;
            r.observacoes_processo = dadosProcesso.observacoes_processo;
            r.validacao_processo = dadosProcesso.validacao;

            // Extração da Narrativa
            const extrairNarrativa = () => {
              let narrativa = null;

              // Buscar tabelas na página
              const tabelas = document.querySelectorAll('table.epaj_sistema_formulario_tabela');

              for (const tabela of tabelas) {
                // Buscar header com "Narrativa"
                const headers = tabela.querySelectorAll('th.epaj_sistema_formulario_tabela_th_titulo');
                let temNarrativa = false;

                for (const th of headers) {
                  if (th.textContent.includes('Narrativa')) {
                    temNarrativa = true;
                    break;
                  }
                }

                if (temNarrativa) {
                  // Encontrou a tabela de Narrativa
                  // Procurar o conteúdo após "Editar Narrativa"
                  const linhas = tabela.querySelectorAll('tbody tr');

                  for (let i = 0; i < linhas.length; i++) {
                    const linha = linhas[i];
                    const textoLinha = linha.textContent;

                    // Procurar por "Editar Narrativa"
                    if (textoLinha.includes('Editar Narrativa')) {
                      // A narrativa está na próxima linha ou na célula seguinte
                      const proxLinha = linhas[i + 1];
                      if (proxLinha) {
                        // Extrai HTML para preservar formatação (negrito, itálicos, parágrafos, etc.)
                        narrativa = proxLinha.innerHTML.trim();
                        break;
                      }
                    } else if (i > 0 && textoLinha && !textoLinha.includes('Editar')) {
                      // Se encontrou texto após "Editar Narrativa", é a narrativa com formatação HTML
                      narrativa = linha.innerHTML.trim();
                      break;
                    }
                  }

                  if (narrativa) break;
                }
              }

              return narrativa;
            };

            const narrativa = extrairNarrativa();
            if (narrativa) r.narrativa = narrativa;

            return r;
        }
        """
    )

    if not dados:
        dados = {}
    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    todas_urls: list[str] = []
    for m in dados.get("movimentacoes", []) or []:
        for u in m.get("links_decisao", []) or []:
            if u not in todas_urls:
                todas_urls.append(u)
    if todas_urls:
        dados["urls_decisoes"] = todas_urls

    # ===== SEGUNDA PASSAGEM: Extrair descricao_retorno para "Atendimento de retorno" =====
    movimentacoes = dados.get("movimentacoes", []) or []

    # 1. Encontrar e clicar em TODOS os toggles de "Atendimento de retorno"
    await page.evaluate("""
        () => {
            const allTrs = document.querySelectorAll('table tbody tr');
            for (const tr of allTrs) {
                const cells = tr.querySelectorAll('td');
                const fases = (cells[4]?.textContent || '').trim();

                if (fases.includes('Atendimento de retorno')) {
                    const toggler = tr.querySelector('.ui-row-toggler');
                    if (toggler && toggler.getAttribute('aria-expanded') !== 'true') {
                        toggler.click();
                    }
                }
            }
        }
    """)

    # 2. Aguardar que as requisições AJAX completem
    await _wait_pf_ajax(page, timeout=5000)

    # 3. Extrair seq -> conteúdo direto em JavaScript
    mapa_conteudos = await page.evaluate("""
        () => {
            const resultado = {};
            const allTrs = document.querySelectorAll('table tbody tr');

            for (let i = 0; i < allTrs.length; i++) {
                const tr = allTrs[i];
                const cells = tr.querySelectorAll('td');
                const seq = (cells[0]?.textContent || '').trim();
                const fases = (cells[4]?.textContent || '').trim();

                // Se esta TR tem "Atendimento de retorno"
                if (fases.includes('Atendimento de retorno')) {
                    // Procurar pela próxima TR (pode haver várias)
                    for (let j = i + 1; j < Math.min(i + 5, allTrs.length); j++) {
                        const proximaTr = allTrs[j];

                        // Se encontrou uma TR expandida
                        if (proximaTr.classList.contains('ui-expanded-row-content')) {
                            const td = proximaTr.querySelector('td');
                            if (td) {
                                resultado[seq] = td.textContent.trim();
                            }
                            break;
                        }
                    }
                }
            }

            return resultado;
        }
    """)

    # 4. Aplicar os conteúdos extraídos às movimentações
    for mov in movimentacoes:
        if "Atendimento de retorno" in mov.get("fases", ""):
            seq = str(mov.get("seq", "")).strip()

            # Debug: testar diferentes formatos de chave
            encontrou = False
            for chave_mapa in mapa_conteudos.keys():
                if str(chave_mapa).strip() == seq:
                    mov["descricao_retorno"] = mapa_conteudos[chave_mapa]
                    encontrou = True
                    break

            # Fallback: tentar direto
            if not encontrou and seq in mapa_conteudos:
                mov["descricao_retorno"] = mapa_conteudos[seq]

    return dados


# JS que dispara a busca rapida do SISDPU. Descobertas chave (vistas no
# page.html do debug):
#  - O botao tem onmouseup="buscarProcesso(event)" (funcao JS customizada).
#  - O resultado abre em POPUP via redirecionar(url, idProcesso) ->
#    window.open(...). Por isso a URL da janela atual nunca muda.
#  - O ano/unidade da busca vem dos hidden inputs anoAtualParaPesquisaRapida
#    e codigoUnidadeParaPesquisaRapida (defaults para a unidade do usuario).
#    Para buscar PAJ de outro ano/unidade temos que alterar esses hidden.
_JS_PESQUISAR_PAJ = """
(args) => {
    const { termo, ano, unidade } = args;
    const debug = {};

    // ----- 1) Hidden inputs ano/unidade — sobrescreve para o PAJ desejado -----
    const anoHidden = document.getElementById('anoAtualParaPesquisaRapida');
    const unidHidden = document.getElementById('codigoUnidadeParaPesquisaRapida');
    if (anoHidden) {
        debug.anoAntes = anoHidden.value;
        anoHidden.value = ano;
        debug.anoDepois = anoHidden.value;
    }
    if (unidHidden) {
        debug.unidAntes = unidHidden.value;
        unidHidden.value = unidade;
        debug.unidDepois = unidHidden.value;
    }

    // ----- 2) Input do numero -----
    const input = document.getElementById('processoPesquisaRapidaNumeroProcesso');
    if (!input) return { ok: false, erro: 'input processoPesquisaRapidaNumeroProcesso nao encontrado', debug };
    debug.inputId = input.id;

    const btn = document.getElementById('botaoPesquisarPesquisaRapida');
    if (!btn) return { ok: false, erro: 'botao botaoPesquisarPesquisaRapida nao encontrado', debug };
    debug.btnId = btn.id;

    input.focus();
    input.value = termo;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));

    // ----- 3) Aciona buscarProcesso(event) — o handler do onmouseup -----
    try {
        if (typeof buscarProcesso === 'function') {
            const ev = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
            Object.defineProperty(ev, 'target', { value: btn, writable: false });
            buscarProcesso(ev);
            debug.metodo = 'buscarProcesso(event)';
            return { ok: true, debug };
        }
        debug.buscarProcesso_disponivel = false;
    } catch (e) {
        debug.busca_erro = e.message;
    }

    // Fallback: dispara mouseup real no botao
    try {
        btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
        debug.metodo = (debug.metodo || '') + '+mouseup';
    } catch (e) {
        debug.mouseup_erro = e.message;
    }

    return { ok: true, debug };
}
"""


async def _dump_debug_paj(
    page: Page,
    paj_texto: str,
    motivo: str,
    extras: dict | None = None,
    extra_pages: dict[str, Page] | None = None,
) -> Path:
    """Salva screenshot + HTML + body.innerText + info da pagina atual em
    logs/sisdpu_debug/ para diagnostico. Usado tanto em falhas quanto em
    sucessos durante a fase de afinacao dos seletores. Devolve a pasta criada.

    `extra_pages={"popup": page2, ...}` dumpa tambem essas paginas extras com
    sufixo no nome do arquivo (popup_screenshot.png, popup_body.txt, etc.) —
    util pra capturar popups que foram abertos mas nao bateram a verificacao.
    """
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = paj_texto.replace("/", "-").replace(" ", "_")
    pasta = _DEBUG_DIR / f"{ts}_{slug}"
    pasta.mkdir(parents=True, exist_ok=True)

    # Purga subpastas antigas pra evitar acumulo (cada dump pesa ~1MB).
    _purgar_debug_dir_antigos()

    with contextlib.suppress(Exception):
        await page.screenshot(path=str(pasta / "screenshot.png"), full_page=True)
    with contextlib.suppress(Exception):
        html = await page.content()
        (pasta / "page.html").write_text(html, encoding="utf-8")
    with contextlib.suppress(Exception):
        texto = await page.inner_text("body")
        (pasta / "body.txt").write_text(texto, encoding="utf-8")

    info = [
        f"timestamp: {ts}",
        f"paj_texto: {paj_texto}",
        f"motivo: {motivo}",
        f"url: {page.url}",
    ]
    if extras:
        for k, v in extras.items():
            info.append(f"{k}: {v}")

    if extra_pages:
        for nome, pg in extra_pages.items():
            with contextlib.suppress(Exception):
                await pg.screenshot(path=str(pasta / f"{nome}_screenshot.png"), full_page=True)
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_page.html").write_text(await pg.content(), encoding="utf-8")
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_body.txt").write_text(
                    await pg.inner_text("body"), encoding="utf-8"
                )
            with contextlib.suppress(Exception):
                info.append(f"{nome}_url: {pg.url}")

    (pasta / "info.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    return pasta


async def buscar_paj_global(
    numero: str,
    ano: str,
    unidade: str = "44",
) -> dict:
    """Busca um PAJ via campo de pesquisa global do SISDPU (header da caixa).

    Diferente de `movimentacoes_paj`, esta funcao NAO depende do PAJ estar na
    caixa de entrada do defensor — usa o campo "Pesquisar" que aceita qualquer
    PAJ da unidade. Extrai os mesmos dados de cabecalho e movimentacoes.

    Retorna `{"erro": "..."}` se a busca falhar. Em caso de falha, salva
    screenshot + HTML + info em `logs/sisdpu_debug/` para diagnostico.
    """
    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"
    _log.info("INICIO termo='%s'", paj_texto)

    # Forca sessao fresca — fecha qualquer browser/page residual e refaz login
    # do zero. Garante estado limpo (sem sessao expirada silenciosamente, sem
    # pagina morta de uma execucao anterior).
    _log.info("forcando nova sessao (fechar+login)...")
    with contextlib.suppress(Exception):
        await fechar()
    await _ensure_logged_in()
    page = await _get_page()
    _log.info("logado, url pos-login: %s", page.url)

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(1500)
    await _wait_pf_ajax(page, timeout=10000)
    _log.info("caixa carregada url=%s", page.url)

    # ---------- Verificacoes de sessao viva ----------
    # 1) URL nao caiu pra tela de login (redirect silencioso por sessao expirada).
    if "login" in page.url.lower():
        msg = "apos login, ainda na tela de login — sessao nao autenticada"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(page, paj_texto, msg)
            _log.warning("FIM(login-nao-efetivo) debug salvo em: %s", pasta)
        return {"erro": msg}

    # 2) Estamos de fato na area logada: precisa ter o botao "Pesquisar" do
    #    header (visivel em todas as telas autenticadas) E o titulo da unidade
    #    ("Unidade DPU - ..."). Se nao aparecerem, a sessao esta "morta"
    #    (logado mas sem direitos / popup bloqueando / pagina parcial).
    sessao_viva = await page.evaluate(
        """() => {
            const corpo = (document.body.innerText || '').toLowerCase();
            const temUnidade = corpo.includes('unidade dpu');
            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            let temBtnPesquisar = false;
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim().toLowerCase();
                if (txt === 'pesquisar' || txt === 'buscar' || txt.includes('pesquis')) {
                    temBtnPesquisar = true; break;
                }
            }
            return {
                ok: temUnidade && temBtnPesquisar,
                temUnidade, temBtnPesquisar,
                trecho_inicio_body: corpo.substring(0, 200)
            };
        }"""
    )
    _log.info("sessao_viva=%s", sessao_viva)
    if not sessao_viva.get("ok"):
        msg = (
            f"sessao SIS-DPU nao confirmada como ativa — "
            f"temUnidade={sessao_viva.get('temUnidade')} "
            f"temBtnPesquisar={sessao_viva.get('temBtnPesquisar')}"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"sessao": sessao_viva},
            )
            _log.warning("FIM(sessao-morta) debug salvo em: %s", pasta)
        return {"erro": msg}

    # Diagnostico inicial — lista os inputs de texto e botoes com texto
    # "Pesquisar"/"Buscar" presentes na pagina. Util pra entender se a
    # heuristica esta achando o elemento errado ou nada.
    inventario = await page.evaluate(
        """() => {
            const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                id: el.id || '',
                type: el.type || '',
                name: el.name || '',
                placeholder: el.placeholder || '',
                ariaLabel: el.getAttribute('aria-label') || ''
            })).filter(i => i.type === 'text' || i.type === '' || i.type === 'search');

            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            const botoes = [];
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim();
                if (txt.toLowerCase().includes('pesquis') || txt.toLowerCase().includes('buscar')) {
                    botoes.push({ tag: el.tagName, id: el.id || '', texto: txt.substring(0, 40) });
                }
            }
            return {
                n_inputs_texto: inputs.length,
                inputs_amostra: inputs.slice(0, 12),
                botoes_pesquisar: botoes.slice(0, 8),
                titulo: document.title || ''
            };
        }"""
    )
    _log.info("inventario da pagina: %s", inventario)

    # Regra confirmada pelo usuario: o campo de pesquisa SEMPRE exige o numero
    # completo do PAJ (12 digitos no total: 4 ano + 3 unidade + 5 numero).
    # Caracteres especiais (`/` e `-`) sao opcionais. Pesquisar so pelo numero
    # final (ex: '00090' ou '90') NAO funciona — o SIS-DPU exige ano+unidade
    # tambem. Por isso so' tentamos as 2 formas validas:
    #   1) canonico com separadores  — `2024/003-00512`
    #   2) 12 digitos puros          — `202400300512`
    numero_z = numero.zfill(5)
    unidade_z = unidade.zfill(3)
    tentativas = [
        f"{ano}/{unidade_z}-{numero_z}",  # 2024/003-00512 (formato PAJ canonico)
        f"{ano}{unidade_z}{numero_z}",  # 202400300512 (12 digitos puros)
    ]
    vistos: set[str] = set()
    tentativas = [t for t in tentativas if not (t in vistos or vistos.add(t))]

    # Page do detalhamento — pode ser a propria janela atual (se buscarProcesso
    # navegar) ou um POPUP novo (descobrimos no markup que redirecionar() faz
    # window.open). Comeca como None — a primeira tentativa que abrir popup
    # OU navegar a janela atual sera capturada.
    page_detalhe: Page | None = None
    sucesso = False
    erro_ultimo = ""
    debug_acumulado: list[dict] = []
    # Regex que confirma "estou na tela de detalhamento do PAJ". O SIS-DPU
    # tem mais de uma variante visual de cabecalho — aceitamos qualquer pista:
    #  - "AAAA/UUU-NNNNN - PAJ STATUS" (formato compacto, presente em algumas paginas)
    #  - "Numero do PAJ\tAAAA/UUU-NNNNN" (formato tabular, mais comum na tela atual)
    #  - "Detalhamento do Processo" (titulo do breadcrumb — pista robusta)
    re_pag_detalhe = re.compile(
        r"\d{4}/\d{3}-\d+\s*-\s*PAJ\s"
        r"|N[úu]mero\s+do\s+PAJ\s+\d{4}/\d{3}-\d+"
        r"|Detalhamento\s+do\s+Processo"
    )
    contexto = page.context

    for termo in tentativas:
        _log.info("tentando termo='%s'", termo)

        # Escuta um novo "page" (popup) sendo aberto no contexto. Se a
        # buscarProcesso navegar na janela atual, esse expect_page nao captura
        # nada e cai no fallback abaixo.
        popup_capturado: Page | None = None
        try:
            async with contexto.expect_page(timeout=12000) as popup_info:
                res = await page.evaluate(
                    _JS_PESQUISAR_PAJ,
                    {"termo": termo, "ano": ano, "unidade": unidade_z},
                )
                if not isinstance(res, dict) or not res.get("ok"):
                    erro_ultimo = (res or {}).get("erro", "falha ao localizar campo de pesquisa")
                    debug_acumulado.append({"termo": termo, "res": res})
                    _log.warning("  FALHA: %s debug=%s", erro_ultimo, res)
                    if "campo" in erro_ultimo or "botao" in erro_ultimo:
                        # nao adianta tentar outros termos
                        raise StopIteration
            popup_capturado = await popup_info.value
            _log.info("  popup capturado url=%s", popup_capturado.url)
        except StopIteration:
            break
        except PWTimeoutError:
            _log.info("  nenhum popup aberto neste termo (timeout do expect_page)")
        except Exception as e:
            _log.warning("  erro ao esperar popup: %s: %s", type(e).__name__, e)

        # Se capturou popup, estrategia em 2 etapas:
        #
        # ETAPA 1 — tentar usar o popup direto. Espera load + polling do regex.
        # ETAPA 2 (fallback) — pega URL do popup e NAVEGA a janela principal
        # pra ela. Isso funciona porque `redirecionar(url, idProcesso)` no
        # SIS-DPU e' so' `window.open(url + idProcesso)` — sem state JSF, e a
        # URL e' totalmente stateless (so' precisa de cookie de sessao).
        # Como a janela principal ja tem PrimeFaces carregado da caixa, o load
        # completa de forma previsivel.
        # JS que detecta "estou na tela de detalhamento do PAJ" — aceita 3
        # padroes de cabecalho que o SIS-DPU usa em telas diferentes (mesmos
        # padroes do regex Python `re_pag_detalhe` acima). Compartilhado entre
        # ETAPA 1 (popup) e ETAPA 2 (janela principal pos-goto).
        js_detect_header = r"""() => {
            const txt = (document.body && document.body.innerText) || '';
            return /\d{4}\/\d{3}-\d+\s*-\s*PAJ\s/.test(txt)
                || /N[úu]mero\s+do\s+PAJ\s+\d{4}\/\d{3}-\d+/.test(txt)
                || /Detalhamento\s+do\s+Processo/.test(txt);
        }"""

        if popup_capturado is not None:
            popup_url_inicial = popup_capturado.url
            with contextlib.suppress(Exception):
                await popup_capturado.wait_for_load_state(
                    "domcontentloaded",
                    timeout=15000,
                )

            tem_header = False
            # ETAPA 1: tentar ler do popup (10s)
            with contextlib.suppress(PWTimeoutError, Exception):
                await popup_capturado.wait_for_function(
                    js_detect_header,
                    timeout=10000,
                    polling=250,
                )
                tem_header = True

            if tem_header:
                # Popup funcionou — usa ele direto
                with contextlib.suppress(Exception):
                    await _wait_pf_ajax(popup_capturado, timeout=10000)
                await popup_capturado.wait_for_timeout(500)
                _log.info(
                    "  popup OK url=%s",
                    popup_capturado.url,
                )
                page_detalhe = popup_capturado
                sucesso = True
                break

            # ETAPA 2: fallback — navega janela principal pra popup_url
            popup_url_final = popup_capturado.url
            url_alvo = (
                popup_url_final if "detalhamentoProcesso" in popup_url_final else popup_url_inicial
            )
            _log.info(
                "  popup nao renderizou em 10s — fallback: navegar janela principal pra %s",
                url_alvo,
            )

            # Fecha o popup antes de mexer na janela principal
            with contextlib.suppress(Exception):
                await popup_capturado.close()

            if not url_alvo or "detalhamentoProcesso" not in url_alvo:
                erro_ultimo = f"popup '{termo}' nao renderizou e URL inesperada: {url_alvo!r}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            try:
                await page.goto(url_alvo, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                _log.warning("  fallback goto falhou: %s: %s", type(e).__name__, e)
                erro_ultimo = f"popup '{termo}' + fallback navegacao falhou: {e}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            with contextlib.suppress(Exception):
                await _wait_pf_ajax(page, timeout=15000)
            # Polling do cabecalho na janela principal — 15s
            # Usa o mesmo `js_detect_header` (3 padroes aceitos)
            with contextlib.suppress(PWTimeoutError, Exception):
                await page.wait_for_function(
                    js_detect_header,
                    timeout=15000,
                    polling=250,
                )
                tem_header = True
            await page.wait_for_timeout(500)

            if tem_header:
                _log.info(
                    "  fallback OK — janela principal carregou detalhamento de %s",
                    page.url,
                )
                page_detalhe = page
                sucesso = True
                break

            # Mesmo o fallback falhou — dumpa tudo pra diagnostico
            with contextlib.suppress(Exception):
                pasta_dbg = await _dump_debug_paj(
                    page,
                    paj_texto,
                    f"popup '{termo}' nao renderizou + fallback nao carregou cabecalho",
                    extras={
                        "termo": termo,
                        "popup_url_inicial": popup_url_inicial,
                        "popup_url_final": popup_url_final,
                        "url_alvo": url_alvo,
                        "page_url_pos_goto": page.url,
                    },
                )
                _log.warning("  dump salvo em %s", pasta_dbg)

            erro_ultimo = (
                f"popup '{termo}' nao renderizou e fallback (goto {url_alvo}) "
                f"tambem nao mostrou cabecalho"
            )
            debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
            # Volta pra caixa antes de proxima tentativa
            with contextlib.suppress(Exception):
                await page.goto(
                    f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                    wait_until="domcontentloaded",
                )
                await _wait_pf_ajax(page, timeout=10000)
            continue

        # Fallback: a janela atual pode ter navegado para o detalhamento
        # (caso raro, mas defensivo). Da uns segundos pro PrimeFaces AJAX.
        with contextlib.suppress(Exception):
            await _wait_pf_ajax(page, timeout=15000)
        await page.wait_for_timeout(2000)
        body = ""
        with contextlib.suppress(Exception):
            body = await page.inner_text("body")
        tem_header_detalhe = bool(re_pag_detalhe.search(body))
        _log.info(
            "  pos-pesquisa (janela atual) url=%s tem_header_detalhe=%s",
            page.url,
            tem_header_detalhe,
        )
        if tem_header_detalhe:
            page_detalhe = page
            sucesso = True
            break
        erro_ultimo = (
            f"pesquisa por '{termo}' nao abriu popup nem navegou — "
            f"PAJ pode nao existir nessa unidade/ano"
        )
        debug_acumulado.append({"termo": termo, "url": page.url})

        # Volta pra caixa antes da proxima tentativa
        with contextlib.suppress(Exception):
            await page.goto(
                f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                wait_until="domcontentloaded",
            )
            await _wait_pf_ajax(page, timeout=10000)

    if not sucesso or page_detalhe is None:
        msg = erro_ultimo or "PAJ nao encontrado via pesquisa global"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"tentativas": debug_acumulado},
            )
            _log.warning("FIM(falha) debug salvo em: %s", pasta)
        return {"erro": msg}

    # A partir daqui a extracao roda em `page_detalhe`, que pode ser a janela
    # atual OU um popup aberto por window.open. Substituimos a referencia
    # `page` localmente para que o resto do codigo continue limpo.
    page = page_detalhe

    # Caso o popup tenha sido capturado, promove ele a "page global" — assim
    # operacoes posteriores que dependem de `_get_page()` (ex: listar_arquivos_paj
    # → baixar anexos) atuam no detalhamento do PAJ, e nao na caixa de entrada
    # antiga. Sem isso, sincronizar um PAJ da watchlist via busca global nao
    # conseguiria baixar anexos. Quando `page_detalhe` ja e' a propria `_page`
    # (busca navegou a janela atual em vez de abrir popup), a atribuicao e' no-op.
    global _page
    _page = page_detalhe

    # Extrai os campos do cabecalho do detalhamento. Estrategia hibrida:
    # (1) DOM-based para label/valor (mais robusto que regex no innerText)
    # (2) regex residual para padroes posicionais (numero CNJ, linha PAJ).
    # (3) filtros para descartar valores que sao claramente cabecalhos de tabela.
    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            // ---------- Helper: encontra valor adjacente a um label ----------
            // O regex inclui o ':' ASCII E o U+FF1A (fullwidth colon) porque labels
            // do SISDPU aparecem com qualquer um dos dois, dependendo da fonte.
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim()
                                          .toLowerCase().replace(/[:：]$/, '');

            // Lixo conhecido — cabecalhos de tabelas do SISDPU que aparecem
            // proximos a labels e foram capturados por engano antes.
            const LIXO_TABELA = /^(data de envio|remetente|prazo|descri[çc][ãa]o|tipo|status|data|seq|usu[áa]rio|fases|movimenta[çc][ãa]o|seq\\.?)\\b/i;

            function valorParaLabel(rotulos) {
                const alvos = rotulos.map(norm);
                const candidatos = document.querySelectorAll(
                    'td, th, label, span, dt, dd, li, b, strong, div, p'
                );
                for (const el of candidatos) {
                    const txt = norm(el.textContent || '');
                    if (!alvos.includes(txt)) continue;

                    // 1) Proximo sibling com texto util.
                    let next = el.nextElementSibling;
                    while (next) {
                        const v = (next.textContent || '').trim();
                        if (v) {
                            const linha = v.split('\\n')[0].trim();
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                        next = next.nextElementSibling;
                    }

                    // 2) Pai contendo label e valor juntos no innerText.
                    const pai = el.parentElement;
                    if (pai) {
                        const fullPai = (pai.textContent || '').trim();
                        const idx = fullPai.toLowerCase().indexOf(txt);
                        if (idx >= 0) {
                            const depois = fullPai.substring(idx + (el.textContent || '').length).trim();
                            const linha = depois.split('\\n')[0].trim().replace(/^[:\\-\\s]+/, '');
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                    }
                }
                return '';
            }

            // ---------- Campos do cabecalho (todos os que aparecem na aba Resumo) ----------
            r.assistido = valorParaLabel(['Assistido(s)', 'Assistido', 'Assistidos']);

            // Extrai CPF da tabela "Assistido(s) Pessoa(s) Física:"
            // Seletor: primeira linha de dados, terceira célula (coluna CPF)
            const cpfCell = document.querySelector(
                '#detalhamentoForm\\\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)'
            );
            if (cpfCell) {
                const cpfText = cpfCell.textContent.trim().split('\\n')[0];
                if (cpfText && /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfText)) {
                    r.cpf_assistido = cpfText;
                }
            }

            r.pretensao = valorParaLabel(['Pretensão', 'Pretensao']);
            r.data_abertura = valorParaLabel([
                'Data de Abertura', 'Dt. Abertura', 'Dt Abertura', 'Dt.Abertura', 'Abertura'
            ]);
            r.oficio = valorParaLabel([
                'Ofício', 'Oficio', 'Ofício Responsável', 'Oficio Responsavel',
                'Ofício responsável', 'Ofício atual'
            ]);
            r.juizo = valorParaLabel([
                'Juízo/Orgão Julgador', 'Juizo/Orgao Julgador',
                'Juízo/Órgão Julgador', 'Juízo', 'Juizo',
                'Órgão Julgador', 'Orgao Julgador'
            ]);
            r.foro_detalhado = valorParaLabel([
                'Foro Detalhado', 'Foro detalhado', 'Foro'
            ]);
            r.decurso = valorParaLabel([
                'Decurso', 'Decurso agendado', 'Decurso Agendado'
            ]);

            // ---------- PAJ + status — duas variantes da tela ----------
            //  a) Formato compacto: "AAAA/UUU-NNNNN - PAJ STATUS"
            //  b) Formato tabular:  "Número do PAJ\tAAAA/UUU-NNNNN"  (nesta tela,
            //     o status vem em outra linha, ex: "Urgente" / "Tutela Coletiva".
            //     Deixamos status vazio se nao houver formato compacto.)
            let pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            } else {
                pajMatch = body.match(/N[úu]mero\\s+do\\s+PAJ[\\s\\t]+(\\d{4}\\/\\d{3}-\\d+)/);
                if (pajMatch) {
                    r.paj = pajMatch[1];
                    r.status_paj = '';
                }
            }

            // ---------- Numero do processo judicial (padrao CNJ) ----------
            const procMatch = body.match(
                /(\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4})/
            );
            if (procMatch) r.processo_judicial = procMatch[1];

            // ---------- Movimentacoes (tabela; mesma logica de antes) ----------
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 8) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const anexo = (cells[1]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.innerHTML || '';
                    const usuario = (cells[6]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || fases)) {

                        // descricao_retorno será extraído em segunda passagem (após cliques)
                        let descricao_retorno = '';

                        movs.push({
                            seq: seq,
                            anexo: anexo,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto,
                            links_decisao: [],
                            usuario: usuario,
                            descricao_retorno: descricao_retorno
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 500);

            // ---------- Filtro final: descarta qualquer campo que tenha vazado lixo ----------
            for (const k of ['assistido', 'pretensao', 'oficio', 'juizo', 'foro_detalhado', 'decurso', 'data_abertura']) {
                if (r[k] && LIXO_TABELA.test(r[k])) {
                    r[k] = '';
                }
            }

            return r;
        }
        """
    )

    if not dados:
        dados = {}

    n_movs = len(dados.get("movimentacoes", []) or [])
    _log.info(
        "extracao: assistido=%s paj=%s movs=%d",
        bool(dados.get("assistido")),
        dados.get("paj", "?"),
        n_movs,
    )

    # Validacao rigorosa: precisa do `paj` extraido pelo regex
    # "AAAA/UUU-NNNNN - PAJ STATUS", que so' aparece na tela de detalhamento.
    # Sem ele, ainda estamos na caixa ou em pagina inesperada.
    if not dados.get("paj"):
        msg = (
            "extracao falhou — cabecalho do PAJ nao encontrado na pagina "
            "(provavelmente nao estamos na tela de detalhamento)"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"dados_parciais": {k: v for k, v in dados.items() if k != "movimentacoes"}},
            )
            _log.warning("FIM(sem-paj) debug salvo em: %s", pasta)
        return {"erro": msg}

    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    # ===== SEGUNDA PASSAGEM: Extrair descricao_retorno para "Atendimento de retorno" =====
    movimentacoes = dados.get("movimentacoes", []) or []

    # 1. Encontrar e clicar em TODOS os toggles de "Atendimento de retorno"
    await page.evaluate("""
        () => {
            const allTrs = document.querySelectorAll('table tbody tr');
            for (const tr of allTrs) {
                const cells = tr.querySelectorAll('td');
                const fases = (cells[4]?.textContent || '').trim();

                if (fases.includes('Atendimento de retorno')) {
                    const toggler = tr.querySelector('.ui-row-toggler');
                    if (toggler && toggler.getAttribute('aria-expanded') !== 'true') {
                        toggler.click();
                    }
                }
            }
        }
    """)

    # 2. Aguardar que as requisições AJAX completem
    await _wait_pf_ajax(page, timeout=5000)

    # 3. Extrair seq -> conteúdo direto em JavaScript
    mapa_conteudos = await page.evaluate("""
        () => {
            const resultado = {};
            const allTrs = document.querySelectorAll('table tbody tr');

            for (let i = 0; i < allTrs.length; i++) {
                const tr = allTrs[i];
                const cells = tr.querySelectorAll('td');
                const seq = (cells[0]?.textContent || '').trim();
                const fases = (cells[4]?.textContent || '').trim();

                // Se esta TR tem "Atendimento de retorno"
                if (fases.includes('Atendimento de retorno')) {
                    // Procurar pela próxima TR (pode haver várias)
                    for (let j = i + 1; j < Math.min(i + 5, allTrs.length); j++) {
                        const proximaTr = allTrs[j];

                        // Se encontrou uma TR expandida
                        if (proximaTr.classList.contains('ui-expanded-row-content')) {
                            const td = proximaTr.querySelector('td');
                            if (td) {
                                resultado[seq] = td.textContent.trim();
                            }
                            break;
                        }
                    }
                }
            }

            return resultado;
        }
    """)

    # 4. Aplicar os conteúdos extraídos às movimentações
    for mov in movimentacoes:
        if "Atendimento de retorno" in mov.get("fases", ""):
            seq = str(mov.get("seq", "")).strip()

            # Debug: testar diferentes formatos de chave
            encontrou = False
            for chave_mapa in mapa_conteudos.keys():
                if str(chave_mapa).strip() == seq:
                    mov["descricao_retorno"] = mapa_conteudos[chave_mapa]
                    encontrou = True
                    break

            # Fallback: tentar direto
            if not encontrou and seq in mapa_conteudos:
                mov["descricao_retorno"] = mapa_conteudos[seq]

    # Dump tambem em caso de sucesso durante esta fase de afinacao — ajuda
    # diagnosticar campos vazios sem precisar retentar. Pode ser desligado
    # depois que a extracao estiver estavel.
    with contextlib.suppress(Exception):
        pasta = await _dump_debug_paj(
            page,
            paj_texto,
            "sucesso (dump pra afinacao)",
            extras={
                "campos_extraidos": {
                    k: v for k, v in dados.items() if k not in ("movimentacoes", "sisdpu_raw")
                },
            },
        )
        _log.info("dump de sucesso salvo em: %s", pasta)
    _log.info("FIM(ok) %s", paj_texto)
    return dados


async def baixar_arquivo(url: str, destino) -> bool:
    """Baixa arquivo reaproveitando a sessao autenticada (cookies) do SISDPU.

    Retorna True se o arquivo foi gravado com tamanho > 0.
    """
    try:
        import httpx
    except ImportError:
        return False

    page = await _get_page()
    context = page.context
    cookies = await context.cookies()
    cookie_jar = {c["name"]: c["value"] for c in cookies}

    try:
        async with httpx.AsyncClient(
            cookies=cookie_jar,
            timeout=60,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*;q=0.8",
            },
        ) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            if not resp.content:
                return False
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(resp.content)
            return destino.stat().st_size > 0
    except Exception:
        return False


_EXTRACAO_DOCS_JS_PATH = Path(__file__).resolve().parent / "extrair_documentos_movimentacoes.js"


async def extrair_documentos_por_seq(
    seqs_conhecidos: list[str] | None = None,
    deve_cancelar: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Extrai, por seq de movimentacao, os documentos anexados (via o icone de
    "Anexo" de cada linha) do PAJ atualmente aberto.

    PRE-CONDICAO: a page ja deve estar no detalhamento do PAJ (chamar
    `movimentacoes_paj(...)` antes) — mesma pre-condicao de `listar_arquivos_paj`.

    `seqs_conhecidos`: seqs (str) que ja possuem `documentos` no metadata da
    sincronizacao anterior — sao excluidas da fila, entao so processamos seqs
    novas desde a ultima sync (mantem o custo proporcional ao que mudou).

    Pilota o script `extrair_documentos_movimentacoes.js` (copia adaptada em
    `ingestao/`, NAO o original em C:\\Claude\\Scripts\\SISDPU\\) passo a passo:
    INIT (monta a fila) -> N chamadas de __extracaoProximo() (uma por seq,
    clica/le/fecha o popup) -> __extracaoFinalizar() (consolida o resultado).

    Retorna dict: {paj, assistido, movimentacoes_com_documentos: [{seq,
    dataHora, movimentacao, documentos: [{tipoArquivo, descricao, nomeArquivo,
    urlDownload}]}], total_documentos, seqsPuladas, tabela_markdown}.
    Em caso de erro ou tabela nao encontrada, retorna {"erro": "..."}.
    """

    def _log(msg: str) -> None:
        if log:
            with contextlib.suppress(Exception):
                log(msg)

    page = await _get_page()

    js_source = _EXTRACAO_DOCS_JS_PATH.read_text(encoding="utf-8")
    # Envolve o arquivo inteiro numa arrow function explicita — mesmo padrao
    # usado em todo o resto deste modulo (ex: listar_arquivos_paj). Passar o
    # conteudo bruto (que comeca com um comentario /** */) faz o Playwright
    # nao reconhecer a string como "function-like", o que pode fazer as
    # atribuicoes a `window.*` nao "pegarem" do jeito esperado entre chamadas
    # seguintes de evaluate().
    await page.evaluate("(src) => { (0, eval)(src); }", js_source)

    async def _finalizar_seguro() -> dict:
        tem_estado = await page.evaluate("() => !!window.__extracaoEstado")
        if not tem_estado:
            _log("  [docs] estado da extracao perdido (dialogo/pagina mudou) — resultado descartado")
            return {"movimentacoes_com_documentos": [], "total_documentos": 0}
        return await page.evaluate("window.__extracaoFinalizar()")

    init = await page.evaluate(
        "(seqs) => window.__extracaoInit(seqs)", list(seqs_conhecidos or [])
    )

    if init.get("erro"):
        _log(f"  [docs] {init['erro']}")
        return {"erro": init["erro"], "movimentacoes_com_documentos": [], "total_documentos": 0}

    total_fila = init.get("totalFila", 0)
    puladas = init.get("puladas", 0)
    if puladas:
        _log(f"  [docs] {puladas} seq(s) ja conhecida(s) — pulando")
    if total_fila == 0:
        _log("  [docs] nenhuma seq nova com anexo para extrair")
        return await _finalizar_seguro()

    _log(f"  [docs] extraindo documentos de {total_fila} seq(s) nova(s)...")

    try:
        while True:
            if deve_cancelar is not None and deve_cancelar():
                _log("  [docs] CANCELADO pelo usuario")
                break
            r = await page.evaluate("window.__extracaoProximo()")
            if r.get("fim"):
                break
            progresso = r.get("progresso", "?")
            erro = r.get("erro")
            if erro:
                _log(f"    [docs] seq {r.get('seq')}: {erro} ({progresso})")
            else:
                n_docs = len(r.get("documentos", []))
                _log(f"    [docs] seq {r.get('seq')}: {n_docs} doc(s) ({progresso})")
    except Exception as e:
        _log(f"  [docs] erro durante extracao: {type(e).__name__}: {e}")
        # Garante que nao fica popup aberto atrapalhando o proximo passo (ex:
        # listar_arquivos_paj logo em seguida, no bloco de anexos).
        with contextlib.suppress(Exception):
            await page.evaluate(
                "() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
            )

    return await _finalizar_seguro()


async def listar_arquivos_paj(debug: bool = False) -> dict:
    """Clica no botao "Arquivos" do PAJ atualmente aberto e extrai a lista.

    PRE-CONDICAO: a page ja deve estar no detalhamento do PAJ (chamar
    `movimentacoes_paj(...)` antes). Nao navega — so interage com a pagina atual.

    Retorna dict:
        {
            "ok": bool,
            "itens": [{tipo, descricao, arquivo, row_index, todas_celulas}, ...],
            "diag": {...}   # sempre presente; use pra debug quando itens == []
        }
    `diag` inclui: candidatos_botao_arquivos, clicou, dialogs_visiveis,
    dialog_escolhido, total_linhas, linhas_filtradas_por_texto, html_snippet.
    """
    page = await _get_page()

    # 1) Inventariar candidatos ao botao "Arquivos" ANTES de clicar
    diag: dict = {}
    diag["candidatos_botao_arquivos"] = await page.evaluate(
        """
        () => {
            const out = [];
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt && /arquivo/i.test(txt) && txt.length < 40) {
                    out.push({
                        tag: el.tagName,
                        texto: txt,
                        id: el.id || '',
                        classes: el.className || '',
                        onclick_has: !!el.getAttribute('onclick'),
                    });
                }
            }
            return out;
        }
        """
    )

    # 2) Clica no link "Arquivos" — agora mais tolerante
    clicked = await page.evaluate(
        """
        () => {
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            // Preferencia: match EXATO por "Arquivos" depois de trim
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt === 'Arquivos' || txt === 'Arquivo(s)') {
                    el.click();
                    return 'exato';
                }
            }
            // Fallback: contem "Arquivos" (mas nao "Arquivo anexo" de movimentacao)
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/^Arquivos?$/i.test(txt) || /^Arquivos\\s*\\(\\d+\\)$/.test(txt)) {
                    el.click();
                    return 'regex';
                }
            }
            // Fallback extremo: qualquer elemento com "Arquivo" curto
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/arquivo/i.test(txt) && txt.length < 15) {
                    el.click();
                    return 'fuzzy:' + txt;
                }
            }
            return '';
        }
        """
    )
    diag["clicou"] = clicked or "NADA"
    if not clicked:
        return {"ok": False, "itens": [], "diag": diag}

    # Aguarda AJAX + eventual navegacao (o link "Arquivos" navega para outra tela).
    # Tenta networkidle primeiro; se falhar, cai pra wait_pf_ajax + sleep.
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(1500)

    # 3) Inventariar dialogs REALMENTE visiveis (filtra aria-hidden=true)
    diag["dialogs_visiveis"] = await page.evaluate(
        """
        () => {
            const dialogs = document.querySelectorAll('.ui-dialog');
            return Array.from(dialogs)
                .filter(d => {
                    if (d.getAttribute('aria-hidden') === 'true') return false;
                    if (d.classList.contains('ui-overlay-hidden')) return false;
                    const st = window.getComputedStyle(d);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    return true;
                })
                .map(d => ({
                    id: d.id || '',
                    titulo: (d.querySelector('.ui-dialog-title')?.textContent || '').trim(),
                    linhas_tabela: d.querySelectorAll('table tbody tr').length,
                }));
        }
        """
    )

    # Registra URL pra saber se navegou
    with contextlib.suppress(Exception):
        diag["url_apos_click"] = page.url

    # 4) Extrai linhas — a pagina de Arquivos do SISDPU tem MULTIPLAS tabelas,
    # uma por categoria ("Juntada de documento", "Concluso ao defensor",
    # "Oficio/Carta/Comunicacao expedida", etc.). Iteramos TODAS as tabelas
    # visiveis com botoes "Visualizar" em ordem do DOM e concatenamos as linhas
    # com indice global. A funcao baixar_anexo_por_indice usa a MESMA estrategia
    # de flatten para que `row_index` seja consistente.
    extrato = await page.evaluate(
        """
        () => {
            const diag = {estrategia: '', total_linhas: 0, linhas_filtradas: 0,
                          tabelas_analisadas: 0, tabelas_com_visualizar: 0,
                          categorias: []};

            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                        if (cur.classList && (
                            cur.classList.contains('ui-overlay-hidden') ||
                            cur.classList.contains('ui-hidden-container')
                        )) {
                            if (cur.getAttribute('aria-hidden') === 'true') return false;
                        }
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            // Tenta extrair o rotulo da categoria de uma tabela: o texto curto
            // mais proximo (header/legend/div) que aparece imediatamente antes.
            const detectaCategoria = (tbl) => {
                let prev = tbl.previousElementSibling;
                let tentativas = 0;
                while (prev && tentativas < 6) {
                    const t = (prev.textContent || '').trim();
                    if (t && t.length < 100 && t.length > 2 && !/^(Seq\\.|Nenhum registro)/i.test(t)) {
                        return t.split('\\n')[0].trim().slice(0, 80);
                    }
                    prev = prev.previousElementSibling;
                    tentativas++;
                }
                // Fallback: legend de fieldset ancestral
                let p = tbl.parentElement;
                while (p) {
                    const lg = p.querySelector(':scope > legend, :scope > .ui-fieldset-legend');
                    if (lg) {
                        const t = (lg.textContent || '').trim();
                        if (t) return t.slice(0, 80);
                    }
                    p = p.parentElement;
                }
                return '';
            };

            const tables = document.querySelectorAll('table');
            diag.tabelas_analisadas = tables.length;

            // Coleta tabelas visiveis com links "Visualizar"
            const uteis = [];
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }
            diag.tabelas_com_visualizar = uteis.length;

            if (uteis.length === 0) {
                // Fallback: tabelas visiveis cujos headers mencionem "arquivo"
                for (const tbl of tables) {
                    const ths = Array.from(tbl.querySelectorAll('th')).map(th => (th.textContent || '').toLowerCase());
                    if (!/arquivo|documento/.test(ths.join('|'))) continue;
                    if (!tbl.querySelector('tbody tr')) continue;
                    if (!isVisivel(tbl)) continue;
                    uteis.push(tbl);
                }
                if (uteis.length > 0) diag.estrategia = 'tabela_por_header';
            } else {
                diag.estrategia = uteis.length > 1
                    ? `multi_tabela_visualizar (${uteis.length})`
                    : 'tabela_com_visualizar';
            }

            if (uteis.length === 0) {
                diag.erro = 'nenhuma_tabela_util_encontrada';
                return {resultado: [], diag};
            }

            // Flatten: percorre todas as tabelas em ordem do DOM e concatena as
            // linhas com indice global crescente. A categoria de cada tabela e'
            // anexada a cada linha — util pra naming/diagnostico.
            const resultado = [];
            let idx = 0;
            const snippets = [];
            for (const tbl of uteis) {
                const categoria = detectaCategoria(tbl);
                diag.categorias.push({categoria: categoria, linhas: 0});
                const catIdx = diag.categorias.length - 1;
                const rows = tbl.querySelectorAll('tbody tr');
                diag.total_linhas += rows.length;
                for (const tr of rows) {
                    if (tr.classList.contains('ui-widget-header')) continue;
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 5) continue;  // Precisa das 5 primeiras colunas no mínimo
                    const textos = Array.from(cells).map(c => (c.textContent || '').trim());
                    // Primeira coluna DEVE ser um número válido (Seq.)
                    const seq = textos[0] || '';
                    if (!seq || !/^\\d+$/.test(seq)) continue;
                    const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                    let temVis = false;
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { temVis = true; break; }
                    }
                    if (!temVis) continue;
                    // Extrai nome do arquivo DIRETAMENTE do link "Visualizar" (fonte de verdade)
                    const linkVis = tr.querySelector('a, button, span[role="button"]');
                    let nome_arquivo = '';
                    let href = '';
                    if (linkVis) {
                        // Tenta extrair em ordem de confiabilidade:
                        // 1. Atributo download (HTML5)
                        // 2. Atributo data-filename (customizado)
                        // 3. Href com padrão comuns (ex: /download/123)
                        const downloadAttr = linkVis.getAttribute('download');
                        const dataNome = linkVis.getAttribute('data-filename');
                        let hrefAttr = linkVis.getAttribute('href') || '';

                        // Torna href absoluto se for relativo
                        if (hrefAttr && !hrefAttr.startsWith('http')) {
                            hrefAttr = window.location.origin + (hrefAttr.startsWith('/') ? '' : '/sisdpu/') + hrefAttr;
                        }
                        href = hrefAttr;

                        if (downloadAttr) nome_arquivo = downloadAttr;
                        else if (dataNome) nome_arquivo = dataNome;
                        else if (href && /\\.(pdf|docx?|xlsx?|txt|zip|jpg|png|gif)$/i.test(href)) {
                            // Se href tem extensão, usar último segmento como nome
                            nome_arquivo = href.split('/').pop();
                        }
                    }
                    // Fallback para coluna "Arquivo" se não conseguir extrair do link
                    if (!nome_arquivo) nome_arquivo = textos[4] || '';

                    resultado.push({
                        row_index: idx++,
                        seq: textos[0] || '',
                        data: textos[1] || '',
                        descricao: textos[2] || '',
                        tipo_arquivo: textos[3] || '',
                        arquivo: nome_arquivo,
                        href: href,
                        categoria: categoria,
                        todas_celulas: textos,
                    });
                    diag.categorias[catIdx].linhas++;
                }
                try { snippets.push((tbl.outerHTML || '').slice(0, 800)); } catch (e) {}
            }
            // Remove duplicatas exatas (seq + arquivo idênticos)
            const vistos = new Set();
            const resultadoDeduplic = [];
            for (const item of resultado) {
                const chave = item.seq + '|' + item.arquivo;
                if (!vistos.has(chave)) {
                    vistos.add(chave);
                    resultadoDeduplic.push(item);
                }
            }
            const n_removidas = resultado.length - resultadoDeduplic.length;
            if (n_removidas > 0) diag.linhas_removidas = n_removidas;

            // Recalcula row_index sequencial (0, 1, 2, ...) após desduplicação
            for (let i = 0; i < resultadoDeduplic.length; i++) {
                resultadoDeduplic[i].row_index = i;
            }

            diag.linhas_filtradas = resultadoDeduplic.length;
            diag.html_snippet = snippets.join('\\n---\\n').slice(0, 2000);
            return {resultado: resultadoDeduplic, diag};
        }
        """
    )

    diag.update(extrato.get("diag", {}))
    itens = extrato.get("resultado", []) or []
    return {"ok": True, "itens": itens, "diag": diag}


_JS_MARCAR_POR_INDICE = """
(idx) => {
    const isVisivel = (el) => {
        let cur = el;
        while (cur) {
            if (cur.nodeType === 1) {
                if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
            }
            cur = cur.parentElement;
        }
        try {
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return false;
        } catch (e) {}
        return true;
    };
    const tables = document.querySelectorAll('table');
    const uteis = [];
    for (const tbl of tables) {
        const rows = tbl.querySelectorAll('tbody tr');
        if (rows.length === 0) continue;
        // Filtra tabelas que sao placeholders "Nenhum registro"
        let todosVazios = true;
        for (const r of rows) {
            const cells = r.querySelectorAll('td');
            if (cells.length === 0) continue;
            const textos = Array.from(cells).map(c => (c.textContent || '').trim());
            if (!/nenhum\\s+registro/i.test(textos.join(' '))) {
                todosVazios = false;
                break;
            }
        }
        if (todosVazios) continue;

        let comVis = 0;
        for (const r of rows) {
            const alvos = r.querySelectorAll('a, button, span[role="button"]');
            for (const a of alvos) {
                if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
            }
        }
        if (comVis === 0) continue;
        if (!isVisivel(tbl)) continue;
        uteis.push(tbl);
    }
    if (uteis.length === 0) return false;
    const linhas = [];
    for (const tbl of uteis) {
        const rows = Array.from(tbl.querySelectorAll('tbody tr'))
            .filter(tr => {
                if (tr.classList.contains('ui-widget-header')) return false;
                if (tr.querySelectorAll('td').length === 0) return false;
                const cells = tr.querySelectorAll('td');
                const textos = Array.from(cells).map(c => (c.textContent || '').trim());
                const seq = (textos[0] || '').trim();
                if (!seq || /nenhum|cabeçalho|conclus[aã]o|juntada|oficio/i.test(seq)) return false;
                const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                for (const a of alvos) {
                    if (/visualizar/i.test((a.textContent || '').trim())) return true;
                }
                return false;
            });
        for (const r of rows) linhas.push(r);
    }
    if (idx >= linhas.length) return false;
    const row = linhas[idx];
    const alvos = row.querySelectorAll('a, button, span[role="button"]');
    for (const el of alvos) {
        if (/visualizar/i.test((el.textContent || '').trim())) {
            el.setAttribute('data-dpu-pick', '1');
            return true;
        }
    }
    return false;
}
"""

_JS_MARCAR_POR_SEQ = """
(seq) => {
    // Busca a linha pela coluna Seq. (primeira coluna) em TODAS as tabelas.
    // Mais robusto que posicao quando a pagina e' atualizada entre downloads.
    const tables = document.querySelectorAll('table');
    for (const tbl of tables) {
        for (const tr of tbl.querySelectorAll('tbody tr')) {
            const cells = tr.querySelectorAll('td');
            if (cells.length === 0) continue;
            if ((cells[0].textContent || '').trim() !== seq) continue;
            const alvos = tr.querySelectorAll('a, button, span[role="button"]');
            for (const a of alvos) {
                if (/visualizar/i.test((a.textContent || '').trim())) {
                    a.setAttribute('data-dpu-pick', '1');
                    return true;
                }
            }
        }
    }
    return false;
}
"""

_JS_CLICAR_ARQUIVOS = """
() => {
    const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
    for (const el of els) {
        const txt = (el.textContent || '').trim();
        if (txt === 'Arquivos' || txt === 'Arquivo(s)') { el.click(); return 'exato'; }
    }
    for (const el of els) {
        const txt = (el.textContent || '').trim();
        if (/^Arquivos?$/i.test(txt) || /^Arquivos\\s*\\(\\d+\\)$/.test(txt)) { el.click(); return 'regex'; }
    }
    return '';
}
"""

# Extensoes conhecidas para deteccao via nome de arquivo (ultima extensao valida).
_EXTS_ARQUIVO = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".txt", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".csv", ".xml", ".html", ".htm",
    ".odt", ".ods", ".odp", ".rtf",
}


def extrair_id_arquivo(url: str) -> str:
    """Extrai o ID numerico do documento a partir da URL de download do SISDPU.

    Ex: '.../rest/download/pdf/42296157/Nome.pdf' -> '42296157'.
    Esse ID pertence ao ARQUIVO (nao a seq/listagem) e e' o mesmo em qualquer
    mecanismo que o descubra (listar_arquivos_paj ou extracao por seq de
    movimentacao) — usado como chave de juncao para nomear os arquivos
    baixados de forma que o frontend consiga casar o link da coluna
    "Documentos" com a copia local ja baixada, sem depender do nome do
    arquivo (que pode se repetir, ex: "email.pdf").
    """
    m = re.search(r"/(\d+)/[^/]*$", url or "")
    return m.group(1) if m else ""


def _ext_de_nome_arquivo(nome: str) -> str:
    """Extrai extensao do FINAL do nome de arquivo validando contra lista conhecida.

    Usa rfind('.') para pegar sempre o ULTIMO ponto — evita interpretar pontos
    no meio do nome (ex: '1234-56.789.4.01.2345') como extensao.
    Retorna '' se nao reconhecida.
    """
    if not nome:
        return ""
    lower = nome.lower().strip()
    last = lower.rfind(".")
    if last < 0 or last == len(lower) - 1:
        return ""
    ext = lower[last:]
    return ext if ext in _EXTS_ARQUIVO else ""


async def extrair_lista_arquivos_paj() -> list[dict]:
    """Extrai a lista de arquivos do PAJ atualmente aberto via JavaScript.

    Itera todas as tabelas visiveis e retorna uma lista de documentos com:
    {seq, data, nomeArq, href}

    Estrategia identica a SISDPU-AnalisarPAJ2Cat skill.
    """
    page = await _get_page()

    resultado = await page.evaluate(
        """
        () => {
            const docs = [];
            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            const tables = document.querySelectorAll('table');
            const uteis = [];

            // Coleta tabelas visiveis com links "Visualizar"
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }

            // Itera todas as tabelas uteis e coleta arquivos
            for (const tbl of uteis) {
                const rows = tbl.querySelectorAll('tbody tr');
                for (const tr of rows) {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 5) continue;

                    const seq = (cells[0]?.textContent || '').trim();
                    if (!seq || !/^\\d+$/.test(seq)) continue;

                    const data = (cells[1]?.textContent || '').trim();

                    // Nome do arquivo vem da coluna cells[4] (coluna "Arquivo")
                    const nomeArq = (cells[4]?.textContent || '').trim().split('\\n')[0].trim();

                    // Link "Visualizar" — usar getAttribute('href') em vez de .href
                    // para evitar parametros JSF que quebram downloads
                    let href = '';
                    const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) {
                            href = a.getAttribute('href') || '';
                            break;
                        }
                    }

                    if (nomeArq && href) {
                        docs.push({
                            seq: seq,
                            data: data,
                            nomeArq: nomeArq,
                            href: href
                        });
                    }
                }
            }

            return docs;
        }
        """
    )

    return resultado or []


async def baixar_todos_arquivos(
    pasta_destino: Path,
    lista_arquivos: list[dict] | None = None,
    log: Callable[[str], None] | None = None,
    seq_por_file_id: dict[str, int] | None = None,
) -> dict:
    """Baixa todos os arquivos do PAJ via JavaScript fetch + local server.

    Se lista_arquivos não for fornecida, extrai via listar_arquivos_paj().
    Caso contrário, usa a lista ja fornecida (cada item deve ter: arquivo, seq, data, href ou as keys esperadas).

    `seq_por_file_id`: mapa {id_do_arquivo (str) -> seq_real (int)} vindo da
    extracao por seq de movimentacao (`extrair_documentos_por_seq`). Usado
    pra nomear o arquivo com a seq REAL da movimentacao — se o id do arquivo
    (extraido do href) nao estiver no mapa, cai no `seq` fornecido pela lista
    de origem (listar_arquivos_paj, que e' so um indice de listagem).

    Estrategia:
    1. Extrai lista de arquivos (ou usa fornecida)
    2. Inicia local server (download_server.py) na porta 8765
    3. Para cada arquivo: fetch() + Base64 + POST ao servidor local
    4. Servidor salva arquivo em disco
    5. Mata o processo do servidor

    Retorna {baixados: int, erros: list, total: int}
    """
    import subprocess
    import asyncio

    def _log(msg: str) -> None:
        if log:
            with contextlib.suppress(Exception):
                log(msg)

    pasta_destino = Path(pasta_destino)
    pasta_destino.mkdir(parents=True, exist_ok=True)

    # 1) Extrai lista de arquivos
    _log("[fetch] preparando downloads...")
    if lista_arquivos is None:
        _log("[fetch] extraindo lista de arquivos...")
        resultado = await listar_arquivos_paj()
        docs = resultado.get("itens", []) if isinstance(resultado, dict) else (resultado or [])
    else:
        docs = lista_arquivos

    # Normaliza para formato esperado pelo fetch (precisa de: seq, data, nomeArq, href)
    # Converte da lista de listar_arquivos_paj que tem: seq, data, descricao, tipo_arquivo, arquivo
    docs_normalizados = []
    for doc in docs:
        seq = doc.get("seq", "?")
        data = doc.get("data", "")
        # Nome do arquivo: tenta 'arquivo' primeiro (de listar_arquivos_paj), depois 'nomeArq' (de extrair_lista_arquivos_paj)
        nome_arq = doc.get("arquivo") or doc.get("nomeArq", "arquivo")
        # href: tenta 'href' direto ou reconstrói... deixa vazio se não houver
        href = doc.get("href", "")

        docs_normalizados.append({
            "seq": seq,
            "data": data,
            "nomeArq": nome_arq,
            "href": href,
        })

    docs = docs_normalizados
    _log(f"[fetch] {len(docs)} arquivo(s) encontrado(s)")

    if not docs:
        return {"baixados": 0, "erros": [], "total": 0}

    # 2) Inicia servidor local
    _log("[fetch] iniciando servidor local na porta 8765...")

    # Tenta em ordem: projeto local, depois PREV4
    server_path = Path(__file__).resolve().parents[1] / ".claude" / "download_server.py"
    if not server_path.exists():
        server_path = Path("C:/Claude/DPU/PREV4/.claude/download_server.py")

    if not server_path.exists():
        _log(f"[fetch] ERRO: servidor {server_path} não encontrado")
        return {"baixados": 0, "erros": ["servidor download_server.py não encontrado"], "total": len(docs)}

    proc = None
    try:
        # Inicia o servidor como background process (Windows)
        # Usa cwd do diretório do projeto para que paths relativos funcionem
        cwd = Path(__file__).resolve().parents[1].parent

        proc = subprocess.Popen(
            ["python", str(server_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
        )
        await asyncio.sleep(1)  # Aguarda servidor iniciar
    except Exception as e:
        _log(f"[fetch] ✗ falha ao iniciar servidor: {type(e).__name__}")
        if proc:
            with contextlib.suppress(Exception):
                proc.terminate()
        return {"baixados": 0, "erros": [f"falha ao iniciar servidor"], "total": len(docs)}

    page = await _get_page()
    context = page.context
    baixados = 0
    erros = []

    try:
        # 3) Download via Playwright (usa cookies da session autenticada) + POST ao servidor
        import httpx
        import base64

        # Extrai cookies do contexto Playwright para passar ao httpx
        cookies_list = await context.cookies()
        cookies_dict = {c["name"]: c["value"] for c in cookies_list}

        for idx, doc in enumerate(docs, start=1):
            seq = doc.get("seq", "?")
            nomeArq = doc.get("nomeArq", "arquivo")
            href = doc.get("href", "")

            # Formata nome para POST: seqNNN - ID - NOME
            # O ID (extraido do href) e' a chave estavel que o frontend usa
            # pra casar o link da coluna "Documentos" com este arquivo local
            # — nao depende do nome (que pode se repetir, ex: "email.pdf").
            # A seq usa a REAL (do mapa por id), com 3 digitos pra ordenar
            # certo na pasta quando passar de 100 seqs.
            file_id = extrair_id_arquivo(href)
            seq_real = (seq_por_file_id or {}).get(file_id, seq)
            try:
                seq_fmt = f"{int(seq_real):03d}"
            except (TypeError, ValueError):
                seq_fmt = str(seq_real)

            nome_para_post = f"seq{seq_fmt} - {file_id or '0'} - {nomeArq}"

            _log(f"[{idx}/{len(docs)}]   {nomeArq}")

            # Se href estiver vazio, trata como erro
            if not href:
                _log(f"               ... ERRO     ✗ href vazio")
                erros.append(f"{nomeArq}: href vazio")
                continue

            try:
                # Fetch do arquivo via Playwright (reutiliza cookies autenticados)
                pasta_abs = str(pasta_destino).replace("\\", "/")

                # Usa httpx síncrono com cookies do contexto do Playwright
                async with httpx.AsyncClient(cookies=cookies_dict, verify=False) as client:
                    # Fetch arquivo
                    resp = await client.get(href, follow_redirects=True, timeout=30.0)
                    if resp.status_code != 200:
                        _log(f"               ... ERRO     ✗ HTTP {resp.status_code}")
                        erros.append(f"{nomeArq}: HTTP {resp.status_code}")
                        continue

                    conteudo = resp.content
                    tamanho = len(conteudo)

                    if tamanho == 0:
                        _log(f"               ... ERRO     ✗ vazio")
                        erros.append(f"{nomeArq}: arquivo vazio")
                        continue

                    # Formata tamanho em KB/MB
                    if tamanho < 1024 * 1024:
                        tam_str = f"{tamanho / 1024:.1f} KB"
                    else:
                        tam_str = f"{tamanho / (1024 * 1024):.1f} MB"

                    # Converte para Base64
                    b64_data = base64.b64encode(conteudo).decode("ascii")

                    # POST ao servidor local
                    try:
                        post_resp = await client.post(
                            "http://127.0.0.1:8765",
                            json={
                                "filename": nome_para_post,
                                "data": b64_data,
                                "target": pasta_abs,
                            },
                            timeout=10.0,
                        )
                        if post_resp.status_code == 200:
                            _log(f"               ... Concluído    ✓ {tam_str}")
                            baixados += 1
                        else:
                            _log(f"               ... ERRO     ✗ POST {post_resp.status_code}")
                            erros.append(f"{nomeArq}: POST HTTP {post_resp.status_code}")
                    except Exception as e:
                        _log(f"               ... ERRO     ✗ {type(e).__name__}")
                        erros.append(f"{nomeArq}: POST {type(e).__name__}")

            except Exception as e:
                _log(f"               ... ERRO     ✗ {type(e).__name__}")
                erros.append(f"{nomeArq}: {type(e).__name__}")

    finally:
        # 4) Mata o servidor
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    if baixados == len(docs):
        _log(f"[fetch] ✓ {baixados} arquivo(s) baixado(s) com sucesso")
    else:
        _log(f"[fetch] ⚠ {baixados}/{len(docs)} arquivo(s) baixado(s) — {len(erros)} erro(s)")
    return {
        "baixados": baixados,
        "erros": erros,
        "total": len(docs),
    }


async def baixar_anexo_por_indice(
    row_index: int,
    destino,
    seq: str = "",
    ext_hint: str = "",
):
    """DEPRECADO: Mantido por compatibilidade. Use extrair_lista_arquivos_paj()
    + baixar_todos_arquivos() em vez disso.

    Clica em "Visualizar" da linha `row_index` e salva o arquivo baixado.
    """
    from pathlib import Path as _Path

    destino = _Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    page = await _get_page()

    found = await page.evaluate(_JS_MARCAR_POR_INDICE, row_index)
    metodo = "indice"

    if not found and seq:
        metodo = "seq-fallback"
        with contextlib.suppress(Exception):
            await page.evaluate(_JS_CLICAR_ARQUIVOS)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(1000)
        found = await page.evaluate(_JS_MARCAR_POR_SEQ, seq)

    if not found:
        return None

    def _limpar_marker():
        with contextlib.suppress(Exception):
            return page.evaluate(
                "() => document.querySelectorAll('[data-dpu-pick]')"
                ".forEach(el => el.removeAttribute('data-dpu-pick'))"
            )

    try:
        async with page.expect_event("download", timeout=60000) as dl_info:
            await page.click('[data-dpu-pick="1"]')
        download = await dl_info.value

        sugerido = (download.suggested_filename or "").strip()
        ext_sugerida = _Path(sugerido).suffix.lower() if sugerido else ""
        if not ext_sugerida or ext_sugerida not in _EXTS_ARQUIVO:
            ext_sugerida = ext_hint if ext_hint else ".bin"

        atual_suffix = destino.suffix.lower()
        if atual_suffix and atual_suffix in _EXTS_ARQUIVO:
            destino_final = destino
        else:
            destino_final = destino.with_suffix(ext_sugerida)

        await download.save_as(str(destino_final))
    except Exception as e:
        await _limpar_marker()
        return None

    await _limpar_marker()

    if destino_final.exists() and destino_final.stat().st_size > 0:
        return destino_final
    return None


async def fechar_dialogo_arquivos() -> None:
    """Fecha o dialogo 'Arquivos' se estiver aberto (para nao interferir em outro PAJ)."""
    page = await _get_page()
    try:
        await page.evaluate(
            """
            () => {
                const dialogs = document.querySelectorAll('.ui-dialog:not(.ui-overlay-hidden)');
                for (const d of dialogs) {
                    const titulo = d.querySelector('.ui-dialog-title');
                    if (titulo && /arquivo/i.test(titulo.textContent || '')) {
                        const btn = d.querySelector('.ui-dialog-titlebar-close');
                        if (btn) btn.click();
                    }
                }
            }
            """
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass


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
