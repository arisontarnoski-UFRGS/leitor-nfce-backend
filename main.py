"""
API de Leitura NFC-e, Compras Online e Faturas  (v6.0.1)
"""
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from decimal import Decimal, InvalidOperation
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

VERSAO = "6.0.1"

ORIGENS_PERMITIDAS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
DOMINIOS_SEFAZ = tuple(
    d.strip().lower()
    for d in os.getenv(
        "SEFAZ_DOMINIOS",
        "sefaz.rs.gov.br,svrs.rs.gov.br,dfe-portal.svrs.rs.gov.br,dfe.svrs.rs.gov.br,fazenda.rs.gov.br"
    ).split(",")
    if d.strip()
)
DB_PATH = os.getenv("CATEGORIAS_DB", "categorias_aprendidas.db")

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

app = FastAPI(
    title="API de Leitura NFC-e, Compras Online e Faturas",
    description="Backend para raspagem SEFAZ/RS, controle de parcelas e integração com Google Sheets",
    version=VERSAO,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENS_PERMITIDAS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NfceRequest(BaseModel):
    url: str


class DashboardRequest(BaseModel):
    webhook_url: str


class ProdutoItem(BaseModel):
    codigo: Optional[str] = ""
    descricao: str
    quantidade: str
    unidade: str
    valor_unitario: str
    valor_total: str
    categoria: Optional[str] = "Outros"


class SheetsPayload(BaseModel):
    webhook_url: str
    chave_acesso: str
    estabelecimento: str
    cnpj: str
    data_emissao: str
    valor_total_nota: str
    forma_pagamento: str = "À vista"
    parcelas: int = 1
    produtos: List[ProdutoItem]


class CompraOnlinePayload(BaseModel):
    webhook_url: str
    descricao: str
    data_emissao: str
    categoria: str
    valor_total_nota: str
    forma_pagamento: str = "Crédito"
    parcelas: int = 1


class CorrecaoCategoria(BaseModel):
    descricao: str
    categoria: str


def normalizar_texto(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    texto_limpo = re.sub(r"[^A-Z0-9/ ]+", " ", sem_acento.upper())
    return re.sub(r"\s+", " ", texto_limpo).strip()


def brl_para_decimal(valor) -> Optional[Decimal]:
    if valor is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(valor))
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def para_float(valor) -> Optional[float]:
    d = brl_para_decimal(valor)
    return float(d) if d is not None else None


def modelo_para_dict(item: BaseModel) -> dict:
    return item.model_dump() if hasattr(item, "model_dump") else item.dict()


REGRAS_LOJA_PRIORITARIA_BRUTAS = [
    ("Restaurante", ["RESTAURANTE", "CHURRASCARIA", "PIZZARIA", "HAMBURGUERIA", "LANCHERIA",
                     "LANCHONETE", "BISTRO", "SUSHI", "CANTINA"]),
    ("Lazer / Viagens", ["HOTEL", "POUSADA", "RESORT", "CINEMA", "PARQUE", "TURISMO", "LIVRARIA"]),
    ("Higiene & Perfumaria", ["BOTICARIO", "NATURA"]),
]

REGRAS_PRODUTO_BRUTAS = [
    ("Combustível / Veículo", [
        "GASOLINA", "ETANOL", "DIESEL", "GNV", "ADITIVO", "OLEO MOTOR",
        "LUBRIFICANTE", "AUTO PECAS", "ESTACIONAMENTO", "PNEU", "LAVAGEM", "PEDAGIO"]),
    ("Farmácia / Saúde", [
        "DIPIRONA", "PARACETAMOL", "IBUPROFENO", "REMEDIO", "VITAMINA",
        "CURATIVO", "ALGODAO", "SERINGA", "MEDICAMENTO", "POMADA", "XAROPE"]),
    ("Limpeza", [
        "DETERGENTE", "SABAO", "AGUA SANITARIA", "DESINFETANTE", "AMACIANTE",
        "ESPONJA", "LIMPADOR", "ALVEJANTE", "SACO LIXO", "MULTIUSO", "CLORO",
        "LUSTRA MOVEIS", "INSETICIDA", "VASSOURA", "RODO"]),
    ("Higiene & Perfumaria", [
        "PERFUME", "ESTJ", "EGEO", "COLONIA",
        "HIDRATANTE", "SHAMPOO", "CONDICIONADOR", "SABONETE", "CREME DENTAL",
        "DESODORANTE", "PAPEL HIGIENICO", "ESCOVA DENTAL", "FIO DENTAL",
        "ABSORVENTE", "FRALDA", "BARBEAR", "PROTETOR SOLAR"]),
    ("Açougue", [
        "CHICKEN", "FILEZINHO", "SASSAMI", "CARNE", "BOV", "BOVINA", "SUINA",
        "SUINO", "FRANGO", "PEITO FRANGO", "COXA", "SOBRECOXA", "ASA",
        "COSTELA", "PICANHA", "ALCATRA", "CONTRA FILE", "PATINHO", "COXAO",
        "ACEM", "PALETA", "MUSCULO", "MOIDA", "LINGUICA", "SALSICHA",
        "BACON", "PEIXE", "FILE TILAPIA", "SALMAO", "HAMBURGUER", "CORACAO",
        "FIGADO", "CHARQUE", "SALSICHAO", "GALETO"]),
    ("Bebidas", [
        "REFRESCO", "REFRIGERANTE", "COCA COLA", "GUARANA", "PEPSI",
        "FANTA", "SPRITE", "SUCO", "NECTAR", "AGUA MINERAL", "AGUA C/ GAS",
        "AGUA S/ GAS", "CERVEJA", "CHOPP", "VINHO", "ESPUMANTE",
        "ENERGETICO", "ISOTONICO", "CHA GELADO", "MATE LEAO", "VODKA",
        "WHISKY", "CACHACA", "H2OH"]),
    ("Hortifrúti", [
        "BANANA", "MACA", "LARANJA", "LIMAO", "MAMAO", "MELANCIA", "MELAO",
        "UVA", "MORANGO", "ABACAXI", "MANGA", "PERA", "ABACATE", "TOMATE",
        "BATATA", "CEBOLA", "ALHO", "CENOURA", "BETERRABA", "ALFACE",
        "RUCULA", "COUVE", "BROCOLIS", "COUVE FLOR", "REPOLHO", "PEPINO",
        "PIMENTAO", "ABOBRINHA", "MORANGA", "MANDIOCA", "AIPIM",
        "TEMPERO VERDE", "CHEIRO VERDE", "HORTIFRUTI", "VERDURA", "LEGUME", "FRUTA"]),
    ("Padaria", [
        "PAO", "PAES", "FRANCES", "CACETINHO", "PAO FORMA", "PAO INTEGRAL",
        "BOLO", "CUCA", "SONHO", "CROISSANT", "ROSCA", "BISNAGUINHA",
        "TORRADA", "TORTA", "SALGADO", "COXINHA", "PASTEL", "EMPADA",
        "SANDUICHE", "BROA", "CHIPAS", "PAO DE QUEIJO"]),
    ("Restaurante", [
        "REFEICAO", "BUFFET", "PRATO FEITO", "ALMOCO", "JANTAR", "MARMITA",
        "MARMITEX", "PIZZA", "XIS", "LANCHE", "HAMBURGUERIA", "SUSHI",
        "TEMAKI", "CHURRASCO ESPETO", "PORCAO", "BATATA FRITA", "SORVETE",
        "ACAI", "CAFE EXPRESSO", "CAPPUCCINO", "SELF SERVICE", "RODIZIO"]),
    ("Lazer / Viagens", [
        "INGRESSO", "CINEMA", "TEATRO", "SHOW", "MUSEU", "PARQUE",
        "DIARIA", "HOSPEDAGEM", "HOTEL", "POUSADA", "RESORT", "PASSAGEM",
        "BRINQUEDO", "JOGO", "LIVRO", "PAPELARIA", "CAMPING", "PASSEIO"]),
    ("Calçados", [
        "TENIS", "SAPATO", "SANDALIA", "BOTA", "CHINELO", "CHUTEIRA",
        "MOCASSIM", "RASTEIRINHA", "SCARPIN", "TAMANCO", "SAPATENIS",
        "CALCADO", "PALMILHA"]),
    ("Vestuário", [
        "CAMISETA", "CALCA", "BERMUDA", "VESTIDO", "BLUSA", "MEIA",
        "CUECA", "CALCINHA", "JAQUETA", "MOLETOM", "CAMISA", "SHORT",
        "SAIA", "CASACO"]),
    ("Casa & Utilidades", [
        "LAMPADA", "PILHA", "FERRAMENTA", "TINTA", "MASSA CORRIDA", "LIXA",
        "CHAVE", "PARAFUSO", "FIO", "TOMADA", "UTENSILIO", "POTE",
        "PANELA", "COPO", "PRATO", "TALHER", "GUARDANAPO", "PAPEL TOALHA",
        "PAPEL ALUMINIO"]),
    ("Despensa / Básico", [
        "ACUCAR", "OLEO", "OLEO DE SOJA", "OLEO SOJA", "ARROZ", "FEIJAO",
        "LEITE", "CAFE", "ERVA MATE", "CHIMARRAO", "MACARRAO", "MASSA",
        "MASSA FRESCA", "AZEITE", "SAL", "FARINHA", "TRIGO", "MILHO",
        "FUBA", "AVEIA", "GRANOLA", "MOLHO", "MOLHO DE TOMATE",
        "MOLHO TOMATE", "EXTRATO TOMATE", "CREME DE LEITE", "LEITE CONDENSADO",
        "ACHOCOLATADO", "NESCAU", "TODDY", "BISCOITO", "BOLACHA", "WAFER",
        "BEBIDA LACTEA", "IOGURTE", "MANTEIGA", "MARGARINA", "QUEIJO",
        "PRESUNTO", "MORTADELA", "REQUEIJAO", "OVO", "OVOS", "ATUM",
        "SARDINHA", "MILHO VERDE", "ERVILHA", "MAIONESE", "KETCHUP",
        "MOSTARDA", "TEMPERO", "CALDO", "FERMENTO",
        "CHOCOLATE", "BOMBOM", "SALGADINHO", "PIPOCA", "GELEIA"]),
]

REGRAS_LOJA_BRUTAS = [
    ("Combustível / Veículo", ["POSTO", "COMBUSTIV", "AUTO PECAS"]),
    ("Farmácia / Saúde", ["FARMACIA", "DROGARIA"]),
    ("Higiene & Perfumaria", ["PERFUM", "TOKKY"]),
    ("Açougue", ["ACOUGUE", "CASA DE CARNES", "BOUTIQUE DE CARNES"]),
    ("Bebidas", ["ADEGA", "DISTRIBUIDORA DE BEBIDAS", "CONVENIENCIA"]),
    ("Hortifrúti", ["FRUTEIRA", "HORTIFRUTI", "SACOLAO"]),
    ("Padaria", ["PADARIA", "PANIFICADORA", "CONFEITARIA"]),
    ("Calçados", ["CALCADO", "SAPATARIA", "TENIS"]),
    ("Vestuário", ["MODA", "CONFECCOES", "ROUPA"]),
    ("Casa & Utilidades", ["FERRAGEM", "CONSTRUCAO", "UTILIDADES"]),
    ("Despensa / Básico", ["SUPERMERCADO", "MERCADO", "ATACADAO", "MINI MERCADO", "ALIMENTICIOS"]),
]


class Regra(NamedTuple):
    categoria: str
    termo: str
    palavras: int
    ordem: int
    padrao: "re.Pattern"


def _compilar(regras_brutas) -> List[Regra]:
    compiladas: List[Regra] = []
    for ordem, (categoria, termos) in enumerate(regras_brutas):
        for termo in termos:
            tn = normalizar_texto(termo)
            if not tn:
                continue
            padrao = re.compile(r"(?<![A-Z0-9])" + re.escape(tn) + r"S?(?![A-Z0-9])")
            compiladas.append(Regra(categoria, tn, len(tn.split()), ordem, padrao))
    return compiladas


REGRAS_LOJA_PRIORITARIA = _compilar(REGRAS_LOJA_PRIORITARIA_BRUTAS)
REGRAS_PRODUTO = _compilar(REGRAS_PRODUTO_BRUTAS)
REGRAS_LOJA = _compilar(REGRAS_LOJA_BRUTAS)

CATEGORIAS: List[str] = []
for _lista in (REGRAS_PRODUTO_BRUTAS, REGRAS_LOJA_PRIORITARIA_BRUTAS, REGRAS_LOJA_BRUTAS):
    for _cat, _ in _lista:
        if _cat not in CATEGORIAS:
            CATEGORIAS.append(_cat)
CATEGORIAS.append("Outros")


def melhor_categoria(texto_norm: str, regras: List[Regra]) -> Optional[str]:
    if not texto_norm:
        return None
    melhor: Optional[Tuple[tuple, str]] = None
    for r in regras:
        m = r.padrao.search(texto_norm)
        if not m:
            continue
        chave = (-r.palavras, m.start(), r.ordem)
        if melhor is None or chave < melhor[0]:
            melhor = (chave, r.categoria)
    return melhor[1] if melhor else None


def chave_memoria(descricao: str) -> str:
    """Normaliza e remove apenas números/medidas isoladas, preservando marcas com números (ex.: H2OH, 3 CORACOES)."""
    texto = normalizar_texto(descricao)
    texto_sem_medidas = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:ML|L|G|KG|MG|UN|UND|PCT|CX|PACK)?\b",
        " ",
        texto
    )
    return re.sub(r"\s+", " ", texto_sem_medidas).strip()


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS aprendidas (chave TEXT PRIMARY KEY, categoria TEXT NOT NULL)")
    return con


