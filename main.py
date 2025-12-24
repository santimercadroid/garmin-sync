import os
import json
import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from datetime import date, timedelta, datetime
import time

# 1. Cargar secretos de GitHub
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

# 4. Configuración: AÑO COMPLETO 2025
# Procesamos desde el 1 de Enero hasta Ayer
fecha_fin_proceso = date.today() - timedelta(days=1)
fecha_inicio_proceso = date(2025, 1, 1)

# Encabezados (En Inglés)
headers = ['Date', 'Steps', 'Gym (Minutes)', 'Ran?', 'Distance (km)', 
           'Weight (kg)', 'Body Fat (%)', 'Sleep Score', 'Sleep Time', 'HRV']

print(f"🚀 Ejecutando Sincronización Total 2025 (GitHub Actions)...")

# --- A. MAPEO DE PESO (CON TRADUCTOR DE FECHAS) ---
weight_map = {}

try:
    print("⚖️ Descargando historial completo de peso...")
    # Descargamos desde 2024 para tener contexto
    w_history = client.get_body_composition('2024-01-01', fecha_fin_proceso.isoformat())
    
    lista_pesos = w_history.get('dateWeightList', []) + w_history.get('entries', [])
    
    for entry in lista_pesos:
        # Obtenemos fecha cruda (puede ser timestamp)
        d_raw = entry.get('date', entry.get('calendarDate', ''))
        
        # Traductor de Fechas (Timestamp -> String)
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
    
    print(f"✅ Historial cargado: {len(weight_map)} registros.")

except Exception as e:
    print(f"⚠️ Alerta historial peso: {e}")

# --- B. PRE-CARGA DE PESO INICIAL ---
# Buscamos el último peso antes de 2025 para empezar el año correctamente
last_w = 0
last_f = 0

start_iso = fecha_inicio_proceso.isoformat()
for f in sorted(weight_map.keys()):
    if f < start_iso:
        last_w = weight_map[f]['w']
        last_f = weight_map[f]['f']

print(f"📍 Peso base inicial: {last_w} kg")


# --- C. PROCESAMIENTO DÍA A DÍA ---
filas_a_subir = []
curr = fecha_inicio_proceso

print("⏳ Procesando días... esto tomará unos minutos.")

while curr <= fecha_fin_proceso:
    iso_date = curr.isoformat()
    try:
        # 1. Pasos
        stats = client.get_user_summary(iso_date)
        steps = stats.get('totalSteps', 0) if stats else 0
        
        # 2. Actividades
        activities = client.get_activities_by_date(iso_date, iso_date)
        gym = "No"
        ran = "No"
        dist_km = 0
        
        if activities:
            for act in activities:
                type_key = act.get('activityType', {}).get('typeKey', '')
                if type_key == 'strength_training':
                    mins = round(act.get('duration', 0) / 60, 1)
                    gym = f"Yes ({mins} min)"
                if "run" in type_key:
                    ran = "Yes"
                    dist_km += round(act.get('distance', 0) / 1000, 2)

        # 3. Peso (Persistencia)
        if iso_date in weight_map:
            last_w = weight_map[iso_date]['w']
            last_f = weight_map[iso_date]['f']

        # 4. Sueño y Tiempo
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

        # 5. HRV
        hrv_val = "N/A"
        try:
            hrv_data = client.get_hrv_data(iso_date)
            if hrv_data:
                hrv_val = hrv_data.get('hrvSummary', {}).get('lastNightAvg', "N/A")
        except: pass

        # Agregar a la lista
        filas_a_subir.append([iso_date, steps, gym, ran, dist_km, 
                              last_w, last_f, sleep_score, sleep_time_str, hrv_val])
        
        # Pausa de seguridad (Vital para evitar bloqueo de Garmin)
        time.sleep(1.2)
        
    except Exception as e:
        print(f"❌ Error en {iso_date}: {e}")
        # Fila vacía en caso de error para mantener continuidad
        filas_a_subir.append([iso_date, 0, "Error", "Error", 0, 0, 0, "Error", "Error", "Error"])

    curr += timedelta(days=1)

# Orden Regresivo (Más reciente arriba)
filas_a_subir.reverse()

# Actualizar Google Sheets (Sobreescribir todo)
worksheet.clear()
worksheet.update(values=[headers] + filas_a_subir, value_input_option='USER_ENTERED')

print(f"✨ ¡Sincronización Completa Finalizada! {len(filas_a_subir)} días actualizados.")
