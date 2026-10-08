DSS – Control de Dosificación de Resina | MP2

Prototipo en Streamlit para recomendar la dosificación necesaria para alcanzar 10.07 kg/t, estimar sobredosificación e impacto económico, registrar la decisión del operador y exportar el historial a Excel.

Ejecución local

pip install -r requirements.txt
streamlit run app.py

Ejecución en Google Colab

Sube app.py y requirements.txt, instala las dependencias y ejecuta Streamlit mediante el túnel que utilices normalmente.

Parámetros actuales

Objetivo: 10.07 kg/t

Costo: USD 1.2723/kg

Densidad provisional: 1.064 kg/L

IBC: 1,000 kg

Velocidad habitual: 1,300–1,350 m/min

La unidad de flujo y la densidad deben validarse antes de usar el prototipo para decisiones reales de planta.