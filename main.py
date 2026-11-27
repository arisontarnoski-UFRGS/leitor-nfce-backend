"""
Leitor NFC-e & Faturas API  (v7.0.1)

Novidades em relação à 6.2.0
- Autenticação opcional por token (APP_TOKEN + header X-App-Token) nas rotas /api/*.
- URL do Apps Script pode ficar SÓ no servidor (WEBHOOK_URL): o front não precisa mais conhecê-la.
  WEBHOOK_SECRET (opcional) é repassado ao Apps Script, que recusa chamadas sem ele.
- Memória de categorias também gravada na planilha (aba Memoria_Categorias): não se perde
  em deploys do Render. SQLite continua como cache local (agora com WAL, timeout e caminho configurável).
- chave_memoria remove apenas MEDIDAS (500ML, 2L, 4UN...), não qualquer token com número.
- Validações: parcelas 1..24, valor > 0, data válida, forma de pagamento.
- SEFAZ: somente https; cada redirecionamento é validado ANTES de ser seguido.
- Total da nota: regex de valor em formato BRL estrito (com R$ opcional) + fallback em <input value="...">.
- CNPJ: não usa mais "qualquer CNPJ do texto" (evita pegar o da software house).
- brl_para_decimal: "1.234" (sem vírgula) = 1234.
- Recusas do Apps Script viram erro HTTP (409 duplicada / 400 demais), não "sucesso falso".
"""
import hmac
import logging
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

VERSAO = "7.0.1"
log = logging.getLogger("nfce")

# --------------------------------------------------------------------------- #
# Configuração (variáveis de ambiente)
# --------------------------------------------------------------------------- #
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
APP_TOKEN = os.getenv("APP_TOKEN", "").strip()
WEBHOOK_URL_ENV = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
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
FORMAS_PAGAMENTO = ("À vista", "Crédito")
MAX_PARCELAS = 24


def _caminho_db() -> str:
    if os.getenv("CATEGORIAS_DB"):
        return os.environ["CATEGORIAS_DB"]
    # Render com Persistent Disk montado em /data
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/categorias_aprendidas.db"
    return "categorias_aprendidas.db"


DB_PATH = _caminho_db()

if "*" in ALLOWED_ORIGINS:
    log.warning("ALLOWED_ORIGINS='*': defina o domínio do seu front em produção.")
if not APP_TOKEN:
    log.warning("APP_TOKEN não definido: qualquer pessoa com a URL pode usar a API.")

app = FastAPI(title="Leitor NFC-e & Faturas API", version=VERSAO)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def exigir_token(x_app_token: Optional[str] = Header(default=None)):
    """Se APP_TOKEN estiver configurado, exige o header X-App-Token."""
    if not APP_TOKEN:
        return
    recebido = (x_app_token or "").encode("utf-8")
    if not hmac.compare_digest(recebido, APP_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Senha de acesso inválida ou ausente.")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _sem_acento_upper(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


def normalizar_texto(texto: str) -> str:
    """Maiúsculas, sem acentos, sem pontuação (mantém / para 'C/ GAS')."""
    limpo = re.sub(r"[^A-Z0-9/ ]+", " ", _sem_acento_upper(texto))
    return re.sub(r"\s+", " ", limpo).strip()


def brl_para_decimal(valor) -> Optional[Decimal]:
    """'1.234,56' / 'R$ 12,90' / '1.234' -> Decimal. None se não for número."""
    if valor is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(valor))
    if not re.search(r"\d", s):
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):  # "1.234" = mil duzentos e trinta e quatro
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def formatar_brl(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):.2f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# Regras de categorização  (EDITE AQUI)
#   Termos casam por PALAVRA INTEIRA (CUCA não casa em ACUCAR) e aceitam plural com S.
#   Terminado em * = prefixo ("BOVIN*" -> BOVINA, BOVINO).
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
# Memória de correções: SQLite local + aba "Memoria_Categorias" na planilha
# --------------------------------------------------------------------------- #
_MEDIDA = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:LITROS?|LTS?|ML|L|KG|GR?|UND?|CX|PCT?|PC|MM|CM|M)\b"
)


