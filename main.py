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

VERSAO = "6.2.0"

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
DB_PATH = os.getenv("CATEGORIAS_DB", "categorias_aprendidas.db")
DOMINIOS_SEFAZ = tuple(
    d.strip().lower()
    for d in os.getenv(
        "SEFAZ_DOMINIOS",
        "sefaz.rs.gov.br,svrs.rs.gov.br,dfe-portal.svrs.rs.gov.br,dfe.svrs.rs.gov.br,fazenda.rs.gov.br",
    ).split(",")
    if d.strip()
)
HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

app = FastAPI(title="Leitor NFC-e & Faturas API", version=VERSAO)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def normalizar_texto(texto: str) -> str:
    """Maiúsculas, sem acentos, sem pontuação (mantém / para 'C/ GAS')."""
    nfkd = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = re.sub(r"[^A-Z0-9/ ]+", " ", sem_acento.upper())
    return re.sub(r"\s+", " ", limpo).strip()


def brl_para_decimal(valor) -> Optional[Decimal]:
    """'1.234,56' / 'R$ 12,90' -> Decimal. None se não for número."""
    if valor is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(valor))
    if not re.search(r"\d", s):
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


def formatar_brl(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):.2f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# Regras de categorização
# --------------------------------------------------------------------------- #
REGRAS_LOJA_PRIORITARIA_BRUTAS = [
    ("Restaurante", ["RESTAURANTE", "CHURRASCARIA", "PIZZARIA", "HAMBURGUERIA", "LANCHERIA",
                     "LANCHONETE", "BISTRO", "SUSHI", "CANTINA"]),
    ("Lazer / Viagens", ["HOTEL", "POUSADA", "RESORT", "CINEMA", "PARQUE", "TURISMO", "LIVRARIA"]),
    ("Higiene & Perfumaria", ["BOTICARIO", "NATURA"]),
]