def carregar_memoria() -> Dict[str, str]:
    try:
        with closing(_conectar()) as con:
            return dict(con.execute("SELECT chave, categoria FROM aprendidas").fetchall())
    except sqlite3.Error:
        return {}


def salvar_memoria(descricao: str, categoria: str) -> None:
    chave = chave_memoria(descricao)
    if not chave:
        raise ValueError("Descrição inválida para aprendizado.")
    with closing(_conectar()) as con, con:
        con.execute(
            "INSERT INTO aprendidas(chave, categoria) VALUES(?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET categoria = excluded.categoria",
            (chave, categoria),
        )


def classificar(descricao: str, estabelecimento: str,
                memoria: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    desc = normalizar_texto(descricao)
    loja = normalizar_texto(estabelecimento)

    if memoria:
        cat = memoria.get(chave_memoria(descricao))
        if cat:
            return cat, "memoria"

    cat = melhor_categoria(loja, REGRAS_LOJA_PRIORITARIA)
    if cat:
        return cat, "loja_prioritaria"

    cat = melhor_categoria(desc, REGRAS_PRODUTO)
    if cat:
        return cat, "produto"

    cat = melhor_categoria(loja, REGRAS_LOJA)
    if cat:
        return cat, "loja"

    return "Outros", "padrao"


def sugerir_categoria(descricao: str, estabelecimento: str) -> str:
    return classificar(descricao, estabelecimento)[0]


def url_sefaz_valida(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in DOMINIOS_SEFAZ)


def validar_webhook(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() != "script.google.com":
        raise HTTPException(status_code=400, detail="Informe uma URL válida (/exec) do Google Apps Script.")
    return url


def chamar_webhook(metodo: str, url: str, payload: Optional[dict] = None):
    try:
        if metodo == "POST":
            resposta = requests.post(url, json=payload, timeout=20)
        else:
            resposta = requests.get(url, timeout=20)
        resposta.raise_for_status()
        return resposta.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao falar com o Google Apps Script: {e}")
    except ValueError:
        raise HTTPException(status_code=502, detail="O Apps Script não retornou JSON válido (verifique a implantação).")


def baixar_html(sessao: requests.Session, url: str) -> BeautifulSoup:
    if not url_sefaz_valida(url):
        raise ValueError("URL fora dos domínios permitidos da SEFAZ.")
    resposta = sessao.get(url, headers=HEADERS_HTTP, timeout=15)
    resposta.raise_for_status()
    if not url_sefaz_valida(resposta.url):
        raise ValueError("A SEFAZ redirecionou para um domínio não permitido.")
    return BeautifulSoup(resposta.content, "html.parser")


def _extrair_produtos(soup: BeautifulSoup, estabelecimento: str, memoria: Dict[str, str]) -> List[dict]:
    produtos: List[dict] = []

    def adicionar(codigo, descricao, quantidade, unidade, valor_unitario, valor_total):
        categoria, origem = classificar(descricao, estabelecimento, memoria)
        produtos.append({
            "codigo": codigo,
            "descricao": descricao,
            "quantidade": quantidade,
            "unidade": unidade,
            "valor_unitario": valor_unitario,
            "valor_total": valor_total,
            "valor_total_num": para_float(valor_total),
            "categoria": categoria,
            "categoria_origem": origem,
        })

    for linha in soup.find_all("tr", id=re.compile(r"^Item")):
        nome = linha.find("span", class_="txtTit") or linha.find("span", class_="txtTit2")
        codigo = linha.find("span", class_="RCod")
        qtd = linha.find("span", class_="Rqtd")
        un = linha.find("span", class_="RUN")
        vl_unit = linha.find("span", class_="RvlUnit")
        vl_total = linha.find("span", class_="valor")
        adicionar(
            re.sub(r"[^\d]", "", codigo.get_text()) if codigo else "",
            nome.get_text(strip=True) if nome else "Item",
            qtd.get_text(strip=True).replace("Qtde.:", "").strip() if qtd else "1",
            un.get_text(strip=True).replace("UN:", "").strip() if un else "UN",
            vl_unit.get_text(strip=True).replace("Vl. Unit.:", "").strip() if vl_unit else "0,00",
            vl_total.get_text(strip=True) if vl_total else "0,00",
        )

    if not produtos:
        for tr in soup.find_all("tr", class_=re.compile(r"NFCDetalhe_Item")):
            c = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(c) >= 6:
                adicionar(c[0], c[1], c[2], c[3], c[4], c[5])

    return produtos


def resumir_por_categoria(produtos: List[dict]) -> Dict[str, float]:
    soma: Dict[str, Decimal] = {}
    for p in produtos:
        v = brl_para_decimal(p["valor_total"]) or Decimal("0")
        soma[p["categoria"]] = soma.get(p["categoria"], Decimal("0")) + v
    ordenado = sorted(soma.items(), key=lambda kv: kv[1], reverse=True)
    return {k: float(v.quantize(Decimal("0.01"))) for k, v in ordenado}


def raspar_dados_sefaz_rs(url_qrcode: str) -> dict:
    sessao = requests.Session()
    soup = baixar_html(sessao, url_qrcode)

    iframe = soup.find("iframe", id="iframeConteudo") or soup.find("iframe")
    if iframe and iframe.get("src"):
        url_interna = urljoin("https://www.sefaz.rs.gov.br/NFCE/", iframe.get("src"))
        soup = baixar_html(sessao, url_interna)

    texto = soup.get_text(" ", strip=True)

    emitente_el = (soup.find("div", id="u20") or soup.find("div", class_="txtTopo")
                   or soup.find("td", class_="NFCCabecalho_SubTitulo"))
    estabelecimento = emitente_el.get_text(strip=True) if emitente_el else "Estabelecimento não identificado"

    m_cnpj = (re.search(r"CNPJ\s*:\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", texto, re.IGNORECASE)
              or re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto))
    cnpj = m_cnpj.group(1) if m_cnpj else "Não identificado"

    m_data = (re.search(r"Emiss[aã]o\s*:?\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})", texto, re.IGNORECASE)
              or re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})", texto)
              or re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", texto))
    data_emissao = m_data.group(1) if m_data else "Não identificada"

    produtos = _extrair_produtos(soup, estabelecimento, carregar_memoria())

    total_el = soup.find("span", class_="totalNumb txtMax")
    if total_el:
        valor_total_nota = total_el.get_text(strip=True)
    else:
        m_total = re.search(r"Valor\s+a\s+pagar\s*R\$\s*:?\s*([\d.,]+)", texto, re.IGNORECASE)
        valor_total_nota = m_total.group(1) if m_total else "0,00"

    return {
        "estabelecimento": estabelecimento,
        "cnpj": cnpj,
        "data_emissao": data_emissao,
        "valor_total_nota": valor_total_nota,
        "valor_total_nota_num": para_float(valor_total_nota),
        "quantidade_itens": len(produtos),
        "itens_sem_categoria": sum(1 for p in produtos if p["categoria"] == "Outros"),
        "resumo_por_categoria": resumir_por_categoria(produtos),
        "produtos": produtos,
    }


