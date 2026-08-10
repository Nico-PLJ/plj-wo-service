# -*- coding: utf-8 -*-
"""
Serviço PLJ — Gerador de Work Order (padrão PLJ, com fotos traduzidas em PT).

Fluxo:
  POST /gerar {"num": "4807"}  + header Authorization: Bearer <token do usuário Supabase>
    -> confere se o usuário é admin (tabela app_users no Supabase)
    -> acha a estimate no JobNimbus pelo número
    -> baixa o PDF do SumoQuote (attachment_id)
    -> pdfimages extrai as fotos (filtra as pequenas: logo/ícone)
    -> Claude lê o PDF + as fotos numeradas e devolve JSON estruturado em PT
    -> reportlab monta o PDF no padrão PLJ
    -> sobe no Storage do Supabase (bucket workorders) e devolve a URL pública
"""
import os
import io
import json
import glob
import base64
import tempfile
import subprocess

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image as PILImage

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                Table, TableStyle, KeepTogether, HRFlowable,
                                PageBreak)

# ---------------------------------------------------------------- config
JOBNIMBUS_KEY = os.environ.get("JOBNIMBUS_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
BUCKET = os.environ.get("WO_BUCKET", "workorders")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

JN_BASE = "https://app.jobnimbus.com/api1"
MAX_FOTOS = 24          # teto de fotos mandadas pro Claude
FOTO_MAX_PX = 1400      # redimensiona antes de mandar pra API

app = FastAPI(title="PLJ Work Order generator")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class Req(BaseModel):
    num: str


# ---------------------------------------------------------------- estilos
YEL = colors.HexColor("#F2C200")
DARK = colors.HexColor("#1F1F1F")
GREY = colors.HexColor("#555555")

S_TITULO = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=16,
                          leading=19, textColor=DARK, alignment=TA_CENTER,
                          spaceAfter=2)
S_SUB = ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=12,
                       textColor=GREY, alignment=TA_CENTER, spaceAfter=6)
S_SECAO = ParagraphStyle("secao", fontName="Helvetica-Bold", fontSize=12,
                         leading=15, textColor=DARK, spaceBefore=10,
                         spaceAfter=4)
S_NOTA = ParagraphStyle("nota", fontName="Helvetica-Oblique", fontSize=9.5,
                        leading=12.5, textColor=GREY, spaceAfter=6)
S_ETAPA = ParagraphStyle("etapa", fontName="Helvetica", fontSize=10,
                         leading=13.5, spaceAfter=5)
S_BULLET = ParagraphStyle("bullet", fontName="Helvetica", fontSize=10,
                          leading=13.5, leftIndent=12, bulletIndent=2,
                          spaceAfter=3)
S_FOTOT = ParagraphStyle("fotot", fontName="Helvetica-Bold", fontSize=10.5,
                         leading=13, textColor=DARK, spaceBefore=6,
                         spaceAfter=2)
S_FOTOD = ParagraphStyle("fotod", fontName="Helvetica", fontSize=9.5,
                         leading=12.5, spaceAfter=10)
S_CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=9.5, leading=12)
S_CELLB = ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=9.5,
                         leading=12)


