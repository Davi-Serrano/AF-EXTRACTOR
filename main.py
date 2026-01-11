from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader

app = FastAPI(title="AF Extractor (PDF -> JSON)", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Helpers
# -----------------------------
def normalize_spaces(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()

def only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

def br_num_to_dot(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip().replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        Decimal(s)
        return s
    except InvalidOperation:
        return None

def ddmmyyyy_to_iso(s: str) -> Optional[str]:
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", s or "")
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd).isoformat()
    except Exception:
        return None

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: List[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).replace("\r", "\n")

def iter_clean_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in (text or "").split("\n"):
        ln = normalize_spaces(raw)
        if ln:
            out.append(ln)
    return out

def prev_line(lines: List[str], i: int) -> Optional[str]:
    if i <= 0:
        return None
    v = lines[i - 1].strip()
    return v if v else None

def window_text(lines: List[str], center: int, radius: int = 3) -> str:
    a = max(0, center - radius)
    b = min(len(lines), center + radius + 1)
    return " ".join(lines[a:b])

# -----------------------------
# Itens (layout CECÂM extraído pelo pypdf)
# -----------------------------
RX_MONEY_2 = re.compile(r"^\d[\d\.]*,\d{2}$")
RX_MONEY_2_4 = re.compile(r"^\d[\d\.]*,\d{2,4}$")
RX_QTD = re.compile(r"^\d[\d\.]*,\d{1,4}$")
RX_UM = re.compile(r"^[A-Za-z]{1,6}$")
RX_ITEM_CODE = re.compile(r"^\d{1,3}\.\d{4}$")

def extract_items(lines: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines):
        if lines[i].upper() != "DESCRIÇÃO:":
            i += 1
            continue

        desc_lines: List[str] = []
        j = i + 1

        while j < len(lines):
            if j + 5 < len(lines):
                vl_total = lines[j]
                vl_unit = lines[j + 1]
                qtd = lines[j + 2]
                um = lines[j + 3]
                codigo = lines[j + 4]
                dash = lines[j + 5]

                if (
                    RX_MONEY_2.match(vl_total)
                    and RX_MONEY_2_4.match(vl_unit)
                    and RX_QTD.match(qtd)
                    and RX_UM.match(um)
                    and RX_ITEM_CODE.match(codigo)
                    and dash == "-"
                ):
                    descricao = normalize_spaces(" ".join(desc_lines))

                    items.append(
                        {
                            "item_codigo": codigo,
                            "item_descricao": descricao,
                            "item_um": um.upper(),
                            "item_quantidade": br_num_to_dot(qtd),
                            "item_valor_unitario": br_num_to_dot(vl_unit),
                            "item_valor_total": br_num_to_dot(vl_total),
                        }
                    )

                    i = j + 6
                    break

            desc_lines.append(lines[j])
            j += 1
        else:
            i += 1

    return items

def sum_items_total(items: List[Dict[str, Any]]) -> Optional[str]:
    total = Decimal("0")
    ok = False
    for it in items:
        v = it.get("item_valor_total")
        if not v:
            continue
        try:
            total += Decimal(v)
            ok = True
        except Exception:
            pass
    if not ok:
        return None
    return f"{total:.2f}"

# -----------------------------
# Field extractor
# -----------------------------
def extract_fields(text: str) -> Dict[str, Any]:
    lines = iter_clean_lines(text)
    joined = "\n".join(lines)

    data: Dict[str, Any] = {
        "af_numero_ano": None,
        "pedido_numero_ano": None,
        "processo_adm": None,
        "modalidade": None,
        "contratada_nome": None,
        "cnpj": None,
        "data_af": None,
        "valor_total_pedido": None,
        "empenho": None,
        "itens": [],
    }

    # AF
    m = re.search(r"\bN[ºo]\s*(\d{1,6}/\d{4})\b", joined, re.IGNORECASE)
    if m:
        data["af_numero_ano"] = normalize_spaces(m.group(1))

    # Total Pedido (valor vem na linha anterior ao rótulo "Total Pedido")
    for idx, ln in enumerate(lines):
        if ln.upper() == "TOTAL PEDIDO":
            v = prev_line(lines, idx)
            if v and RX_MONEY_2.match(v):
                data["valor_total_pedido"] = br_num_to_dot(v)
            break

    # Pedido (Nº/Ano): -> valor vem antes
    for idx, ln in enumerate(lines):
        up = ln.upper()
        if "PEDIDO (Nº/ANO)" in up:
            v = prev_line(lines, idx)
            if v and re.match(r"^\d{1,6}/\d{4}$", v):
                data["pedido_numero_ano"] = v
            break

    # Modalidade: -> texto vem antes | Nº/Ano: -> número vem antes
    modalidade_txt = None
    modalidade_num = None
    for idx, ln in enumerate(lines):
        if ln.upper() == "MODALIDADE:":
            v = prev_line(lines, idx)
            if v:
                modalidade_txt = v.upper()

        if ln.upper() == "Nº/ANO:":
            v = prev_line(lines, idx)
            if v and re.match(r"^\d{1,6}/\d{4}$", v):
                modalidade_num = v

    if modalidade_txt and modalidade_num:
        data["modalidade"] = f"{modalidade_txt} {modalidade_num}"

    # Proc. Adm.: -> valor vem antes
    for idx, ln in enumerate(lines):
        if ln.upper() == "PROC. ADM.:":
            v = prev_line(lines, idx)
            if v and re.match(r"^\d{1,6}/\d{4}$", v):
                data["processo_adm"] = v
            break

    # Data Autorização: -> valor vem antes
    for idx, ln in enumerate(lines):
        up = ln.upper()
        if "DATA AUTORIZA" in up:
            v = prev_line(lines, idx)
            iso = ddmmyyyy_to_iso(v or "")
            if iso:
                data["data_af"] = iso
            break

    # Nome/Razão Social: -> valor vem antes
    for idx, ln in enumerate(lines):
        up = ln.upper()
        if up == "NOME/RAZÃO SOCIAL:" or up == "NOME/RAZAO SOCIAL:":
            v = prev_line(lines, idx)
            if v and not re.search(r"\bAUTORIZAMOS A EMPRESA\b", v, re.IGNORECASE):
                data["contratada_nome"] = v
            break

    # CPF/CNPJ: -> valor vem antes (CNPJ da contratada)
    for idx, ln in enumerate(lines):
        if ln.upper() == "CPF/CNPJ:":
            v = prev_line(lines, idx)
            digits = only_digits(v or "")
            if len(digits) == 14:
                data["cnpj"] = digits
            break

    # -----------------------------
    # EMPENHO (robusto)
    # Quando achar o cabeçalho da tabela financeira, procura NNN/AAAA numa janela
    # -----------------------------
    empenho_found = None
    for idx, ln in enumerate(lines):
        up = ln.upper()

        # cabeçalho pode vir como:
        # "Empenho Dotação Orçamentária Dest. Recurso Centro de Custo Saldo"
        if ("EMPENHO" in up) and ("DOTA" in up) and ("CENTRO" in up or "CUSTO" in up):
            blob = window_text(lines, idx, radius=4)
            found = re.findall(r"\b(\d{1,6}/\d{4})\b", blob)
            if found:
                # pega o primeiro (normalmente é o empenho)
                empenho_found = found[0]
            break

    # fallback: se não achou pelo cabeçalho, procura o primeiro NNN/AAAA perto de "Centro de Custo"
    if not empenho_found:
        for idx, ln in enumerate(lines):
            if "CENTRO DE CUSTO" in ln.upper():
                blob = window_text(lines, idx, radius=6)
                found = re.findall(r"\b(\d{1,6}/\d{4})\b", blob)
                if found:
                    empenho_found = found[0]
                break

    data["empenho"] = empenho_found

    # Itens
    items = extract_items(lines)
    data["itens"] = items

    # Fallback do total: soma itens se não tiver Total Pedido
    if not data["valor_total_pedido"]:
        data["valor_total_pedido"] = sum_items_total(items)

    return data

# -----------------------------
# API
# -----------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.post("/extract")
async def extract(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Envie um arquivo .pdf")

    pdf_bytes = await file.read()
    if not pdf_bytes or len(pdf_bytes) < 100:
        raise HTTPException(status_code=400, detail="PDF vazio ou inválido")

    try:
        text = extract_text_from_pdf_bytes(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao ler PDF: {e}")

    if not normalize_spaces(text):
        raise HTTPException(status_code=422, detail="Não foi possível extrair texto do PDF (texto vazio).")

    return extract_fields(text)