@app.get("/")
def home():
    return {"status": "Servidor rodando com sucesso!", "versao": VERSAO}


@app.get("/api/categorias")
def listar_categorias():
    return {"categorias": CATEGORIAS}


@app.post("/api/processar-nfce")
def processar_nfce(dados: NfceRequest):
    url_recebida = dados.url.strip()
    if not url_recebida:
        raise HTTPException(status_code=400, detail="A URL da NFC-e não foi informada.")
    if not url_sefaz_valida(url_recebida):
        raise HTTPException(status_code=400, detail="O conteúdo escaneado não parece ser uma NFC-e válida da SEFAZ.")

    chave_acesso = "Não identificada"
    match_p = re.search(r"[?&]p=([^&]+)", url_recebida)
    if match_p:
        chave_acesso = match_p.group(1).split("|")[0] or chave_acesso
    else:
        match_chave = re.search(r"\d{44}", url_recebida)
        if match_chave:
            chave_acesso = match_chave.group(0)

    try:
        dados_sefaz = raspar_dados_sefaz_rs(url_recebida)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"A SEFAZ não respondeu corretamente: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar a nota: {e}")

    if not dados_sefaz["produtos"]:
        raise HTTPException(status_code=422, detail="Nenhum item encontrado na nota (layout da SEFAZ pode ter mudado).")

    return {
        "sucesso": True,
        "dados_extraidos": {"url": url_recebida, "chave_acesso": chave_acesso, "compra": dados_sefaz},
    }


