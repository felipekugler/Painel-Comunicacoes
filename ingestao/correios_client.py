"""Cliente dos Correios — SMT / e-Carta Fácil (Texto livre) via Playwright headless.

Expede uma CARTA SIMPLES pelo assistente do SMT: login no CAS (usuário+senha do
COFRE), destinatário do PAJ, cabeçalho/corpo/rodapé (CKEditor) e, se `efetivar`,
posta e baixa o COMPROVANTE. Fluxo/seletores aprendidos ao vivo em 2026-07-26
(ver memória project_correios_expedir_carta).

Regra de segurança: `efetivar=False` (padrão) percorre tudo e PARA no Resumo — não
posta, não cobra. `efetivar=True` clica em "Efetivar" (postagem paga). Quem governa
isso é o modo simulado/real do meio 'correspondencia' no chamador.

Seletores: SPA Angular, classes hasheadas → localizar por TEXTO/`name`. Editores são
CKEditor 4 (`CKEDITOR.instances._cartaNacional{Cabecalho,Corpo,Rodape}`). O CEP
autopreenche o endereço só com os eventos de digitação (Playwright `.fill()`).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from playwright.async_api import (Browser, Page, TimeoutError as PWTimeoutError,
                                  async_playwright)

log = logging.getLogger("correios")

CAS_URL = ("https://cas.correios.com.br/login?service="
           "https%3A%2F%2Fsmt.correios.com.br%2Fsmt%2F")
SMT_HOME = "https://smt.correios.com.br/smt/#/home"

_playwright: Any = None
_browser: Browser | None = None
_page: Page | None = None


class CorreiosSemCredenciais(RuntimeError):
    """Não há credenciais do Correios no cofre (ou o painel está trancado)."""


class CorreiosCredenciaisInvalidas(RuntimeError):
    """O CAS recusou usuário/senha do Correios."""


class CorreiosCepNaoIdentificado(RuntimeError):
    """O site dos Correios NÃO reconheceu o CEP do destinatário (não autopreencheu
    logradouro/bairro/cidade/UF) — provável CEP desmembrado. Aborta a expedição."""


# ------------------------------------------------------------------ browser
async def _get_page() -> Page:
    """Retorna a page Playwright (cria o browser headless se preciso). Chromium com
    o PDF viewer desabilitado (o comprovante é baixado como arquivo, não previsto)."""
    global _playwright, _browser, _page
    if _page is not None:
        # Detecta navegador/página FECHADOS (ex.: usuário fechou a janela headed) e recria.
        vivo = False
        with contextlib.suppress(Exception):
            vivo = (not _page.is_closed()) and _browser is not None and _browser.is_connected()
        if vivo:
            return _page
        with contextlib.suppress(Exception):
            if _browser is not None:
                await _browser.close()
        _page = None
        _browser = None
    if _playwright is None:
        _playwright = await async_playwright().start()
    # Por padrão VISÍVEL (headed) — permite acompanhar/concluir o assistente dos Correios.
    # Para voltar a headless: variável de ambiente CORREIOS_HEADLESS=1.
    _headless = os.environ.get("CORREIOS_HEADLESS", "0") == "1"
    _browser = await _playwright.chromium.launch(
        headless=_headless,
        slow_mo=(0 if _headless else 250),   # em modo visível, desacelera p/ dar p/ acompanhar
        args=["--disable-features=PDFViewerUpdate,PdfUnseasoned", "--start-maximized"],
    )
    context = await _browser.new_context(
        viewport={"width": 1366, "height": 900}, accept_downloads=True,
        # UA de Chrome real: alguns sites (CAS) recusam o "HeadlessChrome".
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
    _page = await context.new_page()
    return _page


async def fechar() -> None:
    """Fecha o navegador headless (encerra a sessão do SMT)."""
    global _playwright, _browser, _page
    with contextlib.suppress(Exception):
        if _browser:
            await _browser.close()
    _browser = None
    _page = None
    with contextlib.suppress(Exception):
        if _playwright:
            await _playwright.stop()
    _playwright = None


# ------------------------------------------------------------------ helpers
_JS_CLICK_TEXTO = r"""
(args) => {
  const [txt, tags, exato] = args;
  const sel = tags || 'button,a,div,span,input';
  const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                      .replace(/\s+/g, ' ').trim().toLowerCase();
  const alvo = norm(txt);
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.disabled; };
  const els = [...document.querySelectorAll(sel)].filter(vis);
  let el = els.find(e => norm(e.textContent || e.value) === alvo);          // 1) igualdade normalizada
  if (!el && !exato) el = els.find(e => norm(e.textContent || e.value).includes(alvo));  // 2) contém
  if (el) { el.click(); return true; }
  return false;
}
"""

# Diagnóstico: rótulos dos elementos acionáveis visíveis (p/ descobrir o texto real de um botão).
# Inclui .botao* (Próximo/Efetivar/Visualizar são <a>/<div> com essas classes) e corta textos
# longos (barra do CKEditor).
_JS_LISTAR_ACOES = r"""
() => [...new Set([...document.querySelectorAll(
    'button, a, input[type=button], input[type=submit], [role=button], [class*=botao]')]
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => (e.textContent || e.value || '').replace(/\s+/g, ' ').trim())
  .filter(t => t && t.length < 30))].slice(0, 40)
