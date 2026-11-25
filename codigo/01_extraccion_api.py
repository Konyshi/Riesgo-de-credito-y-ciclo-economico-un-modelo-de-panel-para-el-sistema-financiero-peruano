# Nombres y apellidos: NUÑEZ APOLINARIO FRANZUA KONYSHI
# Código de matrícula: 2024200511D
# Tema N.º 71: Riesgo de crédito y ciclo económico: un modelo de panel para el sistema financiero peruano
# Fecha de extracción: 2026-11-25
"""
01_extraccion_api.py
--------------------
VÍA 1 (API): consume la API REST pública de BCRPData (Banco Central de Reserva del Perú)
y guarda los datos TAL COMO SALEN de la fuente (sin editar).

Endpoint oficial:
  https://estadisticas.bcrp.gob.pe/estadisticas/series/api/{codigo}/json/{inicio}/{fin}/esp

No requiere clave. Las series fueron verificadas en el catálogo oficial de BCRPData.

Salidas (rutas relativas a la carpeta del proyecto):
  datos_crudos/api_bcrp/<codigo>.json           -> respuesta exacta de la API (evidencia primaria)
  datos_crudos/datos_crudos_api_<matricula>.csv -> las 6 series en formato largo, valores como texto
  log_ejecucion.txt                             -> fecha/hora, filas descargadas, código HTTP, hash

Ejecución (desde la carpeta del proyecto):  python codigo/01_extraccion_api.py
"""

import csv
import hashlib
import json
import os
import sys
import time
import urllib.robotparser
from datetime import datetime
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 1. PARÁMETROS CONGELADOS (constantes; NO son fechas dinámicas tipo "hoy")
# ----------------------------------------------------------------------------
CODIGO_MATRICULA = "2024200511D"
FECHA_INICIO = "2018-1"          # enero 2018 (formato exigido por la API: AAAA-M)
FECHA_CORTE = "2025-12"          # diciembre 2025
N_PERIODOS_ESPERADOS = 96        # 8 años x 12 meses
PAUSA_SEGUNDOS = 1.5             # pausa entre solicitudes (mínimo exigido: 1 s)
REINTENTOS = 3                   # reintentos ante fallo de red / error 5xx
TIMEOUT_SEGUNDOS = 30

BASE_API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
ROBOTS_URL = "https://estadisticas.bcrp.gob.pe/robots.txt"

# User-Agent identificable. El contacto se toma de la variable de entorno CONTACTO_UA
# (ver .env.example); si no existe, se usa el correo institucional del estudiante.
CONTACTO = os.getenv("CONTACTO_UA", "e_2024200511D@uncp.edu.pe")
USER_AGENT = (f"UNCP-Finanzas-I-InvestigacionAcademica/1.0 "
              f"(estudiante {CODIGO_MATRICULA}; {CONTACTO})")

# Series a descargar. "token" sirve para verificar que el nombre que devuelve la API
# corresponde a la serie esperada (control de calidad, no altera los datos).
SERIES = [
    {"codigo": "PN01770AM", "variable": "pbi_indice_2007",     "token": "PBI",
     "descripcion": "PBI (índice 2007=100) - mensual - INEI/BCRP"},
    {"codigo": "PN38063GM", "variable": "tasa_desempleo_lima", "token": "Desempleo",
     "descripcion": "Tasa de desempleo (%), Lima Metropolitana, promedio móvil 3 meses - INEI"},
    {"codigo": "PN07807NM", "variable": "tasa_activa_tamn",    "token": "TAMN",
     "descripcion": "Tasa activa promedio en MN (TAMN), términos efectivos anuales - SBS/BCRP"},
    {"codigo": "PN01207PM", "variable": "tc_interbancario_prom", "token": "Interbancario",
     "descripcion": "Tipo de cambio interbancario promedio del período (S/ por US$)"},
    {"codigo": "PN00496MM", "variable": "credito_mn_millones", "token": "Crédito",
     "descripcion": "Crédito de sociedades de depósito al sector privado, MN, fin de período (millones S/)"},
    {"codigo": "PN00499MM", "variable": "credito_me_millones_usd", "token": "Crédito",
     "descripcion": "Crédito de sociedades de depósito al sector privado, ME, fin de período (millones US$)"},
]

# ----------------------------------------------------------------------------
# 2. RUTAS RELATIVAS (nunca absolutas). Se asume ejecución desde la raíz del proyecto.
# ----------------------------------------------------------------------------
RAIZ = Path(".")
DIR_CRUDOS = RAIZ / "datos_crudos"
DIR_JSON = DIR_CRUDOS / "api_bcrp"
ARCHIVO_CRUDO_CSV = DIR_CRUDOS / f"datos_crudos_api_{CODIGO_MATRICULA}.csv"
ARCHIVO_LOG = RAIZ / "log_ejecucion.txt"


# ----------------------------------------------------------------------------
# 3. UTILIDADES
# ----------------------------------------------------------------------------
def log(mensaje: str) -> None:
    """Imprime en consola y agrega la línea (con fecha y hora) a log_ejecucion.txt."""
    linea = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {mensaje}"
    print(linea)
    with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def sha256_archivo(ruta: Path) -> str:
    """Calcula el hash SHA-256 de un archivo (integridad de la evidencia)."""
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def revisar_robots(sesion: requests.Session) -> None:
    """Consulta el robots.txt del portal y registra el resultado en el log."""
    try:
        r = sesion.get(ROBOTS_URL, timeout=TIMEOUT_SEGUNDOS)
        log(f"robots.txt -> HTTP {r.status_code} ({ROBOTS_URL})")
        if r.status_code == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            ruta_api = f"{BASE_API}/PN01770AM/json/{FECHA_INICIO}/{FECHA_CORTE}/esp"
            permitido = rp.can_fetch(USER_AGENT, ruta_api)
            log(f"robots.txt: ruta de la API permitida para este User-Agent = {permitido}")
        else:
            log("robots.txt no disponible como archivo de reglas; la API es un servicio "
                "público documentado para desarrolladores.")
    except requests.RequestException as e:
        log(f"ADVERTENCIA: no se pudo leer robots.txt ({e.__class__.__name__})")


