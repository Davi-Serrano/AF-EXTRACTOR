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

# Alguns PDFs saem do pypdf com valor e rotulo colados na mesma linha, ex.:
# "29/04/2026Data:151/2026Pedido (Nº/Ano):". Quebramos nesses rotulos para cair
# no mesmo formato "valor / rotulo" (uma coisa por linha) que o resto do
# extrator espera. Ordem importa: rotulos mais longos primeiro.
_GLUED_LABELS = [
    "Descrição do Objeto:",
    "Data Prevista de Entrega:",
    "Modo de Fornecimento:",
    "Local de Entrega:",
    "Prazo de Entrega:",
    "Nome/Razão Social:",
    "Data Autorização:",
    "Pedido (Nº/Ano):",
    "Ata R. Preço:",
    "Tipo Objeto:",
    "Justificativa:",
    "Responsável:",
    "Observação:",
    "Modalidade:",
    "Endereço:",
    "Proc. Adm.:",
    "CPF/CNPJ:",
    "Nº/Ano:",
    "Objeto:",
    "Data:",
]
_RX_GLUED_LABEL = re.compile("(" + "|".join(re.escape(l) for l in _GLUED_LABELS) + ")")
# Linha de item colada: total + unitario + qtd + UM + codigo + " - " + nome
_RX_GLUED_ITEM_ROW = re.compile(
    r"^(\d[\d\.]*,\d{2})(\d[\d\.]*,\d{4})(\d[\d\.]*,\d{1,4})([^\W\d_]{1,6})(\d{1,3}\.\d{4})\s*-\s*(.*)$"
)
# Dotacao orcamentaria colada no numero do empenho: "...3.3.90.30.00211/2026"
_RX_GLUED_EMPENHO = re.compile(
    r"(\d{2}\.\d{2}\.\d{2}\.\d{2}\.\d{3}\.\d{4}\.\d{4}\.\d\.\d\.\d{2}\.\d{2}\.\d{2})(\d{1,6}/\d{4})"
)

def normalize_glued_text(text: str) -> str:
    text = _RX_GLUED_LABEL.sub(r"\n\1\n", text or "")
    text = re.sub(r"(\d)(Total Pedido)", r"\1\n\2", text)
    text = _RX_GLUED_EMPENHO.sub(r"\1\n\2", text)

    out: List[str] = []
    for raw in text.split("\n"):
        m = _RX_GLUED_ITEM_ROW.match(raw.strip())
        if m:
            out.extend([m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), "-", m.group(6)])
        else:
            out.append(raw)
    return "\n".join(out)

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
RX_UM = re.compile(r"^[^\W\d_]{1,6}$")  # letras, inclusive acentuadas (ex.: PÇ)
RX_ITEM_CODE = re.compile(r"^\d{1,3}\.\d{4}$")
# N/AAAA que nao seja pedaco de uma data (dd/mm/aaaa)
RX_EMPENHO_NUM = re.compile(r"(?<![\d/])(\d{1,6}/\d{4})(?![\d/])")

TITULO_ITEM_STOP_PREFIXES = ("MARCA:", "OBSERVAÇÃO:", "DESCRIÇÃO:")

def parece_continuar_titulo_item(ln: str) -> bool:
    up = ln.upper()
    if up.startswith(TITULO_ITEM_STOP_PREFIXES) or up == "AUTORIZAÇÃO DE FORNECIMENTO":
        return False
    if "CENTRO DE CUSTO" in up or "PRODUTO/SERVIÇO" in up:
        return False
    # Nome de produto nessas AFs vem sempre em maiusculas; qualquer linha com
    # minuscula e cabecalho/rodape de pagina do PDF ("Vl. Líquido", "Sistema
    # CECAM", "Emitido por:", enderecos, etc.), nao faz parte do nome do item.
    return ln == up