def chave_memoria(descricao: str) -> str:
    """
    Remove só MEDIDAS: 'COCA COLA 2L' e 'COCA COLA 600ML' viram 'COCA COLA';
    'PILHA AA' e 'PILHA AAA 4UN' continuam distintas. Se sobrar vazio, usa o texto todo.
    """
    sem_medida = _MEDIDA.sub(" ", _sem_acento_upper(descricao))
    return normalizar_texto(sem_medida) or normalizar_texto(descricao)


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    with con:  # transação única: criação + migração
        con.execute("CREATE TABLE IF NOT EXISTS aprendidas (chave TEXT PRIMARY KEY, categoria TEXT NOT NULL)")
        colunas = [row[1] for row in con.execute("PRAGMA table_info(aprendidas)").fetchall()]
        if "descricao_norm" in colunas and "chave" not in colunas:
            antigas = con.execute("SELECT descricao_norm, categoria FROM aprendidas").fetchall()
            con.execute("DROP TABLE aprendidas")
            con.execute("CREATE TABLE aprendidas (chave TEXT PRIMARY KEY, categoria TEXT NOT NULL)")
            for desc_antiga, cat in antigas:
                nova = chave_memoria(desc_antiga)
                if nova and cat:
                    con.execute("INSERT OR REPLACE INTO aprendidas(chave, categoria) VALUES(?, ?)", (nova, cat))
    return con


def carregar_memoria() -> Dict[str, str]:
    try:
        with closing(_conectar()) as con:
            return dict(con.execute("SELECT chave, categoria FROM aprendidas").fetchall())
    except sqlite3.Error as e:
        log.warning("SQLite indisponível: %s", e)
        return {}


def salvar_memoria(chave: str, categoria: str) -> None:
    with closing(_conectar()) as con, con:
        con.execute(
            "INSERT INTO aprendidas(chave, categoria) VALUES(?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET categoria = excluded.categoria",
            (chave, categoria),
        )


_cache_planilha = {"url": "", "ts": 0.0, "dados": {}}
CACHE_TTL_S = 60


def memoria_da_planilha(url: Optional[str]) -> Dict[str, str]:
    """Lê a aba Memoria_Categorias via Apps Script (cache de 60 s; falha = ignora)."""
    if not url:
        return {}
    agora = time.time()
    if _cache_planilha["url"] == url and agora - _cache_planilha["ts"] < CACHE_TTL_S:
        return _cache_planilha["dados"]
    try:
        params = {"acao": "memoria"}
        if WEBHOOK_SECRET:
            params["segredo"] = WEBHOOK_SECRET
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        bruto = resp.json().get("memoria") or {}
        dados = {str(k): str(v) for k, v in bruto.items()} if isinstance(bruto, dict) else {}
        _cache_planilha.update(url=url, ts=agora, dados=dados)
    except (requests.RequestException, ValueError, AttributeError) as e:
        log.warning("Memória da planilha indisponível: %s", e)
        if _cache_planilha["url"] != url:
            return {}
    return _cache_planilha["dados"]


def memoria_completa(url: Optional[str]) -> Dict[str, str]:
    return {**carregar_memoria(), **memoria_da_planilha(url)}  # planilha prevalece


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
    return p.scheme == "https" and any(host == d or host.endswith("." + d) for d in DOMINIOS_SEFAZ)