def descargar_serie(sesion: requests.Session, codigo: str):
    """
    Solicita una serie a la API de BCRPData. Reintenta ante errores de red o 5xx.
    Devuelve (texto_respuesta, codigo_http, url).
    """
    url = f"{BASE_API}/{codigo}/json/{FECHA_INICIO}/{FECHA_CORTE}/esp"
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            r = sesion.get(url, timeout=TIMEOUT_SEGUNDOS)
            log(f"GET {url} -> HTTP {r.status_code} (intento {intento})")
            if r.status_code == 200:
                return r.text, r.status_code, url
            if r.status_code < 500:        # error 4xx: reintentar no ayuda
                raise RuntimeError(f"HTTP {r.status_code} para {codigo}")
            ultimo_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo_error = e.__class__.__name__
            log(f"Fallo de red en {codigo}: {ultimo_error} (intento {intento})")
        time.sleep(PAUSA_SEGUNDOS * intento)   # espera creciente entre reintentos
    raise RuntimeError(f"No se pudo descargar {codigo}: {ultimo_error}")


def interpretar_respuesta(texto: str, codigo: str):
    """
    Lee el JSON de la API SIN modificar valores. Estructura esperada:
      {"config": {"series": [{"name": ...}]}, "periods": [{"name": "Ene.2018", "values": ["108.3"]}]}
    Devuelve (nombre_serie, lista_de_(periodo, valor_texto)).
    """
    datos = json.loads(texto)
    if "periods" not in datos:
        raise ValueError(f"Respuesta sin 'periods' para {codigo}")
    nombre = datos.get("config", {}).get("series", [{}])[0].get("name", "")
    filas = []
    for p in datos["periods"]:
        valor = p["values"][0] if p.get("values") else ""
        filas.append((p["name"], valor))   # valor se conserva como texto, tal cual la fuente
    return nombre, filas


# ----------------------------------------------------------------------------
# 4. PROGRAMA PRINCIPAL
# ----------------------------------------------------------------------------
def main() -> int:
    DIR_JSON.mkdir(parents=True, exist_ok=True)

    log("=" * 70)
    log(f"INICIO extracción API BCRPData | matrícula {CODIGO_MATRICULA}")
    log(f"Parámetros congelados: FECHA_INICIO={FECHA_INICIO} | FECHA_CORTE={FECHA_CORTE} "
        f"| períodos esperados={N_PERIODOS_ESPERADOS}")
    log(f"Python {sys.version.split()[0]} | requests {requests.__version__}")

    sesion = requests.Session()
    sesion.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    revisar_robots(sesion)
    time.sleep(PAUSA_SEGUNDOS)

    filas_crudas = []       # (codigo, variable, nombre_api, periodo_crudo, valor_crudo)
    errores = 0

    for serie in SERIES:
        codigo = serie["codigo"]
        try:
            texto, http, url = descargar_serie(sesion, codigo)

            # Se guarda la respuesta EXACTA de la API (evidencia primaria, no se edita).
            ruta_json = DIR_JSON / f"{codigo}.json"
            with open(ruta_json, "w", encoding="utf-8") as f:
                f.write(texto)

            nombre_api, filas = interpretar_respuesta(texto, codigo)

            # Control de calidad: ¿el nombre devuelto corresponde a lo esperado?
            if serie["token"].lower() not in nombre_api.lower():
                log(f"ADVERTENCIA {codigo}: la API devolvió el nombre «{nombre_api}», "
                    f"no contiene «{serie['token']}». Verificar el código en BCRPData.")

            n = len(filas)
            primero = filas[0][0] if filas else "-"
            ultimo = filas[-1][0] if filas else "-"
            n_nd = sum(1 for _, v in filas if v in ("", "n.d.", "n.d", None))
            log(f"{codigo} | «{nombre_api}» | filas={n} | {primero} a {ultimo} "
                f"| valores n.d.={n_nd} | hash JSON={sha256_archivo(ruta_json)[:16]}…")

            if n != N_PERIODOS_ESPERADOS:
                log(f"ADVERTENCIA {codigo}: se esperaban {N_PERIODOS_ESPERADOS} períodos y "
                    f"llegaron {n}.")

            for periodo, valor in filas:
                filas_crudas.append((codigo, serie["variable"], nombre_api, periodo, valor))

        except Exception as e:           # un fallo en una serie no detiene las demás
            errores += 1
            log(f"ERROR en {codigo}: {e}")

        time.sleep(PAUSA_SEGUNDOS)       # pausa obligatoria entre solicitudes

    # CSV crudo consolidado (formato largo, valores como texto, sin transformar)
    with open(ARCHIVO_CRUDO_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo_serie", "variable", "nombre_api", "periodo_crudo", "valor_crudo"])
        w.writerows(filas_crudas)

    log(f"CSV crudo guardado: {ARCHIVO_CRUDO_CSV} | filas={len(filas_crudas)} "
        f"| SHA-256={sha256_archivo(ARCHIVO_CRUDO_CSV)}")
    log(f"FIN extracción API | series OK={len(SERIES) - errores}/{len(SERIES)} | errores={errores}")
    return 1 if errores else 0


if __name__ == "__main__":
    codigo_salida = main()
    if codigo_salida != 0:          # solo termina con error si hubo fallos
        sys.exit(codigo_salida)
