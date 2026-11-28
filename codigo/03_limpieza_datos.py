# Nombres y apellidos: NUÑEZ APOLINARIO FRANZUA KONYSHI
# Código de matrícula: 2024200511D
# Tema N.º 71: Riesgo de crédito y ciclo económico: un modelo de panel para el sistema financiero peruano
# Fecha de extracción: 2026-11-26  (datos crudos extraídos con 01_extraccion_api.py y 02_scraping_web.py)
"""
03_limpieza_datos.py  (compatible con Google Colab / Jupyter y con terminal)
--------------------
Depura, tipifica y UNE las dos fuentes por la llave común (fecha mensual):
  - Fuente 1 (API BCRPData):   datos_crudos/datos_crudos_api_<matrícula>.csv
  - Fuente 2 (SBS, descarga programática): datos_crudos/datos_crudos_sbs_<matrícula>.csv

Qué hace:
  1. Lee los crudos SIN modificarlos (solo se leen; nunca se escriben).
  2. Normaliza las fechas del BCRP y arma la tabla macro mensual (96 meses).
  3. Limpia los nombres de las entidades de la SBS (notas al pie «*», «1/», errores de tipeo)
     y homologa los cambios de nombre (p. ej. Continental -> BBVA Perú) con una tabla EXPLÍCITA.
  4. Verifica la homologación: ninguna entidad puede tener dos filas en el mismo mes.
  5. Calcula la morosidad = (créditos vencidos + en cobranza judicial) / créditos directos.
  6. Une SBS + BCRP por fecha, marca el panel balanceado (entidades con los 96 meses) y guarda:
       datos_procesados/datos_procesados_<matrícula>.csv   (base final)
       datos_procesados/homologacion_entidades.csv          (nombre original -> entidad)
       datos_procesados/hash_sha256_datos_procesados.txt    (hash para el README)
       diccionario_variables.md                             (generado desde este mismo script)
       log_ejecucion.txt                                    (se AGREGA al log de los scripts 01 y 02)

Política de faltantes y atípicos (declarada en el artículo):
  - No se imputa ningún dato. Si una razón no se puede calcular, queda vacía y se registra en el log.
  - Los atípicos NO se eliminan: se marcan (flag_outlier_morosidad). El análisis hace robustez.

Uso:  python codigo/03_limpieza_datos.py
"""

import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

EN_NOTEBOOK = "ipykernel" in sys.modules

# ----------------------------------------------------------------------------
# 1. PARÁMETROS CONGELADOS
# ----------------------------------------------------------------------------
CODIGO_MATRICULA = "2024200511D"
FECHA_INICIO = "2018-01"
FECHA_CORTE = "2025-12"
N_PERIODOS = 96                  # 8 años x 12 meses
N_ENTIDADES_ESPERADAS = 35       # tamaño previsto del panel balanceado (temario)
TOLERANCIA_MILES = 1.0           # tolerancia de cuadre, en miles de soles
UMBRAL_Z_ATIPICO = 5.0           # |z robusto| (mediana/MAD) para marcar un atípico

RAIZ = Path(".")
CRUDO_API = RAIZ / "datos_crudos" / f"datos_crudos_api_{CODIGO_MATRICULA}.csv"
CRUDO_SBS = RAIZ / "datos_crudos" / f"datos_crudos_sbs_{CODIGO_MATRICULA}.csv"
DIR_PROC = RAIZ / "datos_procesados"
ARCHIVO_PROC = DIR_PROC / f"datos_procesados_{CODIGO_MATRICULA}.csv"
ARCHIVO_HOMOL = DIR_PROC / "homologacion_entidades.csv"
ARCHIVO_HASH = DIR_PROC / "hash_sha256_datos_procesados.txt"
ARCHIVO_DICC = RAIZ / "diccionario_variables.md"
ARCHIVO_LOG = RAIZ / "log_ejecucion.txt"