def validar_webhook(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() != "script.google.com":
        raise HTTPException(status_code=400, detail="Informe uma URL válida (/exec) do Google Apps Script.")
    return url


def resolver_webhook(url_req: Optional[str], obrigatorio: bool = True) -> Optional[str]:
    """Prioriza a URL configurada no servidor (WEBHOOK_URL); senão usa a enviada pelo front."""
    if WEBHOOK_URL_ENV:
        return WEBHOOK_URL_ENV
    url = (url_req or "").strip()
    if not url:
        if obrigatorio:
            raise HTTPException(status_code=400, detail="Informe a URL do Apps Script (ou configure WEBHOOK_URL no servidor).")
        return None
    return validar_webhook(url)


def chamar_webhook(metodo: str, url: str, payload: Optional[dict] = None):
    try:
        if metodo == "POST":
            corpo = dict(payload or {})
            if WEBHOOK_SECRET:
                corpo["segredo"] = WEBHOOK_SECRET
            resp = requests.post(url, json=corpo, timeout=25)
        else:
            resp = requests.get(url, params={"segredo": WEBHOOK_SECRET} if WEBHOOK_SECRET else None, timeout=25)
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao falar com o Google Apps Script: {e}")
    except ValueError:
        raise HTTPException(status_code=502, detail="O Apps Script não retornou JSON válido (confira a implantação).")

    if isinstance(dados, dict) and dados.get("sucesso") is False:
        status = 409 if dados.get("duplicada") else 400
        raise HTTPException(status_code=status, detail=dados.get("erro") or "Operação recusada pelo Apps Script.")
    return dados


def baixar_soup(sessao: requests.Session, url: str) -> BeautifulSoup:
    """Segue redirecionamentos manualmente, validando CADA destino antes de acessá-lo."""
    atual = (url or "").strip()
    for _ in range(5):
        if not url_sefaz_valida(atual):
            raise HTTPException(status_code=400, detail="URL inválida ou fora dos domínios oficiais da SEFAZ/RS (https).")
        try:
            resp = sessao.get(atual, headers=HEADERS_HTTP, timeout=20, allow_redirects=False)
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Falha ao acessar a SEFAZ: {e}")
        if resp.is_redirect:
            destino = resp.headers.get("Location")
            if not destino:
                raise HTTPException(status_code=502, detail="Redirecionamento inválido da SEFAZ.")
            atual = urljoin(atual, destino)
            continue
        try:
            resp.raise_for_status()
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Falha ao acessar a SEFAZ: {e}")
        return BeautifulSoup(resp.content, "html.parser")  # bytes: o bs4 detecta o encoding
    raise HTTPException(status_code=502, detail="Redirecionamentos demais ao acessar a SEFAZ.")


# --------------------------------------------------------------------------- #
# Extração
# --------------------------------------------------------------------------- #
_NUMERO = re.compile(r"\d[\d.,]*")  # começa por dígito: não casa o "." de "Qtde.:"
_BRL = r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"


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
            "codigo": codigo, "descricao": descricao,
            "categoria": categoria, "categoria_origem": origem,
            "quantidade": quantidade, "unidade": unidade,
            "valor_unitario": valor_unitario, "valor_total": valor_total,
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

    if not produtos:  # layout alternativo em colunas
        for tr in soup.select('tr[class*="NFCDetalhe_Item"]'):
            c = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(c) >= 6:
                adicionar(c[0], c[1], c[2], c[3], c[4], c[5])
    return produtos


def extrair_total_nota(soup: BeautifulSoup, texto: str, soma_itens: Decimal) -> Tuple[str, str]:
    """Retorna (valor, origem). Prioriza o RÓTULO, nunca a posição do elemento."""
    for origem, rotulo in (("valor_a_pagar", r"Valor\s+a\s+pagar"), ("valor_total", r"Valor\s+total")):
        m = re.search(rotulo + r"\s*(?:\(?\s*R\$\s*\)?)?\s*:?\s*" + _BRL, texto, re.IGNORECASE)
        if m and (brl_para_decimal(m.group(1)) or Decimal(0)) > 0:
            return m.group(1), origem

    el = soup.select_one("span.totalNumb.txtMax")
    if el:
        m = re.search(_BRL, el.get_text(" ", strip=True))
        if m and (brl_para_decimal(m.group(1)) or Decimal(0)) > 0:
            return m.group(1), "css_txtMax"

    # Alguns layouts guardam o total em <input value="12,34">
    for inp in soup.select("input[value]"):
        nome = f"{inp.get('id', '')} {inp.get('name', '')}".lower()
        if "pagar" in nome or "total" in nome:
            m = re.fullmatch(_BRL, (inp.get("value") or "").strip())
            if m and (brl_para_decimal(m.group(1)) or Decimal(0)) > 0:
                return m.group(1), "input_value"

    return formatar_brl(soma_itens), "soma_itens"


def _extrair_cnpj(texto: str) -> str:
    m = re.search(r"CNPJ\s*:\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", texto, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback só no cabeçalho (o emitente vem primeiro; CNPJs no rodapé são de terceiros)
    m = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto[:1200])
    return m.group(1) if m else "Não identificado"


def parsear_nfce(soup: BeautifulSoup, url_original: str, memoria: Dict[str, str]) -> dict:
    texto = soup.get_text("\n", strip=True)

    estab_el = soup.select_one("#u20, .txtTopo, .NFCCabecalho_SubTitulo")
    estabelecimento = estab_el.get_text(" ", strip=True) if estab_el else "Estabelecimento"
    cnpj = _extrair_cnpj(texto)

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
        v = brl_para_decimal(p["valor_total"]) or Decimal(0)
        resumo[p["categoria"]] = resumo.get(p["categoria"], Decimal(0)) + v

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
# Modelos e validações
# --------------------------------------------------------------------------- #
class ProcessarRequest(BaseModel):
    url: str
    webhook_url: Optional[str] = ""


class CorrigirRequest(BaseModel):
    descricao: str
    categoria: str
    webhook_url: Optional[str] = ""


class SalvarSheetsRequest(BaseModel):
    webhook_url: Optional[str] = ""
    chave_acesso: str = ""
    estabelecimento: str = ""
    cnpj: str = ""
    data_emissao: str = ""
    valor_total_nota: str = "0,00"
    forma_pagamento: str = "À vista"
    parcelas: int = Field(1, ge=1, le=MAX_PARCELAS)
    produtos: List[dict] = Field(default_factory=list)


class CompraOnlineRequest(BaseModel):
    webhook_url: Optional[str] = ""
    descricao: str
    data_emissao: str = ""
    categoria: str = "Outros"
    valor_total_nota: str = "0,00"
    forma_pagamento: str = "Crédito"
    parcelas: int = Field(1, ge=1, le=MAX_PARCELAS)


class DashboardRequest(BaseModel):
    webhook_url: Optional[str] = ""


class ExcluirRequest(BaseModel):
    webhook_url: Optional[str] = ""
    identificador: str


def validar_lancamento(valor_total: str, data_br: str, forma: str) -> None:
    d = brl_para_decimal(valor_total)
    if d is None or d <= 0:
        raise HTTPException(status_code=400, detail="Valor total inválido (R$ 0,00). Verifique antes de salvar.")
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", data_br or "")
    try:
        if not m:
            raise ValueError
        datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida ou não identificada. Informe a data da compra.")
    if forma not in FORMAS_PAGAMENTO:
        raise HTTPException(status_code=400, detail="Forma de pagamento inválida.")


CAMPOS_ITEM = ("codigo", "descricao", "quantidade", "unidade", "valor_unitario", "valor_total", "categoria")


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "Servidor rodando com sucesso!", "versao": VERSAO}