def _esc(t):
    return (str(t or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------- jobnimbus
def jn_headers():
    return {"Authorization": "Bearer " + JOBNIMBUS_KEY,
            "Accept": "application/json"}


def find_estimate(num):
    flt = json.dumps({"must": [{"term": {"number": str(num)}}]})
    r = requests.get(f"{JN_BASE}/estimates", headers=jn_headers(),
                     params={"filter": flt}, timeout=60)
    if not r.ok:
        raise HTTPException(502, f"JobNimbus falhou ({r.status_code}).")
    results = (r.json() or {}).get("results") or []
    if not results:
        raise HTTPException(404, f"Estimate {num} não encontrada no JobNimbus.")
    return results[0]


def jn_contato(est):
    """Busca o contato ligado à estimate — é onde mora o endereço bom."""
    c = next((r for r in (est.get("related") or [])
              if r.get("type") == "contact"), None)
    if not c or not c.get("id"):
        return {}
    try:
        r = requests.get(f"{JN_BASE}/contacts/{c['id']}",
                         headers=jn_headers(), timeout=60)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def dados_cliente(est):
    """Nome e endereço em partes, com a estimate como reserva."""
    ct = jn_contato(est)
    rel = next((r for r in (est.get("related") or [])
                if r.get("type") == "contact"), None)
    nome = (ct.get("display_name") or ct.get("name")
            or (rel or {}).get("name") or "").strip()
    def pega(k):
        return str(ct.get(k) or est.get(k) or "").strip()
    rua = " ".join(x for x in [pega("address_line1"),
                               pega("address_line2")] if x).strip()
    cid = pega("city")
    est_uf = pega("state_text")
    cep = pega("zip")
    completo = ", ".join(x for x in
                         [rua, cid, " ".join(y for y in [est_uf, cep] if y)]
                         if x)
    return {"cliente": nome, "rua": rua, "cidade": cid,
            "estado": est_uf, "cep": cep, "endereco": completo}


def jn_link(est):
    """Monta o endereço da estimate no site do JobNimbus.

    O site localiza a estimate pelo jnid; o trecho do job é ignorado,
    então serve qualquer id relacionado (usamos o do job quando existe).
    """
    est_id = str(est.get("jnid") or est.get("guid") or "").strip().lower()
    if not est_id:
        return ""
    rel = est.get("related") or []
    job = next((r for r in rel if r.get("type") == "job"), None)
    jobid = str((job or {}).get("id") or "").strip()
    if not jobid:
        jobid = str((rel[0] if rel else {}).get("id") or "").strip() or "0"
    return ("https://app.jobnimbus.com/job/" + jobid +
            "/estimates/" + est_id + "/view")


def download_pdf(attachment_id):
    r = requests.get(f"{JN_BASE}/files/{attachment_id}",
                     headers=jn_headers(), timeout=180)
    if not r.ok or not r.content[:4] == b"%PDF":
        raise HTTPException(502, "Não consegui baixar o PDF da estimate.")
    return r.content


# ---------------------------------------------------------------- claude
PROMPT = """Você escreve a ORDEM DE SERVIÇO da PLJ Carpentry (Cape Cod, MA) para a
equipe de campo ler no celular, em pé no canteiro. Recebe o PDF de uma estimate do
SumoQuote (em inglês) e as fotos extraídas dele, numeradas.

COMO ESCREVER — isto é o mais importante:
- PORTUGUÊS SIMPLES, do dia a dia da obra. Nada de linguagem formal ou técnica.
- Frases CURTAS: no máximo 12 palavras. Uma ação por frase.
- Verbo direto: "Trocar", "Instalar", "Tirar", "Vedar". Nunca "proceder à instalação",
  "efetuar a remoção", "realizar a substituição".
- Palavra simples sempre que der: "tirar" e não "remover"; "colocar" e não "posicionar";
  "medir" e não "aferir".
- CORTE O ÓBVIO. Não escreva o que qualquer carpinteiro já sabe (medir antes de cortar,
  usar EPI, limpar no fim, trabalhar com cuidado, proteger o piso). Só entra o que é
  específico DESTE job.
- Sem enrolação, sem introdução, sem repetir a mesma informação em dois lugares.
- Se uma etapa não muda nada na prática, não escreva ela.
- Nunca inclua preços, valores ou totais.

REGRAS DE CONTEÚDO:
- Escreva tudo em português (só nomes próprios, marcas e códigos ficam como estão).
- Nas fotos, DIGA A COR DA MARCAÇÃO quando existir ("o que está marcado em vermelho").
- Título da etapa: 1 a 3 palavras (o local ou a peça). Ex.: "PORTA DA FRENTE".
- Descrição da etapa: 1 frase curta.
- Descrição da foto: 1 frase curta dizendo o que fazer ali.
- "atencao": no MÁXIMO 4 itens, só o que causa retrabalho, erro caro ou atraso
  (divergência de marca/medida, esperar permit, material que ainda não chegou).
  Se não tiver nada de verdade, devolva lista vazia.

O CABEÇALHO — só estes campos, nada mais:
- "endereco": endereço do imóvel, copiado do PDF. Se não achar, devolva "".
- "vendedor": nome do vendedor/sales rep, copiado do PDF. Se não achar, devolva "".
- "servicos": lista curta com os TIPOS de serviço, 2 a 5 palavras cada.
  Ex.: ["Troca de 6 janelas", "Trim de PVC"]. Só o tipo, sem detalhe nem medida.
- NÃO INVENTE NADA. Se a informação não está no PDF, devolva "" ou lista vazia.
  Não deduza, não estime, não complete com o que "provavelmente" é.
- NÃO coloque no cabeçalho: forma de pagamento, valores, datas, número do contrato,
  especificação técnica completa, nem repetir o nome do cliente.
- DESCARTE (manter=false) fotos que sejam logo, capa, certificado de seguro,
  certificação de fabricante, tabela de preços, assinatura ou página só de texto.
  Fique só com fotos reais do imóvel.

EXEMPLOS DO TOM CERTO:
  RUIM: "Proceder à remoção do trim de madeira existente ao redor do vão da porta
         frontal e efetuar a instalação de novo trim em PVC."
  BOM:  "Tirar o trim de madeira da porta e colocar trim de PVC."
  RUIM: "Verificar cuidadosamente as medidas antes de realizar os cortes."
  BOM:  (não escrever — é óbvio)

Responda SOMENTE com um objeto JSON válido, sem markdown, sem crases, no formato:
{
  "titulo": "TRIM E JANELAS",
  "endereco": "19 Janet Street, Plymouth, MA 02360",
  "vendedor": "Rob Silva",
  "servicos": ["Troca de 6 janelas", "Trim de PVC"],
  "partes": [
    {"titulo": "PARTE A — TRIM DE PVC",
     "nota": "Trocar só o que está marcado em vermelho.",
     "etapas": [{"titulo": "PORTA DA FRENTE",
                 "descricao": "Tirar o trim de madeira e colocar trim de PVC."}]}
  ],
  "atencao": ["Não começar antes do permit sair."],
  "fotos": [{"foto": 1, "manter": true, "titulo": "Frente da casa",
             "descricao": "Trocar as 2 janelas marcadas em amarelo."}]
}
O campo "foto" é o número da foto conforme enviada. Inclua TODAS as fotos na lista,
com manter=true ou manter=false."""


def _img_block(path):
    im = PILImage.open(path).convert("RGB")
    if max(im.size) > FOTO_MAX_PX:
        r = FOTO_MAX_PX / float(max(im.size))
        im = im.resize((int(im.size[0] * r), int(im.size[1] * r)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(buf.getvalue()).decode()}}


def claude_analyze(pdf_bytes, photos, cliente, endereco, total):
    content = [{
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf",
                   "data": base64.b64encode(pdf_bytes).decode()}
    }]
    for i, p in enumerate(photos[:MAX_FOTOS], 1):
        content.append({"type": "text", "text": f"FOTO {i}:"})
        content.append(_img_block(p))
    content.append({"type": "text", "text":
                    f"Cliente: {cliente}\nEndereço: {endereco}\n\n{PROMPT}"})

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 8000,
              "messages": [{"role": "user", "content": content}]},
        timeout=600)
    if not r.ok:
        raise HTTPException(502, f"Claude falhou ({r.status_code}): {r.text[:300]}")
    txt = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
    a = txt.find("{")
    b = txt.rfind("}")
    if a == -1 or b == -1:
        raise HTTPException(502, "Claude não devolveu JSON.")
    return json.loads(txt[a:b + 1])