# ----------------------------------------------------------------------------
# 2. HOMOLOGACIÓN DE ENTIDADES (cambios de nombre confirmados con las notas al pie de la SBS)
#    Clave: nombre ya limpiado de «*» y «n/».  Valor: identificador único de la entidad.
#    Criterio de verificación (se comprueba en el código): los nombres unidos NO se solapan
#    en ningún mes y juntos cubren la serie sin huecos.
# ----------------------------------------------------------------------------
HOMOLOGACION = {
    "B. Continental": "B. BBVA Perú",                 # BBVA Continental pasó a llamarse BBVA Perú (2019)
    "B. Financiero": "B. Pichincha",                  # Banco Financiero pasó a llamarse Banco Pichincha (2018)
    "B. Azteca Perú": "Alfin Banco",                  # Banco Azteca pasó a llamarse Alfin Banco (2021)
    "B. de Comercio": "BANCOM",                       # Banco de Comercio pasó a llamarse BANCOM (2023)
    "Interbank (con sucursales en el exterior)": "Interbank",
    "B. de Crédito del Perú (con sucursales en el exterior)": "B. de Crédito del Perú",
    "Finaciera TFC S.A.": "Financiera TFC S.A.",      # errata de la SBS (falta una «n»)
}

# Series del BCRP (código -> nombre de variable). Deben coincidir con el script 01.
VARIABLES_BCRP = ["pbi_indice_2007", "tasa_desempleo_lima", "tasa_activa_tamn",
                  "tc_interbancario_prom", "credito_mn_millones", "credito_me_millones_usd"]

MESES_BCRP = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
              "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
              "jan": 1, "apr": 4, "aug": 8, "dec": 12}

COLUMNAS_SALIDA = [
    "entidad", "empresa_crudo", "segmento", "periodo", "fecha", "anio", "mes",
    "creditos_directos_miles", "vencidos_miles", "cobranza_judicial_miles",
    "cartera_atrasada_miles", "morosidad_pct", "refin_reest_miles", "ratio_alto_riesgo_pct",
    "log_creditos_directos", "flag_descuadre_sbs", "flag_outlier_morosidad",
    "meses_observados", "panel_balanceado",
    "pbi_indice_2007", "pbi_var_interanual_pct", "tasa_desempleo_lima", "tasa_activa_tamn",
    "tc_interbancario_prom", "credito_mn_millones", "credito_me_millones_usd",
]

_SBS = "SBS - Boletín estadístico, cuadro «Créditos Directos según Situación» (archivos .XLS mensuales)"
_SBS_URL = ("https://intranet2.sbs.gob.pe/estadistica/financiera/{año}/{Mes}/{cuadro}-{abrev}{año}.XLS "
            "(cuadros: B-2315 banca múltiple, B-3219 empresas financieras, C-1207 cajas municipales, "
            "C-2207 cajas rurales)")
