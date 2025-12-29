import os
import json
import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from datetime import date, timedelta, datetime
import time

# ----------------------------------------------------------------
# CONFIGURACIÓN: VENTANA DE ACTUALIZACIÓN (6 MESES)
# ----------------------------------------------------------------
# El script regenerará los últimos 180 días. 
# Todo lo anterior a esa fecha se dejará QUIETO como histórico.
DIAS_ATRAS = 180 

fecha_fin_proceso = date.today() - timedelta(days=1) # Hasta ayer
fecha_inicio_proceso = date.today() - timedelta(days=DIAS_ATRAS)

print(f"🚀 Iniciando Sincronización Incremental (Últimos {DIAS_ATRAS} días)")
print(f"📅 Rango de actualización: {fecha_inicio_proceso} al {fecha_fin_proceso}")

# 1. Cargar secretos (GitHub Actions)
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

# Encabezados
headers = ['Date', 'Steps', 'Gym (Minutes)', 'Ran?', 'Distance (km)', 
           'Weight (kg)', 'Body Fat (%)', 'Sleep Score', 'Sleep Time', 'HRV']

# --- A. LEER Y PROTEGER HISTÓRICO ---
print("🛡️ Analizando hoja actual para proteger datos antiguos...")

existing_data = worksheet.get_all_values()
historical_rows = []

# Función auxiliar para entender fechas de la hoja
def parse_date_smart(date_str):
    if not date_str: return None
    # Formatos posibles (incluyendo ISO y formatos locales)
    formatos = ["%Y-%m-%d", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y"]
    # Limpieza básica para meses como 'Dec'/'Dic'
    d_clean = date_str.lower().replace("dic", "dec").replace(".", "")
    for fmt in formatos:
        try:
            return datetime.strptime(d_clean, fmt).date()
        except: continue
    return None

if len(existing_data) > 1:
    # Saltamos encabezados
    for row in existing_data[1:]:
        row_date_str = row[0]
        row_date_obj = parse_date_smart(row_date_str)
        
        # LÓGICA DE ORO:
        # Si la fecha de la fila es MENOR a nuestra fecha de inicio de proceso,
        # significa que es data vieja (fuera de los 6 meses). LA GUARDAMOS.
        if row_date_obj and row_date_obj < fecha_inicio_proceso:
            historical_rows.append(row)

print(f"✅ Histórico protegido: {len(historical_rows)} días anteriores al {fecha_inicio_proceso}.")


# --- B. MAPEO DE PESO (Contexto) ---
weight_map = {}
try:
    # Descargamos contexto de peso un poco más atrás para tener referencias
    w_history = client.get_body_composition((fecha_inicio_proceso - timedelta(days=30)).isoformat(), fecha_fin_proceso.isoformat())
    lista_pesos = w_history.get('dateWeightList', []) + w_history.get('entries', [])
    for entry in lista_pesos:
        d_raw = entry.get('date', entry.get('calendarDate', ''))
        d_str = str(d_raw)[:10]
        w_raw = entry.get('weight', 0)
        if w_raw > 0:
            weight_map[d_str] = {
                'w': round(w_raw / 1000, 2),
                'f': round(entry.get('bodyFat', 0), 1)
            }
except Exception as e:
    print(f"⚠️ Aviso menor en peso: {e}")

# Valores por defecto (intentamos tomar el último del histórico si existe)
last_w = 70.0
last_f = 20.0
# Intento simple de recuperar último peso del histórico protegido
if historical_rows:
    try:
        last_row = historical_rows[0] # Como suelen estar ordenados desc, el 0 es el más reciente del histórico
        last_w = float(last_row[5])
        last_f = float(last_row[6])
    except: pass


# --- C. DESCARGA DE DATOS NUEVOS (Últimos 6 Meses) ---
print("⬇️ Descargando datos frescos de Garmin...")

filas_nuevas = []
curr = fecha_inicio_proceso

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

        # 3. Peso
        if iso_date in weight_map:
            last_w = weight_map[iso_date]['w']
            last_f = weight_map[iso_date]['f']

        # 4. Sueño
        sleep_score = "N/A"
        sleep_time_str = "N/A"
        try:
            sleep = client.get_sleep_data(iso_date)
            dto = sleep.get('dailySleepDTO', {})
            if 'sleepScores' in dto:
                sleep_score = dto['sleepScores'].get('overall', {}).get('value', "N/A")
            secs = dto.get('sleepTimeSeconds', 0)
            if secs > 0:
                sleep_time_str = f"{int(secs//3600):02d}:{int((secs%3600)//60):02d}"
        except: pass

        # 5. HRV
        hrv_val = "N/A"
        try:
            hrv_data = client.get_hrv_data(iso_date)
            if hrv_data:
                hrv_val = hrv_data.get('hrvSummary', {}).get('lastNightAvg', "N/A")
        except: pass

        filas_nuevas.append([iso_date, steps, gym, ran, dist_km, 
                             last_w, last_f, sleep_score, sleep_time_str, hrv_val])
        
        time.sleep(1.2) # Pausa amigable
        
    except Exception as e:
        print(f"❌ Error en {iso_date}: {e}")
        filas_nuevas.append([iso_date, 0, "Error", "Error", 0, 0, 0, "Error", "Error", "Error"])

    curr += timedelta(days=1)

# Ordenamos lo nuevo: Más reciente arriba
filas_nuevas.reverse()


# --- D. FUSIÓN Y GUARDADO ---
print("💾 Fusionando y guardando en Sheets...")

# UNIÓN: [Datos Nuevos (6 meses)] + [Datos Históricos (Intactos)]
final_data = filas_nuevas + historical_rows

worksheet.clear()
worksheet.update(values=[headers] + final_data, value_input_option='USER_ENTERED')

print(f"✨ ¡Sincronización Exitosa! Total filas: {len(final_data)}")
