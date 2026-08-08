# -*- coding: utf-8 -*-
"""
Serviço PLJ — Gerador de Work Order (padrão PLJ, com fotos traduzidas).
Fluxo: nº da estimate -> baixa PDF do JobNimbus -> extrai fotos -> Claude analisa
(escopo + legenda de cada foto em PT) -> monta PDF (reportlab) -> sobe no Supabase -> URL.
"""
import os, io, json, base64, subprocess, tempfile, time, glob
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                Table, TableStyle, KeepTogether, HRFlowable, PageBreak)

JOBNIMBUS_KEY = os.environ.get("JOBNIMBUS_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
BUCKET = os.environ.get("WO_BUCKET", "workorders")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class Req(BaseModel):
    num: str

# ---------- estilos reportlab (padrão PLJ) ----------
YEL = colors.HexColor("#F2C200"); DARK = colors.HexColor("#1F1F1F"); GREY = colors.HexColor("#555555")
H1 = ParagraphStyle("H1", fontName="Helvetica-Bold", fontSize=17, textColor=DARK, spaceAfter=2, leading=20)
SUB = ParagraphStyle("SUB", fontName="Helvetica", fontSize=10, textColor=GREY, leading=13)
H2 = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=12, textColor=DARK, spaceBefore=10, spaceAfter=5, leading=15)
NOTE = ParagraphStyle("NOTE", fontName="Helvetica-Oblique", fontSize=8.5, textColor=GREY, leading=11.5, spaceAfter=5)
STEP = ParagraphStyle("STEP", fontName="Helvetica", fontSize=9.5, textColor=DARK, leading=13.5, leftIndent=14, spaceAfter=3.5)
CAPT = ParagraphStyle("CAPT", fontName="Helvetica", fontSize=9, textColor=DARK, leading=12.5)
CAPTT = ParagraphStyle("CAPTT", fontName="Helvetica-Bold", fontSize=9.5, textColor=DARK, leading=13)
IMG_W = 4.9 * inch

def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_pdf(path, wo, client, addr, est_date, contract, info_rows, photos):
    doc = SimpleDocTemplate(path, pagesize=letter, leftMargin=0.7*inch, rightMargin=0.7*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch, title=f"Work Order - {client}")
    s = [Paragraph(f"ORDEM DE SERVIÇO — {esc(wo.get('titulo','TRABALHO'))}", H1),
         Paragraph("PLJ Carpentry Inc. &nbsp;|&nbsp; Cape Cod, MA &nbsp;|&nbsp; Documento de obra — sem valores", SUB),
         Spacer(1, 8), HRFlowable(width="100%", thickness=3, color=YEL, spaceAfter=10)]
    info = [["CLIENTE", esc(client), "DATA DA ESTIMATE", esc(est_date)],
            ["ENDEREÇO", esc(addr), "CONTRATO", esc(contract)]] + info_rows
    t = Table(info, colWidths=[0.95*inch, 3.05*inch, 1.35*inch, 1.75*inch])
    t.setStyle(TableStyle([
        ("FONT",(0,0),(0,-1),"Helvetica-Bold",7.5), ("FONT",(2,0),(2,-1),"Helvetica-Bold",7.5),
        ("FONT",(1,0),(1,-1),"Helvetica",9), ("FONT",(3,0),(3,-1),"Helvetica",9),
        ("TEXTCOLOR",(0,0),(0,-1),GREY), ("TEXTCOLOR",(2,0),(2,-1),GREY),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("LINEBELOW",(0,0),(-1,-2),0.4,colors.HexColor("#DDDDDD")),
    ]))
    s += [t, Spacer(1,4)]
    for sec in wo.get("partes", []):
        s.append(Paragraph(esc(sec.get("titulo","")), H2))
        if sec.get("nota"): s.append(Paragraph(esc(sec["nota"]), NOTE))
        for i, e in enumerate(sec.get("etapas", []), 1):
            s.append(Paragraph(f"<b>{i}. {esc(e.get('titulo',''))}</b> &ndash; {esc(e.get('descricao',''))}", STEP))
    if wo.get("atencao"):
        s.append(Paragraph("ATENÇÃO", H2))
        for n in wo["atencao"]:
            s.append(Paragraph(f"&bull; {esc(n)}", STEP))
    if photos:
        s += [PageBreak(), Paragraph("FOTOS DO LOCAL", H1),
              HRFlowable(width="100%", thickness=3, color=YEL, spaceBefore=6, spaceAfter=14)]
        for path_img, title, desc in photos:
            im = Image(path_img); ratio = im.imageHeight/float(im.imageWidth)
            s.append(KeepTogether([Image(path_img, width=IMG_W, height=IMG_W*ratio), Spacer(1,5),
                                   Paragraph(esc(title), CAPTT), Paragraph(esc(desc), CAPT), Spacer(1,16)]))
    doc.build(s)

