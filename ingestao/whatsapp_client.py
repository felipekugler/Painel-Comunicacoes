"""Cliente WhatsApp Web via Playwright (perfil persistente dedicado).

Meio de contato ASSISTIDO: o painel prepara a conversa (número + texto + anexo)
e o SERVIDOR dá o clique final em enviar; depois capturamos o print do envio.

IMPORTANTE sobre HEADED x HEADLESS:
  - O LOGIN (QR) e o uso em PRODUÇÃO precisam de janela VISÍVEL (headed) — rodam na
    máquina do servidor, pelo painel/atalho. NÃO funcionam no ambiente headless do
    dev (lá a janela não tem rede/tela).
  - Depois de logado uma vez (QR), a sessão fica no user_data_dir e pode ser reaberta
    (inclusive headless para desenvolvimento/captura de seletores).

Sessão isolada em `<PAINEL DIVCOM>/dados/_whatsapp_profile` — separada do SISDPU.
Só UM processo pode usar o perfil por vez (Chromium tranca o user_data_dir).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# <Projeto>/ingestao/whatsapp_client.py -> profile em <PAINEL DIVCOM>/dados/_whatsapp_profile
_PROJ = Path(__file__).resolve().parents[1]
WA_PROFILE_DIR = _PROJ.parent / "dados" / "_whatsapp_profile"

WA_URL = "https://web.whatsapp.com"

# Usar o CHROME INSTALADO (channel), NÃO o Chromium embutido do Playwright: nesta
# máquina/rede (DPU) o Chromium avulso em headed cai em ERR_NETWORK_ACCESS_DENIED,
# enquanto o Chrome/Edge instalado tem rede normal (confirmado por diagnóstico).
WA_CHANNEL = "chrome"


async def abrir_contexto(p, headless: bool = True):
    """Abre o contexto persistente do WhatsApp (perfil dedicado + Chrome instalado).
    headless=False p/ o QR e produção (na máquina do servidor); headless=True p/ o
    assistente reabrir a sessão já logada e desenvolver/testar."""
    WA_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        str(WA_PROFILE_DIR),
        channel=WA_CHANNEL,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1200, "height": 840},
    )

# Detecta a interface logada (lista de conversas). WhatsApp muda o DOM com
# frequência — mantemos vários sinais.
_JS_LOGADO = r"""
() => !!(document.querySelector('#pane-side')
      || document.querySelector('[aria-label="Lista de conversas"]')
      || document.querySelector('[aria-label="Chat list"]')
      || document.querySelector('header [data-icon="new-chat-outline"]'))