@app.get("/api/categorias")
def listar_categorias():
    return {"categorias": CATEGORIAS}


@app.post("/api/processar-nfce", dependencies=[Depends(exigir_token)])
def processar_nfce(req: ProcessarRequest):
    sessao = requests.Session()
    soup = baixar_soup(sessao, req.url)
    memoria = memoria_completa(resolver_webhook(req.webhook_url, obrigatorio=False))

    if not soup.select('tr[id^="Item"], tr[class*="NFCDetalhe_Item"]'):
        iframe = soup.find("iframe", id="iframeConteudo") or soup.find("iframe")
        if iframe and iframe.get("src"):
            soup = baixar_soup(sessao, urljoin(req.url.strip(), iframe["src"]))

    return {"sucesso": True, "dados_extraidos": parsear_nfce(soup, req.url, memoria)}


@app.post("/api/corrigir-categoria", dependencies=[Depends(exigir_token)])
def corrigir_categoria(req: CorrigirRequest):
    categoria = req.categoria.strip()
    if not categoria or len(categoria) > 60:
        raise HTTPException(status_code=400, detail="Categoria inválida.")
    chave = chave_memoria(req.descricao)
    if not chave:
        raise HTTPException(status_code=400, detail="Descrição vazia: não há o que aprender.")

    salvo_em: List[str] = []
    try:
        salvar_memoria(chave, categoria)
        salvo_em.append("sqlite")
    except sqlite3.Error as e:
        log.warning("Falha ao gravar no SQLite: %s", e)

    webhook = resolver_webhook(req.webhook_url, obrigatorio=False)
    if webhook:
        try:
            chamar_webhook("POST", webhook, {"tipo": "memoria", "chave": chave, "categoria": categoria})
            salvo_em.append("planilha")
            _cache_planilha["ts"] = 0.0
        except HTTPException as e:
            log.warning("Falha ao gravar memória na planilha: %s", e.detail)

    if not salvo_em:
        raise HTTPException(status_code=500, detail="Não foi possível salvar a correção (SQLite e planilha indisponíveis).")
    return {"sucesso": True, "chave": chave, "categoria": categoria, "salvo_em": salvo_em}


