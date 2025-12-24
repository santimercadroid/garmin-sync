import os
import json
import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from datetime import date, timedelta
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

# 4. Lógica de Fechas Dinámica y Regresiva
fecha_inicio = date.today() - timedelta(days=1) 
fecha_fin = date(2025, 1, 1)

filas_a_subir = []
encabezados = ['Fecha', 'Pasos', 'Gym (Minutos)', 'Running (km)']

print(f"🚀 Procesando datos para formato fecha...")

fecha_actual = fecha_inicio
while fecha_actual >= fecha_fin:
    iso_date = fecha_actual.isoformat()
    try:
        stats = client.get_user_summary(iso_date)
        pasos = stats.get('totalSteps', 0)
        
        actividades = client.get_activities_by_date(iso_date, iso_date)
        gym_info = "No"
        min_gym = 0
        run_info = "No"
        distancia_run_m = 0
        
        for act in actividades:
            type_key = act['activityType']['typeKey']
            if type_key == 'strength_training':
                min_gym += round(act['duration'] / 60, 1)
                gym_info = f"Sí ({min_gym} min)"
            if "run" in type_key:
                distancia_run_m += act.get('distance', 0)
        
        if distancia_run_m > 0:
            km = round(distancia_run_m / 1000, 2)
            run_info = f"Sí ({km} km)"

        filas_a_subir.append([iso_date, pasos, gym_info, run_info])
        time.sleep(1.2)
        
    except Exception as e:
        filas_a_subir.append([iso_date, "Error", "Error", "Error"])
        
    fecha_actual -= timedelta(days=1)

# 5. Limpiar y actualizar la Trix con USER_ENTERED
worksheet.clear()
# Esta es la línea clave que permite que Google Sheets reconozca la fecha
worksheet.update(values=[encabezados] + filas_a_subir, value_input_option='USER_ENTERED')

print("✨ ¡Trix actualizada! Las fechas ahora son operables.")