REGRAS_PRODUTO_BRUTAS = [
    ("Açougue", ["PICANHA", "ALCATRA", "CONTRA FILE", "COXAO", "PATINHO", "COSTELA", "CARNE", "BOVIN*",
                 "SUIN*", "LOMBO", "BISTECA", "LINGUICA", "SALSICHA", "FRANGO", "SASSAMI", "FILEZINHO",
                 "COXA", "SOBRECOXA", "PEITO DE FRANGO", "CHICKEN", "PRESUNTO", "MORTADELA", "SALAME",
                 "BACON", "PEIXE", "TILAPIA", "SALMAO", "HAMBURGUER"]),
    ("Bebidas", ["REFRESCO", "SUCO", "REFRIGERANTE", "REFRIG*", "COCA", "PEPSI", "GUARANA", "FANTA",
                 "SPRITE", "AGUA MINERAL", "AGUA COM GAS", "AGUA C/ GAS", "AGUA S/ GAS", "CERVEJA",
                 "VINHO", "ESPUMANTE", "CHOPP", "ENERGETICO", "CHA", "ISOTONICO", "LEITE FERMENTADO",
                 "IOGURTE", "BEBIDA LACTEA", "NECTAR", "VODKA", "WHISKY", "CACHACA", "H2OH", "MATE LEAO"]),
    ("Hortifrúti", ["BANANA", "MACA", "LARANJA", "LIMAO", "MANGA", "MAMAO", "MELANCIA", "MELAO", "UVA",
                    "MORANGO", "ABACAXI", "PERA", "ABACATE", "TOMATE", "CEBOLA", "BATATA", "CENOURA",
                    "BETERRABA", "ALFACE", "RUCULA", "COUVE", "BROCOLIS", "REPOLHO", "PEPINO", "PIMENTAO",
                    "ABOBRINHA", "MORANGA", "MANDIOCA", "AIPIM", "ALHO", "CHEIRO VERDE", "TEMPERO VERDE",
                    "HORTIFRUTI", "VERDURA", "LEGUME", "FRUTA", "OVO"]),
    ("Padaria", ["PAO", "PAES", "FRANCES", "CACETINHO", "CACHORRO QUENTE", "HAMBURGUER PAO", "BOLO", "CUCA",
                 "SONHO", "CROISSANT", "ROSCA", "BISNAGUINHA", "TORRADA", "TORTA", "SALGADO", "COXINHA",
                 "PASTEL", "EMPADA", "SANDUICHE", "BROA", "CHIPA", "PAO DE QUEIJO"]),
    ("Limpeza", ["DETERGENTE", "SABAO", "AMACIANTE", "AGUA SANITARIA", "DESINFETANTE", "ESPONJA", "LIMPADOR",
                 "ALVEJANTE", "SACO LIXO", "PAPEL TOALHA", "GUARDANAPO", "LUSTRA MOVEIS", "MULTIUSO",
                 "INSETICIDA", "CLORO", "VASSOURA", "RODO"]),
    ("Higiene & Perfumaria", ["SABONETE", "SHAMPOO", "CONDICIONADOR", "CREME DENTAL", "PASTA DE DENTE",
                              "ESCOVA DENTAL", "FIO DENTAL", "DESODORANTE", "PAPEL HIGIENICO", "FRALDA",
                              "ABSORVENTE", "PERFUME", "COLONIA", "HIDRATANTE", "PROTETOR SOLAR", "BARBEAR",
                              "ESTJ", "EGEO", "BOTICARIO", "NATURA"]),
    ("Combustível / Veículo", ["GASOLINA", "ETANOL", "DIESEL", "GNV", "OLEO MOTOR", "ADITIVO", "LAVAGEM",
                               "LAVA JATO", "ESTACIONAMENTO", "PNEU", "LUBRIFICANTE", "AUTO PECAS", "PEDAGIO"]),
    ("Farmácia / Saúde", ["DIPIRONA", "PARACETAMOL", "IBUPROFENO", "REMEDIO", "MEDICAMENTO", "VITAMINA",
                          "CURATIVO", "ALCOOL GEL", "TERMOMETRO", "FARMACIA", "ALGODAO", "SERINGA",
                          "POMADA", "XAROPE"]),
    ("Restaurante", ["PIZZA", "LANCHE", "XIS", "REFEICAO", "ALMOCO", "JANTAR", "BUFFET", "MARMITA",
                     "MARMITEX", "SORVETE", "ACAI", "PRATO FEITO", "BATATA FRITA", "CAFE EXPRESSO",
                     "CAPPUCCINO", "SELF SERVICE", "RODIZIO", "TEMAKI", "PORCAO"]),
    ("Despensa / Básico", ["ARROZ", "FEIJAO", "ACUCAR", "CAFE", "LEITE", "OLEO", "OLEO DE SOJA", "OLEO SOJA",
                           "AZEITE", "FARINHA", "TRIGO", "MILHO", "FUBA", "MACARRAO", "MASSA", "MOLHO",
                           "MOLHO DE TOMATE", "MOLHO TOMATE", "EXTRATO", "EXTRATO TOMATE", "MAIONESE",
                           "KETCHUP", "MOSTARDA", "TEMPERO", "CALDO", "FERMENTO", "SAL", "BISCOITO", "BISC*",
                           "BOLACHA", "WAFER", "ACHOCOLATADO", "NESCAU", "TODDY", "LEITE CONDENSADO",
                           "CREME DE LEITE", "QUEIJO", "MUSSARELA", "MANTEIGA", "MARGARINA", "REQUEIJAO",
                           "CEREAL", "AVEIA", "GRANOLA", "ERVA MATE", "CHIMARRAO", "ATUM", "SARDINHA",
                           "ERVILHA", "CHOCOLATE", "BOMBOM", "SALGADINHO", "PIPOCA", "GELEIA"]),
    ("Calçados", ["TENIS", "SAPATO", "SANDALIA", "BOTA", "CHINELO", "CHUTEIRA", "MOCASSIM", "RASTEIRINHA",
                  "SCARPIN", "TAMANCO", "SAPATENIS", "CALCADO", "PALMILHA"]),
    ("Vestuário", ["CAMISETA", "CALCA", "BERMUDA", "VESTIDO", "BLUSA", "MEIA", "CUECA", "CALCINHA",
                   "JAQUETA", "MOLETOM", "CAMISA", "SHORT", "SAIA", "CASACO"]),
    ("Casa & Utilidades", ["LAMPADA", "PILHA", "FERRAMENTA", "TINTA", "MASSA CORRIDA", "LIXA", "CHAVE",
                           "PARAFUSO", "FIO", "TOMADA", "UTENSILIO", "POTE", "PANELA", "COPO", "PRATO",
                           "TALHER", "PAPEL ALUMINIO"]),
    ("Lazer / Viagens", ["INGRESSO", "CINEMA", "TEATRO", "SHOW", "MUSEU", "PARQUE", "DIARIA", "HOSPEDAGEM",
                         "HOTEL", "POUSADA", "RESORT", "PASSAGEM", "BRINQUEDO", "JOGO", "LIVRO", "PAPELARIA",
                         "CAMPING", "PASSEIO"]),
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
    palavras: int
    ordem: int
    padrao: "re.Pattern"


def _compilar(regras_brutas) -> List[Regra]:
    compiladas: List[Regra] = []
    for ordem, (categoria, termos) in enumerate(regras_brutas):
        for termo in termos:
            prefixo = termo.endswith("*")
            tn = normalizar_texto(termo)
            if not tn:
                continue
            fim = r"[A-Z0-9]*" if prefixo else r"S?(?![A-Z0-9])"
            padrao = re.compile(r"(?<![A-Z0-9])" + re.escape(tn) + fim)
            compiladas.append(Regra(categoria, len(tn.split()), ordem, padrao))
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
    """Regra mais específica: mais palavras > aparece mais cedo > ordem da categoria."""
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


# --------------------------------------------------------------------------- #
# Memória de correções (SQLite com migração automática de esquema antigo)
# --------------------------------------------------------------------------- #
def chave_memoria(descricao: str) -> str:
    """COCA COLA 2L e COCA COLA 600ML viram a mesma chave (remove tokens com números)."""
    tokens = [t for t in normalizar_texto(descricao).split() if not any(c.isdigit() for c in t)]
    return " ".join(tokens)


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS aprendidas (chave TEXT PRIMARY KEY, categoria TEXT NOT NULL)")

    colunas = [row[1] for row in con.execute("PRAGMA table_info(aprendidas)").fetchall()]
    if "descricao_norm" in colunas and "chave" not in colunas:
        antigas = con.execute("SELECT descricao_norm, categoria FROM aprendidas").fetchall()
        with con:
            con.execute("DROP TABLE aprendidas")
            con.execute("CREATE TABLE aprendidas (chave TEXT PRIMARY KEY, categoria TEXT NOT NULL)")
            for desc_antiga, cat in antigas:
                nova_chave = chave_memoria(desc_antiga)
                if nova_chave and cat:
                    con.execute(
                        "INSERT OR REPLACE INTO aprendidas(chave, categoria) VALUES(?, ?)",
                        (nova_chave, cat),
                    )
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


def classificar(descricao: str, estabelecimento: str = "",
                memoria: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """Retorna (categoria, origem)."""
    if memoria:
        cat = memoria.get(chave_memoria(descricao))
        if cat:
            return cat, "memoria"
    desc, loja = normalizar_texto(descricao), normalizar_texto(estabelecimento)
    for texto, regras, origem in (
        (loja, REGRAS_LOJA_PRIORITARIA, "loja_prioritaria"),
        (desc, REGRAS_PRODUTO, "produto"),
        (loja, REGRAS_LOJA, "loja"),
    ):
        cat = melhor_categoria(texto, regras)
        if cat:
            return cat, origem
    return "Outros", "padrao"


# --------------------------------------------------------------------------- #
# Segurança / HTTP
# --------------------------------------------------------------------------- #
def url_sefaz_valida(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
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
        resp = requests.post(url, json=payload, timeout=25) if metodo == "POST" else requests.get(url, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao falar com o Google Apps Script: {e}")
    except ValueError:
        raise HTTPException(status_code=502, detail="O Apps Script não retornou JSON válido (confira a implantação).")


def baixar_soup(sessao: requests.Session, url: str) -> BeautifulSoup:
    if not url_sefaz_valida(url):
        raise HTTPException(status_code=400, detail="URL inválida ou fora dos domínios oficiais da SEFAZ/RS.")
    try:
        resp = sessao.get(url.strip(), headers=HEADERS_HTTP, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao acessar a SEFAZ: {e}")
    if not url_sefaz_valida(resp.url):
        raise HTTPException(status_code=400, detail="A SEFAZ redirecionou para um domínio não permitido.")
    return BeautifulSoup(resp.content, "html.parser")


# --------------------------------------------------------------------------- #
# Extração
# --------------------------------------------------------------------------- #
_NUMERO = re.compile(r"\d[\d.,]*")


def _numero(el, padrao: str) -> str:
    if el is None:
        return padrao
    m = _NUMERO.search(el.get_text(" ", strip=True))
    return m.group(0) if m else padrao


def _extrair_produtos(soup: BeautifulSoup, estabelecimento: str, memoria: Dict[str, str]) -> List[dict]:
    produtos: List[dict] = []

    def adicionar(codigo, descricao, quantidade, unidade, valor_unitario, valor_total):
        categoria, origem = classificar(descricao, estabelecimento, memoria)
        produtos.append({
            "codigo": codigo,
            "descricao": descricao,
            "categoria": categoria,
            "categoria_origem": origem,
            "quantidade": quantidade,
            "unidade": unidade,
            "valor_unitario": valor_unitario,
            "valor_total": valor_total,
        })

    for tr in soup.select('tr[id^="Item"]'):
        nome = tr.select_one("span.txtTit, span.txtTit2")
        if not nome:
            continue
        cod = tr.select_one(".RCod")
        m_cod = re.search(r"\d+", cod.get_text(" ", strip=True)) if cod else None
        un = tr.select_one(".RUN")
        unidade = (un.get_text(" ", strip=True).replace("UN:", "").strip() if un else "") or "UN"
        valor_unitario = _numero(tr.select_one(".RvlUnit"), "0,00")
        vtot = tr.select_one("span.valor")
        adicionar(
            m_cod.group(0) if m_cod else "",
            nome.get_text(" ", strip=True),
            _numero(tr.select_one(".Rqtd"), "1"),
            unidade,
            valor_unitario,
            vtot.get_text(" ", strip=True) if vtot else valor_unitario,
        )

    if not produtos:
        for tr in soup.select('tr[class*="NFCDetalhe_Item"]'):
            c = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(c) >= 6:
                adicionar(c[0], c[1], c[2], c[3], c[4], c[5])
    return produtos


def extrair_total_nota(soup: BeautifulSoup, texto: str, soma_itens: Decimal) -> Tuple[str, str]:
    """Retorna (valor, origem). Prioriza o RÓTULO, nunca a posição do elemento."""
    for origem, regex in (
        ("valor_a_pagar", r"Valor\s+a\s+pagar\s*R\$\s*:?\s*(\d[\d.,]*)"),
        ("valor_total", r"Valor\s+total\s*R\$\s*:?\s*(\d[\d.,]*)"),
    ):
        m = re.search(regex, texto, re.IGNORECASE)
        if m and (brl_para_decimal(m.group(1)) or Decimal(0)) > 0:
            return m.group(1), origem

    el = soup.select_one("span.totalNumb.txtMax")
    if el and (brl_para_decimal(el.get_text()) or Decimal(0)) > 0:
        return el.get_text(strip=True), "css_txtMax"

    return formatar_brl(soma_itens), "soma_itens"


def parsear_nfce(soup: BeautifulSoup, url_original: str, memoria: Dict[str, str]) -> dict:
    texto = soup.get_text("\n", strip=True)

    estab_el = soup.select_one("#u20, .txtTopo, .NFCCabecalho_SubTitulo")
    estabelecimento = estab_el.get_text(" ", strip=True) if estab_el else "Estabelecimento"

    m_cnpj = (re.search(r"CNPJ\s*:\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", texto, re.IGNORECASE)
              or re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto))
    cnpj = m_cnpj.group(1) if m_cnpj else "Não identificado"

    m_data = (re.search(r"Emiss[aã]o\s*:?\s*(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)", texto, re.IGNORECASE)
              or re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}(?::\d{2})?)", texto))
    data_emissao = m_data.group(1) if m_data else "Não identificada"

    chave = ""
    m_p = re.search(r"[?&]p=(\d{44})", url_original) or re.search(r"(\d{44})", url_original)
    if m_p:
        chave = m_p.group(1)
    else:
        chave_el = soup.select_one(".chave")
        if chave_el:
            chave = re.sub(r"\D", "", chave_el.get_text())

    produtos = _extrair_produtos(soup, estabelecimento, memoria)
    if not produtos:
        raise HTTPException(status_code=422, detail="Nenhum item encontrado na nota (layout da SEFAZ pode ter mudado).")

    soma = sum((brl_para_decimal(p["valor_total"]) or Decimal(0) for p in produtos), Decimal(0))
    total_str, total_origem = extrair_total_nota(soup, texto, soma)
    total = brl_para_decimal(total_str) or Decimal(0)

    resumo: Dict[str, Decimal] = {}
    for p in produtos:
        resumo[p["categoria"]] = resumo.get(p["categoria"], Decimal(0)) + (brl_para_decimal(p["valor_total"]) or Decimal(0))

    return {
        "chave_acesso": chave,
        "compra": {
            "estabelecimento": estabelecimento,
            "cnpj": cnpj,
            "data_emissao": data_emissao,
            "quantidade_itens": len(produtos),
            "valor_total_nota": total_str,
            "valor_total_nota_num": float(total),
            "total_origem": total_origem,
            "soma_itens": float(soma),
            "total_confere": abs(total - soma) <= Decimal("0.05"),
            "itens_sem_categoria": sum(1 for p in produtos if p["categoria"] == "Outros"),
            "resumo_por_categoria": {
                k: float(v.quantize(Decimal("0.01"))) for k, v in sorted(resumo.items(), key=lambda kv: -kv[1])
            },
            "produtos": produtos,
        },
    }


# --------------------------------------------------------------------------- #
# Modelos
# --------------------------------------------------------------------------- #
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
    produtos: List[dict] = []


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


def _dump(m: BaseModel) -> dict:
    return m.model_dump() if hasattr(m, "model_dump") else m.dict()


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@app.get("/")
def health_check():
    return {"status": "Servidor rodando com sucesso!", "versao": VERSAO}


@app.get("/api/categorias")
def listar_categorias():
    return {"categorias": CATEGORIAS}


@app.post("/api/processar-nfce")
def processar_nfce(req: ProcessarRequest):
    sessao = requests.Session()
    soup = baixar_soup(sessao, req.url)
    memoria = carregar_memoria()

    if not soup.select('tr[id^="Item"], tr[class*="NFCDetalhe_Item"]'):
        iframe = soup.find("iframe", id="iframeConteudo") or soup.find("iframe")
        if iframe and iframe.get("src"):
            soup = baixar_soup(sessao, urljoin(req.url.strip(), iframe["src"]))

    dados = parsear_nfce(soup, req.url, memoria)
    return {"sucesso": True, "dados_extraidos": dados}


@app.post("/api/corrigir-categoria")
def corrigir_categoria(req: CorrigirRequest):
    categoria = req.categoria.strip()
    if not categoria or len(categoria) > 60:
        raise HTTPException(status_code=400, detail="Categoria inválida.")
    try:
        salvar_memoria(req.descricao, categoria)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Não foi possível salvar a correção: {e}")
    return {"sucesso": True, "chave": chave_memoria(req.descricao), "categoria": categoria}


@app.post("/api/salvar-sheets")
def salvar_sheets(req: SalvarSheetsRequest):
    url = validar_webhook(req.webhook_url)
    payload = _dump(req)
    payload["tipo"] = "nfce"
    return chamar_webhook("POST", url, payload)


@app.post("/api/salvar-compra-online")
def salvar_compra_online(req: CompraOnlineRequest):
    url = validar_webhook(req.webhook_url)
    payload = _dump(req)
    payload["tipo"] = "online"
    return chamar_webhook("POST", url, payload)


@app.post("/api/dados-dashboard")
def dados_dashboard(req: DashboardRequest):
    return chamar_webhook("GET", validar_webhook(req.webhook_url))


@app.post("/api/excluir-lancamento")
def excluir_lancamento(req: ExcluirRequest):
    url = validar_webhook(req.webhook_url)
    return chamar_webhook("POST", url, {"tipo": "excluir", "identificador": req.identificador})
