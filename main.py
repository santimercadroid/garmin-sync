import os
import json
import gspread
import time
from datetime import date, timedelta, datetime
from google.oauth2.service_account import Credentials
from garminconnect import Garmin
from googleapiclient.discovery import build

# ==========================================
# CONFIGURACIÓN GLOBAL
# ==========================================

# Tu ID de Calendario Smoke Free
CALENDAR_ID = 'c1e6e93707811625119db5e1d581ac7c5bb519022b3d4465cb2b3ccd264e0f80@group.calendar.google.com'

# Pestaña de donde leemos los hábitos
TAB_HABITS_NAME = '[Data] Habit Input'
FECHA_INICIO_CALENDAR_SYNC = "2025-01-01"

# Ventana de actualización para Garmin
DIAS_ATRAS_GARMIN = 180 

# ==========================================
# INICIO DEL SCRIPT
# ==========================================

# 1. Cargar secretos (GitHub Actions)
user = os.environ['GARMIN_USER']
pwd = os.environ['GARMIN_PWD']
trix_id = os.environ['TRIX_ID'] # Este es tu SHEET_ID
google_creds = json.loads(os.environ['GOOGLE_JSON'])

# 2. Conectar a Google (Sheets + Drive + Calendar)
# NOTA: Agregamos el scope de Calendar aquí
scopes = [
    'https://www.googleapis.com/auth/spreadsheets', 
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/calendar'
]
creds = Credentials.from_service_account_info(google_creds, scopes=scopes)
gc = gspread.authorize(creds)
sh = gc.open_by_key(trix_id)


# ==========================================
# PARTE A: SINCRONIZACIÓN GARMIN
# ==========================================

def run_garmin_sync():
    fecha_fin_proceso = date.today() - timedelta(days=1) # Hasta ayer
    fecha_inicio_proceso = date.today() - timedelta(days=DIAS_ATRAS_GARMIN)

    print(f"\n🚀 [GARMIN] Iniciando Sincronización (Últimos {DIAS_ATRAS_GARMIN} días)")
    print(f"📅 Rango: {fecha_inicio_proceso} al {fecha_fin_proceso}")

    try:
        worksheet = sh.worksheet("[Data] Garmin")
        
        # Conectar a Garmin
        client = Garmin(user, pwd)
        client.login()

        # Encabezados
        headers = ['Date', 'Steps', 'Gym (Minutes)', 'Ran?', 'Distance (km)', 
                   'Weight (kg)', 'Body Fat (%)', 'Sleep Score', 'Sleep Time', 'HRV']

        # --- PROTECCIÓN DE HISTÓRICO ---
        print("🛡️ [GARMIN] Analizando hoja actual para proteger datos antiguos...")
        existing_data = worksheet.get_all_values()
        historical_rows = []

        def parse_date_smart(date_str):
            if not date_str: return None
            formatos = ["%Y-%m-%d", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y"]
            d_clean = str(date_str).lower().replace("dic", "dec").replace(".", "")
            for fmt in formatos:
                try:
                    return datetime.strptime(d_clean, fmt).date()
                except: continue
            return None

        if len(existing_data) > 1:
            for row in existing_data[1:]:
                row_date_str = row[0]
                row_date_obj = parse_date_smart(row_date_str)
                if row_date_obj and row_date_obj < fecha_inicio_proceso:
                    historical_rows.append(row)
        
        print(f"✅ [GARMIN] Histórico protegido: {len(historical_rows)} días.")

        # --- MAPEO DE PESO ---
        weight_map = {}
        try:
            start_weight = (fecha_inicio_proceso - timedelta(days=30)).isoformat()
            w_history = client.get_body_composition(start_weight, fecha_fin_proceso.isoformat())
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
            print(f"⚠️ [GARMIN] Aviso menor en peso: {e}")

        # Valores por defecto (del histórico)
        last_w = 70.0
        last_f = 20.0
        if historical_rows:
            try:
                last_row = historical_rows[0] 
                last_w = float(last_row[5])
                last_f = float(last_row[6])
            except: pass

        # --- DESCARGA DE DATOS ---
        print("⬇️ [GARMIN] Descargando datos frescos...")
        filas_nuevas = []
        curr = fecha_inicio_proceso

        while curr <= fecha_fin_proceso:
            iso_date = curr.isoformat()
            try:
                # Stats básicas
                stats = client.get_user_summary(iso_date)
                steps = stats.get('totalSteps', 0) if stats else 0
                
                # Actividades
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

                # Peso
                if iso_date in weight_map:
                    last_w = weight_map[iso_date]['w']
                    last_f = weight_map[iso_date]['f']

                # Sueño & HRV
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

                hrv_val = "N/A"
                try:
                    hrv_data = client.get_hrv_data(iso_date)
                    if hrv_data:
                        hrv_val = hrv_data.get('hrvSummary', {}).get('lastNightAvg', "N/A")
                except: pass

                filas_nuevas.append([iso_date, steps, gym, ran, dist_km, 
                                     last_w, last_f, sleep_score, sleep_time_str, hrv_val])
                
                time.sleep(1.0) # Pausa API
                
            except Exception as e:
                print(f"❌ [GARMIN] Error en {iso_date}: {e}")
                filas_nuevas.append([iso_date, 0, "Error", "Error", 0, 0, 0, "Error", "Error", "Error"])

            curr += timedelta(days=1)

        filas_nuevas.reverse()

        # --- GUARDADO ---
        print("💾 [GARMIN] Guardando en Sheets...")
        final_data = filas_nuevas + historical_rows
        worksheet.clear()
        worksheet.update(values=[headers] + final_data, value_input_option='USER_ENTERED')
        print(f"✨ [GARMIN] Completado. Total filas: {len(final_data)}")

    except Exception as e:
        print(f"❌ [GARMIN] Error crítico: {e}")

# ==========================================
# PARTE B: SINCRONIZACIÓN CALENDARIO
# ==========================================

def parse_date_calendar(date_str):
    """Parsea fechas para el módulo de calendario."""
    if not isinstance(date_str, str): return None
    date_str = date_str.strip()
    if not date_str: return None
    
    meses = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
             'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
             'Ene': 1, 'Abr': 4, 'Ago': 8, 'Dic': 12}
    try:
        parts = date_str.replace('-',' ').split()
        if len(parts) == 3:
            d, m_str, y = parts[0], parts[1], parts[2]
            if len(y) == 2: y = "20" + y
            m_num = meses.get(m_str.title(), 0)
            if m_num > 0:
                return date(int(y), m_num, int(d)).isoformat()
    except: pass
    try:
        return datetime.strptime(date_str, "%d-%b-%y").date().isoformat()
    except: return None