@app.post("/api/corrigir-categoria")
def corrigir_categoria(payload: CorrecaoCategoria):
    if payload.categoria not in CATEGORIAS:
        raise HTTPException(status_code=400, detail=f"Categoria inválida. Use uma de: {', '.join(CATEGORIAS)}")
    try:
        salvar_memoria(payload.descricao, payload.categoria)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Não foi possível salvar a correção: {e}")
    return {"sucesso": True, "chave": chave_memoria(payload.descricao), "categoria": payload.categoria}


@app.post("/api/salvar-sheets")
def salvar_no_google_sheets(payload: SheetsPayload):
    url_script = validar_webhook(payload.webhook_url)
    dados_envio = {
        "tipo": "nfce",
        "chave_acesso": payload.chave_acesso,
        "estabelecimento": payload.estabelecimento,
        "cnpj": payload.cnpj,
        "data_emissao": payload.data_emissao,
        "valor_total_nota": payload.valor_total_nota,
        "forma_pagamento": payload.forma_pagamento,
        "parcelas": payload.parcelas,
        "produtos": [modelo_para_dict(item) for item in payload.produtos],
    }
    return chamar_webhook("POST", url_script, dados_envio)


@app.post("/api/salvar-compra-online")
def salvar_compra_online(payload: CompraOnlinePayload):
    url_script = validar_webhook(payload.webhook_url)
    dados_envio = {
        "tipo": "online",
        "descricao": payload.descricao,
        "data_emissao": payload.data_emissao,
        "categoria": payload.categoria,
        "valor_total_nota": payload.valor_total_nota,
        "forma_pagamento": payload.forma_pagamento,
        "parcelas": payload.parcelas,
    }
    return chamar_webhook("POST", url_script, dados_envio)


@app.post("/api/dados-dashboard")
def buscar_dados_dashboard(payload: DashboardRequest):
    return chamar_webhook("GET", validar_webhook(payload.webhook_url))