_BCRP_URL = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api/{codigo}/json/2018-1/2025-12/esp"
DICCIONARIO = {
    "entidad": ("Identificador único de la empresa del sistema financiero, con los cambios de nombre homologados.",
                "texto", "—", "Elaboración propia a partir de " + _SBS, "Ver homologacion_entidades.csv"),
    "empresa_crudo": ("Nombre de la empresa tal como aparece en el archivo de la SBS de ese mes.",
                      "texto", "Mensual", _SBS, _SBS_URL),
    "segmento": ("Tipo de empresa: banca_multiple, empresas_financieras, cajas_municipales o cajas_rurales.",
                 "categoría", "—", _SBS, _SBS_URL),
    "periodo": ("Mes de observación en formato AAAA-MM (llave común con el BCRP).", "texto", "Mensual", "—", "—"),
    "fecha": ("Primer día del mes de observación.", "fecha", "Mensual", "—", "—"),
    "anio": ("Año de observación.", "año", "Mensual", "—", "—"),
    "mes": ("Mes de observación (1 a 12).", "mes", "Mensual", "—", "—"),
    "creditos_directos_miles": ("Total de créditos directos de la empresa al cierre del mes.",
                                "miles de S/", "Mensual", _SBS, _SBS_URL),
    "vencidos_miles": ("Créditos directos en situación de vencido.", "miles de S/", "Mensual", _SBS, _SBS_URL),
    "cobranza_judicial_miles": ("Créditos directos en cobranza judicial.", "miles de S/", "Mensual", _SBS, _SBS_URL),
    "cartera_atrasada_miles": ("Vencidos + cobranza judicial (cartera atrasada, criterio SBS).",
                               "miles de S/", "Mensual", "Cálculo propio sobre " + _SBS, _SBS_URL),
    "morosidad_pct": ("Ratio de morosidad = cartera atrasada / créditos directos x 100. Variable dependiente.",
                      "%", "Mensual", "Cálculo propio sobre " + _SBS, _SBS_URL),
    "refin_reest_miles": ("Créditos refinanciados y reestructurados (en cajas la SBS los reporta juntos).",
                          "miles de S/", "Mensual", _SBS, _SBS_URL),
    "ratio_alto_riesgo_pct": ("(Cartera atrasada + refinanciados y reestructurados) / créditos directos x 100.",
                              "%", "Mensual", "Cálculo propio sobre " + _SBS, _SBS_URL),
    "log_creditos_directos": ("Logaritmo natural de los créditos directos (tamaño de la cartera).",
                              "log(miles de S/)", "Mensual", "Cálculo propio sobre " + _SBS, _SBS_URL),
    "flag_descuadre_sbs": ("1 si la suma de las situaciones del crédito difiere del total en más de 1 mil de S/ "
                           "(error interno del archivo de la SBS; no se corrige).",
                           "0/1", "Mensual", "Control propio sobre " + _SBS, _SBS_URL),
    "flag_outlier_morosidad": ("1 si la morosidad es atípica dentro de su entidad (|z robusto| > 5, mediana/MAD). "
                               "No se elimina.", "0/1", "Mensual", "Cálculo propio", "—"),
    "meses_observados": ("Número de meses en que la entidad aparece en la ventana 2018-01 a 2025-12.",
                         "meses", "—", "Cálculo propio", "—"),
    "panel_balanceado": ("1 si la entidad tiene los 96 meses (panel principal de 35 entidades).",
                         "0/1", "—", "Cálculo propio", "—"),
    "pbi_indice_2007": ("PBI, índice 2007 = 100.", "índice", "Mensual", "BCRP (serie PN01770AM)",
                        _BCRP_URL.replace("{codigo}", "PN01770AM")),
    "pbi_var_interanual_pct": ("Variación porcentual del PBI respecto al mismo mes del año anterior "
                               "(vacía en los 12 primeros meses). Medida del ciclo económico.",
                               "%", "Mensual", "Cálculo propio sobre BCRP PN01770AM",
                               _BCRP_URL.replace("{codigo}", "PN01770AM")),
    "tasa_desempleo_lima": ("Tasa de desempleo de Lima Metropolitana, promedio móvil de 3 meses.",
                            "%", "Mensual", "BCRP (serie PN38063GM; origen INEI)",
                            _BCRP_URL.replace("{codigo}", "PN38063GM")),
    "tasa_activa_tamn": ("Tasa activa promedio de las empresas bancarias en moneda nacional (TAMN).",
                         "% efectivo anual", "Mensual", "BCRP (serie PN07807NM; origen SBS)",
                         _BCRP_URL.replace("{codigo}", "PN07807NM")),
    "tc_interbancario_prom": ("Tipo de cambio interbancario, promedio del período.", "S/ por US$", "Mensual",
                              "BCRP (serie PN01207PM)", _BCRP_URL.replace("{codigo}", "PN01207PM")),
    "credito_mn_millones": ("Crédito de las sociedades creadoras de depósito al sector privado, moneda nacional, "
                            "fin de período.", "millones de S/", "Mensual", "BCRP (serie PN00496MM)",
                            _BCRP_URL.replace("{codigo}", "PN00496MM")),
    "credito_me_millones_usd": ("Crédito de las sociedades creadoras de depósito al sector privado, moneda "
                                "extranjera, fin de período.", "millones de US$", "Mensual",
                                "BCRP (serie PN00499MM)", _BCRP_URL.replace("{codigo}", "PN00499MM")),
}


# ----------------------------------------------------------------------------
# 3. UTILIDADES
# ----------------------------------------------------------------------------
def log(mensaje: str) -> None:
    linea = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {mensaje}"
    print(linea)
    with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def parse_periodo_bcrp(texto: str) -> pd.Timestamp:
    """Convierte «Ene.2018» (o «Set.2019», «Jan.2018») en el primer día de ese mes."""
    m = re.match(r"\s*([A-Za-z]{3})[a-z]*\.?\s*(\d{4})", str(texto))
    if not m or m.group(1).lower() not in MESES_BCRP:
        raise ValueError(f"Período del BCRP no reconocido: «{texto}»")
    return pd.Timestamp(year=int(m.group(2)), month=MESES_BCRP[m.group(1).lower()], day=1)


def limpiar_nombre(nombre: str) -> str:
    """Quita notas al pie: «Alfin Banco 1/» -> «Alfin Banco»; «CMAC Sullana *» -> «CMAC Sullana»."""
    n = str(nombre).strip()
    n = re.sub(r"\s*\d+/\s*$", "", n)
    n = re.sub(r"[\s\*]+$", "", n)
    return n.strip()