# ---------- JobNimbus ----------
def jn_get(url):
    r = requests.get(url, headers={"Authorization": "Bearer " + JOBNIMBUS_KEY,
                                   "Content-Type": "application/json"}, timeout=60)
    return r

def find_estimate(num):
    flt = {"must": [{"term": {"number": num}}]}
    r = jn_get("https://app.jobnimbus.com/api1/estimates?size=5&filter=" + requests.utils.quote(json.dumps(flt)))
    if not r.ok: raise HTTPException(502, f"Erro JobNimbus estimates: {r.status_code}")
    data = r.json()
    ests = data.get("results") or data.get("estimates") or data.get("data") or []
    if not ests: raise HTTPException(404, f"Nenhuma estimate com número {num}")
    return ests[0]

def download_pdf(att_id):
    r = jn_get("https://app.jobnimbus.com/api1/files/" + att_id)
    if not r.ok: raise HTTPException(502, f"Erro ao baixar PDF: {r.status_code}")
    return r.content

# ---------- Claude ----------
def claude_analyze(pdf_bytes, photo_files, cliente, endereco, est_total):
    """Manda o PDF (escopo) + as fotos numeradas. Recebe JSON: work order + legenda por foto."""
    content = []
    content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                    "data": base64.b64encode(pdf_bytes).decode()}})
    # fotos numeradas
    for i, f in enumerate(photo_files, 1):
        content.append({"type": "text", "text": f"FOTO {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(open(f, "rb").read()).decode()}})
    regras = f"""Você é assistente da PLJ Carpentry (Cape Cod, MA). Analise a proposta (SumoQuote) em anexo e as FOTOS numeradas.
Gere uma ORDEM DE SERVIÇO (Work Order) em PORTUGUÊS, para a equipe de campo, SEM NENHUM VALOR/PREÇO, com MUITO cuidado.

Cliente: {cliente} | Endereço: {endereco}

Responda SOMENTE em JSON válido, sem texto fora do JSON, neste formato:
{{
 "titulo": "resumo curto do serviço em maiúsculas (ex.: TRIM E JANELAS, DECK, SIDING)",
 "info_extra": [["ROTULO","valor","ROTULO2","valor2"]],   // 0 a 2 linhas extras p/ a tabela (ex.: specs de janela, material). Pode ser [].
 "partes": [
   {{"titulo":"PARTE A — ...", "nota":"observação curta em itálico (ou vazio)",
     "etapas":[{{"titulo":"NOME DA ETAPA","descricao":"o que fazer, poucas palavras, claro, verbo no infinitivo"}}]}}
 ],
 "atencao": ["avisos importantes: não começar antes do permit, madeira podre = parar e avisar, proteger jardim/móveis, DigSafe/irrigação por conta do homeowner, divergências que você notar, etc."],
 "fotos": [
   {{"foto": 1, "manter": true, "titulo":"Local — assunto (ex.: Frente da casa — janelas)", "descricao":"o que fazer nessa foto, citando as cores das marcações (vermelho/amarelo) quando houver"}}
 ]
}}

Regras importantes:
- Traduza tudo pro português. NUNCA deixe legenda em inglês.
- Para cada FOTO numerada, diga se deve ENTRAR na Work Order ("manter": true) ou não ("manter": false). Marque "manter": false para: logo, capa, certificado de seguro, certificação de fabricante, páginas de preço/contrato.
- Cite as cores das marcações nas fotos (ex.: "marcado em vermelho", "círculo amarelo").
- Se notar divergência (ex.: marca da janela diferente entre a foto de spec e a legenda), inclua um aviso em "atencao".
- Separe em PARTE A / PARTE B quando houver frentes diferentes (ex.: trim e janelas). Se for um serviço só, uma parte só.
- Seja específico e caprichado, como um bom líder de obra explicaria pra equipe."""
    content.append({"type": "text", "text": regras})

    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 4000, "messages": [{"role": "user", "content": content}]},
        timeout=180)
    if not r.ok: raise HTTPException(502, f"Erro Anthropic: {r.status_code} {r.text[:300]}")
    data = r.json()
    txt = "".join(c.get("text","") for c in data.get("content", []) if c.get("type") == "text")
    txt = txt.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(txt)
    except Exception:
        raise HTTPException(502, "Claude não devolveu JSON válido: " + txt[:300])

