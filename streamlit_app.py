from __future__ import annotations

import io
import json
import shutil
import hmac
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from html import escape

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "historial_resina.db"
TZ = ZoneInfo("America/Lima")
TARGET_KPT = 10.07
RESIN_COST_USD_KG = 1.2723
DENSITY_KG_L = 1.064
PH_MIN, PH_MAX = 3.0, 4.5
WET_MIN, WET_REF, WET_MAX = 40.0, 68.0, 80.0  # N/m; confirmar ficha de laboratorio

st.set_page_config(page_title="DSS – Control de Dosificación RH | MP2", page_icon="📊", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
.stApp{background:#f4f7fb} [data-testid="stSidebar"]{background:linear-gradient(180deg,#f7fbff,#eef4fa);border-right:1px solid #dbe5ef}
.block-container{padding:1.1rem 1.55rem 2rem;max-width:1800px}
.topbar{background:linear-gradient(110deg,#0a3265,#124b84);color:white;padding:18px 25px;border-radius:14px;margin-bottom:16px;box-shadow:0 5px 16px rgba(10,50,101,.18)}
.topbar h1{margin:0;font-size:28px;font-weight:800;color:white}.topbar p{margin:7px 0 0;font-size:15px;font-weight:650;color:white}
.card{background:white;border:1px solid #dbe7f1;border-radius:14px;padding:17px 18px;min-height:110px;box-shadow:0 3px 10px rgba(25,67,105,.07)}
.card .label{color:#253a57;font-size:14px;font-weight:700}.card .value{color:#0967c5;font-size:clamp(18px,2vw,29px);overflow-wrap:anywhere;line-height:1.18;font-weight:850;margin-top:9px}
.card.red .value{color:#d92732}.card.green .value{color:#0d9152}.card.amber .value{color:#b77900}
.panel-title{color:#15365c;font-size:18px;font-weight:800;margin:14px 0 9px}
div[data-testid="stButton"] button,div[data-testid="stDownloadButton"] button{width:100%;min-height:43px;border-radius:9px;font-weight:750}

/* ÚNICAMENTE LAS TARJETAS INDIVIDUALES DE INDICADORES.
   El fondo de la página, las filas y el espacio entre cuadros no cambia. */
div[class*="st-key-mp2_ingenieria_resumen_"],
div[class*="st-key-mp2_resultados_kpis_"],
div[class*="st-key-mp2_resultados_decisiones_"] {
    background: #dceef8 !important;
    border: 1px solid #b9d9ed !important;
    border-radius: 12px !important;
    box-shadow: 0 3px 10px rgba(10, 50, 101, .10);
}
/* Evitar que un fondo interno blanco cubra el celeste de cada tarjeta. */
div[class*="st-key-mp2_ingenieria_resumen_"] [data-testid="stVerticalBlockBorderWrapper"],
div[class*="st-key-mp2_resultados_kpis_"] [data-testid="stVerticalBlockBorderWrapper"],
div[class*="st-key-mp2_resultados_decisiones_"] [data-testid="stVerticalBlockBorderWrapper"] {
    background: transparent !important;
}
/* Solo los textos de las tarjetas: etiquetas y cifras azul oscuro. */
div[class*="st-key-mp2_ingenieria_resumen_"] [data-testid="stMetric"],
div[class*="st-key-mp2_resultados_kpis_"] [data-testid="stMetric"],
div[class*="st-key-mp2_resultados_decisiones_"] [data-testid="stMetric"] {
    color: #173b60 !important;
}
div[class*="st-key-mp2_ingenieria_resumen_"] [data-testid="stMetric"] *,
div[class*="st-key-mp2_resultados_kpis_"] [data-testid="stMetric"] *,
div[class*="st-key-mp2_resultados_decisiones_"] [data-testid="stMetric"] * {
    color: #173b60 !important;
}

</style>""", unsafe_allow_html=True)


def now():
    return datetime.now(TZ)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    with conn() as c:
        # La tabla historial del código anterior permanece intacta: registros legacy.
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jornadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inicio TEXT NOT NULL UNIQUE, fin TEXT NOT NULL, registrado TEXT NOT NULL,
            operador TEXT NOT NULL, produccion_programada REAL NOT NULL,
            velocidad REAL NOT NULL, horas_efectivas REAL NOT NULL, ph REAL NOT NULL,
            flujo_inicial REAL NOT NULL, flujo_recomendado REAL NOT NULL,
            flujo_aplicado REAL NOT NULL, decision TEXT NOT NULL, motivo TEXT NOT NULL,
            observacion TEXT NOT NULL DEFAULT '',
            produccion_real REAL, consumo_real REAL, resistencia_humeda REAL,
            fecha_cierre TEXT, estado TEXT NOT NULL DEFAULT 'ABIERTA'
        );
        CREATE TABLE IF NOT EXISTS ajustes_jornada (
            id INTEGER PRIMARY KEY AUTOINCREMENT, jornada_id INTEGER NOT NULL,
            momento TEXT NOT NULL, registrado TEXT NOT NULL, operador TEXT NOT NULL,
            tipo TEXT NOT NULL, produccion_restante REAL NOT NULL,
            horas_restantes REAL NOT NULL, flujo_anterior REAL NOT NULL,
            flujo_recomendado REAL NOT NULL, flujo_aplicado REAL NOT NULL,
            decision TEXT NOT NULL, motivo TEXT NOT NULL, observacion TEXT NOT NULL DEFAULT '',
            ph REAL, velocidad REAL,
            FOREIGN KEY(jornada_id) REFERENCES jornadas(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_ajustes_jornada ON ajustes_jornada(jornada_id,momento);
        """)
        # Migración no destructiva: preserva datos previos y añade trazabilidad.
        existing = {r[1] for r in c.execute("PRAGMA table_info(ajustes_jornada)")}
        if "horas_transcurridas_dosificacion" not in existing:
            c.execute("ALTER TABLE ajustes_jornada ADD COLUMN horas_transcurridas_dosificacion REAL")
        c.execute("""CREATE TABLE IF NOT EXISTS revisiones_hazop (
            id INTEGER PRIMARY KEY AUTOINCREMENT, jornada_id INTEGER NOT NULL,
            registrado TEXT NOT NULL, revisor TEXT NOT NULL,
            nodo TEXT NOT NULL, parametro TEXT NOT NULL, palabra_guia TEXT NOT NULL,
            desviacion TEXT NOT NULL, causa_confirmada TEXT NOT NULL,
            consecuencia TEXT NOT NULL, salvaguarda TEXT NOT NULL,
            accion TEXT NOT NULL, responsable TEXT NOT NULL, estado TEXT NOT NULL,
            FOREIGN KEY(jornada_id) REFERENCES jornadas(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS doe_ensayos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, jornada_id INTEGER,
            fecha TEXT NOT NULL, autorizado_por TEXT NOT NULL,
            flujo REAL NOT NULL, ph REAL NOT NULL, velocidad REAL NOT NULL,
            kpt_real REAL, resistencia_humeda REAL, observacion TEXT NOT NULL,
            FOREIGN KEY(jornada_id) REFERENCES jornadas(id) ON DELETE SET NULL
        )""")


def query(sql, params=()):
    with conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def load_jornadas():
    d = query("SELECT * FROM jornadas ORDER BY inicio DESC")
    if not d.empty:
        d["codigo"] = d.id.map(lambda n: f"MP2-{n:05d}")
        d["dia_fecha"] = pd.to_datetime(d.inicio).dt.date
        d["kpt_real"] = d.consumo_real / d.produccion_real.replace(0, np.nan)
        d["exceso_real"] = (d.consumo_real - d.produccion_real * TARGET_KPT).clip(lower=0)
        d["impacto_real"] = d.exceso_real * RESIN_COST_USD_KG
        d["kpt_inicial"] = d.flujo_aplicado / 1000 * d.horas_efectivas * 60 * DENSITY_KG_L / d.produccion_programada
    return d


def jornada_segments(j, adjustments):
    """Estimación segmentada SOLO con horas efectivas explícitas.
    El último segmento no puede inferirse a partir de horas de reloj.
    """
    initial_hours = float(j.horas_efectivas)
    elapsed = pd.to_numeric(adjustments.get("horas_transcurridas_dosificacion", pd.Series(dtype=float)), errors="coerce")
    if adjustments.empty:
        return {"completo": True, "flujo_ponderado": float(j.flujo_aplicado),
                "consumo_estimado": float(j.flujo_aplicado)*initial_hours*60*DENSITY_KG_L/1000,
                "horas_registradas": initial_hours}
    if elapsed.isna().any() or (elapsed < 0).any() or elapsed.sum() > initial_hours + 1e-6:
        return {"completo": False, "flujo_ponderado": np.nan, "consumo_estimado": np.nan,
                "horas_registradas": float(elapsed.sum(skipna=True))}
    flows = [float(j.flujo_aplicado)] + adjustments.flujo_aplicado.astype(float).tolist()
    durations = elapsed.tolist() + [initial_hours - float(elapsed.sum())]
    # El último flujo se considera aplicado durante las horas restantes previstas.
    kg = sum(f*h*60*DENSITY_KG_L/1000 for f,h in zip(flows,durations))
    return {"completo": True, "flujo_ponderado": sum(f*h for f,h in zip(flows,durations))/initial_hours,
            "consumo_estimado": kg, "horas_registradas": initial_hours}


def add_segment_metrics(df):
    df = df.copy()
    # Mantener el esquema incluso sin jornadas cerradas.
    # El Soft Sensor debe informar datos insuficientes, no lanzar KeyError.
    if df.empty:
        for column in ("completo", "flujo_ponderado", "consumo_estimado", "horas_registradas", "kpt_proyectado_segmentado"):
            if column not in df.columns:
                df[column] = pd.Series(dtype="float64")
        return df
    records=[]
    for j in df.itertuples():
        seg=jornada_segments(j,load_ajustes(j.id))
        records.append(seg)
    extra=pd.DataFrame(records,index=df.index)
    df=pd.concat([df,extra],axis=1)
    df["kpt_proyectado_segmentado"] = df.consumo_estimado / df.produccion_programada
    return df


def load_ajustes(jornada_id=None):
    if jornada_id is None:
        return query("SELECT * FROM ajustes_jornada ORDER BY momento")
    return query("SELECT * FROM ajustes_jornada WHERE jornada_id=? ORDER BY momento", (int(jornada_id),))


def calc(production, flow, hours):
    if production <= 0 or hours <= 0 or not (0 <= flow <= 100000):
        raise ValueError("Producción y horas deben ser positivas; revise el flujo.")
    target = production * TARGET_KPT
    recommended = target * 1000 / (DENSITY_KG_L * 60 * hours)
    consumed = flow / 1000 * 60 * hours * DENSITY_KG_L
    kpt = consumed / production
    excess = max(0.0, consumed - target)
    return dict(target=target, recommended=recommended, consumed=consumed, kpt=kpt,
                excess=excess, impact=excess * RESIN_COST_USD_KG,
                deviation=(kpt / TARGET_KPT - 1) * 100)


def create_jornada(payload):
    cols = ",".join(payload)
    with conn() as c:
        c.execute(f"INSERT INTO jornadas ({cols}) VALUES ({','.join('?' for _ in payload)})", tuple(payload.values()))


def create_ajuste(payload):
    cols = ",".join(payload)
    with conn() as c:
        c.execute(f"INSERT INTO ajustes_jornada ({cols}) VALUES ({','.join('?' for _ in payload)})", tuple(payload.values()))


def close_jornada(jid, production, consumption, wet):
    with conn() as c:
        c.execute("""UPDATE jornadas SET produccion_real=?,consumo_real=?,resistencia_humeda=?,
                   fecha_cierre=?,estado='CERRADA' WHERE id=? AND estado='ABIERTA'""",
                  (production, consumption, wet, iso(now()), int(jid)))
        if c.total_changes == 0:
            raise ValueError("La jornada ya estaba cerrada.")


def xlsx(sheets):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=name[:31])
    return out.getvalue()


def all_sheets():
    sheets = {"Jornadas": query("SELECT * FROM jornadas"),
              "Recálculos": query("SELECT * FROM ajustes_jornada"),
              "DOE ensayos": query("SELECT * FROM doe_ensayos"),
              "HAZOP revisiones": query("SELECT * FROM revisiones_hazop")}
    try:
        sheets["Historial anterior"] = query("SELECT * FROM historial")
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        pass
    return sheets


def header(title, subtitle):
    st.markdown(f'<div class="topbar"><h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div>', unsafe_allow_html=True)


def card(label, value, tone=""):
    st.markdown(f'<div class="card {tone}"><div class="label">{escape(str(label))}</div><div class="value">{escape(str(value))}</div></div>', unsafe_allow_html=True)


def cards(items, group=None):
    # Cada bloque de indicadores tiene identidad propia por página y por actualización.
    # Evita que el navegador reutilice valores de tarjetas de otra página.
    if group is None:
        group = f"{st.session_state.get('mp2_pagina_actual', 'general')}_indicadores"
    revision = st.session_state.get(f"mp2_revision_{group}", 0)
    for index, (col, (label, value)) in enumerate(zip(st.columns(len(items)), items)):
        with col:
            with st.container(border=True, key=f"mp2_{group}_{revision}_{index}"):
                st.metric(label, value)


def chart_days(df):
    d = df.sort_values("inicio").copy().reset_index(drop=True)
    d["Día"] = [f"Día {i+1}" for i in range(len(d))]
    d["orden"] = np.arange(len(d))
    return d


def line_chart(df, cols, ylabel, height=285, center_target=False, y_max=None):
    if df.empty:
        empty = pd.DataFrame({"Día": pd.Series(dtype=str), "Valor": pd.Series(dtype=float)})
        chart = alt.Chart(empty).mark_point().encode(
            x=alt.X("Día:N", title="Jornada"),
            y=alt.Y("Valor:Q", title=ylabel, scale=alt.Scale(domain=[0, y_max or 40], nice=False))
        ).properties(height=height)
        st.altair_chart(chart, use_container_width=True)
        st.caption("Sin jornadas registradas. El gráfico se completará con datos reales.")
        return
    long = df.melt(id_vars=["Día", "orden", "inicio"], value_vars=cols, var_name="Serie", value_name="Valor").dropna()
    if long.empty:
        st.info("Sin valores disponibles.")
        return
    axis = alt.Y("Valor:Q", title=ylabel)
    if y_max is not None:
        observed_max = float(long["Valor"].max())
        upper = max(float(y_max), float(np.ceil(observed_max / 5) * 5))
        axis = alt.Y("Valor:Q", title=ylabel, scale=alt.Scale(domain=[0, upper], nice=False))
    elif center_target:
        vals = long.Valor.to_numpy(dtype=float)
        delta = max(float(np.max(np.abs(vals - TARGET_KPT))), 1.0)
        low = max(0.0, TARGET_KPT - delta * 1.08)
        high = TARGET_KPT + delta * 1.08
        axis = alt.Y("Valor:Q", title=ylabel, scale=alt.Scale(domain=[low, high], zero=False))
    plot = alt.Chart(long).mark_line(point=True, strokeWidth=2.5).encode(
        x=alt.X("Día:N", sort=df.Día.tolist(), title="Jornada"), y=axis,
        color=alt.Color("Serie:N", scale=alt.Scale(domain=cols, range=["#0878d1", "#18a957", "#e57349", "#9c69c9"][:len(cols)])),
        strokeDash=alt.StrokeDash("Serie:N", scale=alt.Scale(domain=cols, range=[[1, 0], [6, 4], [1, 0], [1, 0]][:len(cols)])),
        tooltip=["Día:N", "inicio:N", "Serie:N", alt.Tooltip("Valor:Q", format=".3f")]
    ).properties(height=height)
    st.altair_chart(plot, use_container_width=True)


def fit_linear_cv(df, features, response, quadratic=False, require_all=False):
    missing = [column for column in features + [response] if column not in df.columns]
    if missing:
        return None, df.iloc[0:0].copy(), "Faltan variables para el modelo: " + ", ".join(missing)
    d = df.dropna(subset=features + [response]).copy().reset_index(drop=True)
    if len(d) < 8:
        return None, d, "Se necesitan más jornadas válidas (mínimo exploratorio: 8)."
    x = d[features].to_numpy(float)
    y = d[response].to_numpy(float)
    variable = np.std(x, axis=0) > 1e-8
    if require_all and not variable.all():
        return None, d, "Los factores del DOE no varían todos: falta variación independiente."
    used = [f for f, v in zip(features, variable) if v]
    if not used:
        return None, d, "Las variables registradas no presentan variación independiente."
    x = x[:, variable]
    def design(z):
        parts = [np.ones(len(z))] + [z[:, i] for i in range(z.shape[1])]
        if quadratic:
            parts += [z[:, i] ** 2 for i in range(z.shape[1])]
            parts += [z[:, i] * z[:, j] for i in range(z.shape[1]) for j in range(i+1, z.shape[1])]
        return np.column_stack(parts)
    mean, std = x.mean(axis=0), x.std(axis=0)
    if np.any(std < 1e-8):
        return None, d, "Factores sin variación suficiente."
    matrix = design((x - mean) / std)
    p = matrix.shape[1]
    if len(d) < max(15, p + 5) or np.linalg.matrix_rank(matrix) < p:
        return None, d, f"No hay datos independientes suficientes para {p} coeficientes del modelo."
    beta = np.linalg.lstsq(matrix, y, rcond=None)[0]
    cv = np.full(len(d), np.nan)
    for i in range(len(d)):
        mask = np.arange(len(d)) != i
        xx, yy = x[mask], y[mask]
        mu, sigma = xx.mean(axis=0), xx.std(axis=0)
        if np.any(sigma < 1e-8):
            continue
        train = design((xx - mu) / sigma)
        if np.linalg.matrix_rank(train) != p:
            continue
        b = np.linalg.lstsq(train, yy, rcond=None)[0]
        cv[i] = float((design((x[i:i+1] - mu) / sigma) @ b)[0])
    valid = np.isfinite(cv)
    if valid.sum() < 5:
        return None, d, "No fue posible validar suficientes predicciones fuera de muestra."
    err = y[valid] - cv[valid]
    denom = np.sum((y[valid] - y[valid].mean()) ** 2)
    metrics = dict(MAE=float(np.mean(np.abs(err))), RMSE=float(np.sqrt(np.mean(err**2))),
                   R2=float(1 - np.sum(err**2)/denom) if denom > 0 else np.nan)
    return dict(features=used, mean=mean, std=std, beta=beta, quadratic=quadratic,
                cv=cv, metrics=metrics, min=x.min(axis=0), max=x.max(axis=0)), d, "Modelo exploratorio ajustado; validación dejando una jornada fuera."


def predict(model, values):
    x = np.array([[float(values[f]) for f in model["features"]]])
    z = (x - model["mean"]) / model["std"]
    parts = [np.ones(1)] + [z[:, i] for i in range(z.shape[1])]
    if model["quadratic"]:
        parts += [z[:, i] ** 2 for i in range(z.shape[1])]
        parts += [z[:, i]*z[:, j] for i in range(z.shape[1]) for j in range(i+1, z.shape[1])]
    return float((np.column_stack(parts) @ model["beta"])[0])


def hazop_rows(jornada, adjustments):
    rows = []
    def add(moment, operator, reason, applied, recommended, ph, note):
        dev = (applied / recommended - 1)*100 if recommended > 0 else np.nan
        if np.isfinite(dev) and dev > 3:
            guide, effect, action = "MÁS", "Posible sobredosificación y mayor costo", "Verificar calibración, flujo y balance de masa"
        elif np.isfinite(dev) and dev < -3:
            guide, effect, action = "MENOS", "Posible dosificación insuficiente y riesgo de calidad", "Verificar resistencia húmeda y condiciones de proceso"
        else:
            guide, effect, action = "SIN DESVIACIÓN DE FLUJO", "Sin desviación de flujo bajo umbral ilustrativo", "Mantener seguimiento"
        rows.append({"Momento": moment, "Operador": operator, "Nodo": "Dosificación RH – MP2", "Parámetro": "Flujo RH",
                     "Palabra guía": guide, "Desviación %": round(dev, 2) if np.isfinite(dev) else None,
                     "Motivo registrado (no causa confirmada)": reason, "Consecuencia potencial": effect,
                     "Salvaguardas a verificar": "Procedimientos, medición de flujo y control de calidad",
                     "Acción propuesta": action, "Observación": note})
        if ph is not None and np.isfinite(float(ph)) and not (PH_MIN <= float(ph) <= PH_MAX):
            rows.append({"Momento": moment, "Operador": operator, "Nodo": "Preparación / dosificación RH", "Parámetro": "pH RH",
                         "Palabra guía": "MÁS" if ph > PH_MAX else "MENOS", "Desviación %": None,
                         "Motivo registrado (no causa confirmada)": reason,
                         "Consecuencia potencial": "Posible variación de desempeño del insumo",
                         "Salvaguardas a verificar": "Muestreo y procedimiento de control de pH",
                         "Acción propuesta": "Confirmar lectura y revisar con Ingeniería", "Observación": note})
    add(jornada.inicio, jornada.operador, jornada.motivo, jornada.flujo_aplicado,
        jornada.flujo_recomendado, jornada.ph, jornada.observacion)
    for a in adjustments.itertuples():
        add(a.momento, a.operador, a.motivo, a.flujo_aplicado, a.flujo_recomendado, a.ph, a.observacion)
    return pd.DataFrame(rows)


init_db()
with st.sidebar:
    st.markdown("### 📊 DSS – MP2")
    page = st.radio("Navegación", ["1 · Operación", "2 · Ingeniería", "3 · Resultados MP2"])
    st.divider()

st.session_state["mp2_pagina_actual"] = page
jornadas = load_jornadas()
# Sin datos reales nunca se inventan cierres, ensayos ni predicciones.
all_adjustments = load_ajustes()


if page == "1 · Operación":
    with st.sidebar:
        st.markdown("### ⚙️ Datos de operación")
        local = now()
        date = st.date_input("Fecha de inicio", value=local.date())
        time = st.time_input("Hora de inicio", value=local.time().replace(second=0, microsecond=0))
        operator = st.text_input("Operador responsable", value="Operador MP2")
        production = st.number_input("Producción programada (t/24 h)", min_value=0.01, value=78.41, step=0.1)
        speed = st.number_input("Velocidad (m/min)", min_value=0.0, value=1325.0, step=1.0)
        flow = st.number_input("Flujo seleccionado (mL/min)", min_value=0.0, value=700.0, step=1.0)
        hours = st.number_input("Horas efectivas de dosificación", min_value=0.1, max_value=24.0, value=24.0, step=0.5)
        ph = st.number_input("pH RH", min_value=0.0, max_value=14.0, value=4.2, step=0.01)
        st.caption("Objetivo 10.07 kg/t · Densidad 1.064 kg/L · USD 1.2723/kg")
        execute_calculation = st.button("🧮 CALCULAR DOSIFICACIÓN", type="primary", use_container_width=True)
    start = datetime.combine(date, time, TZ)
    end = start + timedelta(hours=24)
    input_signature = (iso(start), operator.strip(), float(production), float(speed), float(flow), float(hours), float(ph))
    if execute_calculation:
        st.session_state["mp2_calculated_signature"] = input_signature
    header("📊 DSS – Control de Dosificación de Resina | MP2", f"Objetivo {TARGET_KPT:.2f} kg/t · Jornada {start:%d/%m/%Y %H:%M} a {end:%d/%m/%Y %H:%M}")
    # Vista permanente: el cálculo se presenta desde la primera carga.
    # El botón permite confirmarlo, pero no oculta la interfaz al volver a entrar.
    if execute_calculation:
        st.success("Dosificación calculada con los parámetros actuales.")
    r = calc(production, flow, hours)
    for c, (name, value, tone) in zip(st.columns(4), [
        ("🧪 Resina recomendada", f'{r["target"]:,.2f} kg', ""),
        ("⚙️ Flujo recomendado", f'{r["recommended"]:,.1f} mL/min', ""),
        ("📊 KPT estimado", f'{r["kpt"]:,.2f} kg/t', ""),
        ("⚠️ Exceso estimado", f'{r["excess"]:,.2f} kg', "red")]):
        with c: card(name, value, tone)
    flow_delta = float(flow) - float(r["recommended"])
    flow_delta_pct = (flow_delta / float(r["recommended"]) * 100.0) if r["recommended"] > 0 else 0.0
    st.markdown("**Desviación del flujo seleccionado frente al recomendado**")
    st.caption(f"Flujo recomendado: {r['recommended']:,.1f} mL/min · Flujo seleccionado: {flow:,.1f} mL/min")
    if flow_delta_pct > 3:
        st.markdown(
            '<div style="background:#fff0f1;border:1px solid #ff6470;border-radius:14px;'
            'padding:27px 20px;text-align:center;margin:12px 0 16px">'
            '<span style="color:#c91223;font-size:32px;font-weight:850">ALTO</span>'
            f'<span style="color:#c91223;font-size:21px;font-weight:750;margin-left:24px">'
            f'Reduzca el flujo en aproximadamente {flow_delta:,.0f} mL/min.</span>'
            f'<div style="color:#a92834;margin-top:9px">Desviación del flujo: '
            f'+{flow_delta_pct:.1f}% · Diferencia: +{flow_delta:,.1f} mL/min</div>'
            '</div>', unsafe_allow_html=True)
        st.caption("Alerta de referencia matemática; cualquier ajuste real requiere verificar calidad y autorización de planta.")
    elif flow_delta_pct < -3:
        st.warning(f"BAJO · El flujo seleccionado está {abs(flow_delta):,.1f} mL/min por debajo del recomendado ({flow_delta_pct:+.1f}%). Verificar resistencia húmeda antes de modificar.")
    else:
        st.success(f"Flujo cercano al recomendado: {flow_delta:+,.1f} mL/min ({flow_delta_pct:+.1f}%). Margen ilustrativo ±3%.")
    for c, (name, value, tone) in zip(st.columns(4), [
        ("💰 Impacto estimado", f'USD {r["impact"]:,.2f}', "green"),
        ("📉 Pérdida estimada/t", f'USD {r["impact"]/production:,.2f}', "red"),
        ("🏷️ Costo de resina", f'USD {RESIN_COST_USD_KG:.4f}/kg', ""),
        ("🧪 pH del RH", f'{ph:.2f}', "green" if PH_MIN <= ph <= PH_MAX else "amber")]):
        with c: card(name, value, tone)
    if not PH_MIN <= ph <= PH_MAX:
        st.warning(f"pH RH {ph:.2f} fuera del rango de referencia {PH_MIN:.1f}–{PH_MAX:.1f}. Confirmar con laboratorio.")
    # Fuera de st.form: Streamlit actualiza el selector de motivos inmediatamente.
    st.markdown("### Confirmación de la decisión operativa")
    decision = st.radio("Decisión", ["Aceptar recomendación", "Mantener selección"], horizontal=True, index=0, key="decision_mp2")
    reason = "Recomendación aceptada"
    if decision == "Mantener selección":
        reason = st.selectbox("¿Por qué mantiene el flujo seleccionado? *", [
            "Seleccione un motivo", "Baja resistencia húmeda", "Inestabilidad del proceso",
            "Cambio de producto", "Orden del jefe de turno", "Falla del dosificador",
            "Parada o reducción de velocidad", "Cambio de producción", "Otro",
        ], key="motivo_mp2")
    note = st.text_input("Observación / comentario adicional", placeholder="Detalle opcional para retroalimentación", key="obs_mp2")
    final_flow = r["recommended"] if decision == "Aceptar recomendación" else flow
    st.info(f"Flujo inicial seleccionado: **{flow:,.1f} mL/min** · Flujo recomendado: **{r['recommended']:,.1f} mL/min** · Flujo decidido: **{final_flow:,.1f} mL/min**")
    st.caption("GUARDAR JORNADA registra la decisión; CALCULAR DOSIFICACIÓN solo actualiza el cálculo.")
    submit = st.button("💾 GUARDAR JORNADA", type="primary", key="guardar_jornada_mp2")
    if submit:
        if not operator.strip() or reason == "Seleccione un motivo":
            st.error("Complete operador y motivo obligatorio.")
        elif start > now():
            st.error("No registre jornadas con fecha de inicio futura.")
        elif (not jornadas.empty and any(start < datetime.fromisoformat(row.fin) and end > datetime.fromisoformat(row.inicio) for row in jornadas.itertuples())):
            st.error("La jornada se superpone con otra jornada registrada. No se permite duplicar las 24 horas.")
        else:
            try:
                create_jornada(dict(inicio=iso(start), fin=iso(end), registrado=iso(now()), operador=operator.strip(),
                    produccion_programada=production, velocidad=speed, horas_efectivas=hours, ph=ph,
                    flujo_inicial=flow, flujo_recomendado=r["recommended"], flujo_aplicado=final_flow,
                    decision=decision, motivo=reason, observacion=note))
                st.success("Jornada guardada correctamente.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Ya existe una jornada con ese inicio.")
    st.markdown("### 📈 Tendencia diaria del KPT y flujo recomendado vs. aplicado")
    c1, c2 = st.columns(2)
    recent = chart_days(add_segment_metrics(jornadas.head(30))) if not jornadas.empty else pd.DataFrame()
    with c1:
        st.markdown("**Tendencia diaria del KPT**")
        if not recent.empty:
            recent["Objetivo 10.07"] = TARGET_KPT
            recent["KPT proyectado"] = recent.kpt_proyectado_segmentado
            line_chart(recent, ["KPT proyectado", "Objetivo 10.07"], "KPT estimado por segmentos (kg/t)", y_max=40)
            st.caption("Proyección física por horas efectivas y cambios registrados; el KPT real se obtiene en el cierre.")
        else:
            line_chart(pd.DataFrame(), [], "KPT estimado por segmentos (kg/t)", y_max=40)
    with c2:
        st.markdown("**Flujo recomendado vs. seleccionado**")
        if not recent.empty:
            recent["Flujo aplicado ponderado"] = recent.flujo_ponderado
            recent["Flujo recomendado"] = recent.flujo_recomendado
            flows = recent.melt(id_vars=["Día", "inicio"], value_vars=["Flujo recomendado", "Flujo aplicado ponderado"], var_name="Serie", value_name="mL/min").dropna()
            st.altair_chart(alt.Chart(flows).mark_bar().encode(x=alt.X("Día:N", sort=recent.Día.tolist()), xOffset="Serie:N", y=alt.Y("mL/min:Q", scale=alt.Scale(domain=[0, max(2000, float(flows["mL/min"].max()) * 1.05)], nice=False), title="mL/min"), color="Serie:N", tooltip=["Día", "inicio", "Serie", "mL/min"]).properties(height=285), use_container_width=True)
            st.caption("Flujo aplicado ponderado por horas efectivas de dosificación; los recálculos están incluidos cuando se registran sus duraciones.")
        else:
            empty_flows = pd.DataFrame({"Día": pd.Series(dtype=str), "mL/min": pd.Series(dtype=float)})
            empty_chart = alt.Chart(empty_flows).mark_bar().encode(
                x=alt.X("Día:N", title="Día"),
                y=alt.Y("mL/min:Q", title="mL/min", scale=alt.Scale(domain=[0, 2000], nice=False))
            ).properties(height=285)
            st.altair_chart(empty_chart, use_container_width=True)
            st.caption("Sin jornadas registradas. El gráfico se completará con datos reales.")
    st.markdown("### 🗒️ Historial y trazabilidad")
    if jornadas.empty: st.info("Todavía no hay jornadas registradas.")
    else:
        st.dataframe(add_segment_metrics(jornadas)[["codigo", "inicio", "fin", "operador", "estado", "produccion_programada", "produccion_real", "flujo_recomendado", "flujo_ponderado", "kpt_proyectado_segmentado", "kpt_real"]], hide_index=True, use_container_width=True)
        st.download_button("⬇️ EXPORTAR HISTORIAL A EXCEL", xlsx(all_sheets()), "historial_dosificacion_MP2.xlsx")

    # Al final de Operación, después de gráficos e historial.
    st.markdown("""<div class="topbar"><h1>🔄 RECALCULAR DOSIFICACIÓN – MP2</h1><p>Incidencias y ajustes durante una jornada de 24 horas</p></div>""", unsafe_allow_html=True)
    with st.expander("Abrir recálculo por incidencia", expanded=False):
        open_j = jornadas[jornadas.estado == "ABIERTA"] if not jornadas.empty else pd.DataFrame()
        if open_j.empty:
            st.info("Registre primero una jornada abierta.")
        else:
            ids = open_j.id.tolist()
            jid = st.selectbox("Jornada a ajustar", ids, format_func=lambda x: f"MP2-{x:05d} · {open_j.loc[open_j.id == x, 'inicio'].iloc[0]}")
            j = open_j.loc[open_j.id == jid].iloc[0]
            adj = load_ajustes(jid)
            current_flow = float(adj.iloc[-1].flujo_aplicado) if not adj.empty else float(j.flujo_aplicado)
            # Recalculo reactivo: el reloj determina horas restantes y produccion teórica.
            # La producción acumulada y las paradas pueden corregirse con evidencia real.
            col1, col2 = st.columns(2)
            start_j = datetime.fromisoformat(str(j.inicio))
            end_j = datetime.fromisoformat(str(j.fin))
            last_change = datetime.fromisoformat(str(adj.iloc[-1].momento)) if not adj.empty else start_j
            suggested_change = min(max(now(), start_j), end_j)
            with col1:
                change_date = st.date_input("Fecha del cambio", value=suggested_change.date(), key=f"change_date_{jid}")
                change_time = st.time_input("Hora del cambio", value=suggested_change.time().replace(second=0, microsecond=0), key=f"change_time_{jid}")
                who = st.text_input("Operador responsable", value=str(j.operador), key=f"who_{jid}")
                kind = st.selectbox("Motivo de incidencia", ["Parada de máquina", "Reducción de velocidad", "Pérdida de resistencia húmeda", "Cambio de producción", "Cambio de producto", "Otro"], key=f"kind_{jid}")
            change = datetime.combine(change_date, change_time, TZ)
            elapsed_clock = max(0., (change-start_j).total_seconds()/3600)
            remaining_clock = max(0., (end_j-change).total_seconds()/3600)
            # Suposición transparente: tasa programada uniforme por hora de reloj.
            produced_theory = float(j.produccion_programada)*min(elapsed_clock/24.,1.)
            remaining_theory = max(0.,float(j.produccion_programada)-produced_theory)
            with col2:
                st.metric("Horas de reloj restantes (automático)", f"{remaining_clock:.2f} h")
                st.metric("Producción restante teórica (automático)", f"{remaining_theory:.2f} t")
                old_flow = st.number_input("Flujo aplicado actualmente (mL/min)", min_value=0., value=current_flow, step=1., key=f"old_flow_{jid}")
                ph_change = st.number_input("pH medido durante incidencia", min_value=0., max_value=14., value=float(j.ph), step=.01, key=f"ph_change_{jid}")
                speed_change = st.number_input("Velocidad actual (m/min)", min_value=0., value=float(j.velocidad), step=1., key=f"speed_change_{jid}")
            st.caption(f"Tiempo desde el inicio: {elapsed_clock:.2f} h · Producción teórica acumulada: {produced_theory:.2f} t. Son estimaciones, no mediciones reales.")
            use_actual = st.checkbox("Tengo la producción REAL acumulada hasta la incidencia", key=f"use_actual_{jid}")
            if use_actual:
                actual_produced = st.number_input("Producción real acumulada (t)", min_value=0., value=float(produced_theory), step=.1, key=f"actual_produced_{jid}")
                remaining = max(0.,float(j.produccion_programada)-actual_produced)
            else:
                remaining = remaining_theory
            st.metric("Producción pendiente para cumplir el programa", f"{remaining:.2f} t")
            st.caption("La producción pendiente se calcula como programada menos acumulada. Si cambia la meta diaria, debe actualizarse la programación con autorización.")
            # Horas de dosificación: por defecto prorrateo de horas efectivas programadas.
            # Con paradas, el operador puede corregir usando horas efectivas reales.
            planned_effective = float(j.horas_efectivas)
            default_remaining_effective = min(remaining_clock, planned_effective * remaining_clock/24.)
            use_effective = st.checkbox("Hubo paradas: corregir horas efectivas de dosificación", key=f"use_effective_{jid}")
            if use_effective:
                remaining_hours = st.number_input("Horas EFECTIVAS previstas hasta el cierre", min_value=0., max_value=24., value=float(default_remaining_effective), step=.25, key=f"remaining_effective_{jid}")
                elapsed_h = st.number_input("Horas EFECTIVAS dosificadas desde el último cambio", min_value=0., max_value=24., value=float(min(max(0.,(change-last_change).total_seconds()/3600),planned_effective)), step=.25, key=f"elapsed_effective_{jid}")
            else:
                remaining_hours = default_remaining_effective
                elapsed_h = max(0.,min(planned_effective,(change-last_change).total_seconds()/3600 * planned_effective/24.))
                st.caption(f"Horas efectivas restantes estimadas: {remaining_hours:.2f} h · Horas efectivas desde último cambio: {elapsed_h:.2f} h")
            valid_recalc = remaining>0 and remaining_hours>0
            if valid_recalc:
                rec = calc(remaining, old_flow, remaining_hours)
                st.info(f"Resina restante recomendada: **{rec['target']:,.2f} kg** · Nuevo flujo recomendado: **{rec['recommended']:,.1f} mL/min** · Cambio: **{rec['recommended']-old_flow:+,.1f} mL/min**")
            else:
                rec = None
                st.warning("Sin producción pendiente u horas efectivas restantes: no se puede calcular un flujo positivo.")
            choice = st.radio("Decisión ante el recálculo", ["Aceptar recomendación", "Elegir otro flujo"], horizontal=True, key=f"choice_{jid}")
            chosen = rec["recommended"] if rec else 0.
            if choice == "Elegir otro flujo":
                chosen = st.number_input("Flujo finalmente elegido (mL/min)", min_value=0., value=float(old_flow), step=1., key=f"chosen_{jid}")
            motive = st.text_input("Justificación obligatoria si modifica el recomendado", key=f"motive_{jid}")
            obs = st.text_area("Observación de la incidencia", height=70, key=f"obs_{jid}")
            if valid_recalc:
                st.caption(f"KPT proyectado solo para producción restante: {calc(remaining,chosen,remaining_hours)['kpt']:.3f} kg/t. No representa el KPT real acumulado.")
            if st.button("💾 GUARDAR RECÁLCULO", type="primary", key=f"save_adj_{jid}"):
                if not valid_recalc:
                    st.error("No existe producción pendiente con horas efectivas suficientes.")
                elif not who.strip() or (choice=="Elegir otro flujo" and not motive.strip()):
                    st.error("Complete operador y justificación obligatoria.")
                elif not start_j <= change < end_j:
                    st.error("La incidencia debe ocurrir dentro de la jornada de 24 horas.")
                elif change > now():
                    st.error("No se puede registrar una incidencia futura.")
                elif change <= last_change:
                    st.error("La hora debe ser posterior al inicio o al último ajuste registrado.")
                elif elapsed_h + pd.to_numeric(adj.get("horas_transcurridas_dosificacion",pd.Series(dtype=float)),errors="coerce").sum() + remaining_hours > planned_effective + 1e-6:
                    st.error("Las horas efectivas calculadas superan las programadas; revise las paradas y las horas restantes.")
                else:
                    create_ajuste(dict(jornada_id=int(jid), momento=iso(change), registrado=iso(now()), operador=who.strip(), tipo=kind,
                        produccion_restante=remaining, horas_restantes=remaining_hours, horas_transcurridas_dosificacion=elapsed_h,
                        flujo_anterior=old_flow, flujo_recomendado=rec["recommended"], flujo_aplicado=chosen,
                        decision=choice, motivo=motive.strip() if choice=="Elegir otro flujo" else kind,
                        observacion=obs, ph=ph_change, velocidad=speed_change))
                    st.success("Recálculo vinculado a la jornada.")
                    st.rerun()
            if not adj.empty:
                st.dataframe(adj[["momento", "operador", "tipo", "flujo_anterior", "flujo_recomendado", "flujo_aplicado", "decision", "motivo"]], hide_index=True, use_container_width=True)
            st.warning("El recálculo es una recomendación de balance físico; respetar interbloqueos, procedimientos y autorización de planta. Una pérdida de resistencia no implica automáticamente falta de resina.")
    with st.expander("🔐 ADMINISTRACIÓN DEL DSS"):
        st.warning("Reiniciar elimina las jornadas, recálculos, ensayos DOE y revisiones HAZOP de esta versión. La tabla histórica anterior se conserva. Descargue un respaldo antes de continuar.")
        st.download_button("⬇️ DESCARGAR RESPALDO EXCEL", xlsx(all_sheets()), "respaldo_MP2.xlsx", key="backup_reset")
        st.caption("En Streamlit Community Cloud el archivo SQLite local puede perderse al reiniciar o redesplegar la app. Para una prueba persistente use almacenamiento externo o descargue respaldos periódicos.")
        password = st.text_input("Contraseña de administrador", type="password")
        confirmation = st.checkbox("Confirmo que descargué un respaldo y deseo eliminar las jornadas nuevas")
        if st.button("🗑️ RESETEAR JORNADAS NUEVAS"):
            try: secret = str(st.secrets.get("ADMIN_PASSWORD", ""))
            except Exception: secret = ""
            if not secret: st.error("Configure ADMIN_PASSWORD en Streamlit Secrets antes de habilitar el reinicio.")
            elif not hmac.compare_digest(password, secret): st.error("Contraseña incorrecta.")
            elif not confirmation: st.error("Debe confirmar la operación.")
            else:
                with conn() as c:
                    c.execute("DELETE FROM revisiones_hazop")
                    c.execute("DELETE FROM doe_ensayos")
                    c.execute("DELETE FROM ajustes_jornada")
                    c.execute("DELETE FROM jornadas")
                    c.execute("DELETE FROM sqlite_sequence WHERE name IN ('jornadas','ajustes_jornada','revisiones_hazop','doe_ensayos')")
                st.success("Jornadas nuevas eliminadas; historial anterior conservado.")
                st.rerun()
    st.caption("Sistema de apoyo a decisiones; no acciona bombas ni modifica el DCS automáticamente. Verificar parámetros en planta.")

elif page == "2 · Ingeniería":
    header("🔬 DSS – Ingeniería de Procesos | MP2", "Página 2 – DOE–RSM · Soft Sensor · HAZOP")
    if jornadas.empty:
        st.info("Registre primero una jornada en Operación.")
        st.stop()
    jid = st.selectbox("1. Seleccionar jornada de 24 horas", jornadas.id.tolist(),
        format_func=lambda x: f"{jornadas.loc[jornadas.id==x, 'inicio'].iloc[0]} · {jornadas.loc[jornadas.id==x, 'operador'].iloc[0]}")
    j = jornadas.loc[jornadas.id == jid].iloc[0]
    adj = load_ajustes(jid)
    st.markdown("### 2. Resumen de la jornada seleccionada")
    cards([("Producción programada", f"{j.produccion_programada:.2f} t"), ("Producción real", f"{j.produccion_real:.2f} t" if pd.notna(j.produccion_real) else "Pendiente"),
           ("pH RH", f"{j.ph:.2f}"), ("Velocidad", f"{j.velocidad:.0f} m/min")], group="ingenieria_resumen_1")
    cards([("Flujo recomendado", f"{j.flujo_recomendado:.1f} mL/min"), ("Flujo inicial aplicado", f"{j.flujo_aplicado:.1f} mL/min"),
           ("KPT inicial estimado", f"{j.kpt_inicial:.2f}"), ("KPT real", f"{j.kpt_real:.2f}" if pd.notna(j.kpt_real) else "Pendiente")], group="ingenieria_resumen_2")
    if not PH_MIN <= j.ph <= PH_MAX:
        st.warning(f"pH {j.ph:.2f} fuera del rango de referencia {PH_MIN}–{PH_MAX}.")
    st.caption(f"Jornada {j.inicio} a {j.fin} · Operador {j.operador} · {len(adj)} recálculos registrados")
    left, right = st.columns(2, gap="large")
    closed = jornadas[(jornadas.estado == "CERRADA") & jornadas.produccion_real.notna() & jornadas.consumo_real.notna()].copy()
    closed = add_segment_metrics(closed)
    with left:
        st.markdown("### 3. DOE–RSM: optimización")
        tab1, tab2, tab3 = st.tabs(["Sensibilidad física", "Superficie pH–flujo", "Modelo y datos"])
        with tab1:
            st.caption("Análisis inmediato de balance físico; no constituye por sí mismo un diseño experimental.")
            flows = np.linspace(max(0.0, j.flujo_recomendado * .6), j.flujo_recomendado * 1.4, 50)
            sens = pd.DataFrame({"Flujo": flows, "KPT": [calc(j.produccion_programada, f, j.horas_efectivas)["kpt"] for f in flows]})
            curve = alt.Chart(sens).mark_line(color="#0878d1", strokeWidth=3).encode(x=alt.X("Flujo:Q", title="Flujo RH (mL/min)"), y=alt.Y("KPT:Q", title="KPT por balance físico"), tooltip=["Flujo", "KPT"]).properties(height=280)
            rule = alt.Chart(pd.DataFrame({"Objetivo": [TARGET_KPT]})).mark_rule(color="#18a957", strokeDash=[6, 4]).encode(y="Objetivo:Q")
            st.altair_chart(curve + rule, use_container_width=True)
        with tab2:
            st.markdown("**Superficie de respuesta: resistencia húmeda estimada (N/m)**")
            st.caption("Eje X: flujo RH; eje Y: pH. La velocidad se mantiene fija. Solo se muestra si existe un modelo cuadrático estimable.")
            features = ["ph", "flujo_aplicado", "velocidad"]
            # La superficie usa solo ensayos registrados y autorizados, no datos simulados.
            trials = query("SELECT * FROM doe_ensayos WHERE kpt_real IS NOT NULL AND resistencia_humeda IS NOT NULL")
            trials = trials.rename(columns={"flujo": "flujo_aplicado"})
            model, md, msg = fit_linear_cv(trials, features, "resistencia_humeda", quadratic=True, require_all=True) if not trials.empty else (None, trials, "Registre ensayos reales autorizados para generar la superficie.")
            st.markdown("**Registros observados desde la primera jornada**")
            points = add_segment_metrics(jornadas.copy())
            if not points.empty:
                points["Tipo de registro"] = "Jornada"
                pts_chart = alt.Chart(points).mark_circle(size=110, color="#0878d1").encode(
                    x=alt.X("flujo_ponderado:Q", title="Flujo aplicado ponderado (mL/min)"),
                    y=alt.Y("ph:Q", title="pH RH"),
                    tooltip=["inicio:N", "operador:N", "flujo_ponderado:Q", "ph:Q", "velocidad:Q", "kpt_proyectado_segmentado:Q"]
                ).properties(height=290)
                st.altair_chart(pts_chart, use_container_width=True)
                st.caption("Cada punto es una jornada real registrada. La posición NO representa una superficie de resistencia húmeda.")
            else:
                st.info("Aún no hay jornadas; el gráfico se llenará con el primer registro.")
            if not trials.empty:
                st.markdown("**Ensayos DOE reales registrados**")
                st.altair_chart(alt.Chart(trials).mark_circle(size=120,color="#e68a37").encode(
                    x=alt.X("flujo_aplicado:Q",title="Flujo RH (mL/min)"),
                    y=alt.Y("resistencia_humeda:Q",title="Resistencia húmeda real (N/m)"),
                    tooltip=["fecha:N","ph:Q","velocidad:Q","resistencia_humeda:Q"]
                ).properties(height=230),use_container_width=True)
            st.info(msg)
            if model and "ph" in model["features"] and "flujo_aplicado" in model["features"]:
                speed_fix = st.slider("Velocidad fija (m/min)", float(trials.velocidad.min()), float(trials.velocidad.max()), float(np.clip(j.velocidad, trials.velocidad.min(), trials.velocidad.max()))) if trials.velocidad.max() > trials.velocidad.min() else float(j.velocidad)
                pts = []
                for phv in np.linspace(float(trials.ph.min()), float(trials.ph.max()), 24):
                    for fv in np.linspace(float(trials.flujo_aplicado.min()), float(trials.flujo_aplicado.max()), 24):
                        value = predict(model, {"ph": phv, "flujo_aplicado": fv, "velocidad": speed_fix})
                        pts.append({"pH": phv, "Flujo": fv, "Resistencia": value})
                heat = alt.Chart(pd.DataFrame(pts)).mark_rect().encode(
                    x=alt.X("Flujo:Q", bin=alt.Bin(maxbins=24), title="Flujo RH (mL/min)"),
                    y=alt.Y("pH:Q", bin=alt.Bin(maxbins=24), title="pH RH"),
                    color=alt.Color("mean(Resistencia):Q", title="Resistencia (N/m)", scale=alt.Scale(scheme="viridis")),
                    tooltip=[alt.Tooltip("mean(Resistencia):Q", format=".2f", title="N/m")]).properties(height=310)
                st.altair_chart(heat, use_container_width=True)
                st.caption("Escala de color: azul/violeta = menor resistencia; verde/amarillo = mayor resistencia. Solo dentro de los factores ensayados.")
                st.caption("Superficie exploratoria ajustada a ensayos autorizados: revisar residuos, repeticiones, rango experimental y validación antes de optimizar.")
                st.markdown("**Curva: flujo RH vs. resistencia húmeda estimada**")
                curve_rows=[]
                for f in np.linspace(float(trials.flujo_aplicado.min()), float(trials.flujo_aplicado.max()), 50):
                    curve_rows.append({"Flujo":f,"Resistencia":predict(model,{"flujo_aplicado":f,"ph":float(j.ph) if trials.ph.min()<=j.ph<=trials.ph.max() else float(trials.ph.median()),"velocidad":speed_fix})})
                st.altair_chart(alt.Chart(pd.DataFrame(curve_rows)).mark_line(point=True,color="#0c7a9a").encode(x=alt.X("Flujo:Q",title="Flujo RH (mL/min)"),y=alt.Y("Resistencia:Q",title="Resistencia húmeda estimada (N/m)"),tooltip=["Flujo:Q","Resistencia:Q"]).properties(height=230),use_container_width=True)
                st.warning("No se calcula un óptimo autorizado hasta verificar calidad, estabilidad y límites operativos.")
            else:
                st.warning("La superficie real todavía no es estimable. Se mostrará cuando existan suficientes mediciones independientes de flujo, pH, velocidad y resistencia húmeda.")
        with tab3:
            st.write("Respuestas: **KPT real (kg/t)** y **resistencia húmeda real (N/m)**. Referencias de calidad: 40 / 68 / 80 N/m.")
            st.caption("Confirmar los límites y la unidad en la ficha de laboratorio. Para un DOE–RSM formal se requiere diseño experimental autorizado, niveles y puntos centrales/repeticiones, control de factores y validación.")
            st.info("Plan DOE sugerido para evaluación de Ingeniería: A = flujo RH, B = pH RH, C = velocidad. Definir por escrito niveles bajo/centro/alto, número de réplicas, aleatorización y seguridad antes de realizar pruebas. Nunca cambiar consignas de planta por indicación automática del DSS.")
            if not trials.empty:
                st.dataframe(trials[["fecha", "autorizado_por", "flujo_aplicado", "ph", "velocidad", "kpt_real", "resistencia_humeda"]], hide_index=True, use_container_width=True)
            st.markdown("**Registrar ensayo real autorizado**")
            with st.form("doe_run"):
                auth=st.text_input("Responsable que autorizó el ensayo")
                f_run=st.number_input("Flujo RH del ensayo (mL/min)",min_value=0.01,value=float(j.flujo_aplicado))
                ph_run=st.number_input("pH RH del ensayo",min_value=0.0,max_value=14.0,value=float(j.ph),step=0.01)
                v_run=st.number_input("Velocidad del ensayo (m/min)",min_value=0.01,value=max(1.0,float(j.velocidad)))
                k_run=st.text_input("KPT REAL del ensayo (kg/t), obligatorio")
                w_run=st.text_input("Resistencia húmeda REAL del ensayo (N/m), obligatorio")
                notes_run=st.text_input("Código de muestra, condiciones, observaciones")
                if st.form_submit_button("💾 REGISTRAR ENSAYO DOE"):
                    try:
                        k_val=float(k_run.replace(",","."));w_val=float(w_run.replace(",","."))
                        if not auth.strip() or k_val<=0 or w_val<=0 or not np.isfinite(k_val+w_val): raise ValueError()
                        with conn() as c:
                            c.execute("INSERT INTO doe_ensayos(jornada_id,fecha,autorizado_por,flujo,ph,velocidad,kpt_real,resistencia_humeda,observacion) VALUES(?,?,?,?,?,?,?,?,?)",(int(jid),iso(now()),auth.strip(),f_run,ph_run,v_run,k_val,w_val,notes_run))
                        st.success("Ensayo real registrado.");st.rerun()
                    except ValueError:
                        st.error("Ingrese responsable y resultados reales numéricos positivos.")
            if model:
                cards([("R² validación", f"{model['metrics']['R2']:.3f}"), ("RMSE resistencia", f"{model['metrics']['RMSE']:.2f} N/m")])
    with right:
        st.markdown("### 4. Soft Sensor: predicción de KPT")
        features = ["produccion_programada", "horas_efectivas", "velocidad", "ph", "flujo_ponderado"]
        sensor, sd, smsg = fit_linear_cv(closed, features, "kpt_real", quadratic=False)
        st.markdown("**Seguimiento desde la primera jornada**")
        sensor_view = chart_days(add_segment_metrics(jornadas.copy()))
        sensor_view["KPT físico proyectado"] = sensor_view["kpt_proyectado_segmentado"]
        sensor_view["KPT real"] = sensor_view["kpt_real"]
        sensor_view["Objetivo 10.07"] = TARGET_KPT
        line_chart(sensor_view,["KPT físico proyectado","KPT real","Objetivo 10.07"],"KPT (kg/t)",y_max=40)
        st.caption("El KPT proyectado es balance físico, no predicción del Soft Sensor. El real aparece al cerrar la jornada.")
        st.info(smsg)
        if sensor:
            m = sensor["metrics"]
            cards([("R²", f"{m['R2']:.3f}"), ("RMSE", f"{m['RMSE']:.2f} kg/t"), ("MAE", f"{m['MAE']:.2f} kg/t")])
            sd = sd.copy()
            sd["Predicho CV"] = sensor["cv"]
            d = chart_days(sd)
            d["KPT real"] = d.kpt_real
            d["Objetivo 10.07"] = TARGET_KPT
            line_chart(d, ["KPT real", "Predicho CV", "Objetivo 10.07"], "KPT (kg/t)", center_target=True)
            if np.any(sensor["cv"] < 0):
                st.error("El modelo produjo predicciones negativas no físicas. No utilizarlo para recomendar dosificación.")
            if m["R2"] < 0.7 or m["MAE"] > 1.0:
                st.warning("Precisión predictiva insuficiente según umbrales ilustrativos; no usar para control operativo. Los criterios definitivos deben acordarse con Ingeniería.")
            with st.expander("Validación estadística · dispersión"):
                d2 = d.dropna(subset=["Predicho CV", "KPT real"])
                st.altair_chart(alt.Chart(d2).mark_circle(size=85).encode(x="Predicho CV:Q", y="KPT real:Q", tooltip=["inicio", "Predicho CV", "KPT real"]).properties(height=250), use_container_width=True)
            with st.expander("Variables del modelo"):
                st.write(", ".join(sensor["features"]))
        else:
            st.metric("KPT estimado por balance físico", f"{j.kpt_inicial:.3f} kg/t")
            st.caption("El balance físico no se presenta como predicción de Soft Sensor.")
        st.markdown("#### Cierre real de jornada (24 horas)")
        if j.estado == "CERRADA":
            wet_label=f"{j.resistencia_humeda:.2f} N/m" if pd.notna(j.resistencia_humeda) else "pendiente laboratorio"
            st.success(f"Cerrada · Producción real {j.produccion_real:.2f} t · Consumo {j.consumo_real:.2f} kg · Resistencia {wet_label}")
        else:
            with st.form(f"close_{jid}"):
                actual_t_text=st.text_input("Producción REAL total de las 24 horas (t) — obligatorio",placeholder="Ej.: 76.50")
                actual_kg_text=st.text_input("Resina REAL consumida en las mismas 24 horas (kg) — obligatorio",placeholder="Ej.: 840.00")
                wet_text=st.text_input("Resistencia húmeda REAL de laboratorio (N/m) — opcional",placeholder="Ej.: 68.0; dejar vacío si aún no hay ensayo")
                st.caption("No se precargan valores ficticios. El KPT real y el exceso se calcularán al guardar.")
                if st.form_submit_button("💾 CERRAR JORNADA", type="primary"):
                    if now() < datetime.fromisoformat(str(j.fin)):
                        st.error("La jornada de 24 horas aún no ha terminado. Verifique fecha y hora de inicio.")
                    else:
                        try:
                            actual_t=float(actual_t_text.replace(",","."))
                            actual_kg=float(actual_kg_text.replace(",","."))
                            wet=float(wet_text.replace(",",".")) if wet_text.strip() else None
                            if actual_t<=0 or actual_kg<=0 or (wet is not None and wet<=0) or not np.isfinite(actual_t+actual_kg+(wet or 0)): raise ValueError()
                            close_jornada(jid, actual_t, actual_kg, wet)
                            st.success("Cierre real guardado.");st.rerun()
                        except ValueError:
                            st.error("Ingrese datos reales positivos. La resistencia es opcional.")
        if pd.notna(j.resistencia_humeda):
            if j.resistencia_humeda < WET_MIN or j.resistencia_humeda > WET_MAX:
                st.warning(f"Resistencia húmeda {j.resistencia_humeda:.2f} N/m fuera de referencia 40–80 N/m.")
            else:
                st.success(f"Resistencia húmeda {j.resistencia_humeda:.2f} N/m dentro de referencia; objetivo intermedio 68 N/m.")
    st.markdown("### 5. HAZOP: desviaciones y riesgos")
    st.dataframe(hazop_rows(j, adj), hide_index=True, use_container_width=True)
    st.warning("Registro preliminar basado en reglas y tolerancia ilustrativa ±3%. No sustituye un HAZOP formal revisado por un equipo competente; las causas registradas no se consideran verificadas.")
    with st.expander("🛡️ Revisión HAZOP por Ingeniería (registro formal de hallazgos)"):
        reviews=query("SELECT * FROM revisiones_hazop WHERE jornada_id=? ORDER BY id DESC",(int(jid),))
        if not reviews.empty: st.dataframe(reviews,hide_index=True,use_container_width=True)
        with st.form("hazop_review"):
            reviewer=st.text_input("Revisor / equipo responsable")
            node=st.selectbox("Nodo",["Dosificación RH","Almacenamiento RH","Trasvase RH","Control de pH","Calidad del papel"])
            parameter=st.text_input("Parámetro analizado",value="Flujo de resina")
            guide=st.selectbox("Palabra guía",["MÁS","MENOS","NINGUNO","INVERSO","OTRO"])
            deviation=st.text_input("Desviación observada")
            cause=st.text_input("Causa confirmada (o 'No confirmada')",value="No confirmada")
            consequence=st.text_input("Consecuencia potencial")
            safeguard=st.text_input("Salvaguardas existentes verificadas")
            action=st.text_input("Acción propuesta")
            owner=st.text_input("Responsable de la acción")
            status=st.selectbox("Estado de revisión",["Pendiente de validación","Revisado por Ingeniería","Acción en curso","Cerrado"])
            if st.form_submit_button("💾 GUARDAR REVISIÓN HAZOP"):
                if not all(x.strip() for x in [reviewer,node,parameter,deviation,cause,consequence,safeguard,action,owner]): st.error("Complete todos los campos del hallazgo.")
                else:
                    with conn() as c:
                        c.execute("INSERT INTO revisiones_hazop(jornada_id,registrado,revisor,nodo,parametro,palabra_guia,desviacion,causa_confirmada,consecuencia,salvaguarda,accion,responsable,estado) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(int(jid),iso(now()),reviewer,node,parameter,guide,deviation,cause,consequence,safeguard,action,owner,status))
                    st.success("Revisión registrada; su estado no implica aprobación automática del HAZOP.");st.rerun()

elif page == "3 · Resultados MP2":
    header("📊 DSS – Resultados y Seguimiento | MP2", "Página 3 – Indicadores, tendencias e impacto económico por jornada de 24 horas")
    if jornadas.empty:
        st.info("Registre jornadas para habilitar resultados.")
        st.stop()
    a, b, c = st.columns([2, 1, 1])
    with a:
        period = st.date_input("Periodo de análisis", value=(min(jornadas.dia_fecha), max(jornadas.dia_fecha)))
    with b:
        op = st.selectbox("Operador inicial", ["Todos"] + sorted(jornadas.operador.unique().tolist()))
    with c:
        state = st.selectbox("Estado", ["Todas", "CERRADA", "ABIERTA"])
    d = jornadas.copy()
    if isinstance(period, (tuple, list)) and len(period) == 2:
        d = d[d.dia_fecha.between(period[0], period[1])]
    elif isinstance(period, (tuple, list)) and len(period) == 1:
        d = d[d.dia_fecha == period[0]]
    if op != "Todos": d = d[d.operador == op]
    if state != "Todas": d = d[d.estado == state]
    if d.empty:
        st.warning("No hay jornadas para los filtros.")
        st.stop()
    real = d[(d.estado == "CERRADA") & d.produccion_real.notna() & d.consumo_real.notna()].copy()
    total_t = real.produccion_real.sum()
    weighted = real.consumo_real.sum()/total_t if total_t > 0 else None
    adjustments = load_ajustes()
    adjustments = adjustments[adjustments.jornada_id.isin(d.id)] if not adjustments.empty else adjustments
    accepted = int((d.decision == "Aceptar recomendación").sum()) + (int((adjustments.decision == "Aceptar recomendación").sum()) if not adjustments.empty else 0)
    decisions = len(d) + len(adjustments)
    acceptance = 100 * accepted / decisions if decisions else 0
    st.markdown("### 1. Indicadores principales")
    cards([("KPT real ponderado", f"{weighted:.2f} kg/t" if weighted is not None else "Pendiente"),
           ("Sobreconsumo real", f"{real.exceso_real.sum():,.2f} kg" if not real.empty else "Pendiente"),
           ("Valoración del exceso", f"USD {real.impacto_real.sum():,.2f}" if not real.empty else "Pendiente"),
           ("Aceptación DSS", f"{acceptance:.1f}%")], group="resultados_kpis")
    st.caption(f"{len(real)} jornadas cerradas de {len(d)} seleccionadas. Aceptación sobre {decisions} decisiones iniciales y recálculos. La valoración económica no es ahorro realizado.")
    st.markdown("### Seguimiento progresivo desde el primer registro")
    progressive = chart_days(add_segment_metrics(d.copy()))
    progressive["KPT físico proyectado"] = progressive["kpt_proyectado_segmentado"]
    progressive["KPT real"] = progressive["kpt_real"]
    progressive["Objetivo 10.07"] = TARGET_KPT
    left_prog, right_prog = st.columns(2)
    with left_prog:
        st.markdown("**KPT estimado, real y objetivo**")
        line_chart(progressive,["KPT físico proyectado","KPT real","Objetivo 10.07"],"KPT (kg/t)",y_max=40)
    with right_prog:
        st.markdown("**Flujo recomendado vs. aplicado ponderado**")
        fl = progressive.melt(id_vars=["Día","inicio"],value_vars=["flujo_recomendado","flujo_ponderado"],var_name="Serie",value_name="mL/min")
        upper=max(2000.,float(fl["mL/min"].max())*1.08)
        st.altair_chart(alt.Chart(fl).mark_bar().encode(x=alt.X("Día:N",sort=progressive.Día.tolist()),xOffset="Serie:N",y=alt.Y("mL/min:Q",scale=alt.Scale(domain=[0,upper],nice=False)),color="Serie:N",tooltip=["Día:N","Serie:N","mL/min:Q"]).properties(height=285),use_container_width=True)
    st.caption("Los indicadores de consumo REAL y costo REAL solo se calculan con jornadas cerradas; sin cierre se muestran como pendientes.")
    if not real.empty:
        real = add_segment_metrics(real)
        sensor3, sd3, msg3 = fit_linear_cv(real,["produccion_programada","horas_efectivas","velocidad","ph","flujo_ponderado"],"kpt_real")
        r = chart_days(real)
        r["KPT real"] = r.kpt_real
        r["KPT predicho (CV)"] = np.nan
        if sensor3 is not None:
            preds = dict(zip(sd3.id.astype(int),sensor3["cv"]))
            r["KPT predicho (CV)"] = r.id.map(preds)
        r["Objetivo 10.07"] = TARGET_KPT
        l, rr = st.columns([1.4, 1])
        with l:
            st.markdown("### 2. Evolución diaria del KPT")
            line_chart(r, ["KPT real", "KPT predicho (CV)", "Objetivo 10.07"], "KPT (kg/t)", center_target=True)
            st.caption("Predicción fuera de muestra (una jornada excluida) cuando el modelo tiene datos suficientes. No equivale a ahorro real.")
        with rr:
            st.markdown("### 3. Sobreconsumo por día")
            st.altair_chart(alt.Chart(r).mark_bar(color="#2e96ee").encode(x=alt.X("Día:N", sort=r.Día.tolist()), y=alt.Y("exceso_real:Q", title="Exceso (kg)"), tooltip=["Día", "inicio", "exceso_real"]).properties(height=285), use_container_width=True)
        a1, a2 = st.columns(2)
        with a1:
            st.markdown("### 4. Impacto económico por día")
            st.altair_chart(alt.Chart(r).mark_bar(color="#ed6870").encode(x=alt.X("Día:N", sort=r.Día.tolist()), y=alt.Y("impacto_real:Q", title="USD"), tooltip=["Día", "inicio", "impacto_real"]).properties(height=280), use_container_width=True)
        with a2:
            st.markdown("### 5. Resistencia húmeda por día")
            q = r.dropna(subset=["resistencia_humeda"])
            if not q.empty:
                wetline = alt.Chart(q).mark_line(point=True, color="#0878d1").encode(x=alt.X("Día:N", sort=r.Día.tolist()), y=alt.Y("resistencia_humeda:Q", title="N/m"), tooltip=["Día", "inicio", "resistencia_humeda"]).properties(height=280)
                rules = alt.Chart(pd.DataFrame({"limite": [WET_MIN, WET_REF, WET_MAX], "nombre": ["Mínimo", "Referencia", "Máximo"]})).mark_rule(strokeDash=[5, 4]).encode(y="limite:Q", color="nombre:N")
                st.altair_chart(wetline + rules, use_container_width=True)
                st.metric("Cumplimiento resistencia 40–80 N/m", f"{100*q.resistencia_humeda.between(WET_MIN,WET_MAX).mean():.1f}%")
            else: st.info("Sin mediciones de resistencia húmeda.")
    else: st.info("Aún no hay jornadas cerradas con producción y consumo reales.")
    st.markdown("### 6. Efectividad y decisiones")
    cards([("Jornadas", str(len(d))), ("Recálculos", str(len(adjustments))), ("Decisiones aceptadas", str(accepted)), ("Decisiones modificadas", str(decisions - accepted))], group="resultados_decisiones")
    if not adjustments.empty:
        st.markdown("**Incidencias y recálculos registrados**")
        st.dataframe(adjustments[["jornada_id", "momento", "operador", "tipo", "flujo_anterior", "flujo_recomendado", "flujo_aplicado", "decision", "motivo"]], hide_index=True, use_container_width=True)
    st.markdown("### 7. Historial consolidado MP2")
    st.dataframe(d[["codigo", "inicio", "fin", "operador", "estado", "produccion_programada", "produccion_real", "consumo_real", "kpt_real", "resistencia_humeda", "exceso_real", "impacto_real"]], hide_index=True, use_container_width=True)
    st.download_button("⬇️ EXPORTAR RESULTADOS A EXCEL", xlsx({"Jornadas filtradas": d, "Recálculos filtrados": adjustments}), "resultados_MP2.xlsx")
    st.caption("Los registros antiguos de 'historial' no se incluyen en los indicadores diarios porque no se ha confirmado que representen jornadas completas de 24 horas.")