# ---------------------------------------------------------------- pdf
def _linha_amarela():
    return HRFlowable(width="100%", thickness=2.2, color=YEL,
                      spaceBefore=3, spaceAfter=8)


def build_pdf(out_path, wo, cliente, endereco, vendedor, servicos, photos):
    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                            title="Ordem de Serviço — PLJ Carpentry")
    F = []
    titulo = _esc(wo.get("titulo", "")).upper()
    F.append(Paragraph(f"ORDEM DE SERVIÇO — {titulo}", S_TITULO))
    F.append(Paragraph("PLJ CARPENTRY &amp; REMODELING", S_SUB))
    F.append(_linha_amarela())

    if isinstance(servicos, (list, tuple)):
        servicos = " · ".join(str(s) for s in servicos if s)
    linhas = [["CLIENTE", cliente], ["ENDEREÇO", endereco],
              ["VENDEDOR", vendedor], ["SERVIÇOS", servicos]]
    linhas = [[k, v] for k, v in linhas if str(v or "").strip()]
    dados = [[Paragraph(_esc(k), S_CELLB), Paragraph(_esc(v), S_CELL)]
             for k, v in linhas]
    t = Table(dados, colWidths=[1.35 * inch, 5.55 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#CCCCCC")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F5F5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    F.append(t)
    F.append(Spacer(1, 10))

    for parte in (wo.get("partes") or []):
        F.append(Paragraph(_esc(parte.get("titulo", "")).upper(), S_SECAO))
        if parte.get("nota"):
            F.append(Paragraph(_esc(parte["nota"]), S_NOTA))
        for i, et in enumerate(parte.get("etapas") or [], 1):
            tit = _esc(et.get("titulo", ""))
            des = _esc(et.get("descricao", ""))
            F.append(Paragraph(f"<b>{i}. {tit}</b> — {des}", S_ETAPA))

    if wo.get("atencao"):
        F.append(Paragraph("ATENÇÃO", S_SECAO))
        for b in wo["atencao"]:
            F.append(Paragraph(_esc(b), S_BULLET, bulletText="•"))

    if photos:
        F.append(PageBreak())
        F.append(Paragraph("FOTOS DO LOCAL", S_SECAO))
        F.append(_linha_amarela())
        for path, tit, des in photos:
            try:
                iw, ih = PILImage.open(path).size
            except Exception:
                continue
            w = 4.9 * inch
            h = w * (ih / float(iw))
            if h > 5.4 * inch:
                h = 5.4 * inch
                w = h * (iw / float(ih))
            bloco = [Image(path, width=w, height=h)]
            if tit:
                bloco.append(Paragraph(_esc(tit), S_FOTOT))
            if des:
                bloco.append(Paragraph(_esc(des), S_FOTOD))
            F.append(KeepTogether(bloco))

    doc.build(F)
    return out_path


# ---------------------------------------------------------------- supabase
def check_admin(authorization):
    if not authorization:
        raise HTTPException(401, "Sem token.")
    tok = authorization.replace("Bearer ", "").strip()
    u = requests.get(f"{SUPABASE_URL}/auth/v1/user",
                     headers={"Authorization": "Bearer " + tok,
                              "apikey": SUPABASE_ANON_KEY}, timeout=30)
    if not u.ok:
        raise HTTPException(401, "Token inválido.")
    email = (u.json() or {}).get("email")
    q = requests.get(f"{SUPABASE_URL}/rest/v1/app_users",
                     params={"email": f"eq.{email}", "select": "role"},
                     headers={"Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
                              "apikey": SUPABASE_SERVICE_KEY}, timeout=30)
    rows = q.json() if q.ok else []
    if not rows or rows[0].get("role") != "admin":
        raise HTTPException(403, "Só admin pode gerar Work Order.")
    return email


def upload_pdf(local_path, num):
    nome = f"WO_{num}_{os.urandom(3).hex()}.pdf"
    with open(local_path, "rb") as f:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{nome}",
            headers={"Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
                     "apikey": SUPABASE_SERVICE_KEY,
                     "Content-Type": "application/pdf",
                     "x-upsert": "true"},
            data=f.read(), timeout=180)
    if not r.ok:
        raise HTTPException(502, f"Falha ao subir no Storage: {r.text[:200]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nome}"


def extrair_fotos(pdf_path, tmpdir):
    subprocess.run(["pdfimages", "-all", "-p", pdf_path,
                    os.path.join(tmpdir, "img")], check=False)
    out = []
    for p in sorted(glob.glob(os.path.join(tmpdir, "img*"))):
        try:
            w, h = PILImage.open(p).size
            if w >= 200 and h >= 150:
                if not p.lower().endswith(".png"):
                    np_ = p + ".png"
                    PILImage.open(p).convert("RGB").save(np_)
                    p = np_
                out.append(p)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- rotas
@app.get("/")
def health():
    return {"ok": True, "service": "PLJ Work Order generator"}


@app.post("/link")
def link(req: Req, authorization: str = Header(default="")):
    """Só devolve o endereço da estimate no JobNimbus. Rápido, sem gerar PDF."""
    check_admin(authorization)
    est = find_estimate(req.num)
    d = dados_cliente(est)
    return {"ok": True, "num": req.num, "jn_url": jn_link(est),
            "cliente": d["cliente"], "rua": d["rua"], "cidade": d["cidade"],
            "estado": d["estado"], "cep": d["cep"], "endereco": d["endereco"],
            "total": est.get("total"), "vendedor": est.get("sales_rep_name", "")}


@app.post("/gerar")
def gerar(req: Req, authorization: str = Header(default="")):
    check_admin(authorization)
    est = find_estimate(req.num)
    att = est.get("attachment_id")
    if not att:
        raise HTTPException(404, "Estimate sem PDF anexo.")

    d = dados_cliente(est)
    cliente = d["cliente"]
    endereco = d["endereco"]
    vendedor = str(est.get("sales_rep_name", "") or "").strip()
    pdf_bytes = download_pdf(att)

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.pdf")
        with open(src, "wb") as f:
            f.write(pdf_bytes)

        fotos = extrair_fotos(src, tmp)
        analise = claude_analyze(pdf_bytes, fotos, cliente, endereco,
                                 est.get("total"))

        info = {int(f.get("foto", 0)): f for f in (analise.get("fotos") or [])}
        wo_photos = []
        for i, p in enumerate(fotos[:MAX_FOTOS], 1):
            d = info.get(i)
            if d and d.get("manter"):
                wo_photos.append((p, d.get("titulo", ""), d.get("descricao", "")))

        out = os.path.join(tmp, "out.pdf")
        end_final = endereco or str(analise.get("endereco", "") or "").strip()
        vend_final = vendedor or str(analise.get("vendedor", "") or "").strip()
        build_pdf(out, analise, cliente, end_final, vend_final,
                  analise.get("servicos") or "", wo_photos)
        url = upload_pdf(out, req.num)

    return {"ok": True, "cliente": cliente, "url": url,
            "jn_url": jn_link(est),
            "titulo": analise.get("titulo", ""),
            "fotos_incluidas": len(wo_photos),
            "fotos_extraidas": len(fotos)}


# =====================================================================
# QUICKBOOKS — OAuth, tokens e financeiro por projeto
# =====================================================================
import re as _re
import secrets as _secrets
from datetime import date as _date
from fastapi.responses import RedirectResponse, HTMLResponse

QB_ID = os.environ.get("QB_CLIENT_ID", "")
QB_SECRET = os.environ.get("QB_CLIENT_SECRET", "")
SERVICE_URL = os.environ.get("SERVICE_URL", "").rstrip("/")
QB_REDIRECT = SERVICE_URL + "/qb/callback"
QB_AUTH = "https://appcenter.intuit.com/connect/oauth2"
QB_TOKEN = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_API = "https://quickbooks.api.intuit.com/v3/company"
QB_MINOR = "75"
_qb_estado = {}


# ---------------------------------------------------------- tokens
def qb_sb_headers():
    return {"Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
            "apikey": SUPABASE_SERVICE_KEY,
            "Content-Type": "application/json"}


def qb_ler():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/qb_tokens",
                     params={"id": "eq.1", "select": "*"},
                     headers=qb_sb_headers(), timeout=30)
    linhas = r.json() if r.ok else []
    return linhas[0] if linhas else None


def qb_gravar(realm, refresh):
    requests.post(f"{SUPABASE_URL}/rest/v1/qb_tokens",
                  headers={**qb_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                  json={"id": 1, "realm_id": realm, "refresh_token": refresh},
                  timeout=30)


def qb_token(codigo=None, refresh=None):
    dados = ({"grant_type": "authorization_code", "code": codigo,
              "redirect_uri": QB_REDIRECT} if codigo else
             {"grant_type": "refresh_token", "refresh_token": refresh})
    r = requests.post(QB_TOKEN, data=dados,
                      auth=(QB_ID, QB_SECRET),
                      headers={"Accept": "application/json"}, timeout=60)
    if not r.ok:
        raise HTTPException(502, f"QuickBooks recusou o token: {r.text[:200]}")
    return r.json()


def qb_acesso():
    """Devolve (access_token, realm_id). Salva o refresh novo — ele gira todo dia."""
    linha = qb_ler()
    if not linha or not linha.get("refresh_token"):
        raise HTTPException(428, "QuickBooks não está conectado. "
                                 "Abra /qb/connect uma vez para autorizar.")
    j = qb_token(refresh=linha["refresh_token"])
    novo = j.get("refresh_token")
    if novo and novo != linha["refresh_token"]:
        qb_gravar(linha["realm_id"], novo)
    return j["access_token"], linha["realm_id"]


# ---------------------------------------------------------- chamadas
def qb_get(caminho, params=None):
    tok, realm = qb_acesso()
    p = dict(params or {})
    p["minorversion"] = QB_MINOR
    r = requests.get(f"{QB_API}/{realm}/{caminho}",
                     headers={"Authorization": "Bearer " + tok,
                              "Accept": "application/json"},
                     params=p, timeout=90)
    if not r.ok:
        raise HTTPException(502, f"QuickBooks ({r.status_code}): {r.text[:200]}")
    return r.json()


def qb_query(sql):
    return qb_get("query", {"query": sql}).get("QueryResponse", {})


def _esc_sql(v):
    return str(v or "").replace("'", "''")


# ---------------------------------------------------------- busca do projeto
def qb_por_estimate(num):
    """Acha o projeto pelo número da estimate escrito no memo da fatura."""
    q = ("select Id, CustomerRef, PrivateNote from Invoice "
         "where PrivateNote like '%Estimate " + _esc_sql(num) + "%' maxresults 5")
    inv = (qb_query(q).get("Invoice") or [])
    for i in inv:
        ref = i.get("CustomerRef") or {}
        if ref.get("value"):
            return {"id": ref["value"], "nome": ref.get("name", ""),
                    "via": "estimate"}
    return None


def qb_por_endereco(rua, cidade=""):
    """Acha o projeto pelo endereço que está dentro do nome do projeto."""
    chave = _re.sub(r"\s+", " ", str(rua or "")).strip()
    if len(chave) < 5:
        return None
    q = ("select Id, DisplayName from Customer where Active = true "
         "and DisplayName like '%" + _esc_sql(chave) + "%' maxresults 10")
    lista = (qb_query(q).get("Customer") or [])
    if not lista:
        return None
    if cidade:
        c = cidade.lower()
        exato = [x for x in lista if c in (x.get("DisplayName", "")).lower()]
        if exato:
            lista = exato
    x = lista[0]
    return {"id": x["Id"], "nome": x.get("DisplayName", ""), "via": "endereco",
            "outros": [y.get("DisplayName", "") for y in lista[1:5]]}


# ---------------------------------------------------------- números
def qb_faturas(cid):
    q = ("select Id, DocNumber, TotalAmt, Balance, TxnDate, DueDate, PrivateNote "
         "from Invoice where CustomerRef = '" + _esc_sql(cid) + "' maxresults 200")
    inv = (qb_query(q).get("Invoice") or [])
    faturado = sum(float(i.get("TotalAmt") or 0) for i in inv)
    saldo = sum(float(i.get("Balance") or 0) for i in inv)
    itens = [{"num": i.get("DocNumber"), "valor": float(i.get("TotalAmt") or 0),
              "saldo": float(i.get("Balance") or 0), "data": i.get("TxnDate"),
              "vence": i.get("DueDate"), "memo": i.get("PrivateNote", "")}
             for i in inv]
    itens.sort(key=lambda x: x.get("data") or "")
    return {"faturado": faturado, "saldo": saldo, "recebido": faturado - saldo,
            "faturas": itens}


def _pl_valor(rows, alvo):
    """Varre o relatório e devolve o total do grupo pedido."""
    for row in (rows or []):
        if row.get("group") == alvo:
            col = ((row.get("Summary") or {}).get("ColData") or [])
            if len(col) > 1:
                try:
                    return float(str(col[1].get("value") or 0).replace(",", ""))
                except ValueError:
                    return 0.0
        filhos = (row.get("Rows") or {}).get("Row")
        if filhos:
            v = _pl_valor(filhos, alvo)
            if v is not None:
                return v
    return None


def qb_custos(cid):
    """Lucro do projeto pelo relatório de P&L filtrado por aquele projeto."""
    rel = qb_get("reports/ProfitAndLoss",
                 {"customer": cid, "start_date": "2015-01-01",
                  "end_date": _date.today().isoformat(),
                  "accounting_method": "Accrual"})
    linhas = (rel.get("Rows") or {}).get("Row") or []
    receita = _pl_valor(linhas, "Income") or 0.0
    cogs = _pl_valor(linhas, "COGS") or 0.0
    desp = _pl_valor(linhas, "Expenses") or 0.0
    liquido = _pl_valor(linhas, "NetIncome")
    custos = cogs + desp
    if liquido is None:
        liquido = receita - custos
    return {"receita": receita, "custos": custos, "cogs": cogs,
            "despesas": desp, "lucro": liquido}


# ---------------------------------------------------------- rotas
@app.get("/qb/connect")
def qb_connect():
    if not QB_ID or not SERVICE_URL:
        raise HTTPException(500, "Faltam QB_CLIENT_ID / SERVICE_URL no Render.")
    estado = _secrets.token_urlsafe(16)
    _qb_estado[estado] = True
    url = (QB_AUTH + "?client_id=" + QB_ID +
           "&response_type=code&scope=com.intuit.quickbooks.accounting" +
           "&redirect_uri=" + requests.utils.quote(QB_REDIRECT, safe="") +
           "&state=" + estado)
    return RedirectResponse(url)


@app.get("/qb/callback")
def qb_callback(code: str = "", state: str = "", realmId: str = ""):
    if not code or not realmId:
        return HTMLResponse("<h3>Autorização cancelada.</h3>", status_code=400)
    _qb_estado.pop(state, None)
    j = qb_token(codigo=code)
    qb_gravar(realmId, j.get("refresh_token"))
    return HTMLResponse(
        "<div style='font-family:system-ui;padding:40px;text-align:center'>"
        "<h2>QuickBooks conectado</h2>"
        "<p>Empresa " + realmId + ". Pode fechar esta aba.</p></div>")


@app.get("/qb/disconnect")
def qb_disconnect():
    """A Intuit exige um endereço de desconexão. Apaga o token guardado."""
    try:
        requests.delete(f"{SUPABASE_URL}/rest/v1/qb_tokens",
                        params={"id": "eq.1"}, headers=qb_sb_headers(),
                        timeout=30)
    except Exception:
        pass
    return HTMLResponse(
        "<div style='font-family:system-ui;padding:40px;text-align:center'>"
        "<h2>QuickBooks disconnected</h2>"
        "<p>PLJ Schedule no longer has access to your QuickBooks data.</p>"
        "<p><a href='/qb/connect'>Connect again</a></p></div>")


@app.get("/qb/status")
def qb_status():
    linha = qb_ler()
    if not linha:
        return {"conectado": False}
    try:
        tok, realm = qb_acesso()
        info = qb_get("companyinfo/" + realm).get("CompanyInfo", {})
        return {"conectado": True, "realm": realm,
                "empresa": info.get("CompanyName", "")}
    except HTTPException as e:
        return {"conectado": False, "erro": e.detail}


class ProjReq(BaseModel):
    num: str = ""
    rua: str = ""
    cidade: str = ""


@app.post("/qb/projeto")
def qb_projeto(req: ProjReq, authorization: str = Header(default="")):
    check_admin(authorization)
    proj = None
    if req.num:
        proj = qb_por_estimate(req.num)
    if not proj and req.rua:
        proj = qb_por_endereco(req.rua, req.cidade)
    if not proj:
        return {"ok": False, "motivo": "Projeto não encontrado no QuickBooks."}

    fat = qb_faturas(proj["id"])
    try:
        pl = qb_custos(proj["id"])
    except HTTPException:
        pl = {"receita": fat["faturado"], "custos": 0.0, "lucro": 0.0}

    base = pl["receita"] or fat["faturado"]
    pct = round((pl["custos"] / base) * 100, 1) if base else None
    margem = round((pl["lucro"] / base) * 100, 1) if base else None
    return {"ok": True, "projeto": proj["nome"], "id": proj["id"],
            "via": proj.get("via"), "outros": proj.get("outros", []),
            "faturado": round(fat["faturado"], 2),
            "recebido": round(fat["recebido"], 2),
            "deve": round(fat["saldo"], 2),
            "custos": round(pl["custos"], 2),
            "lucro": round(pl["lucro"], 2),
            "pct_gasto": pct, "margem": margem,
            "faturas": fat["faturas"][:12]}
