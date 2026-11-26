# Nombres y apellidos: NUÑEZ APOLINARIO FRANZUA KONYSHI
# Código de matrícula: 2024200511D
# Tema N.º 71: Riesgo de crédito y ciclo económico: un modelo de panel para el sistema financiero peruano
# Fecha de extracción: 2026-11-26  (el log registra la hora exacta en UTC)
"""
02_scraping_web.py  (versión compatible con Google Colab / Jupyter y con terminal)
------------------
VÍA 2 (descarga programática, numeral 2.4.3 de la consigna): obtiene, desde el portal oficial
de estadísticas de la SBS, el cuadro «Créditos Directos según Situación» de cada empresa, mes a mes.

Procedimiento (todo automatizado, nada manual):
  1. Lee la página del boletín de cada segmento y UBICA el enlace del cuadro.
  2. Lee la página del cuadro y extrae con una expresión regular los enlaces .XLS mensuales
     dentro de la ventana FECHA_INICIO – FECHA_CORTE.
  3. Descarga cada .XLS, con pausa y User-Agent identificable, y lo deja INTACTO en datos_crudos/sbs/<segmento>/.
  4. Consolida las filas tal como vienen en datos_crudos/datos_crudos_sbs_<matrícula>.csv.

Uso en Colab (en una celda previa):
    !pip install -q xlrd openpyxl beautifulsoup4 requests pandas
Uso en terminal:
    python 02_scraping_web.py          # completo
    python 02_scraping_web.py --demo   # 3 últimos meses
"""

import argparse
import csv
import hashlib
import re
import sys
import time
import unicodedata
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# 0. AJUSTES PARA NOTEBOOK
# ----------------------------------------------------------------------------
EN_NOTEBOOK = "ipykernel" in sys.modules   # True en Colab/Jupyter
MODO_DEMO_NOTEBOOK = False                 # pon True para probar solo con 3 meses en Colab

# ----------------------------------------------------------------------------
# 1. PARÁMETROS CONGELADOS
# ----------------------------------------------------------------------------
CODIGO_MATRICULA = "2024200511D"
FECHA_INICIO = "2018-01"
FECHA_CORTE = "2025-12"
PAUSA_SEGUNDOS = 1.5
REINTENTOS = 3
TIMEOUT_SEGUNDOS = 60

BASE_PORTAL = "https://www.sbs.gob.pe/app/stats_net/stats/"
ROBOTS_URLS = ["https://www.sbs.gob.pe/robots.txt", "https://intranet2.sbs.gob.pe/robots.txt"]

TEXTO_CUADRO = "Créditos Directos según Situación"

SEGMENTOS = [
    {"segmento": "banca_multiple",       "portal": BASE_PORTAL + "EstadisticaBoletinEstadistico.aspx?p=1"},
    {"segmento": "empresas_financieras", "portal": BASE_PORTAL + "EstadisticaBoletinEstadistico.aspx?p=2"},
    {"segmento": "cajas_municipales",    "portal": BASE_PORTAL + "EstadisticaBoletinEstadistico.aspx?p=3"},
    {"segmento": "cajas_rurales",        "portal": BASE_PORTAL + "EstadisticaBoletinEstadistico.aspx?p=4"},
]

CONTACTO = "e_2024200511D@uncp.edu.pe"
USER_AGENT = (f"UNCP-Finanzas-I-InvestigacionAcademica/1.0 "
              f"(estudiante {CODIGO_MATRICULA}; {CONTACTO})")

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10, "noviembre": 11,
         "diciembre": 12}

MAGIC_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAGIC_ZIP = b"PK"

COLUMNAS_CSV = ["segmento", "anio", "mes", "archivo", "fila_excel", "empresa_crudo", "es_total",
                "vigentes_total", "vigentes_corto_plazo", "vigentes_largo_plazo",
                "reestructurados", "refinanciados", "reest_y_refinanciados",
                "vencidos", "cobranza_judicial", "total_creditos", "fecha_en_archivo", "unidad"]
COLUMNAS_OBLIGATORIAS = {"vencidos", "cobranza_judicial", "total_creditos"}

ARCHIVO_LOG = Path("log_ejecucion.txt")   # se redefine en modo --demo


