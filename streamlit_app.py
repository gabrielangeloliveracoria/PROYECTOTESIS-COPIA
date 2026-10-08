from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "historial_resina.db"

TARGET_KPT = 10.07
RESIN_COST_USD_KG = 1.2723
DENSITY_KG_L = 1.064

# Rango provisional para monitoreo del pH
PH_MIN = 3.00
PH_MAX = 4.50


st.set_page_config(
    page_title="DSS – Control de Dosificación de Resina | MP2",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# ESTILO DE LA INTERFAZ
# ============================================================

st.markdown(
    """
<style>

.stApp {
    background: #f4f7fb;
}

/* Mantiene disponible el botón para abrir/cerrar el sidebar */
[data-testid="stHeader"] {
    background: transparent;
}

[data-testid="stSidebar"] {
    background: linear-gradient(180deg,#f7fbff 0%,#eef4fa 100%);
    border-right: 1px solid #dbe5ef;
}

[data-testid="stSidebar"] .block-container {
    padding-top: 1.25rem;
}

.block-container {
    padding: 1.1rem 1.55rem 2rem;
    max-width: 1800px;
}

.topbar {
    background: linear-gradient(110deg,#0a3265,#124b84);
    color: white;
    padding: 18px 25px;
    border-radius: 14px;
    margin-bottom: 16px;
    box-shadow: 0 5px 16px rgba(10,50,101,.18);
}

.topbar h1 {
    margin: 0;
    font-size: 29px;
    font-weight: 800;
    color: white;
}

.topbar p {
    margin: 7px 0 0;
    font-size: 17px;
    font-weight: 650;
    color: white;
}

.card {
    background: white;
    border: 1px solid #dbe7f1;
    border-radius: 14px;
    padding: 17px 18px;
    min-height: 112px;
    box-shadow: 0 3px 10px rgba(25,67,105,.07);
}

.card .label {
    color: #253a57;
    font-size: 15px;
    font-weight: 700;
}

.card .value {
    color: #0967c5;
    font-size: 31px;
    line-height: 1.15;
    font-weight: 850;
    margin-top: 9px;
}

.card.red .value {
    color: #d92732;
}

.card.green .value {
    color: #0d9152;
}

.card.amber .value {
    color: #b77900;
}

.alert {
    border-radius: 13px;
    padding: 18px 25px;
    margin: 12px 0;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 23px;
    font-size: 20px;
    font-weight: 750;
}

.alert strong {
    font-size: 29px;
}

.alert-red {
    background: #fff0f1;
    border: 1px solid #f05b63;
    color: #bd1721;
}

.alert-green {
    background: #eaf8f0;
    border: 1px solid #49b77d;
    color: #087641;
}

.alert-amber {
    background: #fff7e8;
    border: 1px solid #e4a83d;
    color: #9a6508;
}

.panel-title {
    color: #15365c;
    font-size: 18px;
    font-weight: 800;
    margin: 6px 0 10px;
}

div[data-testid="stButton"] button,
div[data-testid="stDownloadButton"] button {
    width: 100%;
    min-height: 45px;
    border-radius: 9px;
    font-weight: 750;
}

.small-note {
    color: #65768a;
    font-size: 12px;
    line-height: 1.35;
}

.sidebar-title {
    color: #123d6d;
    font-size: 21px;
    font-weight: 850;
    margin-bottom: 12px;
}

hr {
    border-color: #d9e4ee !important;
}

</style>
""",
    unsafe_allow_html=True,
)


# ============================================================
# BASE DE DATOS
# ============================================================

def connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def initialize_database() -> None:

    with connection() as con:

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS historial (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_hora TEXT NOT NULL,
                turno TEXT NOT NULL,
                operador TEXT NOT NULL,
                produccion_t REAL NOT NULL,
                velocidad_m_min REAL NOT NULL,
                horas REAL NOT NULL,
                ph_rh REAL,
                flujo_recomendado REAL NOT NULL,
                flujo_seleccionado REAL NOT NULL,
                resina_objetivo_kg REAL NOT NULL,
                consumo_estimado_kg REAL NOT NULL,
                kpt_estimado REAL NOT NULL,
                exceso_kg REAL NOT NULL,
                perdida_usd REAL NOT NULL,
                estado TEXT NOT NULL,
                decision TEXT NOT NULL,
                motivo TEXT NOT NULL,
                observacion TEXT NOT NULL
            )
            """
        )

        # Agrega pH a bases existentes sin borrar registros
        columnas = [
            fila[1]
            for fila in con.execute(
                "PRAGMA table_info(historial)"
            ).fetchall()
        ]

        if "ph_rh" not in columnas:
            con.execute(
                "ALTER TABLE historial ADD COLUMN ph_rh REAL"
            )


# ============================================================
# CÁLCULOS
# ============================================================

def calculate(
    production: float,
    selected_flow: float,
    hours: float,
) -> dict[str, float | str]:

    target_kg = production * TARGET_KPT

    recommended_flow = (
        target_kg / (DENSITY_KG_L * hours * 60) * 1000
        if hours
        else 0
    )

    estimated_kg = (
        selected_flow / 1000
        * 60
        * hours
        * DENSITY_KG_L
    )

    estimated_kpt = (
        estimated_kg / production
        if production
        else 0
    )

    difference_kg = estimated_kg - target_kg

    excess_kg = max(0.0, difference_kg)
    shortage_kg = max(0.0, -difference_kg)

    economic_impact = excess_kg * RESIN_COST_USD_KG

    loss_per_t = (
        economic_impact / production
        if production
        else 0
    )

    flow_adjustment = selected_flow - recommended_flow

    deviation_pct = (
        (estimated_kpt / TARGET_KPT - 1) * 100
        if TARGET_KPT
        else 0
    )

    if estimated_kpt > TARGET_KPT * 1.03:

        status = "ALTO"
        alert_class = "alert-red"

        action = (
            f"Reduzca el flujo en aproximadamente "
            f"{abs(flow_adjustment):.0f} mL/min."
        )

    elif estimated_kpt < TARGET_KPT * 0.97:

        status = "BAJO"
        alert_class = "alert-amber"

        action = (
            f"Revise el proceso; faltan aproximadamente "
            f"{shortage_kg:.2f} kg."
        )

    else:

        status = "OK"
        alert_class = "alert-green"
        action = "Mantenga la dosificación dentro del rango objetivo."

    return {
        "target_kg": target_kg,
        "recommended_flow": recommended_flow,
        "estimated_kg": estimated_kg,
        "estimated_kpt": estimated_kpt,
        "excess_kg": excess_kg,
        "shortage_kg": shortage_kg,
        "economic_impact": economic_impact,
        "loss_per_t": loss_per_t,
        "flow_adjustment": flow_adjustment,
        "deviation_pct": deviation_pct,
        "status": status,
        "alert_class": alert_class,
        "action": action,
    }


# ============================================================
# EVALUACIÓN DEL pH
# ============================================================

def evaluate_ph(ph_value: float) -> dict[str, str]:

    if PH_MIN <= ph_value <= PH_MAX:

        return {
            "status": "NORMAL",
            "class": "alert-green",
            "message": (
                f"pH RH {ph_value:.2f} dentro del rango de referencia."
            ),
        }

    elif ph_value > PH_MAX:

        return {
            "status": "pH ALTO",
            "class": "alert-amber",
            "message": (
                f"pH RH {ph_value:.2f}. Revise la condición del RH."
            ),
        }

    else:

        return {
            "status": "pH BAJO",
            "class": "alert-amber",
            "message": (
                f"pH RH {ph_value:.2f}. Revise la condición del RH."
            ),
        }


# ============================================================
# HISTORIAL
# ============================================================

def save_record(payload: dict) -> None:

    columns = ",".join(payload.keys())
    placeholders = ",".join("?" for _ in payload)

    with connection() as con:

        con.execute(
            f"INSERT INTO historial ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )


def load_history() -> pd.DataFrame:

    with connection() as con:

        return pd.read_sql_query(
            "SELECT * FROM historial ORDER BY id DESC",
            con,
        )


# ============================================================
# EXPORTAR A EXCEL
# ============================================================

def export_excel(history: pd.DataFrame) -> bytes:

    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl",
    ) as writer:

        history.to_excel(
            writer,
            index=False,
            sheet_name="Historial operativo",
        )

        if not history.empty:

            daily = (
                history.assign(
                    fecha=pd.to_datetime(
                        history["fecha_hora"]
                    ).dt.date
                )
                .groupby(
                    "fecha",
                    as_index=False,
                )
                .agg(
                    produccion_t=(
                        "produccion_t",
                        "sum",
                    ),
                    consumo_estimado_kg=(
                        "consumo_estimado_kg",
                        "sum",
                    ),
                    exceso_kg=(
                        "exceso_kg",
                        "sum",
                    ),
                    perdida_usd=(
                        "perdida_usd",
                        "sum",
                    ),
                    ph_promedio=(
                        "ph_rh",
                        "mean",
                    ),
                    registros=(
                        "id",
                        "count",
                    ),
                )
            )

            daily["kpt_ponderado"] = (
                daily["consumo_estimado_kg"]
                / daily["produccion_t"]
            )

            daily.to_excel(
                writer,
                index=False,
                sheet_name="Resumen diario",
            )

    return output.getvalue()


# ============================================================
# TARJETAS
# ============================================================

def metric_card(
    label: str,
    value: str,
    color: str = "",
) -> None:

    html = (
        f'<div class="card {color}">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f'</div>'
    )

    st.markdown(
        html,
        unsafe_allow_html=True,
    )


# ============================================================
# INICIALIZAR BASE DE DATOS
# ============================================================

initialize_database()


# ============================================================
# PANEL LATERAL
# ============================================================

with st.sidebar:

    st.markdown(
        '<div class="sidebar-title">⚙️ Datos de operación</div>',
        unsafe_allow_html=True,
    )

    production = st.number_input(
        "Producción programada (t)",
        min_value=0.01,
        value=78.41,
        step=0.10,
    )

    speed = st.number_input(
        "Velocidad (m/min)",
        min_value=0.0,
        value=1325.0,
        step=1.0,
    )

    selected_flow = st.number_input(
        "Flujo seleccionado (mL/min)",
        min_value=0.0,
        value=700.0,
        step=1.0,
    )

    hours = st.number_input(
        "Horas efectivas",
        min_value=0.1,
        max_value=24.0,
        value=24.0,
        step=0.5,
    )

    ph_rh = st.number_input(
        "pH actual del RH",
        min_value=0.00,
        max_value=14.00,
        value=4.20,
        step=0.01,
        format="%.2f",
    )

    shift = st.selectbox(
        "Turno",
        [
            "Turno A",
            "Turno B",
            "Turno C",
        ],
    )

    operator = st.text_input(
        "Operador",
        value="Operador MP2",
    )

    calculate_button = st.button(
        "▶ CALCULAR Y REGISTRAR",
        type="primary",
    )

    st.divider()

    st.markdown(
        (
            '<div class="small-note">'
            '<b>Parámetros del modelo</b><br>'
            'Objetivo: 10.07 kg/t<br>'
            'Costo: USD 1.27/kg<br>'
            'Densidad: 1.064 kg/L<br>'
            'IBC: 1,000 kg<br><br>'
            'El flujo y la densidad deben validarse '
            'antes de uso operativo.'
            '</div>'
        ),
        unsafe_allow_html=True,
    )


# ============================================================
# RESULTADOS
# ============================================================

result = calculate(
    production,
    selected_flow,
    hours,
)

ph_result = evaluate_ph(
    ph_rh
)

now_label = datetime.now().strftime(
    "%d/%m/%Y · %H:%M"
)


# ============================================================
# ENCABEZADO CORREGIDO
# ============================================================

st.markdown(
    (
        '<div class="topbar">'
        '<h1>📊 DSS – Control de Dosificación de Resina | MP2</h1>'
        f'<p>Objetivo: {TARGET_KPT:.2f} kg/t · {now_label}</p>'
        '</div>'
    ),
    unsafe_allow_html=True,
)


# ============================================================
# INDICADORES PRINCIPALES
# ============================================================

top = st.columns(4)

with top[0]:

    metric_card(
        "🧪 Resina recomendada",
        f'{result["target_kg"]:,.2f} kg',
    )

with top[1]:

    metric_card(
        "⚙️ Flujo recomendado",
        f'{result["recommended_flow"]:,.0f} mL/min',
    )

with top[2]:

    metric_card(
        "📊 KPT estimado",
        f'{result["estimated_kpt"]:,.2f} kg/t',
    )

with top[3]:

    metric_card(
        "⚠️ Exceso estimado",
        f'{result["excess_kg"]:,.2f} kg',
        "red",
    )


# ============================================================
# ALERTA PRINCIPAL CORREGIDA
# ============================================================

st.markdown(
    (
        f'<div class="alert {result["alert_class"]}">'
        f'<strong>{result["status"]}</strong>'
        f'<span>{result["action"]}</span>'
        '</div>'
    ),
    unsafe_allow_html=True,
)


# ============================================================
# IMPACTO ECONÓMICO + pH
# ============================================================

money = st.columns(4)

with money[0]:

    metric_card(
        "💰 Impacto económico",
        f'USD {result["economic_impact"]:,.2f}',
        "green",
    )

with money[1]:

    metric_card(
        "📉 Pérdida por tonelada",
        f'USD {result["loss_per_t"]:,.2f}/t',
        "red",
    )

with money[2]:

    metric_card(
        "🏷️ Costo de resina",
        f"USD {RESIN_COST_USD_KG:.2f}/kg",
    )

with money[3]:

    ph_card_color = (
        "green"
        if ph_result["status"] == "NORMAL"
        else "amber"
    )

    metric_card(
        "🧪 pH del RH",
        f"{ph_rh:.2f}",
        ph_card_color,
    )


# ============================================================
# ALERTA pH
# Solo aparece si el pH está fuera del rango
# ============================================================

if ph_result["status"] != "NORMAL":

    st.markdown(
        (
            f'<div class="alert {ph_result["class"]}">'
            f'<strong>{ph_result["status"]}</strong>'
            f'<span>{ph_result["message"]}</span>'
            '</div>'
        ),
        unsafe_allow_html=True,
    )


# ============================================================
# DECISIÓN DEL OPERADOR
# ============================================================

if calculate_button:

    st.session_state["show_decision"] = True


if st.session_state.get(
    "show_decision",
    False,
):

    st.markdown(
        (
            '<div class="panel-title">'
            'Confirmación de la decisión operativa'
            '</div>'
        ),
        unsafe_allow_html=True,
    )

    decision_col, reason_col = st.columns(
        [1, 2]
    )

    with decision_col:

        decision = st.radio(
            "Decisión",
            [
                "Aceptar recomendación",
                "Mantener selección",
            ],
            horizontal=True,
        )

    with reason_col:

        if decision == "Mantener selección":

            reason = st.selectbox(
                "Motivo obligatorio",
                [
                    "Seleccione un motivo",
                    "Baja resistencia",
                    "Inestabilidad del proceso",
                    "Cambio de producto",
                    "Orden del jefe de turno",
                    "Falla del dosificador",
                    "Parada o reducción de velocidad",
                    "Otro",
                ],
            )

        else:

            reason = "Recomendación aceptada"

    observation = st.text_input(
        "Observación",
        placeholder="Detalle opcional para retroalimentación",
    )

    if st.button(
        "💾 GUARDAR EN HISTORIAL",
        type="primary",
    ):

        if (
            decision == "Mantener selección"
            and reason == "Seleccione un motivo"
        ):

            st.error(
                "Seleccione el motivo por el que se mantuvo "
                "una dosificación diferente."
            )

        else:

            final_flow = (
                result["recommended_flow"]
                if decision == "Aceptar recomendación"
                else selected_flow
            )

            final_result = calculate(
                production,
                final_flow,
                hours,
            )

            save_record(
                {
                    "fecha_hora":
                        datetime.now().isoformat(
                            timespec="seconds"
                        ),

                    "turno":
                        shift,

                    "operador":
                        operator.strip()
                        or "Sin identificar",

                    "produccion_t":
                        production,

                    "velocidad_m_min":
                        speed,

                    "horas":
                        hours,

                    # pH guardado en el mismo registro
                    "ph_rh":
                        ph_rh,

                    "flujo_recomendado":
                        result["recommended_flow"],

                    "flujo_seleccionado":
                        final_flow,

                    "resina_objetivo_kg":
                        result["target_kg"],

                    "consumo_estimado_kg":
                        final_result["estimated_kg"],

                    "kpt_estimado":
                        final_result["estimated_kpt"],

                    "exceso_kg":
                        final_result["excess_kg"],

                    "perdida_usd":
                        final_result["economic_impact"],

                    "estado":
                        final_result["status"],

                    "decision":
                        decision,

                    "motivo":
                        reason,

                    "observacion":
                        observation,
                }
            )

            st.success(
                "Registro guardado correctamente con el pH asociado."
            )

            st.session_state["show_decision"] = False

            st.rerun()


# ============================================================
# CARGAR HISTORIAL
# ============================================================

history = load_history()


# ============================================================
# DATOS PARA LOS GRÁFICOS
# ============================================================

if history.empty:

    hours_axis = list(
        range(0, 24, 2)
    )

    demo = pd.DataFrame(
        {
            "Hora":
                hours_axis,

            "KPT real":
                [
                    8.8,
                    9.2,
                    9.9,
                    10.7,
                    11.8,
                    12.7,
                    13.1,
                    13.7,
                    13.4,
                    12.1,
                    11.3,
                    12.0,
                ],

            "Objetivo":
                [TARGET_KPT] * 12,

            "Recomendado":
                [
                    480,
                    455,
                    470,
                    485,
                    510,
                    540,
                    575,
                    610,
                    625,
                    590,
                    560,
                    545,
                ],

            "Seleccionado":
                [
                    600,
                    550,
                    575,
                    600,
                    680,
                    720,
                    790,
                    770,
                    805,
                    740,
                    700,
                    725,
                ],
        }
    )

else:

    recent = (
        history
        .head(12)
        .sort_values("fecha_hora")
    )

    demo = pd.DataFrame(
        {
            "Hora":
                pd.to_datetime(
                    recent["fecha_hora"]
                ).dt.strftime(
                    "%d/%m %H:%M"
                ),

            "KPT real":
                recent["kpt_estimado"],

            "Objetivo":
                TARGET_KPT,

            "Recomendado":
                recent["flujo_recomendado"],

            "Seleccionado":
                recent["flujo_seleccionado"],
        }
    )


# ============================================================
# GRÁFICOS
# ============================================================

chart_left, chart_right = st.columns(2)


# ============================================================
# GRÁFICO KPT
# ============================================================

with chart_left:

    st.markdown(
        '<div class="panel-title">📈 Tendencia diaria del KPT</div>',
        unsafe_allow_html=True,
    )

    line_data = demo.melt(
        "Hora",
        [
            "KPT real",
            "Objetivo",
        ],
        var_name="Serie",
        value_name="KPT",
    )

    chart = (
        alt.Chart(line_data)
        .mark_line(
            point=True,
            strokeWidth=3,
        )
        .encode(
            x=alt.X(
                "Hora:N",
                title="Periodo",
                sort=None,
            ),

            y=alt.Y(
                "KPT:Q",
                title="KPT (kg/t)",
                scale=alt.Scale(
                    zero=False
                ),
            ),

            color=alt.Color(
                "Serie:N",
                scale=alt.Scale(
                    domain=[
                        "KPT real",
                        "Objetivo",
                    ],
                    range=[
                        "#0878d1",
                        "#18a957",
                    ],
                ),
                legend=alt.Legend(
                    orient="bottom"
                ),
            ),

            strokeDash=alt.StrokeDash(
                "Serie:N",
                scale=alt.Scale(
                    domain=[
                        "KPT real",
                        "Objetivo",
                    ],
                    range=[
                        [1, 0],
                        [7, 5],
                    ],
                ),
                legend=None,
            ),

            tooltip=[
                "Hora:N",
                "Serie:N",
                alt.Tooltip(
                    "KPT:Q",
                    format=".2f",
                ),
            ],
        )
        .properties(
            height=290
        )
    )

    st.altair_chart(
        chart,
        use_container_width=True,
    )


# ============================================================
# GRÁFICO DE FLUJO
# ============================================================

with chart_right:

    st.markdown(
        (
            '<div class="panel-title">'
            '📊 Flujo recomendado vs. seleccionado'
            '</div>'
        ),
        unsafe_allow_html=True,
    )

    flow_data = demo.melt(
        "Hora",
        [
            "Recomendado",
            "Seleccionado",
        ],
        var_name="Flujo",
        value_name="mL/min",
    )

    bars = (
        alt.Chart(flow_data)
        .mark_bar()
        .encode(
            x=alt.X(
                "Hora:N",
                title="Periodo",
                sort=None,
            ),

            y=alt.Y(
                "mL/min:Q",
                title="Flujo (mL/min)",
            ),

            xOffset="Flujo:N",

            color=alt.Color(
                "Flujo:N",
                scale=alt.Scale(
                    domain=[
                        "Recomendado",
                        "Seleccionado",
                    ],
                    range=[
                        "#0878d1",
                        "#e5434d",
                    ],
                ),
                legend=alt.Legend(
                    orient="bottom"
                ),
            ),

            tooltip=[
                "Hora:N",
                "Flujo:N",
                alt.Tooltip(
                    "mL/min:Q",
                    format=".0f",
                ),
            ],
        )
        .properties(
            height=290
        )
    )

    st.altair_chart(
        bars,
        use_container_width=True,
    )


# ============================================================
# HISTORIAL Y TRAZABILIDAD
# ============================================================

st.markdown(
    '<div class="panel-title">🗒️ Historial y trazabilidad</div>',
    unsafe_allow_html=True,
)


if history.empty:

    st.info(
        "Todavía no hay registros. "
        "Calcula una dosificación y confirma "
        "la decisión del operador."
    )

else:

    display_columns = {
        "fecha_hora":
            "Fecha y hora",

        "turno":
            "Turno",

        "operador":
            "Operador",

        "produccion_t":
            "Producción (t)",

        "velocidad_m_min":
            "Velocidad (m/min)",

        "ph_rh":
            "pH RH",

        "flujo_recomendado":
            "Recomendado (mL/min)",

        "flujo_seleccionado":
            "Seleccionado (mL/min)",

        "kpt_estimado":
            "KPT",

        "exceso_kg":
            "Exceso (kg)",

        "perdida_usd":
            "Pérdida (USD)",

        "estado":
            "Estado",

        "motivo":
            "Motivo",
    }

    visible = history[
        list(display_columns)
    ].rename(
        columns=display_columns
    )

    st.dataframe(
        visible,
        use_container_width=True,
        hide_index=True,

        column_config={
            "Producción (t)":
                st.column_config.NumberColumn(
                    format="%.2f"
                ),

            "Velocidad (m/min)":
                st.column_config.NumberColumn(
                    format="%.0f"
                ),

            "pH RH":
                st.column_config.NumberColumn(
                    format="%.2f"
                ),

            "Recomendado (mL/min)":
                st.column_config.NumberColumn(
                    format="%.0f"
                ),

            "Seleccionado (mL/min)":
                st.column_config.NumberColumn(
                    format="%.0f"
                ),

            "KPT":
                st.column_config.NumberColumn(
                    format="%.2f"
                ),

            "Exceso (kg)":
                st.column_config.NumberColumn(
                    format="%.2f"
                ),

            "Pérdida (USD)":
                st.column_config.NumberColumn(
                    format="USD %.2f"
                ),
        },
    )

    st.download_button(
        "⬇️ EXPORTAR HISTORIAL A EXCEL",

        data=export_excel(
            history
        ),

        file_name=(
            f"historial_dosificacion_MP2_"
            f"{datetime.now():%Y%m%d}.xlsx"
        ),

        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )


# ============================================================
# NOTA FINAL
# ============================================================

st.caption(
    "Prototipo DSS de apoyo a la decisión. "
    "No modifica automáticamente la bomba o válvula. "
    "La unidad de flujo, densidad y límites deben validarse "
    "en planta antes de uso operativo."
)

# ============================================================
# ADMINISTRACIÓN - RESETEAR HISTORIAL
# ============================================================

st.divider()

with st.expander("🔐 ADMINISTRACIÓN DEL DSS"):

    st.warning(
        "El reseteo eliminará permanentemente todos los registros "
        "del historial operativo. Exporte el historial a Excel antes "
        "de continuar si desea conservar los resultados."
    )

    reset_password = st.text_input(
        "Contraseña de administrador",
        type="password",
        key="reset_password"
    )

    confirm_reset = st.checkbox(
        "Confirmo que deseo eliminar todo el historial",
        key="confirm_reset"
    )

    if st.button(
        "🗑️ RESETEAR HISTORIAL",
        type="secondary",
        use_container_width=True
    ):

        if reset_password != "72379280":

            st.error("❌ Contraseña incorrecta.")

        elif not confirm_reset:

            st.warning(
                "⚠️ Marque la casilla de confirmación antes de resetear."
            )

        else:

            with connection() as con:

                # Elimina todos los registros
                con.execute("DELETE FROM historial")

                # Reinicia el contador ID para que vuelva a comenzar en 1
                con.execute(
                    "DELETE FROM sqlite_sequence "
                    "WHERE name='historial'"
                )

                con.commit()

            # Limpia estados temporales de la interfaz
            st.session_state["show_decision"] = False

            st.success(
                "✅ Historial eliminado correctamente. "
                "El DSS comenzará una nueva prueba desde cero."
            )

            st.rerun()