def run_calendar_sync():
    print("\n🔄 [CALENDAR] Iniciando Sincronización de Días Smoke Free...")
    
    try:
        # Usamos las credenciales YA cargadas globalmente
        service_cal = build('calendar', 'v3', credentials=creds)

        # Leer Hoja de Hábitos
        print(f"📂 [CALENDAR] Leyendo hoja: {TAB_HABITS_NAME}...")
        ws_habits = sh.worksheet(TAB_HABITS_NAME)
        records = ws_habits.get_all_records()

        # Filtrar fechas validas en Excel
        fechas_sheet_validas = set()
        for row in records:
            fecha_raw = str(row.get('Effective Date', ''))
            status = str(row.get('Smoke today', ''))
            
            if "No" in status:
                f_iso = parse_date_calendar(fecha_raw)
                if f_iso and f_iso >= FECHA_INICIO_CALENDAR_SYNC:
                    fechas_sheet_validas.add(f_iso)
        
        print(f"✅ [CALENDAR] Días sin fumar en Sheet: {len(fechas_sheet_validas)}")

        # Obtener eventos actuales en Google Calendar
        events_result = service_cal.events().list(
            calendarId=CALENDAR_ID,
            timeMin=f"{FECHA_INICIO_CALENDAR_SYNC}T00:00:00Z",
            singleEvents=True,
            q="✅ Smoke Free"
        ).execute()
        
        mapa_calendario = {}
        for ev in events_result.get('items', []):
            if ev.get('summary') == "✅ Smoke Free":
                start = ev.get('start', {}).get('date') or ev.get('start', {}).get('dateTime', '')[:10]
                mapa_calendario[start] = ev['id']

        # Sincronización
        fechas_a_borrar = set(mapa_calendario.keys()) - fechas_sheet_validas
        fechas_a_crear = fechas_sheet_validas - set(mapa_calendario.keys())

        print(f"📊 [CALENDAR] Cambios: {len(fechas_a_crear)} crear, {len(fechas_a_borrar)} borrar.")

        # Borrar
        for fecha in fechas_a_borrar:
            try:
                service_cal.events().delete(calendarId=CALENDAR_ID, eventId=mapa_calendario[fecha]).execute()
                print(f"   🗑️ Eliminado: {fecha}")
                time.sleep(0.3)
            except Exception as e: print(f"   ⚠️ Error borrando {fecha}: {e}")

        # Crear
        for fecha in fechas_a_crear:
            event_body = {
                'summary': '✅ Smoke Free',
                'start': {'date': fecha}, 'end': {'date': fecha},
                'colorId': '2', 'transparency': 'transparent'
            }
            try:
                service_cal.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
                print(f"   ✨ Creado: {fecha}")
                time.sleep(0.5)
            except Exception as e: print(f"   ⚠️ Error creando {fecha}: {e}")

        print("🎉 [CALENDAR] Sincronización finalizada.")

    except Exception as e:
        print(f"❌ [CALENDAR] Error: {e}")

# ==========================================
# EJECUCIÓN PRINCIPAL
# ==========================================
if __name__ == "__main__":
    # 1. Correr Garmin
    run_garmin_sync()
    
    # 2. Correr Calendar (Si Garmin falla, esto igual se intenta ejecutar si está separado, 
    # pero aquí si run_garmin_sync tiene un try/except interno, el script continuará).
    run_calendar_sync()