# ---------- Supabase upload ----------
def upload_pdf(local_path, num):
    fname = f"wo-{num}-{int(time.time())}.pdf"
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{fname}"
    with open(local_path, "rb") as f:
        r = requests.post(url, headers={"Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
                                        "Content-Type": "application/pdf", "x-upsert": "true"},
                          data=f.read(), timeout=120)
    if not r.ok: raise HTTPException(500, f"Erro upload Supabase: {r.status_code} {r.text[:200]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{fname}"

# ---------- auth (só admin) ----------
def check_admin(authorization):
    if not authorization: raise HTTPException(401, "Sem token.")
    token = authorization.replace("Bearer ", "")
    u = requests.get(f"{SUPABASE_URL}/auth/v1/user",
                     headers={"Authorization": "Bearer " + token, "apikey": SUPABASE_ANON_KEY}, timeout=30)
    if not u.ok: raise HTTPException(401, "Token inválido.")
    email = u.json().get("email")
    q = requests.get(f"{SUPABASE_URL}/rest/v1/app_users?email=eq.{email}&select=role",
                     headers={"Authorization": "Bearer " + SUPABASE_SERVICE_KEY, "apikey": SUPABASE_SERVICE_KEY}, timeout=30)
    rows = q.json() if q.ok else []
    if not rows or rows[0].get("role") != "admin":
        raise HTTPException(403, "Só admin pode gerar Work Order.")

@app.get("/")
def health():
    return {"ok": True, "service": "PLJ Work Order generator"}

@app.post("/gerar")
def gerar(req: Req, authorization: str = Header(default="")):
    check_admin(authorization)
    est = find_estimate(req.num)
    att = est.get("attachment_id")
    if not att: raise HTTPException(404, "Estimate sem PDF anexo.")
    contato = next((r for r in (est.get("related") or []) if r.get("type") == "contact"), None)
    cliente = contato["name"] if contato else ""
    pdf_bytes = download_pdf(att)

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.pdf")
        open(src, "wb").write(pdf_bytes)
        # extrai imagens
        subprocess.run(["pdfimages", "-all", "-p", src, os.path.join(tmp, "img")], check=False)
        imgs = sorted(glob.glob(os.path.join(tmp, "img*")))
        # filtra imagens muito pequenas (logos/ícones)
        from PIL import Image as PILImage
        photos = []
        for p in imgs:
            try:
                w, h = PILImage.open(p).size
                if w >= 200 and h >= 150:
                    # normaliza p/ png
                    if not p.lower().endswith(".png"):
                        np = p + ".png"; PILImage.open(p).convert("RGB").save(np); p = np
                    photos.append(p)
            except Exception:
                pass

        analysis = claude_analyze(pdf_bytes, photos, cliente,
                                  f"{est.get('address_line1','')}", est.get("total"))

        # casa a análise das fotos com os arquivos
        wo_photos = []
        fotos_info = {f["foto"]: f for f in analysis.get("fotos", [])}
        for i, p in enumerate(photos, 1):
            info = fotos_info.get(i)
            if info and info.get("manter"):
                wo_photos.append((p, info.get("titulo",""), info.get("descricao","")))

        out = os.path.join(tmp, "out.pdf")
        addr = f"{est.get('address_line1','')}".strip()
        build_pdf(out, analysis, cliente or "", addr, "", "—",
                  analysis.get("info_extra", []) if isinstance(analysis.get("info_extra"), list) else [],
                  wo_photos)
        public_url = upload_pdf(out, req.num)

    return {"ok": True, "cliente": cliente, "url": public_url,
            "fotos_incluidas": len(wo_photos), "titulo": analysis.get("titulo","")}