# ----------------------------------------------------------------------------
# 2. UTILIDADES
# ----------------------------------------------------------------------------
def log(mensaje: str) -> None:
    linea = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {mensaje}"
    print(linea)
    ARCHIVO_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def normalizar(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def revisar_robots(sesion: requests.Session) -> None:
    for url in ROBOTS_URLS:
        try:
            r = sesion.get(url, timeout=TIMEOUT_SEGUNDOS)
            log(f"robots.txt -> HTTP {r.status_code} ({url})")
            if r.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                prueba = BASE_PORTAL + "EstadisticaSistemaFinancieroResultados.aspx"
                if "intranet2" in url:
                    prueba = "https://intranet2.sbs.gob.pe/estadistica/financiera/2025/Diciembre/"
                log(f"robots.txt: ruta de interés permitida para este User-Agent = "
                    f"{rp.can_fetch(USER_AGENT, prueba)}")
            else:
                log("robots.txt sin reglas publicadas en ese servidor (no declara restricciones).")
        except requests.RequestException as e:
            log(f"ADVERTENCIA: no se pudo leer {url} ({e.__class__.__name__})")
        time.sleep(PAUSA_SEGUNDOS)


def obtener(sesion: requests.Session, url: str) -> requests.Response:
    ultimo = None
    for intento in range(1, REINTENTOS + 1):
        try:
            r = sesion.get(url, timeout=TIMEOUT_SEGUNDOS)
            log(f"GET {url} -> HTTP {r.status_code} (intento {intento})")
            if r.status_code == 200:
                return r
            if r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            ultimo = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo = e.__class__.__name__
            log(f"Fallo de red: {ultimo} (intento {intento})")
        time.sleep(PAUSA_SEGUNDOS * intento)
    raise RuntimeError(f"No se pudo obtener {url}: {ultimo}")


# ----------------------------------------------------------------------------
# 3. UBICAR EL CUADRO Y LOS ENLACES MENSUALES
# ----------------------------------------------------------------------------
def ubicar_cuadro(sesion: requests.Session, portal: str):
    html = obtener(sesion, portal).text
    soup = BeautifulSoup(html, "html.parser")
    objetivo = normalizar(TEXTO_CUADRO)
    for a in soup.find_all("a", href=True):
        if normalizar(a.get_text()) == objetivo:
            url = urljoin(portal, a["href"])
            codigo = parse_qs(urlparse(url).query).get("c", [""])[0]
            if codigo:
                return url, codigo
    raise RuntimeError(f"No se halló el enlace «{TEXTO_CUADRO}» en {portal}")


def listar_enlaces(sesion: requests.Session, url_cuadro: str, codigo: str):
    html = obtener(sesion, url_cuadro).text
    soup = BeautifulSoup(html, "html.parser")
    patron = re.compile(
        rf"/financiera/(\d{{4}})/([A-Za-z]+)/{re.escape(codigo)}-[A-Za-z]{{2}}\d{{2,4}}\.XLS",
        re.IGNORECASE)
    a_i, m_i = (int(x) for x in FECHA_INICIO.split("-"))
    a_c, m_c = (int(x) for x in FECHA_CORTE.split("-"))
    enlaces = {}
    for a in soup.find_all("a", href=True):
        m = patron.search(a["href"])
        if not m:
            continue
        anio, nombre = int(m.group(1)), m.group(2)
        mes = MESES.get(normalizar(nombre))
        if mes is None:
            continue
        if not (a_i * 12 + m_i <= anio * 12 + mes <= a_c * 12 + m_c):
            continue
        enlaces[(anio, mes)] = (nombre, urljoin(url_cuadro, a["href"]))
    return dict(sorted(enlaces.items()))


def meses_esperados():
    a_i, m_i = (int(x) for x in FECHA_INICIO.split("-"))
    a_c, m_c = (int(x) for x in FECHA_CORTE.split("-"))
    return [((n - 1) // 12, (n - 1) % 12 + 1) for n in range(a_i * 12 + m_i, a_c * 12 + m_c + 1)]


# ----------------------------------------------------------------------------
# 4. LECTURA DE CADA EXCEL
# ----------------------------------------------------------------------------
def clasificar_columna(texto: str) -> str:
    t = normalizar(texto)
    if "cobranza" in t:
        return "cobranza_judicial"
    if "vencid" in t:
        return "vencidos"
    if "total" in t:
        return "total_creditos"
    if "corto" in t:
        return "vigentes_corto_plazo"
    if "largo" in t:
        return "vigentes_largo_plazo"
    if "reestructurados" in t and "refinanciados" in t:
        return "reest_y_refinanciados"
    if "reestructurados" in t:
        return "reestructurados"
    if "refinanciados" in t:
        return "refinanciados"
    if "vigentes" in t:
        return "vigentes_total"
    return ""


def _num(v) -> float:
    x = pd.to_numeric(v, errors="coerce")
    return 0.0 if pd.isna(x) else float(x)


def leer_cuadro(ruta: Path, segmento: str, anio: int, mes: int):
    with open(ruta, "rb") as f:
        firma = f.read(8)
    motor = "xlrd" if firma.startswith(MAGIC_OLE) else "openpyxl"
    bruto = pd.read_excel(ruta, sheet_name=0, header=None, dtype=object, engine=motor)

    fecha_archivo, unidad = "", ""
    for i in range(min(8, len(bruto))):
        v = bruto.iat[i, 0]
        if isinstance(v, (datetime, pd.Timestamp)):
            fecha_archivo = pd.Timestamp(v).strftime("%Y-%m-%d")
        elif isinstance(v, str) and "miles" in normalizar(v):
            unidad = v.strip()

        # El encabezado es la primera fila que tiene «vigentes» o «vencidos» en alguna columna.
    # No depende del rótulo de la primera columna, que cambia entre segmentos.
    fila_enc = None
    for i in range(len(bruto)):
        textos = [normalizar(v) for v in bruto.iloc[i, 1:].tolist() if isinstance(v, str)]
        if any(("vigentes" in t) or ("vencidos" in t) for t in textos):
            fila_enc = i
            break
    if fila_enc is None:
        raise ValueError("no se encontró la fila de encabezado (columnas «Vigentes»/«Vencidos»)")

    mapa = {}
    for j in range(1, bruto.shape[1]):
        texto = " ".join(str(bruto.iat[k, j]) for k in (fila_enc, fila_enc + 1)
                         if k < len(bruto) and pd.notna(bruto.iat[k, j]))
        nombre = clasificar_columna(texto)
        if nombre and nombre not in mapa.values():
            mapa[j] = nombre

    faltan = COLUMNAS_OBLIGATORIAS - set(mapa.values())
    if faltan:
        raise ValueError(f"faltan columnas obligatorias {sorted(faltan)}; detectadas {mapa}")

    filas, controles = [], []
    for i in range(fila_enc + 2, len(bruto)):
        empresa = bruto.iat[i, 0]
        if not isinstance(empresa, str) or not empresa.strip():
            continue
        es_total = normalizar(empresa).startswith("total")
        fila = {"segmento": segmento, "anio": anio, "mes": mes, "archivo": ruta.name,
                "fila_excel": i + 1, "empresa_crudo": empresa, "es_total": es_total,
                "fecha_en_archivo": fecha_archivo, "unidad": unidad}
        for j, nombre in mapa.items():
            v = bruto.iat[i, j]
            fila[nombre] = "" if pd.isna(v) else v
        filas.append(fila)
        if es_total:
            break

    if fecha_archivo and (int(fecha_archivo[:4]) != anio or int(fecha_archivo[5:7]) != mes):
        controles.append(f"la fecha interna del archivo ({fecha_archivo}) no coincide con {anio}-{mes:02d}")
    if not any(f["es_total"] for f in filas):
        controles.append("no se halló fila TOTAL")

    # control de cuadre: vigentes + reest./refinanc. + vencidos + cobranza = total
    descuadres = 0
    usa_desglose = "vigentes_corto_plazo" in mapa.values()
    # «reest_y_refinanciados» solo se excluye si el archivo trae reestructurados y/o refinanciados
    # por separado (banca, financieras); en cajas es la única columna y SÍ debe sumarse.
    hay_separadas = ("reestructurados" in mapa.values()) or ("refinanciados" in mapa.values())
    partes = [c for c in mapa.values()
              if c != "total_creditos"
              and not (c == "reest_y_refinanciados" and hay_separadas)
              and not (c == "vigentes_total" and usa_desglose)]
    for f in filas:
        total = pd.to_numeric(f.get("total_creditos"), errors="coerce")
        suma = sum(_num(f.get(c)) for c in partes)
        if pd.notna(total) and abs(suma - float(total)) > 1:
            descuadres += 1
    if descuadres:
        controles.append(f"{descuadres} fila(s) donde la suma de situaciones no coincide con el total")
    return filas, mapa, controles


# ----------------------------------------------------------------------------
# 5. PROGRAMA PRINCIPAL
# ----------------------------------------------------------------------------
def main(demo: bool = False) -> int:
    global ARCHIVO_LOG

    raiz = Path("demo_en_vivo") if demo else Path("datos_crudos")
    if demo:
        ARCHIVO_LOG = Path("demo_en_vivo") / "log_demo.txt"
    dir_sbs = raiz / "sbs"
    archivo_csv = raiz / f"datos_crudos_sbs_{CODIGO_MATRICULA}.csv"
    archivo_manifiesto = dir_sbs / f"manifiesto_descargas_{CODIGO_MATRICULA}.csv"
    dir_sbs.mkdir(parents=True, exist_ok=True)

    log("=" * 70)
    log(f"INICIO descarga SBS | matrícula {CODIGO_MATRICULA} | modo={'DEMO' if demo else 'COMPLETO'}")
    log(f"Parámetros congelados: FECHA_INICIO={FECHA_INICIO} | FECHA_CORTE={FECHA_CORTE}")
    log(f"Python {sys.version.split()[0]} | requests {requests.__version__} | pandas {pd.__version__}")

    sesion = requests.Session()
    sesion.headers.update({"User-Agent": USER_AGENT})
    revisar_robots(sesion)

    filas_totales, manifiesto, errores = [], [], 0
    esperados = meses_esperados()

    for seg in SEGMENTOS:
        nombre_seg = seg["segmento"]
        log(f"--- Segmento: {nombre_seg} ---")
        try:
            url_cuadro, codigo = ubicar_cuadro(sesion, seg["portal"])
            log(f"Cuadro ubicado: código {codigo} | {url_cuadro}")
            time.sleep(PAUSA_SEGUNDOS)
            enlaces = listar_enlaces(sesion, url_cuadro, codigo)
        except Exception as e:
            errores += 1
            log(f"ERROR en segmento {nombre_seg}: {e}")
            continue

        faltantes = [f"{a}-{m:02d}" for (a, m) in esperados if (a, m) not in enlaces]
        log(f"Enlaces en la ventana: {len(enlaces)} de {len(esperados)} esperados"
            + (f" | meses sin enlace: {faltantes}" if faltantes else ""))
        if demo:
            enlaces = dict(list(enlaces.items())[-3:])

        carpeta = dir_sbs / nombre_seg
        carpeta.mkdir(parents=True, exist_ok=True)
        mapa_previo = None

        for (anio, mes), (nombre_mes, url) in enlaces.items():
            ruta = carpeta / f"{anio}-{mes:02d}_{Path(urlparse(url).path).name}"
            time.sleep(PAUSA_SEGUNDOS)
            try:
                r = obtener(sesion, url)
                contenido = r.content
                if not (contenido.startswith(MAGIC_OLE) or contenido.startswith(MAGIC_ZIP)) or len(contenido) < 2000:
                    raise ValueError("la respuesta no es un archivo Excel válido")
                with open(ruta, "wb") as f:
                    f.write(contenido)
                manifiesto.append([nombre_seg, anio, mes, url, r.status_code, len(contenido),
                                   sha256_bytes(contenido),
                                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "OK"])
                filas, mapa, controles = leer_cuadro(ruta, nombre_seg, anio, mes)
                filas_totales.extend(filas)
                if mapa != mapa_previo:
                    log(f"  Diseño de columnas detectado ({anio}-{mes:02d}): {mapa}")
                    mapa_previo = mapa
                n_emp = sum(1 for f in filas if not f["es_total"])
                log(f"  {anio}-{mes:02d} | {len(contenido)} bytes | empresas={n_emp} "
                    f"| SHA-256={sha256_bytes(contenido)[:16]}…")
                for c in controles:
                    log(f"  ADVERTENCIA {anio}-{mes:02d}: {c}")
            except Exception as e:
                errores += 1
                manifiesto.append([nombre_seg, anio, mes, url, "", "", "",
                                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), f"ERROR: {e}"])
                log(f"  ERROR {nombre_seg} {anio}-{mes:02d}: {e}")

    with open(archivo_manifiesto, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["segmento", "anio", "mes", "url", "http", "bytes", "sha256", "fecha_hora_utc", "estado"])
        w.writerows(manifiesto)

    with open(archivo_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_CSV, extrasaction="ignore")
        w.writeheader()
        for fila in filas_totales:
            w.writerow({c: fila.get(c, "") for c in COLUMNAS_CSV})

    for seg in SEGMENTOS:
        s = [f for f in filas_totales if f["segmento"] == seg["segmento"]]
        entidades = {f["empresa_crudo"] for f in s if not f["es_total"]}
        log(f"RESUMEN {seg['segmento']}: filas={len(s)} | nombres de empresa distintos={len(entidades)}")
    log(f"CSV crudo guardado: {archivo_csv} | filas={len(filas_totales)} | SHA-256={sha256_archivo(archivo_csv)}")
    log(f"FIN descarga SBS | errores={errores}")
    return 1 if errores else 0


def _leer_modo_demo() -> bool:
    """Lee --demo sin romperse con los argumentos que Colab/Jupyter inyecta (-f kernel.json)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="demostración: 3 últimos meses, salidas en demo_en_vivo/")
    args, _desconocidos = ap.parse_known_args()
    return args.demo or (EN_NOTEBOOK and MODO_DEMO_NOTEBOOK)


if __name__ == "__main__":
    codigo_salida = main(demo=_leer_modo_demo())
    if EN_NOTEBOOK:
        print(f"\nTerminado. Código de salida = {codigo_salida} (0 = sin errores)")
    elif codigo_salida != 0:
        sys.exit(codigo_salida)