# ----------------------------------------------------------------------------
# 4. FUENTE 1: BCRP (formato largo -> tabla mensual ancha)
# ----------------------------------------------------------------------------
def cargar_bcrp() -> pd.DataFrame:
    d = pd.read_csv(CRUDO_API, dtype=str)
    d["fecha"] = d["periodo_crudo"].map(parse_periodo_bcrp)
    d["valor"] = pd.to_numeric(d["valor_crudo"], errors="coerce")
    n_nan = int(d["valor"].isna().sum())
    log(f"BCRP: {len(d)} filas crudas | valores no numéricos = {n_nan}")

    ancho = d.pivot(index="fecha", columns="variable", values="valor").sort_index()
    faltan = [v for v in VARIABLES_BCRP if v not in ancho.columns]
    if faltan:
        raise ValueError(f"Faltan series del BCRP en el crudo: {faltan}")

    esperado = pd.date_range(f"{FECHA_INICIO}-01", f"{FECHA_CORTE}-01", freq="MS")
    if len(ancho) != N_PERIODOS or not ancho.index.equals(esperado):
        raise ValueError(f"La tabla del BCRP no cubre exactamente {N_PERIODOS} meses consecutivos")

    ancho["pbi_var_interanual_pct"] = ancho["pbi_indice_2007"].pct_change(12) * 100
    log(f"BCRP: tabla mensual {ancho.index.min():%Y-%m} a {ancho.index.max():%Y-%m} | "
        f"{len(ancho)} meses | variables = {len(ancho.columns)}")
    return ancho[VARIABLES_BCRP + ["pbi_var_interanual_pct"]]


# ----------------------------------------------------------------------------
# 5. FUENTE 2: SBS (limpieza, homologación, razones)
# ----------------------------------------------------------------------------
COLUMNAS_NUM_SBS = ["vigentes_total", "vigentes_corto_plazo", "vigentes_largo_plazo", "reestructurados",
                    "refinanciados", "reest_y_refinanciados", "vencidos", "cobranza_judicial", "total_creditos"]


def cargar_sbs() -> pd.DataFrame:
    d = pd.read_csv(CRUDO_SBS)
    d["es_total"] = d["es_total"].astype(str).str.lower().eq("true")
    antes_nan = d[COLUMNAS_NUM_SBS].isna().sum()
    for c in COLUMNAS_NUM_SBS:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    nuevos_nan = int((d[COLUMNAS_NUM_SBS].isna().sum() - antes_nan).clip(lower=0).sum())
    d["fecha"] = pd.to_datetime(dict(year=d["anio"], month=d["mes"], day=1))
    log(f"SBS: {len(d)} filas crudas ({int(d.es_total.sum())} filas TOTAL de control) | "
        f"valores que no eran numéricos = {nuevos_nan}")

    # Control: la suma de las empresas debe coincidir con la fila TOTAL del propio archivo
    tot = d[d.es_total].set_index(["segmento", "fecha"])["total_creditos"]
    suma = d[~d.es_total].groupby(["segmento", "fecha"])["total_creditos"].sum()
    dif = (suma - tot).abs().dropna()
    malos = int((dif > TOLERANCIA_MILES).sum())
    if malos:
        log(f"ADVERTENCIA: en {malos} de {len(dif)} segmento-mes la suma de empresas no coincide con la fila TOTAL")
    else:
        log(f"SBS: la suma de empresas coincide con la fila TOTAL en los {len(dif)} segmento-mes")

    d = d[~d.es_total].copy()

    # Homologación de nombres
    d["entidad"] = d["empresa_crudo"].map(limpiar_nombre).map(lambda x: HOMOLOGACION.get(x, x))

    # Verificación 1: una entidad no puede tener dos filas en el mismo mes
    dup = d[d.duplicated(["entidad", "fecha"], keep=False)]
    if len(dup):
        ejemplos = dup[["entidad", "periodo" if "periodo" in dup else "fecha", "empresa_crudo"]].head(6)
        raise ValueError(f"La homologación genera duplicados entidad-mes:\n{ejemplos}")
    # Verificación 2: una entidad pertenece a un solo segmento
    multi = d.groupby("entidad")["segmento"].nunique()
    if (multi > 1).any():
        raise ValueError(f"Entidades en más de un segmento: {list(multi[multi > 1].index)}")
    log(f"SBS: {d['empresa_crudo'].nunique()} nombres originales -> {d['entidad'].nunique()} entidades "
        f"(sin duplicados entidad-mes)")
    return d