def extract_items(lines: List[str]) -> List[Dict[str, Any]]:
    # Varre o texto inteiro atras do bloco numerico de cada item (valor total,
    # valor unitario, quantidade, unidade, codigo e "-"). Nao depende do
    # rotulo "DESCRIÇÃO:", que nem sempre vem antes do bloco (o primeiro item
    # de uma pagina costuma aparecer antes de qualquer "DESCRIÇÃO:").
    items: List[Dict[str, Any]] = []

    for j in range(len(lines) - 5):
        vl_total, vl_unit, qtd, um, codigo, dash = lines[j : j + 6]

        if not (
            RX_MONEY_2.match(vl_total)
            and RX_MONEY_2_4.match(vl_unit)
            and RX_QTD.match(qtd)
            and RX_UM.match(um)
            and RX_ITEM_CODE.match(codigo)
            and dash == "-"
        ):
            continue

        # O nome curto do item vem logo depois do codigo/traco
        # (ex.: "TONER BROTHER TN 419BK PRETO"), ate achar o proximo rotulo.
        # Preferimos esse nome curto ao paragrafo gigante de "DESCRIÇÃO:" (que
        # e so texto de especificacao). Limite de linhas como rede de seguranca
        # contra texto de rodape/cabecalho de pagina que escape da heuristica.
        titulo_lines: List[str] = []
        k = j + 6
        while (
            k < len(lines)
            and len(titulo_lines) < 10
            and parece_continuar_titulo_item(lines[k])
        ):
            titulo_lines.append(lines[k])
            k += 1

        item = {
            "item_codigo": codigo,
            "item_descricao": normalize_spaces(" ".join(titulo_lines))
            or f"Item {codigo}",
            "item_um": um.upper(),
            "item_quantidade": br_num_to_dot(qtd),
            "item_valor_unitario": br_num_to_dot(vl_unit),
            "item_valor_total": br_num_to_dot(vl_total),
        }

        # O mesmo item pode aparecer duas vezes no texto quando atravessa a
        # quebra de pagina; ficamos so com a ultima ocorrencia.
        items = [it for it in items if it != item]
        items.append(item)

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
    lines = iter_clean_lines(normalize_glued_text(text))
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
        "prazo_entrega": None,
        "data_prevista_entrega": None,
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

    # Data Prevista de Entrega: -> valor vem antes (dd/mm/aaaa)
    idx_data_prevista_entrega: Optional[int] = None
    for idx, ln in enumerate(lines):
        if ln.upper() == "DATA PREVISTA DE ENTREGA:":
            idx_data_prevista_entrega = idx
            v = prev_line(lines, idx)
            iso = ddmmyyyy_to_iso(v or "")
            if iso:
                data["data_prevista_entrega"] = iso
            break

    # Prazo de Entrega: -> texto vem antes, podendo ocupar varias linhas
    # entre o rotulo "Data Prevista de Entrega:" (se achado) e este rotulo
    for idx, ln in enumerate(lines):
        if ln.upper() == "PRAZO DE ENTREGA:":
            start = (
                idx_data_prevista_entrega + 1
                if idx_data_prevista_entrega is not None
                else max(0, idx - 3)
            )
            valor = normalize_spaces(" ".join(lines[start:idx]))
            if valor:
                data["prazo_entrega"] = valor.rstrip(" -")
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
            found = RX_EMPENHO_NUM.findall(blob)
            if found:
                # pega o primeiro (normalmente é o empenho)
                empenho_found = found[0]
            break

    # fallback: procura "Centro de Custo" perto de "Empenho"/"Dotação".
    # O documento pode ter mais de uma ocorrência de "Centro de Custo"
    # (ex.: uma no cabeçalho da lista de itens, sem relação com o empenho) -
    # por isso não pode parar na primeira, só na que de fato é a tabela
    # financeira (tem "Empenho"/"Dotação" por perto).
    if not empenho_found:
        for idx, ln in enumerate(lines):
            if "CENTRO DE CUSTO" in ln.upper():
                blob = window_text(lines, idx, radius=6)
                blob_up = blob.upper()
                if "EMPENHO" not in blob_up or "DOTA" not in blob_up:
                    continue
                found = RX_EMPENHO_NUM.findall(blob)
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
