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

# 4. Lógica Regresiva (Igual que en Colab)
fecha_inicio = date.today() - timedelta(days=1)
fecha_fin = date(2025, 1, 1)
filas_a_subir = []
encabezados = ['Fecha', 'Pasos', 'Gym (Minutos)']

fecha_actual = fecha_inicio
while fecha_actual >= fecha_fin:
    iso_date = fecha_actual.isoformat()
    try:
        stats = client.get_user_summary(iso_date)
        pasos = stats.get('totalSteps', 0)
        actividades = client.get_activities_by_date(iso_date, iso_date)
        gym_info = "No"
        min_totales = 0
        for act in actividades:
            if act['activityType']['typeKey'] == 'strength_training':
                min_totales += round(act['duration'] / 60, 1)
                gym_info = f"Sí ({min_totales} min)"
        filas_a_subir.append([iso_date, pasos, gym_info])
        time.sleep(1.2)
    except:
        filas_a_subir.append([iso_date, "Error", "Error"])
    fecha_actual -= timedelta(days=1)

# 5. Limpiar y escribir en la Trix
worksheet.clear()
worksheet.update([encabezados] + filas_a_subir)
print("¡Trix actualizada exitosamente!")