"""


async def _click_texto(page: Page, txt: str, tags: str | None = None, timeout_ms: int = 8000,
                       exato: bool = True) -> bool:
    """Clica no 1º elemento visível cujo texto casa com `txt` (SPA Angular; espera aparecer).
    `exato=True`: igualdade normalizada (sem acento/maiúsculas). `exato=False`: também aceita 'contém'."""
    fim = timeout_ms
    while fim > 0:
        if await page.evaluate(_JS_CLICK_TEXTO, [txt, tags, exato]):
            return True
        await page.wait_for_timeout(250)
        fim -= 250
    return False


async def _clicar_ref(page: Page, texto: str, timeout_ms: int = 8000) -> bool:
    """Clique REAL por REFERÊNCIA do elemento: o Playwright localiza o elemento pelo texto exato e
    clica no CENTRO dele (dispara eventos confiáveis) — nunca por coordenada fixa. Dispara os
    handlers do Angular de forma bem mais confiável que o `.click()` via JS. Tenta button, depois
    link, e por fim qualquer elemento com o texto exato (cobre <div>/<a> estilizados: Próximo,
    Efetivar, Salvar)."""
    fim = timeout_ms
    while fim > 0:
        for loc in (page.get_by_role("button", name=texto, exact=True),
                    page.get_by_role("link", name=texto, exact=True),
                    page.get_by_text(texto, exact=True)):
            with contextlib.suppress(Exception):
                alvo = loc.first
                if await alvo.is_visible():
                    with contextlib.suppress(Exception):
                        await alvo.scroll_into_view_if_needed(timeout=1500)
                    await alvo.click(timeout=2500)
                    return True
        await page.wait_for_timeout(250)
        fim -= 250
    return False


async def _hash(page: Page) -> str:
    with contextlib.suppress(Exception):
        return (await page.evaluate("() => location.hash")) or ""
    return ""


async def _diagnostico(page: Page) -> str:
    """Retrato do estado atual p/ calibrar seletores: hash + nº de frames + botões de CADA frame
    (revela se o assistente está dentro de um iframe)."""
    partes = ["hash=" + (await _hash(page))]
    frames = list(page.frames)
    partes.append(f"frames={len(frames)}")
    for i, fr in enumerate(frames):
        with contextlib.suppress(Exception):
            btns = await fr.evaluate(_JS_LISTAR_ACOES)
            url = (fr.url or "")[-45:]
            if btns:
                partes.append(f"[f{i} …{url}] " + " | ".join(btns[:18]))
    return " ;; ".join(partes)


# ------------------------------------------------------------------ login
async def esta_logado() -> bool:
    """True se a sessão do SMT está ativa (home acessível sem cair no CAS)."""
    page = await _get_page()
    with contextlib.suppress(Exception):
        await page.goto(SMT_HOME, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(800)
        return "cas.correios.com.br" not in (page.url or "")
    return False


async def _login() -> None:
    """Login no CAS com usuário+senha do COFRE (meio Correios). ⚠️ seletores do form
    do CAS a CONFIRMAR no teste ao vivo — padrão CAS: input[name=username]/[password]."""
    from services import credenciais_service
    cred = credenciais_service.get_credenciais_correios()
    if not cred:
        # Distingue "cofre trancado" (credenciais existem, só não conectado nesta sessão)
        # de "credenciais realmente não cadastradas" — o cofre cifrado é opaco quando trancado.
        if credenciais_service.configurado() and not credenciais_service.destrancado():
            raise CorreiosSemCredenciais(
                "O painel está trancado — as credenciais não foram carregadas nesta sessão. "
                "Destranque o acesso com a senha-mestra (barra de status → Reconectar, ou "
                "Configurações → Acesso) e tente novamente.")
        raise CorreiosSemCredenciais(
            "Credenciais do Correios não cadastradas. Cadastre-as em Configurações → "
            "Acesso ao Correios (com o painel destrancado).")
    user, senha = cred
    page = await _get_page()
    await page.goto(CAS_URL, wait_until="domcontentloaded", timeout=20000)
    # Form CAS padrão: #username / #password / button[name=submitBtn] "ENTRAR" (form#fm1).
    await page.wait_for_selector("#username", timeout=15000)
    await page.fill("#username", user)
    await page.fill("#password", senha)
    # Se houver reCAPTCHA ativo, o login headless NÃO passa — avisa claramente.
    if await page.evaluate("() => !!document.querySelector('.g-recaptcha, iframe[src*=recaptcha], [data-sitekey]')"):
        raise CorreiosCredenciaisInvalidas(
            "O CAS está exigindo reCAPTCHA agora — o login automático (headless) não "
            "consegue resolver. Tente novamente mais tarde ou faça um login manual "
            "para 'destravar' o CAPTCHA.")
    try:
        await page.click("button[name='submitBtn']", timeout=4000)
    except Exception:  # noqa: BLE001 — fallback: Enter no campo de senha
        await page.press("#password", "Enter")
    try:
        await page.wait_for_url("**/smt/**", timeout=20000)
    except PWTimeoutError:
        if "cas.correios.com.br" in (page.url or ""):
            # Lê a mensagem de erro do CAS (div.mensagem / .errors / .alert) p/ diagnóstico.
            msg = await page.evaluate(r"""() => {
                for (const s of ['.mensagem','#msg','.errors','.alert-danger','.alert','.text-danger']) {
                    const el=document.querySelector(s);
                    if (el && (el.innerText||'').trim()) return (el.innerText||'').trim();
                }
                return ''; }""")
            raise CorreiosCredenciaisInvalidas(
                (f"O CAS recusou o login: {msg[:200]}" if msg else
                 "O CAS não autenticou (usuário/senha incorretos ou etapa extra de "
                 "segurança). Confira as credenciais do Correios.")) from None
        raise
    await page.wait_for_timeout(800)


async def _ensure_logged_in() -> None:
    """Garante sessão ativa no SMT; refaz login se necessário."""
    if await esta_logado():
        return
    await _login()


async def testar_login() -> dict:
    """Testa o acesso ao Correios (headless). Retorna {ok, status, detalhe}."""
    try:
        await _ensure_logged_in()
    except CorreiosSemCredenciais as e:
        return {"ok": False, "status": "indisponivel", "detalhe": str(e)}
    except CorreiosCredenciaisInvalidas as e:
        return {"ok": False, "status": "credenciais", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "erro", "detalhe": f"Falha ao testar ({e})."}
    return {"ok": True, "status": "ok", "detalhe": "Acesso ao Correios confirmado."}


# ------------------------------------------------------------------ wizard
async def _ler_lotes(page: Page) -> list[str]:
    """Lê os números de Lote/Mensagem das linhas de 'Remessas enviadas no dia' (#/criar)."""
    with contextlib.suppress(Exception):
        return await page.evaluate(r"""() => {
            const rows = [...document.querySelectorAll('tr')].filter(tr =>
                tr.querySelector('a[title="Abrir comprovante de postagem"]'));
            const out = [];
            for (const tr of rows) {
                const cel = [...tr.querySelectorAll('td')].map(td => td.textContent.trim())
                    .find(t => /^\d{6,}$/.test(t));
                if (cel) out.push(cel);
            }
            return out;
        }""")
    return []


async def _abrir_assistente(page: Page, lotes_snapshot: list | None = None) -> None:
    """Home → menu → Criar Mensagens → cartão → e-Carta Fácil Texto livre → wizard.
    Se `lotes_snapshot` for uma lista, guarda nela os Lotes JÁ existentes na tela #/criar
    ANTES de compor a carta — para depois identificar SEM AMBIGUIDADE a remessa recém-postada
    (o lote que NÃO estava no snapshot), essencial quando há vários envios no mesmo dia."""
    await page.goto(SMT_HOME, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(600)
    # menu hambúrguer (id estável) → MENSAGENS → Criar Mensagens
    with contextlib.suppress(Exception):
        await page.click("a#nav-menu", timeout=5000)
    await page.wait_for_timeout(300)
    await _click_texto(page, "MENSAGENS", tags="a")
    await page.wait_for_timeout(300)
    await _click_texto(page, "Criar Mensagens", tags="a")
    await page.wait_for_timeout(1000)               # → #/lista-cartoes-do-contrato
    await page.click("div.cartao", timeout=8000)
    await page.wait_for_timeout(1200)               # → #/criar (lista de remessas do dia visível)
    if lotes_snapshot is not None:
        lotes_snapshot[:] = await _ler_lotes(page)
        log.info("[correios] lotes já existentes antes de postar: %s", lotes_snapshot)
    await _click_texto(page, "e-Carta Fácil - Texto livre", tags="div")
    await page.wait_for_timeout(1200)               # → #/wizard-carta-internet


async def _preencher_destinatario(page: Page, d: dict) -> None:
    """Passo Destinatário: abre o form, preenche e Salva (CEP autopreenche o endereço)."""
    await page.click('a.novo[title="Incluir Destinatário"]', timeout=8000)
    await page.wait_for_selector('input[name="nomeContato"]', timeout=8000)
    await page.fill('input[name="nomeContato"]', (d.get("nome") or "").strip())
    if d.get("cpf"):
        with contextlib.suppress(Exception):
            await page.fill('input[name="cpfCnpj"]', str(d["cpf"]).strip())
    # CEP → autopreenche logradouro/bairro/cidade/uf (aguarda a consulta).
    cep = "".join(ch for ch in str(d.get("cep") or "") if ch.isdigit())
    await page.fill('input[name="cep"]', cep)
    await page.evaluate("""() => { const e=document.querySelector('input[name=cep]');
        e.dispatchEvent(new Event('change',{bubbles:true})); e.blur(); }""")
    # Aguarda o autopreenchimento do CEP (até ~4s): lê logradouro, cidade e UF de forma
    # robusta (retorna '' quando o campo não existe). Sinal de que a consulta respondeu =
    # qualquer um deles preenchido.
    _JS_END = """() => {
        const g = n => { const e = document.querySelector('input[name="' + n + '"]'); return e ? (e.value||'').trim() : ''; };
        return { logr: g('endereco.logradouro'),
                 cidade: g('endereco.cidade') || g('endereco.localidade') || g('endereco.municipio'),
                 uf: g('endereco.uf') || g('endereco.estado') };
    }"""
    vals = {"logr": "", "cidade": "", "uf": ""}
    for _ in range(20):
        vals = await page.evaluate(_JS_END)
        if vals.get("logr") or vals.get("cidade") or vals.get("uf"):
            break
        await page.wait_for_timeout(200)
    # CEP NÃO IDENTIFICADO pelos Correios: a consulta não trouxe NEM cidade NEM UF → o site
    # não preencheu os campos e o cadastro do destinatário quebraria. Aborta com mensagem clara
    # (provável CEP desmembrado). Não vale a pena tentar preencher tudo à mão.
    if not vals.get("cidade") and not vals.get("uf"):
        raise CorreiosCepNaoIdentificado(
            "CEP não identificado nos correios. Verifique se o CEP não foi desmembrado.")
    # CEP de localidade (cidade inteira): veio cidade/UF, mas NÃO o logradouro →
    # usa o logradouro do cadastro do PAJ (item 2 da conferência de endereço).
    if not vals.get("logr") and (d.get("logradouro") or "").strip():
        with contextlib.suppress(Exception):
            await page.fill('input[name="endereco.logradouro"]', str(d["logradouro"]).strip())
    await page.fill('input[name="endereco.numero"]', str(d.get("numero") or "").strip())
    if d.get("complemento"):
        await page.fill('input[name="endereco.complemento"]', str(d["complemento"]).strip())
    await _click_texto(page, "Salvar", tags="a")     # a.botao-principal
    await page.wait_for_timeout(1200)


_JS_SET_CKEDITOR_CORREIOS = r"""
(args) => {
  const [inst, html] = args;
  const ck = window.CKEDITOR && CKEDITOR.instances && CKEDITOR.instances[inst];
  if (!ck) return false;
  ck.setData(html);                 // idêntico ao que funcionou na inspeção manual
  try { ck.fire('change'); } catch (e) {}
  try { ck.updateElement(); } catch (e) {}   // sincroniza p/ o <textarea> (o app lê daqui)
  return true;
}
"""


def _txt_para_html(txt: str) -> str:
    """Converte texto simples (com \n) em HTML seguro para o CKEditor."""
    esc = (txt or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return esc.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


# Abre a PRÓXIMA seção da Mensagem: clica no <div> "Clique Aqui" (leaf) da 1ª seção ainda
# não preenchida. Aprendido ao vivo (2026-07-27): o título é um <h2> (não abre nada); quem abre
# o editor é o "Clique Aqui". Seções já preenchidas exibem o texto (deixam de mostrar "Clique Aqui").
_JS_CLICAR_CLIQUE_AQUI = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const el = [...document.querySelectorAll('div')].find(
    e => e.childElementCount === 0 && norm(e.textContent) === 'Clique Aqui');
  if (el) { el.click(); return true; }
  return false;
}
"""

# Nome da instância CKEditor recém-aberta (a que ainda não foi preenchida).
_JS_NOVA_INSTANCIA = r"""
(feitos) => {
  const ks = (window.CKEDITOR && CKEDITOR.instances) ? Object.keys(CKEDITOR.instances) : [];
  return ks.find(k => !feitos.includes(k)) || null;
}
"""

# "Visualizar" só aparece após Salvar e começa DESABILITADO (cinza). Um JS checa se está
# habilitado (sem clicar) e o outro clica — assim dá p/ preparar a captura da nova aba (PDF) antes.
_JS_VISUALIZAR_HABILITADO = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('button, a, input, [role=button]')].some(e => {
    const t = norm(e.textContent || e.value); const r = e.getBoundingClientRect();
    return t === 'Visualizar' && r.width > 0 && r.height > 0 && !e.disabled;
  });
}
"""
_JS_CLICAR_VISUALIZAR = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const b = [...document.querySelectorAll('button, a, input, [role=button]')].find(e => {
    const t = norm(e.textContent || e.value);
    const r = e.getBoundingClientRect();
    return t === 'Visualizar' && r.width > 0 && r.height > 0 && !e.disabled;
  });
  if (b) { b.click(); return true; }
  return false;
}
"""


async def _preencher_mensagem(page: Page, cabecalho: str, corpo: str, rodape: str) -> bool:
    """Passo Mensagem: SANFONA de UM CKEditor por vez. Cada seção só cria seu editor ao clicar em
    "Clique Aqui"; abrir outra DESTRÓI a instância anterior, mas o texto já setado PERSISTE. Então
    abrimos e preenchemos UMA seção por vez, identificando a instância que surge
    (_cartaNacional{Cabecalho,Corpo,Rodape}). Depois clica em SALVAR (por referência) e confirma o
    salvamento pelo SURGIMENTO do "Visualizar" (NÃO clica em Visualizar — o PDF é pego no Resumo).
    Retorna True se confirmou o salvamento."""
    textos = {"_cartaNacionalCabecalho": cabecalho,
              "_cartaNacionalCorpo": corpo,
              "_cartaNacionalRodape": rodape}
    feitos: list[str] = []
    for _ in range(3):
        if not await page.evaluate(_JS_CLICAR_CLIQUE_AQUI):
            break  # não há mais seção a abrir
        await page.wait_for_timeout(500)    # espera o editor inicializar
        inst = None
        for _ in range(15):  # espera a instância recém-aberta (≠ das já feitas)
            inst = await page.evaluate(_JS_NOVA_INSTANCIA, feitos)
            if inst:
                break
            await page.wait_for_timeout(200)
        if not inst:
            break
        await page.evaluate(_JS_SET_CKEDITOR_CORREIOS, [inst, _txt_para_html(textos.get(inst, ""))])
        feitos.append(inst)
        await page.wait_for_timeout(500)    # espera o conteúdo registrar/sincronizar antes da próxima
    # Salvar por REFERÊNCIA (clique real). O último editor às vezes exige 2º clique; repete até o
    # "Visualizar" APARECER — que é a CONFIRMAÇÃO de que o conteúdo foi salvo.
    salvou = False
    for _ in range(6):
        await _clicar_ref(page, "Salvar")
        await page.wait_for_timeout(1200)
        if await page.evaluate(_JS_VISUALIZAR_HABILITADO):
            salvou = True
            break
    log.info("[correios] Salvar → 'Visualizar' apareceu (salvou)=%s", salvou)
    return salvou


# Lê os bytes de uma URL (http OU blob:) DENTRO da página que a criou e devolve base64.
# Blob URLs são escopados à origem/documento que os gerou → só dá p/ ler via fetch no próprio
# contexto. Base64 em chunks p/ não estourar a pilha com Uint8Array grande.
_JS_FETCH_B64 = r"""async (u) => {
  const r = await fetch(u);
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = ''; const CH = 0x8000;
  for (let i = 0; i < buf.length; i += CH)
    s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
  return btoa(s);
}"""


async def _capturar_carta_pdf(page: Page, salvar_dir: str, capt: dict) -> dict:
    """No RESUMO: obtém o PDF do CONTEÚDO da carta (…/recuperar-pdf) e o baixa.
    Estratégia:
      1) se o listener do `expedir_carta` já capturou a RESPOSTA do recuperar-pdf, usa os bytes;
      2) senão, clica o ícone de OLHO (<a title="Visualizar">) — que abre o PDF como `blob:` numa
         POPUP — e lê os bytes do blob com `fetch` DENTRO da popup (base64), pois blob não dá p/
         baixar por request.get e o `context.on(response)` não vem sempre.
    Retorna {carta_pdf_nome, carta_pdf_caminho}."""
    out = {"carta_pdf_nome": "", "carta_pdf_caminho": ""}
    if not salvar_dir:
        return out
    ctx = page.context
    body, url = None, ""
    # (1) já capturado no Salvar?
    if capt.get("resp") is not None:
        with contextlib.suppress(Exception):
            url = capt["resp"].url or ""
            body = await capt["resp"].body()
    # (2) plano B — clica o olho, pega a popup do blob e lê os bytes dela.
    if not body:
        popup = None
        with contextlib.suppress(Exception):
            olho = page.locator('a[title="Visualizar"]').first
            await olho.wait_for(state="visible", timeout=6000)
            try:
                async with ctx.expect_page(timeout=8000) as pinfo:
                    await olho.click(timeout=4000)
                popup = await pinfo.value
            except PWTimeoutError:
                popup = None
        # se o olho tiver disparado o recuperar-pdf via listener, aproveita
        if capt.get("resp") is not None and not body:
            with contextlib.suppress(Exception):
                url = capt["resp"].url or ""
                body = await capt["resp"].body()
        if not body and popup is not None:
            with contextlib.suppress(Exception):
                await popup.wait_for_load_state("domcontentloaded", timeout=8000)
                blob_url = popup.url or ""
                b64 = None
                # fetch primeiro na PÁGINA que criou o blob (mais confiável que o visualizador
                # de PDF da popup, cujo contexto JS costuma bloquear fetch); popup como reserva.
                for tgt in (page, popup):
                    with contextlib.suppress(Exception):
                        b64 = await tgt.evaluate(_JS_FETCH_B64, blob_url)
                        if b64:
                            break
                if b64:
                    body = base64.b64decode(b64)
                    url = blob_url
    if body:
        with contextlib.suppress(Exception):
            Path(salvar_dir).mkdir(parents=True, exist_ok=True)
            m = re.search(r"mensagem/([A-Za-z0-9]+)", url)
            nome = f"carta_{m.group(1) if m else 'conteudo'}.pdf"
            caminho = Path(salvar_dir) / nome
            caminho.write_bytes(body)
            out["carta_pdf_nome"] = nome
            out["carta_pdf_caminho"] = str(caminho)
            log.info("[correios] PDF do conteúdo da carta salvo (Resumo): %s (%d bytes)", nome, len(body))
    else:
        log.warning("[correios] olho 'Visualizar' não retornou o PDF (recuperar-pdf/blob).")
    for p in list(ctx.pages):              # fecha a aba do blob (visualização do PDF)
        if p is not page:
            with contextlib.suppress(Exception):
                await p.close()
    with contextlib.suppress(Exception):   # remove o listener de resposta registrado no expedir_carta
        if capt.get("_handler"):
            ctx.remove_listener("response", capt["_handler"])
    return out


def _extrair_previsao(pdf_path: str) -> str:
    """Extrai a 'Previsão de Entrega' (dd/mm/aaaa) do COMPROVANTE DA POSTAGEM via pypdf.
    Retorna '' se não encontrar. Estratégia: 1) data logo após o rótulo 'Previsão de Entrega';
    2) senão, a MAIOR data dd/mm/aaaa do documento — a previsão é sempre posterior à data/hora
    de postagem, então é a data mais recente (robusto ao layout: no PDF a tabela concatena
    'Previsão de EntregaQt Páginas' e há dígitos de identificador/CEP entre o rótulo e a data,
    o que quebra o casamento rotulado — validado no comprovante lote 9220960)."""
    with contextlib.suppress(Exception):
        from pypdf import PdfReader
        txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(pdf_path).pages)
        txt = re.sub(r"\s+", " ", txt)
        m = re.search(r"Previs[ãa]o\s*de\s*Entrega\D{0,3}(\d{2}/\d{2}/\d{4})", txt, re.I)
        if m:
            return m.group(1)
        datas = re.findall(r"\d{2}/\d{2}/\d{4}", txt)
        if datas:
            # ordena por (ano, mês, dia) e devolve a mais recente = previsão de entrega
            return max(datas, key=lambda d: (d[6:10], d[3:5], d[0:2]))
    return ""


def _extrair_destinatario(pdf_path: str) -> str:
    """Nome do destinatário no COMPROVANTE DA POSTAGEM (após o identificador de 13 dígitos,
    antes do CEP). '' se não achar. Usado p/ casar um comprovante ao PAJ certo na recuperação."""
    with contextlib.suppress(Exception):
        from pypdf import PdfReader
        txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(pdf_path).pages)
        txt = re.sub(r"\s+", " ", txt)
        m = re.search(r"\d{13}\s+([A-ZÀ-Ú][A-ZÀ-Ú .'-]+?)\s+\d{2}\.?\d{3}-?\d{3}", txt)
        if m:
            return m.group(1).strip()
    return ""


async def _baixar_comprovante_do_lote(page: Page, lote: str, comprovante_dir: str) -> dict:
    """Marca o olho da linha do `lote`, abre o comprovante, baixa e extrai dest/previsão.
    Retorna {lote, dest, previsao, nome, caminho} (campos vazios se falhar)."""
    res = {"lote": lote, "dest": "", "previsao": "", "nome": "", "caminho": ""}
    ctx = page.context
    with contextlib.suppress(Exception):
        await page.evaluate(r"""(lote) => {
            document.querySelectorAll('[data-alvo-comprovante]').forEach(e => e.removeAttribute('data-alvo-comprovante'));
            const rows = [...document.querySelectorAll('tr')].filter(tr =>
                tr.querySelector('a[title="Abrir comprovante de postagem"]'));
            for (const tr of rows) {
                const cel = [...tr.querySelectorAll('td')].map(td => td.textContent.trim())
                    .find(t => /^\d{6,}$/.test(t));
                if (cel === lote) {
                    const o = tr.querySelector('a[title="Abrir comprovante de postagem"]');
                    if (o) o.setAttribute('data-alvo-comprovante', '1');
                    break;
                }
            }
        }""", lote)
    comp = None
    with contextlib.suppress(Exception):
        async with ctx.expect_page(timeout=6000) as pinfo:
            await page.locator('a[data-alvo-comprovante="1"]').first.click(timeout=3000)
        comp = await pinfo.value
    if comp is None:
        return res
    body, url = None, ""
    with contextlib.suppress(Exception):
        await comp.wait_for_load_state("domcontentloaded", timeout=8000)
        url = comp.url or ""
    if "gerar-comprovante" in url:
        with contextlib.suppress(Exception):
            r = await ctx.request.get(url)
            if r.ok:
                body = await r.body()
    if body is None:
        for tgt in (page, comp):
            with contextlib.suppress(Exception):
                b64 = await tgt.evaluate(_JS_FETCH_B64, url)
                if b64:
                    body = base64.b64decode(b64)
                    break
    with contextlib.suppress(Exception):
        await comp.close()
    if body and comprovante_dir:
        with contextlib.suppress(Exception):
            Path(comprovante_dir).mkdir(parents=True, exist_ok=True)
            nome = f"comprovante_correios_{lote}.pdf"
            caminho = Path(comprovante_dir) / nome
            caminho.write_bytes(body)
            res.update(nome=nome, caminho=str(caminho),
                       dest=_extrair_destinatario(str(caminho)),
                       previsao=_extrair_previsao(str(caminho)))
    return res


async def _recarregar_criar(page: Page) -> None:
    """Navega (RECARREGA de verdade, pelo menu) até a tela #/criar — 'Remessas enviadas no
    dia'. Usada quando esperar a lista SPA reagir sozinha (Angular) NÃO é confiável o
    bastante; um reload completo se mostrou mais robusto (ver _capturar_comprovante)."""
    await page.goto(SMT_HOME, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(600)
    with contextlib.suppress(Exception):
        await page.click("a#nav-menu", timeout=5000)
    await page.wait_for_timeout(300)
    await _click_texto(page, "MENSAGENS", tags="a")
    await page.wait_for_timeout(300)
    await _click_texto(page, "Criar Mensagens", tags="a")
    await page.wait_for_timeout(1200)
    with contextlib.suppress(Exception):
        await page.click("div.cartao", timeout=8000)
    await page.wait_for_timeout(1500)


async def baixar_comprovantes_dia(comprovante_dir: str) -> dict:
    """RECUPERAÇÃO: navega até 'Remessas enviadas no dia' e baixa o comprovante de CADA remessa,
    extraindo o destinatário — para recasar comprovantes trocados ao PAJ certo. Não posta nada.
    Retorna {ok, itens:[{lote,dest,previsao,nome,caminho}], detalhe}."""
    out: dict = {"ok": False, "itens": [], "detalhe": ""}
    try:
        await _ensure_logged_in()
        page = await _get_page()
        await _recarregar_criar(page)
        lotes = await _ler_lotes(page)
        log.info("[correios] recuperação: %d remessa(s) no dia: %s", len(lotes), lotes)
        for lote in lotes:
            item = await _baixar_comprovante_do_lote(page, lote, comprovante_dir)
            out["itens"].append(item)
            log.info("[correios] recuperação: lote %s → dest=%s previsão=%s",
                     lote, item.get("dest") or "?", item.get("previsao") or "?")
        out["ok"] = True
        out["detalhe"] = f"{len(out['itens'])} comprovante(s) baixado(s)."
    except Exception as e:  # noqa: BLE001
        out["detalhe"] = f"Falha na recuperação: {e}"
        log.warning("[correios] baixar_comprovantes_dia falhou: %s", e)
    return out


async def _capturar_comprovante(page: Page, comprovante_dir: str, capt: dict | None = None,
                                lotes_antes: list | None = None) -> dict:
    """Após efetivar: identifica a remessa RECÉM-POSTADA, baixa o PDF do COMPROVANTE DA POSTAGEM
    (…/gerar-comprovante) DELA e extrai a Previsão de Entrega. ⚠️ CORREÇÃO 2026-07-27: com vários
    envios no MESMO dia a lista 'Remessas enviadas no dia' tem várias linhas; pegar a `.first`
    capturava o comprovante ERRADO. Agora usa `lotes_antes` (snapshot dos lotes existentes ANTES de
    postar) para achar o **lote NOVO** (o que não estava lá) e clica o olho DAQUELA linha. Clique
    por REFERÊNCIA (o `.click()` JS não aciona o Angular). Retorna {lote, previsao_entrega,
    comprovante_nome, comprovante_caminho}."""
    res = {"lote": "", "previsao_entrega": "", "comprovante_nome": "", "comprovante_caminho": ""}
    ctx = page.context
    antes = set(str(x) for x in (lotes_antes or []))
    # 1) espera voltar a #/criar com a lista de "Remessas enviadas no dia" (growl de sucesso).
    for _ in range(20):
        if "criar" in (await _hash(page)):
            break
        await page.wait_for_timeout(500)
    # 2) POLL até surgir um lote NOVO (não estava no snapshot) = a remessa recém-postada.
    # ~5s (10×500ms): o reload logo abaixo se mostrou mais confiável que insistir esperando
    # o Angular reagir sozinho — não vale a pena esticar essa espera inicial.
    novo_lote = ""
    for _ in range(10):
        agora = await _ler_lotes(page)
        novos = [x for x in agora if x not in antes]
        if novos:
            novo_lote = max(novos, key=lambda s: int(s))    # o maior entre os novos
            break
        await page.wait_for_timeout(500)
    if not novo_lote:
        # ⚠️ CORREÇÃO 2026-08-20: o poll acima só espera o Angular reagir SOZINHO (sem
        # reload) — em pelo menos um caso real isso não bastou (a lista ficou "presa" sem
        # mostrar a remessa recém-postada mesmo após ~15s). Um RELOAD de verdade pelo menu
        # (mesma navegação usada em `baixar_comprovantes_dia`, que sempre achou de primeira
        # nesse mesmo cenário) é mais confiável do que insistir esperando no DOM atual.
        log.warning("[correios] comprovante: não detectei lote NOVO após poll — recarregando #/criar…")
        with contextlib.suppress(Exception):
            await _recarregar_criar(page)
            agora = await _ler_lotes(page)
            novos = [x for x in agora if x not in antes]
            if novos:
                novo_lote = max(novos, key=lambda s: int(s))
    if not novo_lote and not antes:
        # fallback SÓ quando não havia snapshot prévio (nada a excluir) — 1º envio do dia,
        # sem `lotes_antes`. ⚠️ NUNCA fazer isso quando `antes` não é vazio: com mais de uma
        # carta no MESMO dia, "maior lote da tela" pode ser um lote de OUTRA carta já
        # anexado a OUTRO PAJ — reanexar o PDF errado é pior do que não anexar nenhum
        # (era exatamente o bug de comprovante trocado corrigido em 2026-07-27).
        with contextlib.suppress(Exception):
            todos = await _ler_lotes(page)
            if todos:
                novo_lote = max(todos, key=lambda s: int(s))
        log.warning("[correios] comprovante: não detectei lote NOVO (nem após reload) — fallback lote=%s",
                   novo_lote or "—")
    elif not novo_lote:
        log.warning("[correios] comprovante: não detectei lote NOVO (nem após reload) — COM histórico prévio "
                   "(%d lote(s) conhecido(s)), NÃO uso fallback 'maior da tela' (risco de pegar comprovante "
                   "de OUTRA carta do dia). Comprovante ficará vazio — use a recuperação manual depois.", len(antes))
    res["lote"] = novo_lote
    # 3) marca o olho da linha do LOTE alvo (recém-postado) e clica-o por REFERÊNCIA.
    marcado = False
    with contextlib.suppress(Exception):
        marcado = await page.evaluate(r"""(lote) => {
            document.querySelectorAll('[data-alvo-comprovante]').forEach(e => e.removeAttribute('data-alvo-comprovante'));
            const rows = [...document.querySelectorAll('tr')].filter(tr =>
                tr.querySelector('a[title="Abrir comprovante de postagem"]'));
            let alvo = null;
            for (const tr of rows) {
                const cel = [...tr.querySelectorAll('td')].map(td => td.textContent.trim())
                    .find(t => /^\d{6,}$/.test(t));
                if (cel === lote) { alvo = tr; break; }
            }
            if (!alvo && rows.length) alvo = rows[0];          // fallback defensivo
            const olho = alvo && alvo.querySelector('a[title="Abrir comprovante de postagem"]');
            if (olho) { olho.setAttribute('data-alvo-comprovante', '1'); return true; }
            return false;
        }""", novo_lote)
    log.info("[correios] pós-efetivar: lote alvo=%s (linha marcada=%s)", novo_lote or "—", marcado)
    comp = None
    seletores = (['a[data-alvo-comprovante="1"]'] if marcado else []) + \
                ['a[title="Abrir comprovante de postagem"]', 'a[title*="omprovante"]', 'a.visualizar']
    for sel in seletores:
        loc = page.locator(sel).first
        with contextlib.suppress(Exception):
            if not await loc.is_visible():
                continue
            async with ctx.expect_page(timeout=6000) as pinfo:
                await loc.click(timeout=3000)
            comp = await pinfo.value
            log.info("[correios] comprovante: nova aba aberta pelo seletor %s", sel)
            break
    if comp is None:
        log.warning("[correios] comprovante: não abri nova aba (olho não localizado/clicado).")
        return res
    # 4) lê os bytes: http gerar-comprovante → request.get; caso contrário (blob) → fetch base64.
    with contextlib.suppress(Exception):
        await comp.wait_for_load_state("domcontentloaded", timeout=8000)
    url = comp.url or ""
    body = None
    if "gerar-comprovante" in url:
        with contextlib.suppress(Exception):
            r = await ctx.request.get(url)
            if r.ok:
                body = await r.body()
    if body is None:                       # blob: (ou request.get falhou) → plano B
        for tgt in (page, comp):
            with contextlib.suppress(Exception):
                b64 = await tgt.evaluate(_JS_FETCH_B64, url)
                if b64:
                    body = base64.b64decode(b64)
                    break
    if body and comprovante_dir:
        with contextlib.suppress(Exception):
            Path(comprovante_dir).mkdir(parents=True, exist_ok=True)
            nome = f"comprovante_correios_{res['lote'] or 'lote'}.pdf"
            caminho = Path(comprovante_dir) / nome
            caminho.write_bytes(body)
            res["comprovante_nome"] = nome
            res["comprovante_caminho"] = str(caminho)
            res["previsao_entrega"] = _extrair_previsao(str(caminho))
            log.info("[correios] comprovante da postagem salvo: %s (%d bytes) · previsão=%s",
                     nome, len(body), res["previsao_entrega"] or "não extraída")
    else:
        log.warning("[correios] comprovante: aba abriu (url=%s) mas não obtive os bytes.", url[:90])
    with contextlib.suppress(Exception):
        await comp.close()
    return res


async def expedir_carta(destinatario: dict, cabecalho: str, corpo: str, rodape: str,
                        efetivar: bool = False, comprovante_dir: str = "", emit=None) -> dict:
    """Expede uma CARTA SIMPLES pelo SMT. `destinatario` = {nome, cep, numero,
    complemento?, cpf?}. Com `efetivar=False` para no Resumo (não posta). `emit`, se dado,
    é chamado com um dict {level,msg} a CADA etapa do site (para o log ao vivo do painel).
    Retorna {ok, status, lote, previsao_entrega, comprovante_nome, comprovante_caminho, detalhe}.
    status: 'resumo' (dry, chegou ao resumo) | 'efetivado' | 'falha'."""
    def _ev(msg: str, level: str = "info"):
        if emit is not None:
            with contextlib.suppress(Exception):
                emit({"level": level, "msg": "     " + msg})
    out = {"ok": False, "status": "falha", "lote": "", "previsao_entrega": "",
           "comprovante_nome": "", "comprovante_caminho": "",
           "carta_pdf_nome": "", "carta_pdf_caminho": "", "detalhe": ""}
    try:
        _ev("Conectando ao Correios (login)…")
        await _ensure_logged_in()
        page = await _get_page()
        _ev("Sessão do Correios ativa. Abrindo o assistente de e-Carta…", "ok")
        # Escuta a resposta do recuperar-pdf durante TODO o fluxo: o app busca esse PDF já no
        # SALVAR da mensagem (não no clique do olho). Captura os bytes crus assim que aparecer.
        capt: dict = {"resp": None}

        def _grab_pdf(resp):
            try:
                if capt["resp"] is None and "recuperar-pdf" in (resp.url or ""):
                    capt["resp"] = resp
            except Exception:  # noqa: BLE001
                pass

        capt["_handler"] = _grab_pdf
        with contextlib.suppress(Exception):
            page.context.on("response", _grab_pdf)
        lotes_antes: list = []                 # snapshot dos lotes já enviados hoje (p/ achar o novo)
        await _abrir_assistente(page, lotes_antes)
        # Passo 1 Serviços Adicionais: garante "Carta Simples" (já é o padrão) → Próximo
        await _click_texto(page, "Carta Simples", tags="div", timeout_ms=3000)
        await _clicar_ref(page, "Próximo")
        await page.wait_for_timeout(700)
        # Passo 2 Remetente (DPU fixo) → Próximo
        await _clicar_ref(page, "Próximo")
        await page.wait_for_timeout(700)
        # Passo 3 Destinatário
        _ev(f"Preenchendo o destinatário: {destinatario.get('nome', '')} · CEP {destinatario.get('cep', '')}…")
        await _preencher_destinatario(page, destinatario)
        await _clicar_ref(page, "Próximo")
        await page.wait_for_timeout(800)
        # Passo 4 Mensagem — preenche e SALVA (o "Visualizar" aparecendo = confirmação). NÃO clica
        # em Visualizar: o PDF do conteúdo é capturado no Resumo (ícone de olho).
        _ev("Escrevendo a carta (cabeçalho, corpo e rodapé)…")
        salvou = await _preencher_mensagem(page, cabecalho, corpo, rodape)
        if not salvou:
            log.warning("[correios] mensagem NÃO confirmou salvamento ('Visualizar' não apareceu).")
        _ev("Carta redigida e salva." if salvou else "Carta redigida (salvamento não confirmado).",
            "ok" if salvou else "aviso")
        await _clicar_ref(page, "Próximo")
        await page.wait_for_timeout(1200)
        # Passo 5 Resumo — captura o PDF do CONTEÚDO da carta pelo ícone de olho (coluna Ação).
        diag = await _diagnostico(page)
        log.info("[correios] estado no Passo 5 (Resumo): %s", diag)
        _ev("Conferindo o Resumo e salvando o PDF da carta…")
        carta = await _capturar_carta_pdf(page, comprovante_dir, capt)
        out["carta_pdf_nome"] = carta.get("carta_pdf_nome", "")
        out["carta_pdf_caminho"] = carta.get("carta_pdf_caminho", "")
        _ev("PDF da carta salvo." if carta.get("carta_pdf_nome") else "PDF da carta não capturado.",
            "ok" if carta.get("carta_pdf_nome") else "aviso")
        if not efetivar:
            _ev("Modo simulado — parando no Resumo (não posta, não cobra).", "aviso")
            out.update(ok=True, status="resumo",
                       detalhe="Chegou ao Resumo (modo simulado — não efetivado).")
            return out
        _ev("Efetivando a postagem nos Correios (pode levar alguns segundos)…")
        if not await _clicar_ref(page, "Efetivar"):
            log.warning("[correios] Efetivar não encontrado. Diagnóstico: %s", diag)
            _ev("Botão Efetivar não encontrado no Resumo.", "erro")
            out["detalhe"] = "Botão Efetivar não encontrado no Resumo. Diagnóstico: " + diag
            return out
        # aguarda o sucesso ("Remessa enviada com sucesso" / volta a #/criar)
        await page.wait_for_timeout(2500)
        _ev("Postagem efetivada. Baixando o comprovante da postagem…", "ok")
        cap = await _capturar_comprovante(page, comprovante_dir, lotes_antes=lotes_antes)
        _ev(f"Comprovante da postagem salvo · Lote {cap.get('lote') or '—'} · "
            f"previsão de entrega {cap.get('previsao_entrega') or 'não informada'}.",
            "ok" if cap.get("comprovante_nome") else "aviso")
        out.update(ok=True, status="efetivado", lote=cap["lote"],
                   previsao_entrega=cap.get("previsao_entrega", ""),
                   comprovante_nome=cap["comprovante_nome"],
                   comprovante_caminho=cap["comprovante_caminho"],
                   detalhe="Carta expedida.")
        return out
    except CorreiosCepNaoIdentificado as e:
        # CEP não reconhecido pelo site → aborta a expedição SEM postar, com status próprio
        # para a tela exibir o badge de falha específico.
        _ev(str(e), "erro")
        out["status"] = "cep_nao_identificado"
        out["detalhe"] = str(e)
        return out
    except (CorreiosSemCredenciais, CorreiosCredenciaisInvalidas) as e:
        out["detalhe"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["detalhe"] = f"Falha na expedição: {e}"
        log.warning("[correios] expedir_carta falhou: %s", e)
        return out
