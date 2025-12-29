import os
import json
import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from datetime import date, timedelta, datetime
import time

# 1. Cargar secretos de GitHub (Variables de Entorno)
user = os.environ['GARMIN_USER']
pwd = os.environ['GARMIN_PWD']
trix_id = os.environ['TRIX_ID']
google_creds = json.loads(os.environ['GOOGLE_JSON'])

# 2. Conectar a Google Sheets
scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_info(google_creds, scopes=scopes)
gc = gspread.authorize(creds)
sh = gc.open_by_key(trix_id)
worksheet = sh.worksheet("[Data] Garmin")

# 3. Conectar a Garmin
client = Garmin(user, pwd)
client.login()

# 4. Configuración: ACTUALIZACIÓN INCREMENTAL
# -------------------------------------------------------------------------
# DIAS_ATRAS controla cuánto historial revisamos y reescribimos.
# - Para uso diario normal: Usa 30 (corrige el último mes si algo cambió).
# - Para RECUPERAR historial perdido: Usa 600 (aprox. 2 años) UNA SOLA VEZ.
# -------------------------------------------------------------------------
DIAS_ATRAS = 365

fecha_fin_proceso = date.today() - timedelta(days=1)
fecha_inicio_proceso = date.today() - timedelta(days=DIAS_ATRAS)

# Encabezados de la hoja
headers = ['Date', 'Steps', 'Gym (Minutes)', 'Ran?', 'Distance (km)', 
           'Weight (kg)', 'Body Fat (%)', 'Sleep Score', 'Sleep Time', 'HRV']

print(f"🚀 Iniciando Sincronización Incremental (Últimos {DIAS_ATRAS} días)...")
print(f"📅 Periodo: {fecha_inicio_proceso} a {fecha_fin_proceso}")

# --- A. MAPEO DE PESO (CON TRADUCTOR DE FECHAS) ---
weight_map = {}

try:
    print("⚖️ Descargando historial de referencia de peso...")
    # Siempre descargamos desde 2024 para asegurar que tenemos un "último peso conocido"
    # incluso si procesamos solo esta semana.
    w_history = client.get_body_composition('2024-01-01', fecha_fin_proceso.isoformat())
    
    lista_pesos = w_history.get('dateWeightList', []) + w_history.get('entries', [])
    
    for entry in lista_pesos:
        # Obtenemos fecha cruda
        d_raw = entry.get('date', entry.get('calendarDate', ''))
        
        # Traductor de Fechas (Timestamp -> String ISO YYYY-MM-DD)
        d_str = ""
        if isinstance(d_raw, (int, float)):
            try:
                d_str = datetime.fromtimestamp(d_raw / 1000).date().isoformat()
            except: pass
        else:
            d_str = str(d_raw)[:10]

        w_raw = entry.get('weight', 0)
        if w_raw > 0 and d_str:
            w_kg = round(w_raw / 1000, 2)
            f_pct = round(entry.get('bodyFat', 0), 1)
            weight_map[d_str] = {'w': w_kg, 'f': f_pct}
    
    print(f"✅ Mapa de peso cargado: {len(weight_map)} registros.")

except Exception as e:
    print(f"⚠️ Alerta historial peso: {e}")

# --- B. PRE-CARGA DE PESO INICIAL ---
# Buscamos el último peso registrado ANTES de la fecha de inicio del proceso
# para rellenar los días sin registro de báscula.
last_w = 0
last_f = 0

start_iso = fecha_inicio_proceso.isoformat()
for f in sorted(weight_map.keys()):
    if f < start_iso:
        last_w = weight_map[f]['w']
        last_f = weight_map[f]['f']

print(f"📍 Peso base inicial tomado del historial: {last_w} kg")


# --- C. PROCESAMIENTO DÍA A DÍA (GARMIN) ---
filas_nuevas = []
curr = fecha_inicio_proceso

print("⏳ Procesando días con la API de Garmin...")