"""


def normalizar_numero(raw: str) -> str:
    """Número BR -> dígitos internacionais para o wa/whatsapp send.

    Regra (herdada do painel): decide pelo COMPRIMENTO. 10/11 dígitos (DDD+número)
    -> prefixa '55'. NÃO checar prefixo '55' cegamente (DDD 55 = Santa Maria/RS
    existe). Se já vier com 12/13 dígitos, assume que já tem o país.
    """
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return ""
    if len(d) in (10, 11):          # DDD (2) + número (8/9)
        return "55" + d
    if len(d) in (12, 13) and d.startswith("55"):
        return d
    return d                         # deixa como veio (casos atípicos)


async def esta_logado(page) -> bool:
    try:
        return bool(await page.evaluate(_JS_LOGADO))
    except Exception:
        return False


async def login_interativo(timeout_seg: int = 600) -> bool:
    """Abre o WhatsApp Web HEADED e espera o usuário escanear o QR.
    RODAR NA MÁQUINA DO SERVIDOR (headed precisa de tela+rede reais).
    Ao detectar login, a sessão já está persistida no perfil; fecha e retorna True."""
    print(f"[wa] perfil: {WA_PROFILE_DIR} (Chrome instalado, channel={WA_CHANNEL})", flush=True)
    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
        print(">>> Escaneie o QR com o telefone do NÚMERO DEDICADO "
              "(WhatsApp > Aparelhos conectados > Conectar aparelho). <<<", flush=True)
        logado = False
        for i in range(max(1, timeout_seg // 2)):
            await page.wait_for_timeout(2000)
            if await esta_logado(page):
                logado = True
                break
            if i % 5 == 0:
                print(f"[wa] aguardando login... ({i * 2}s)", flush=True)
        if logado:
            print("[wa] LOGADO! Sessão salva no perfil (pode fechar e reabrir depois).", flush=True)
            await page.wait_for_timeout(5000)  # garante gravação do IndexedDB da sessão
        else:
            print("[wa] NÃO detectei login no tempo limite. Rode novamente para tentar.", flush=True)
        await ctx.close()
        return logado


# ---------------- Seletores do WhatsApp Web (capturados ao vivo) ----------------
_SEL_COMPOSE = 'footer div[contenteditable="true"][role="textbox"]'
_SEL_SEND_FOOTER = 'footer [aria-label="Enviar"]'          # aparece após digitar texto
_SEL_ATTACH = 'footer [aria-label="Anexar"]'               # botão "+" (data-icon plus-rounded)
_SEL_CAPTION = 'div[contenteditable="true"][aria-label="Digite uma mensagem"]'  # legenda no preview
_SEL_SEND_PREVIEW = '[aria-label^="Enviar"]'               # enviar na tela de preview do anexo


async def _numero_invalido(page) -> bool:
    try:
        return bool(await page.evaluate(
            r"""() => [...document.querySelectorAll('[role="dialog"]')]
                    .some(d => /n[ãa]o est[áa] no WhatsApp/i.test(d.textContent || ''))"""))
    except Exception:
        return False


async def _confirmar_enviado(page, texto: str = "", timeout_seg: int = 20) -> bool:
    """Confirma que a mensagem SAIU. O DOM do WhatsApp é ofuscado (classes atômicas
    da Meta); em vez de classes instáveis, checamos:
      - se há texto: ele aparece em #main (área de mensagens) e NÃO no compose (rodapé);
      - sem texto (anexo sem legenda): presença de bolha de saída (`data-icon=tail-out`).
    """
    snip = re.sub(r"\s+", " ", texto or "").strip()[:30]
    js_texto = r"""(snip) => {
        const main = document.querySelector('#main'); if (!main) return false;
        const foot = main.querySelector('footer');
        const footTxt = foot ? (foot.innerText || '') : '';
        const mainTxt = main.innerText || '';
        return snip.length > 0 && mainTxt.includes(snip) && !footTxt.includes(snip);
    }"""
    js_tail = r"""() => !!document.querySelector('#main [data-icon="tail-out"]')"""
    for _ in range(max(1, timeout_seg * 2)):
        await page.wait_for_timeout(500)
        try:
            if snip:
                if await page.evaluate(js_texto, snip):
                    return True
            elif await page.evaluate(js_tail):
                return True
        except Exception:
            pass
    return False


# ---------------- Comprovante (PDF de 2 páginas, sem libs externas) ----------------
def _jpeg_dims(data: bytes) -> tuple[int, int]:
    """Largura x altura de um JPEG (marcador SOF)."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            return ((data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6])
        if 0xD0 <= m <= 0xD9 or m == 0x01:
            i += 2
            continue
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return (1000, 1400)


