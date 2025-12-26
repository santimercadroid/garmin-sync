import os
import json
import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from datetime import date, timedelta, datetime
import time

# 1. Cargar secretos de GitHub
# Asegúrate de que estos nombres coincidan con tus Secrets en GitHub
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

# 4. Configuración: VENTANA MÓVIL (ROLLING WINDOW)
# Definimos "Hoy" y la "Fecha de Corte" (hace 6 meses / 180 días)
hoy = date.today()
dias_ventana = 180  
fecha_corte = hoy - timedelta(days=dias_ventana)

# Garmin procesará desde la fecha de corte hasta Ayer
fecha_fin_proceso = hoy - timedelta(days=1)
fecha_inicio_proceso = fecha_corte

# Encabezados (Deben coincidir siempre para que la fusión funcione)
headers = ['Date', 'Steps', 'Gym (Minutes)', 'Ran?', 'Distance (km)', 
           'Weight (kg)', 'Body Fat (%)', 'Sleep Score', 'Sleep Time', 'HRV']

print(f"🚀 Ejecutando Sincronización Móvil (Ventana de {dias_ventana} días)...")
print(f"🔄 Se reescribirán datos desde: {fecha_inicio_proceso}")
print(f"🔒 Los datos anteriores a esa fecha se preservarán intactos.")

# --- A. MAPEO DE PESO (CON TRADUCTOR DE FECHAS) ---
weight_map = {}

try:
    print("⚖️ Descargando historial de peso reciente...")
    # Descargamos un poco más atrás de la fecha de corte para asegurar contexto
    start_weight = (fecha_inicio_proceso - timedelta(days=30)).isoformat()
    w_history = client.get_body_composition(start_weight, fecha_fin_proceso.isoformat())
    
    lista_pesos = w_history.get('dateWeightList', []) + w_history.get('entries', [])
    
    for entry in lista_pesos:
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
    
    print(f"✅ Historial peso cargado: {len(weight_map)} registros.")

except Exception as e:
    print(f"⚠️ Alerta historial peso: {e}")

# --- B. PRE-CARGA DE PESO INICIAL ---
# Buscamos el último peso conocido antes del inicio del proceso para evitar ceros
last_w = 0
last_f = 0
start_iso = fecha_inicio_proceso.isoformat()

# Ordenamos fechas para encontrar la más cercana anterior
for f in sorted(weight_map.keys()):
    if f < start_iso:
        last_w = weight_map[f]['w']
        last_f = weight_map[f]['f']

print(f"📍 Peso base inicial (arrastrado): {last_w} kg")


# --- C. PROCESAMIENTO DÍA A DÍA (SOLO VENTANA NUEVA) ---
filas_nuevas = []
curr = fecha_inicio_proceso

print("⏳ Procesando días nuevos... esto tomará unos minutos.")

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
        filas_nuevas.append([iso_date, steps, gym, ran, dist_km, 
                              last_w, last_f, sleep_score, sleep_time_str, hrv_val])
        
        # Pausa de seguridad
        time.sleep(1.2)
        
    except Exception as e:
        print(f"❌ Error en {iso_date}: {e}")
        # Fila vacía con error para mantener continuidad
        filas_nuevas.append([iso_date, 0, "Error", "Error", 0, 0, 0, "Error", "Error", "Error"])

    curr += timedelta(days=1)

# Orden Regresivo (Más reciente arriba) para los nuevos datos
filas_nuevas.reverse()


# --- D. FUSIÓN Y ACTUALIZACIÓN (DATA NUEVA + DATA VIEJA) ---
print("💾 Combinando datos nuevos con históricos...")

try:
    # 1. Leer todo lo que hay actualmente en el Sheet
    datos_actuales = worksheet.get_all_values()
    
    datos_finales = []

    if not datos_actuales:
        # Si la hoja está vacía, solo ponemos encabezados y lo nuevo
        datos_finales = [headers] + filas_nuevas
        print("📂 Hoja vacía. Se subirán solo los datos nuevos.")
    else:
        # Separamos encabezados y cuerpo viejo
        # Nota: Asumimos que la fila 1 son headers.
        filas_viejas = datos_actuales[1:]
        
        filas_historicas = []
        fecha_corte_iso = fecha_corte.isoformat()

        # 2. Filtrar: Rescatar SOLO lo que es más viejo que la fecha de corte
        for fila in filas_viejas:
            if not fila: continue # Saltar filas vacías si las hay
            
            fecha_fila = fila[0] # Asumiendo columna A es Date
            
            # Comparación de Strings ISO (YYYY-MM-DD)
            # Si fecha_fila < fecha_corte, es historia antigua -> SE QUEDA
            # Si fecha_fila >= fecha_corte, es reciente -> SE REEMPLAZA por lo nuevo
            try:
                # Limpieza simple y validación de longitud para evitar errores con filas basura
                if len(fecha_fila) >= 10 and fecha_fila < fecha_corte_iso:
                    filas_historicas.append(fila)
            except:
                # Si falla la comparación, por seguridad lo guardamos como histórico
                filas_historicas.append(fila)

        print(f"📚 Se rescataron {len(filas_historicas)} registros históricos (anteriores a {fecha_corte}).")

        # 3. Construir la lista final: 
        # [HEADERS] + [NUEVOS (Recientes)] + [HISTÓRICOS (Viejos)]
        datos_finales = [headers] + filas_nuevas + filas_historicas

    # 4. Actualizar Google Sheets (Sobreescritura completa con la lista fusionada)
    worksheet.clear()
    worksheet.update(values=datos_finales, value_input_option='USER_ENTERED')

    print(f"✨ ¡Sincronización Completa! {len(filas_nuevas)} días refrescados. {len(datos_finales)-1} días totales en hoja.")

except Exception as e:
    print(f"❌ Error crítico al actualizar Sheets: {e}")
    exit(1) # Forzar error en GitHub Actions si falla esta parte crítica