while curr <= fecha_fin_proceso:
    iso_date = curr.isoformat()
    try:
        # 1. Pasos
        stats = client.get_user_summary(iso_date)
        steps = stats.get('totalSteps', 0) if stats else 0
        
        # 2. Actividades (Gym / Correr)
        activities = client.get_activities_by_date(iso_date, iso_date)
        gym = "No"
        ran = "No"
        dist_km = 0
        
        if activities:
            for act in activities:
                type_key = act.get('activityType', {}).get('typeKey', '')
                # Gimnasio
                if type_key == 'strength_training':
                    mins = round(act.get('duration', 0) / 60, 1)
                    gym = f"Yes ({mins} min)"
                # Correr
                if "run" in type_key:
                    ran = "Yes"
                    dist_km += round(act.get('distance', 0) / 1000, 2)

        # 3. Peso (Lógica de persistencia: mantiene el último conocido)
        if iso_date in weight_map:
            last_w = weight_map[iso_date]['w']
            last_f = weight_map[iso_date]['f']

        # 4. Sueño
        sleep_score = "N/A"
        sleep_time_str = "N/A"
        try:
            sleep = client.get_sleep_data(iso_date)
            if sleep and 'dailySleepDTO' in sleep:
                dto = sleep['dailySleepDTO']
                # Score
                if 'sleepScores' in dto and 'overall' in dto['sleepScores']:
                    sleep_score = dto['sleepScores']['overall']['value']
                # Tiempo (HH:MM)
                seconds = dto.get('sleepTimeSeconds', 0)
                if seconds > 0:
                    horas = int(seconds // 3600)
                    minutos = int((seconds % 3600) // 60)
                    sleep_time_str = f"{horas:02d}:{minutos:02d}"
        except: pass

        # 5. HRV (Variabilidad de la frecuencia cardíaca)
        hrv_val = "N/A"
        try:
            hrv_data = client.get_hrv_data(iso_date)
            if hrv_data:
                hrv_val = hrv_data.get('hrvSummary', {}).get('lastNightAvg', "N/A")
        except: pass

        # Agregar fila a la lista temporal
        filas_nuevas.append([iso_date, steps, gym, ran, dist_km, 
                             last_w, last_f, sleep_score, sleep_time_str, hrv_val])
        
        # Pausa de seguridad para evitar bloqueo de API (Rate Limiting)
        time.sleep(1.2)
        
    except Exception as e:
        print(f"❌ Error procesando {iso_date}: {e}")
        # Insertamos fila de error para no romper la secuencia
        filas_nuevas.append([iso_date, 0, "Error", "Error", 0, 0, 0, "Error", "Error", "Error"])

    curr += timedelta(days=1)

# Ordenar los nuevos datos: Más reciente ARRIBA (Descendente)
filas_nuevas.reverse()


# --- D. FUSIÓN INTELIGENTE (MERGE) Y GUARDADO ---
print("🔄 Fusionando con datos históricos de Google Sheets...")

try:
    # 1. Leer TODOS los datos actuales de la hoja
    existing_data = worksheet.get_all_values()
    historical_rows = []
    
    # Definimos la fecha de corte: Todo lo anterior a nuestra fecha de inicio se conserva.
    # Todo lo posterior o igual se reemplaza por la nueva data de Garmin.
    cutoff_date = fecha_inicio_proceso.isoformat()
    
    if len(existing_data) > 1:
        # Iteramos desde la fila 1 (saltando los encabezados)
        for row in existing_data[1:]:
            # Asumimos que la Fecha está siempre en la Columna A (índice 0)
            if row and len(row) > 0:
                row_date = row[0] 
                
                # Si la fila es más vieja que lo que estamos procesando, la guardamos.
                if row_date < cutoff_date:
                    historical_rows.append(row)
    
    print(f"✅ Se conservaron {len(historical_rows)} registros históricos (anteriores a {cutoff_date}).")

    # 2. Unir: [Nuevos Datos] + [Datos Históricos]
    # (Los nuevos van arriba porque ordenamos 'filas_nuevas' con reverse)
    final_data = filas_nuevas + historical_rows

    # 3. Sobreescribir la hoja con la lista combinada
    worksheet.clear()
    worksheet.update(values=[headers] + final_data, value_input_option='USER_ENTERED')
    
    print(f"✨ ¡Sincronización Completa! Total filas en hoja: {len(final_data)}")

except Exception as e:
    print(f"❌ Error crítico al guardar en Sheets: {e}")
