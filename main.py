import os
import re
import sqlite3
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Leitor NFC-e & Faturas API", version="6.1.0")

ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.getenv("CATEGORIAS_DB", "categorias_aprendidas.db")

DOMINIOS_SEFAZ = tuple(
    d.strip().lower()
    for d in os.getenv(
        "SEFAZ_DOMINIOS",
        "sefaz.rs.gov.br,svrs.rs.gov.br,dfe-portal.svrs.rs.gov.br,dfe.svrs.rs.gov.br,fazenda.rs.gov.br",
    ).split(",")
    if d.strip()
)

REGRAS_CATEGORIAS = [
    ("Açougue", ["PICANHA", "ALCATRA", "CONTRA FILE", "COXAO", "PATINHO", "COSTELA", "CARNE", "BOVIN", "SUIN", "LOMBO", "BISTECA", "LINGUICA", "SALSICHA", "FRANGO", "SASSAMI", "FILEZINHO", "COXA", "SOBRECOXA", "PEITO DE FRANGO", "CHICKEN", "PRESUNTO", "MORTADELA", "SALAME", "BACON", "PEIXE", "TILAPIA", "SALMAO", "HAMBURGUER"]),
    ("Bebidas", ["REFRESCO", "SUCO", "REFRIGERANTE", "COCA", "PEPSI", "GUARANA", "FANTA", "SPRITE", "AGUA MINERAL", "AGUA COM GAS", "CERVEJA", "VINHO", "ENERGETICO", "CHA ", "ISOTONICO", "LEITE FERMENTADO", "IOGURTE", "BEBIDA LACTEA"]),
    ("Hortifrúti", ["BANANA", "MACA", "LARANJA", "LIMAO", "MANGA", "MAMAO", "MELANCIA", "UVA", "MORANGO", "ABACAXI", "TOMATE", "CEBOLA", "BATATA", "CENOURA", "ALFACE", "RUCULA", "COUVE", "BROCOLIS", "PIMENTAO", "ALHO", "CHEIRO VERDE", "OVOS", "OVO "]),
    ("Padaria", ["PAO", "FRANCES", "CACHORRO QUENTE", "HAMBURGUER PAO", "BOLO", "CUCA", "SONHO", "CROISSANT", "TORRADA", "TORTA", "SALGADO", "COXINHA", "PASTEL", "EMPADA"]),
    ("Limpeza", ["DETERGENTE", "SABAO", "AMACIANTE", "AGUA SANITARIA", "DESINFETANTE", "ESPONJA", "LIMPADOR", "ALVEJANTE", "SACO LIXO", "PAPEL TOALHA", "GUARDANAPO", "LUSTRA", "MULTIUSO", "INSETICIDA"]),
    ("Higiene & Perfumaria", ["SABONETE", "SHAMPOO", "CONDICIONADOR", "CREME DENTAL", "PASTA DE DENTE", "ESCOVA DENTAL", "DESODORANTE", "PAPEL HIGIENICO", "FRALDA", "ABSORVENTE", "PERFUME", "COLONIA", "HIDRATANTE", "PROTETOR SOLAR", "BARBEAR", "ESTJ", "EGEO", "BOTICARIO", "NATURA"]),
    ("Combustível / Veículo", ["GASOLINA", "ETANOL", "DIESEL", "GNV", "OLEO MOTOR", "ADITIVO", "LAVA", "ESTACIONAMENTO", "PNEU"]),
    ("Farmácia / Saúde", ["DIPIRONA", "PARACETAMOL", "IBUPROFENO", "REMEDIO", "MEDICAMENTO", "VITAMINA", "CURATIVO", "ALCOOL GEL", "TERMOMETRO", "FARMACIA"]),
    ("Restaurante", ["PIZZA", "LANCHE", "XIS ", "REFEICAO", "ALMOCO", "JANTAR", "BUFFET", "MARMITA", "SORVETE", "ACAI"]),
    ("Despensa / Básico", ["ARROZ", "FEIJAO", "ACUCAR", "CAFE", "LEITE", "OLEO DE SOJA", "AZEITE", "FARINHA", "MACARRAO", "MASSA", "MOLHO DE TOMATE", "EXTRATO", "MAIONESE", "KETCHUP", "MOSTARDA", "TEMPERO", "SAL ", "BISCOITO", "BOLACHA", "WAFER", "ACHOCOLATADO", "LEITE CONDENSADO", "CREME DE LEITE", "QUEIJO", "MUSSARELA", "MANTEIGA", "MARGARINA", "REQUEIJAO", "CEREAL", "AVEIA"]),
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aprendidas (
            descricao_norm TEXT PRIMARY KEY,
            categoria TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def normalizar_texto(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").strip().upper())


def buscar_categoria_aprendida(descricao: str):
    norm = normalizar_texto(descricao)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT categoria FROM aprendidas WHERE descricao_norm = ?", (norm,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def salvar_categoria_aprendida(descricao: str, categoria: str):
    norm = normalizar_texto(descricao)
    if not norm:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO aprendidas (descricao_norm, categoria) VALUES (?, ?)",
        (norm, categoria),
    )
    conn.commit()
    conn.close()


def classificar_produto(descricao: str) -> str:
    aprendida = buscar_categoria_aprendida(descricao)
    if aprendida:
        return aprendida

    desc_up = normalizar_texto(descricao)
    for categoria, palavras in REGRAS_CATEGORIAS:
        for palavra in palavras:
            if palavra in desc_up:
                return categoria
    return "Outros"


def url_sefaz_valida(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return any(host == dom or host.endswith("." + dom) for dom in DOMINIOS_SEFAZ)
    except Exception:
        return False


def baixar_html(url: str) -> str:
    if not url_sefaz_valida(url):
        raise HTTPException(status_code=400, detail="URL inválida ou fora dos domínios oficiais da SEFAZ/RS.")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url.strip(), headers=headers, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao acessar a SEFAZ: {str(e)}")

    if not url_sefaz_valida(resp.url):
        raise HTTPException(status_code=400, detail="A SEFAZ redirecionou para um domínio não permitido.")

    return resp.text


def _extrair_produtos(soup: BeautifulSoup) -> list:
    produtos = []
    linhas = soup.select('tr[id^="Item"], tr.NFCDetalhe_Item')

    for tr in linhas:
        tit_el = tr.select_one(".txtTit")
        if not tit_el:
            continue
        descricao = tit_el.get_text(" ", strip=True)

        cod_el = tr.select_one(".RCod")
        codigo = ""
        if cod_el:
            m_cod = re.search(r"(\d+)", cod_el.get_text(" ", strip=True))
            if m_cod:
                codigo = m_cod.group(1)

        qtd_el = tr.select_one(".Rqtd")
        quantidade = "1"
        if qtd_el:
            m_qtd = re.search(r"([\d.,]+)", qtd_el.get_text(" ", strip=True))
            if m_qtd:
                quantidade = m_qtd.group(1)

        un_el = tr.select_one(".RUN")
        unidade = "UN"
        if un_el:
            txt_un = un_el.get_text(" ", strip=True).replace("UN:", "").strip()
            if txt_un:
                unidade = txt_un

        vunit_el = tr.select_one(".RvlUnit")
        valor_unitario = "0,00"
        if vunit_el:
            m_vu = re.search(r"([\d.,]+)", vunit_el.get_text(" ", strip=True))
            if m_vu:
                valor_unitario = m_vu.group(1)

        vtot_el = tr.select_one(".valor")
        valor_total = vtot_el.get_text(" ", strip=True) if vtot_el else valor_unitario

        produtos.append(
            {
                "codigo": codigo,
                "descricao": descricao,
                "categoria": classificar_produto(descricao),
                "quantidade": quantidade,
                "unidade": unidade,
                "valor_unitario": valor_unitario,
                "valor_total": valor_total,
            }
        )

    return produtos


def parsear_nfce(html: str, url_original: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    texto_completo = soup.get_text("\n", strip=True)

    estab_el = soup.select_one("#u20, .txtTopo")
    estabelecimento = estab_el.get_text(" ", strip=True) if estab_el else "Estabelecimento"

    cnpj = "Não identificado"
    m_cnpj = re.search(r"CNPJ:\s*([\d./-]+)", texto_completo)
    if m_cnpj:
        cnpj = m_cnpj.group(1)

    data_emissao = "Não identificada"
    m_data = re.search(r"Emiss[aã]o:\s*(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)", texto_completo)
    if m_data:
        data_emissao = m_data.group(1)

    chave_acesso = ""
    m_chave_url = re.search(r"p=(\d{44})", url_original)
    if m_chave_url:
        chave_acesso = m_chave_url.group(1)
    else:
        chave_el = soup.select_one(".chave")
        if chave_el:
            chave_acesso = re.sub(r"\D", "", chave_el.get_text())

    valor_total_nota = "0,00"
    tot_el = soup.select_one(".totalNumb.txtMax, #linhaTotal .totalNumb")
    if tot_el:
        valor_total_nota = tot_el.get_text(" ", strip=True)

    produtos = _extrair_produtos(soup)
    if not produtos:
        raise HTTPException(status_code=422, detail="Nenhum item encontrado na nota (layout da SEFAZ pode ter mudado).")

    return {
        "chave_acesso": chave_acesso,
        "compra": {
            "estabelecimento": estabelecimento,
            "cnpj": cnpj,
            "data_emissao": data_emissao,
            "quantidade_itens": len(produtos),
            "valor_total_nota": valor_total_nota,
            "produtos": produtos,
        },
    }


class ProcessarRequest(BaseModel):
    url: str


class CorrigirRequest(BaseModel):
    descricao: str
    categoria: str


class SalvarSheetsRequest(BaseModel):
    webhook_url: str
    chave_acesso: str = ""
    estabelecimento: str = ""
    cnpj: str = ""
    data_emissao: str = ""
    valor_total_nota: str = "0,00"
    forma_pagamento: str = "À vista"
    parcelas: int = 1
    produtos: list = []


class CompraOnlineRequest(BaseModel):
    webhook_url: str
    descricao: str
    data_emissao: str = ""
    categoria: str = "Outros"
    valor_total_nota: str = "0,00"
    forma_pagamento: str = "Crédito"
    parcelas: int = 1


class DashboardRequest(BaseModel):
    webhook_url: str


class ExcluirRequest(BaseModel):
    webhook_url: str
    identificador: str


@app.get("/")
def health_check():
    return {"status": "Servidor rodando com sucesso!", "versao": "6.1.0"}


@app.post("/api/processar-nfce")
def processar_nfce(req: ProcessarRequest):
    html = baixar_html(req.url)
    dados = parsear_nfce(html, req.url)
    return {"sucesso": True, "dados_extraidos": dados}


@app.post("/api/corrigir-categoria")
def corrigir_categoria(req: CorrigirRequest):
    salvar_categoria_aprendida(req.descricao, req.categoria)
    return {"sucesso": True}


@app.post("/api/salvar-sheets")
def salvar_sheets(req: SalvarSheetsRequest):
    payload = req.model_dump()
    payload["tipo"] = "nfce"
    try:
        resp = requests.post(req.webhook_url.strip(), json=payload, timeout=25)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao comunicar com Google Sheets: {str(e)}")


@app.post("/api/salvar-compra-online")
def salvar_compra_online(req: CompraOnlineRequest):
    payload = req.model_dump()
    payload["tipo"] = "online"
    try:
        resp = requests.post(req.webhook_url.strip(), json=payload, timeout=25)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao salvar compra online: {str(e)}")


@app.post("/api/dados-dashboard")
def dados_dashboard(req: DashboardRequest):
    try:
        resp = requests.get(req.webhook_url.strip(), timeout=25)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao ler dados da planilha: {str(e)}")


@app.post("/api/excluir-lancamento")
def excluir_lancamento(req: ExcluirRequest):
    payload = {
        "tipo": "excluir",
        "identificador": req.identificador,
    }
    try:
        resp = requests.post(req.webhook_url.strip(), json=payload, timeout=25)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao excluir lançamento: {str(e)}")