def _jpegs_to_pdf(jpegs: list[bytes], path: str) -> None:
    """Monta um PDF (1 página por JPEG) embutindo via /DCTDecode. Sem dependências."""
    objs: dict[int, bytes] = {}
    page_ids: list[int] = []
    nid = 3
    for data in jpegs:
        w, h = _jpeg_dims(data)
        pid, iid, cid = nid, nid + 1, nid + 2
        nid += 3
        page_ids.append(pid)
        content = f"q\n{w} 0 0 {h} 0 0 cm\n/Im0 Do\nQ".encode()
        objs[pid] = (f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
                     f"/Resources << /XObject << /Im0 {iid} 0 R >> >> /Contents {cid} 0 R >>\nendobj\n").encode()
        objs[iid] = (f"{iid} 0 obj\n<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                     f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\n"
                     .encode() + b"stream\n" + data + b"\nendstream\nendobj\n")
        objs[cid] = (f"{cid} 0 obj\n<< /Length {len(content)} >>\n".encode()
                     + b"stream\n" + content + b"\nendstream\nendobj\n")
    objs[1] = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[2] = f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n".encode()
    pdf, offs = bytearray(b"%PDF-1.4\n"), {}
    for i in range(1, nid):
        offs[i] = len(pdf)
        pdf.extend(objs[i])
    xref = len(pdf)
    pdf.extend(f"xref\n0 {nid}\n0000000000 65535 f \n".encode())
    for i in range(1, nid):
        pdf.extend(f"{offs[i]:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {nid} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    Path(path).write_bytes(pdf)


_JS_DRAWER_CONTATO = r"""
() => {
    const vw = innerWidth; let best = null, area = 0;
    for (const el of document.querySelectorAll('div,section')) {
        const r = el.getBoundingClientRect();
        if (r.left > vw * 0.35 && r.width > 300 && r.height > 500 &&
            /Dados do contato|Dados da empresa/i.test(el.textContent || '')) {
            const a = r.width * r.height;
            if (a > area) { area = a; best = el; }
        }
    }
    return best;
}
"""


async def _capturar_comprovante(page, comprovante_dir: str, paj_ref: str = "") -> str | None:
    """Gera o comprovante PDF de 2 páginas: (1) Dados do contato (foto+nome+número
    do destinatário — SÓ o painel, sem barra lateral) e (2) a conversa (#main).
    Nome: Envio_Whats_Painel_[PAJ_]AAAA-MM-DD_HH-MM-SS.pdf. Retorna o caminho ou None."""
    try:
        conv = await page.locator("#main").first.screenshot(type="jpeg", quality=85)
        contato = None
        with contextlib.suppress(Exception):
            await page.locator("#main header").first.click()
            await page.wait_for_function(
                r"""() => /Dados do contato|Dados da empresa/i.test(document.body.innerText||'')""",
                timeout=6000)
            await page.wait_for_timeout(1200)
            h = await page.evaluate_handle(_JS_DRAWER_CONTATO)
            el = h.as_element()
            if el:
                contato = await el.screenshot(type="jpeg", quality=85)
            await page.keyboard.press("Escape")  # fecha o painel
        jpegs = ([bytes(contato)] if contato else []) + [bytes(conv)]
        ref = re.sub(r"[^\w.-]", "_", paj_ref or "").strip("_")
        ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        nome = f"Envio_Whats_Painel_{ref + '_' if ref else ''}{ts}.pdf"
        Path(comprovante_dir).mkdir(parents=True, exist_ok=True)
        destino = str(Path(comprovante_dir) / nome)
        _jpegs_to_pdf(jpegs, destino)
        return destino
    except Exception as e:  # noqa: BLE001
        _log.warning("falha ao gerar comprovante: %r", e)
        return None


async def enviar_mensagem(
    numero: str,
    mensagem: str,
    anexos: list[dict] | None = None,
    headless: bool = True,
    comprovante_dir: str | None = None,
    paj_ref: str = "",
    mensagem_prazo: str | None = None,
) -> dict:
    """Envia uma mensagem de WhatsApp pelo perfil dedicado, em SEQUÊNCIA: primeiro o
    TEXTO (mensagem própria), depois os ANEXOS (lote único via "Documento", SEM
    legenda) e, por fim, se `mensagem_prazo` for dado, uma 3ª mensagem informando o
    prazo/urgência. Modelo escolhido: 1 clique no painel → o Playwright envia tudo.
    Se `comprovante_dir` for dado, gera o comprovante PDF de 2 páginas (dados do
    contato + conversa) nomeado com o PAJ (`paj_ref`) e o retorna em `comprovante`.
    Retorna {ok, status, detalhe, comprovante}. status: enviado|parcial|falha.
    """
    import mimetypes
    from urllib.parse import quote

    anexos = anexos or []
    intl = normalizar_numero(numero)
    if not intl:
        return {"ok": False, "status": "falha", "detalhe": "Número vazio/inválido."}
    texto = (mensagem or "").strip()
    msg_prazo = (mensagem_prazo or "").strip()
    validos = [a for a in anexos if a.get("caminho") and Path(a["caminho"]).exists()]
    ausentes = [a for a in anexos if not (a.get("caminho") and Path(a["caminho"]).exists())]
    if not texto and not validos and not msg_prazo:
        return {"ok": False, "status": "falha",
                "detalhe": "Nada a enviar (sem texto e sem anexo válido)."}

    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            url = f"{WA_URL}/send?phone={intl}" + (f"&text={quote(texto)}" if texto else "")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # aguarda: chat abrir (compose) OU diálogo de número inválido
            aberto = False
            for _ in range(30):
                await page.wait_for_timeout(1000)
                if await _numero_invalido(page):
                    return {"ok": False, "status": "falha",
                            "detalhe": f"Número {intl} não está no WhatsApp."}
                if await page.locator(_SEL_COMPOSE).count():
                    aberto = True
                    break
            if not aberto:
                return {"ok": False, "status": "falha", "detalhe": "Conversa não abriu (timeout)."}

            etapas: list[tuple[str, bool]] = []

            # 1) TEXTO primeiro (mensagem própria)
            if texto:
                await page.locator(_SEL_SEND_FOOTER).first.click()
                ok_txt = await _confirmar_enviado(page, texto, timeout_seg=25)
                etapas.append(("texto", ok_txt))
                await page.wait_for_timeout(600)

            # 2) ANEXOS depois (um único lote via "Documento", sem legenda)
            if validos:
                payloads = []
                for a in validos:
                    nome = a.get("nome") or Path(a["caminho"]).name
                    data = Path(a["caminho"]).read_bytes()
                    mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
                    payloads.append({"name": nome, "mimeType": mime, "buffer": data})
                await page.locator(_SEL_ATTACH).first.click()
                await page.wait_for_timeout(700)
                async with page.expect_file_chooser() as fc:
                    await page.get_by_text("Documento", exact=True).click()
                chooser = await fc.value
                await chooser.set_files(payloads)
                await page.wait_for_timeout(2500)
                await page.locator(_SEL_SEND_PREVIEW).first.click()
                # confirma pelo NOME do 1º arquivo (aparece na bolha do documento)
                ok_anx = await _confirmar_enviado(page, payloads[0]["name"], timeout_seg=30)
                etapas.append((f"{len(payloads)} anexo(s)", ok_anx))

            # 3) MENSAGEM DE PRAZO (condicional) — digitada no chat já aberto e enviada por último
            if msg_prazo:
                compose = page.locator(_SEL_COMPOSE).first
                await compose.click()
                await page.wait_for_timeout(300)
                await page.keyboard.type(msg_prazo)
                await page.wait_for_timeout(400)
                # envia clicando no botão "Enviar" (mesmo padrão comprovado do passo 1,
                # que aparece no rodapé assim que há texto no compose); Enter é fallback
                botao_prz = page.locator(_SEL_SEND_FOOTER)
                if await botao_prz.count():
                    await botao_prz.first.click()
                else:
                    await page.keyboard.press("Enter")
                ok_prz = await _confirmar_enviado(page, msg_prazo, timeout_seg=20)
                etapas.append(("prazo", ok_prz))
                await page.wait_for_timeout(600)

            todos_ok = bool(etapas) and all(ok for _, ok in etapas)
            algum_ok = any(ok for _, ok in etapas)
            comprovante = None
            if comprovante_dir and algum_ok:
                await page.wait_for_timeout(800)
                comprovante = await _capturar_comprovante(page, comprovante_dir, paj_ref)
            desc = ", ".join(f"{n}{'' if ok else ' (não confirmado)'}" for n, ok in etapas)
            nota_aus = f" · {len(ausentes)} anexo(s) ausente(s)" if ausentes else ""
            return {"ok": todos_ok,
                    "status": "enviado" if todos_ok else ("parcial" if algum_ok else "falha"),
                    "detalhe": f"WhatsApp p/ {intl}: {desc}{nota_aus}",
                    "comprovante": comprovante}
        finally:
            await ctx.close()


# ------------------------------------------------------------------
# TODO próximas etapas:
#   - modo ASSISTIDO em produção (headed, na máquina do servidor) integrado ao painel.
#   - 2ª automação SISDPU: registrar movimentação + anexar o print (tela a MAPEAR).
# ------------------------------------------------------------------


async def diagnostico() -> None:
    """Testa, em HEADED, qual motor consegue acessar a rede nesta máquina:
    Chromium embutido x Chrome instalado x Edge instalado (com/sem flags de sandbox).
    Rodar NA MÁQUINA DO SERVIDOR."""
    url = "https://example.com"
    flags = ["--no-sandbox", "--disable-features=NetworkServiceSandbox"]
    combos = [
        ("chromium embutido | sem flags", {}, []),
        ("chromium embutido | com flags", {}, flags),
        ("channel=chrome | sem flags", {"channel": "chrome"}, []),
        ("channel=chrome | com flags", {"channel": "chrome"}, flags),
        ("channel=msedge | sem flags", {"channel": "msedge"}, []),
        ("channel=msedge | com flags", {"channel": "msedge"}, flags),
    ]
    async with async_playwright() as p:
        for nome, extra, args in combos:
            try:
                br = await p.chromium.launch(headless=False, args=args, **extra)
                page = await br.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    print(f"OK    | {nome}", flush=True)
                finally:
                    await br.close()
            except Exception as e:
                print(f"FALHA | {nome} -> {str(e).splitlines()[0][:90]}", flush=True)


def _main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "login":
        ok = asyncio.run(login_interativo())
        sys.exit(0 if ok else 1)
    if cmd == "diag":
        asyncio.run(diagnostico())
        sys.exit(0)
    print(f"comando desconhecido: {cmd} (use: login | diag)", flush=True)
    sys.exit(2)


if __name__ == "__main__":
    _main()