def calcular_variables(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["creditos_directos_miles"] = d["total_creditos"]
    d["vencidos_miles"] = d["vencidos"]
    d["cobranza_judicial_miles"] = d["cobranza_judicial"]
    d["cartera_atrasada_miles"] = d["vencidos"] + d["cobranza_judicial"]

    # Reestructurados + refinanciados: en banca y financieras vienen separados; en cajas, juntos
    d["refin_reest_miles"] = d[["reestructurados", "refinanciados", "reest_y_refinanciados"]].sum(axis=1, min_count=1)

    base = d["creditos_directos_miles"].where(d["creditos_directos_miles"] > 0)
    sin_base = int(d["creditos_directos_miles"].isna().sum() + (d["creditos_directos_miles"] <= 0).sum())
    if sin_base:
        log(f"ADVERTENCIA: {sin_base} filas sin créditos directos positivos; la razón queda vacía (no se imputa)")
    d["morosidad_pct"] = d["cartera_atrasada_miles"] / base * 100
    d["ratio_alto_riesgo_pct"] = (d["cartera_atrasada_miles"] + d["refin_reest_miles"].fillna(0)) / base * 100
    d["log_creditos_directos"] = np.log(base)

    # Control de cuadre interno del archivo (no corrige nada: solo marca)
    partes = ["vigentes_total", "vigentes_corto_plazo", "vigentes_largo_plazo", "reestructurados",
              "refinanciados", "reest_y_refinanciados", "vencidos", "cobranza_judicial"]
    suma_partes = d[partes].sum(axis=1, min_count=1)
    d["flag_descuadre_sbs"] = ((suma_partes - d["total_creditos"]).abs() > TOLERANCIA_MILES).astype(int)

    # Atípicos: z robusto (mediana y MAD) dentro de cada entidad; se marcan, no se eliminan
    def z_robusto(s: pd.Series) -> pd.Series:
        med = s.median()
        mad = (s - med).abs().median()
        if not mad or np.isnan(mad):
            return pd.Series(0.0, index=s.index)
        return 0.6745 * (s - med) / mad

    z = d.groupby("entidad")["morosidad_pct"].transform(z_robusto)
    d["flag_outlier_morosidad"] = (z.abs() > UMBRAL_Z_ATIPICO).astype(int)
    log(f"SBS: filas con descuadre interno = {int(d.flag_descuadre_sbs.sum())} | "
        f"morosidad atípica marcada (no eliminada) = {int(d.flag_outlier_morosidad.sum())} | "
        f"morosidad vacía = {int(d.morosidad_pct.isna().sum())}")
    return d


# ----------------------------------------------------------------------------
# 6. UNIÓN, PANEL BALANCEADO Y SALIDAS
# ----------------------------------------------------------------------------
def escribir_diccionario(columnas) -> None:
    faltan = [c for c in columnas if c not in DICCIONARIO]
    if faltan:
        raise ValueError(f"Variables sin definición en el diccionario: {faltan}")
    lineas = ["# Diccionario de variables", "",
              f"Archivo: `datos_procesados/datos_procesados_{CODIGO_MATRICULA}.csv` · "
              f"Ventana: {FECHA_INICIO} a {FECHA_CORTE} · Matrícula {CODIGO_MATRICULA}", "",
              "| Variable | Definición | Unidad | Frecuencia | Fuente exacta | URL o endpoint de origen |",
              "|---|---|---|---|---|---|"]
    for c in columnas:
        definicion, unidad, frec, fuente, url = DICCIONARIO[c]
        lineas.append(f"| `{c}` | {definicion} | {unidad} | {frec} | {fuente} | {url} |")
    ARCHIVO_DICC.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def main() -> int:
    DIR_PROC.mkdir(parents=True, exist_ok=True)
    log("=" * 70)
    log(f"INICIO limpieza de datos | matrícula {CODIGO_MATRICULA}")
    log(f"Parámetros: FECHA_INICIO={FECHA_INICIO} | FECHA_CORTE={FECHA_CORTE} | períodos={N_PERIODOS}")
    log(f"Python {sys.version.split()[0]} | pandas {pd.__version__} | numpy {np.__version__}")
    log(f"Hash SHA-256 del crudo API: {sha256_archivo(CRUDO_API)}")
    log(f"Hash SHA-256 del crudo SBS: {sha256_archivo(CRUDO_SBS)}")

    bcrp = cargar_bcrp()
    sbs = calcular_variables(cargar_sbs())

    # Unión por la llave común: fecha mensual (cada fila de la SBS recibe el macro de su mes)
    panel = sbs.merge(bcrp.reset_index(), on="fecha", how="left", validate="many_to_one")
    macro_vacio = {c: int(panel[c].isna().sum()) for c in VARIABLES_BCRP}
    if any(macro_vacio.values()):
        raise ValueError(f"Meses de la SBS sin dato macro del BCRP: {macro_vacio}")

    panel["periodo"] = panel["fecha"].dt.strftime("%Y-%m")
    panel["anio"] = panel["fecha"].dt.year
    panel["mes"] = panel["fecha"].dt.month
    panel["meses_observados"] = panel.groupby("entidad")["fecha"].transform("nunique")
    panel["panel_balanceado"] = (panel["meses_observados"] == N_PERIODOS).astype(int)
    panel["fecha"] = panel["fecha"].dt.strftime("%Y-%m-%d")

    panel = panel.sort_values(["segmento", "entidad", "fecha"]).reset_index(drop=True)[COLUMNAS_SALIDA]

    # Resumen del panel balanceado
    pb = panel[panel.panel_balanceado == 1]
    n_ent = pb["entidad"].nunique()
    log(f"Panel balanceado: {n_ent} entidades x {N_PERIODOS} meses = {len(pb)} observaciones")
    for seg, g in pb.groupby("segmento"):
        log(f"  {seg}: {g['entidad'].nunique()} entidades")
    if n_ent != N_ENTIDADES_ESPERADAS:
        log(f"ADVERTENCIA: se esperaban {N_ENTIDADES_ESPERADAS} entidades y resultaron {n_ent}")
    log(f"Base completa (incluye entidades que entraron o salieron): {len(panel)} filas | "
        f"{panel['entidad'].nunique()} entidades")
    log(f"Morosidad del panel balanceado (%): mín={pb.morosidad_pct.min():.2f} | "
        f"mediana={pb.morosidad_pct.median():.2f} | máx={pb.morosidad_pct.max():.2f}")
    fuera = panel[panel.panel_balanceado == 0].groupby("entidad")["meses_observados"].first()
    log(f"Entidades fuera del panel principal ({len(fuera)}): "
        + "; ".join(f"{k} ({v} meses)" for k, v in fuera.sort_values(ascending=False).items()))

    # Tabla de homologación (evidencia para la defensa)
    homol = (sbs.groupby(["entidad", "empresa_crudo"])
                .agg(segmento=("segmento", "first"), desde=("fecha", "min"), hasta=("fecha", "max"),
                     meses=("fecha", "nunique")).reset_index())
    homol["desde"] = homol["desde"].dt.strftime("%Y-%m")
    homol["hasta"] = homol["hasta"].dt.strftime("%Y-%m")
    homol.sort_values(["segmento", "entidad", "desde"]).to_csv(ARCHIVO_HOMOL, index=False, encoding="utf-8")

    # Base procesada (formato fijo para que el hash sea estable)
    panel.to_csv(ARCHIVO_PROC, index=False, encoding="utf-8", float_format="%.6f")
    h = sha256_archivo(ARCHIVO_PROC)
    ARCHIVO_HASH.write_text(f"{h}  datos_procesados_{CODIGO_MATRICULA}.csv\n", encoding="utf-8")
    escribir_diccionario(COLUMNAS_SALIDA)

    n_sust = sum(1 for c in COLUMNAS_SALIDA
                 if c not in ("entidad", "empresa_crudo", "segmento", "periodo", "fecha", "anio", "mes"))
    log(f"Base procesada guardada: {ARCHIVO_PROC} | filas={len(panel)} | columnas={len(COLUMNAS_SALIDA)} "
        f"(variables sustantivas={n_sust})")
    log(f"SHA-256 de datos_procesados: {h}")
    log("FIN limpieza de datos | errores=0")
    return 0


if __name__ == "__main__":
    try:
        codigo_salida = main()
    except Exception as e:
        log(f"ERROR: {e}")
        codigo_salida = 1
    if EN_NOTEBOOK:
        print(f"\nTerminado. Código de salida = {codigo_salida} (0 = sin errores)")
    elif codigo_salida != 0:
        sys.exit(codigo_salida)