@app.post("/api/salvar-sheets", dependencies=[Depends(exigir_token)])
def salvar_sheets(req: SalvarSheetsRequest):
    url = resolver_webhook(req.webhook_url)
    forma = req.forma_pagamento
    validar_lancamento(req.valor_total_nota, req.data_emissao, forma)
    payload = {
        "tipo": "nfce",
        "chave_acesso": req.chave_acesso,
        "estabelecimento": req.estabelecimento,
        "cnpj": req.cnpj,
        "data_emissao": req.data_emissao,
        "valor_total_nota": req.valor_total_nota,
        "forma_pagamento": forma,
        "parcelas": 1 if forma == "À vista" else req.parcelas,
        "produtos": [{k: item.get(k, "") for k in CAMPOS_ITEM} for item in req.produtos],
    }
    return chamar_webhook("POST", url, payload)


@app.post("/api/salvar-compra-online", dependencies=[Depends(exigir_token)])
def salvar_compra_online(req: CompraOnlineRequest):
    url = resolver_webhook(req.webhook_url)
    if not req.descricao.strip():
        raise HTTPException(status_code=400, detail="Informe a descrição da compra.")
    validar_lancamento(req.valor_total_nota, req.data_emissao, req.forma_pagamento)
    payload = {
        "tipo": "online",
        "descricao": req.descricao.strip(),
        "data_emissao": req.data_emissao,
        "categoria": req.categoria,
        "valor_total_nota": req.valor_total_nota,
        "forma_pagamento": req.forma_pagamento,
        "parcelas": 1 if req.forma_pagamento == "À vista" else req.parcelas,
    }
    return chamar_webhook("POST", url, payload)


@app.post("/api/dados-dashboard", dependencies=[Depends(exigir_token)])
def dados_dashboard(req: DashboardRequest):
    return chamar_webhook("GET", resolver_webhook(req.webhook_url))


@app.post("/api/excluir-lancamento", dependencies=[Depends(exigir_token)])
def excluir_lancamento(req: ExcluirRequest):
    url = resolver_webhook(req.webhook_url)
    if not req.identificador.strip():
        raise HTTPException(status_code=400, detail="Identificador não informado.")
    return chamar_webhook("POST", url, {"tipo": "excluir", "identificador": req.identificador.strip()})
