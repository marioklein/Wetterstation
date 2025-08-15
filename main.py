#reines Auslesen des Sensors und Darstellen im seriellen Monitor
# nach x Minuten in Deepsleep
#einbinden einer LCD
#WLAN und OpenSenseMap

from machine import Pin, I2C, deepsleep
from time import sleep_ms
import sys
import network
import ujson as json
from ota import OTAUpdater

# ====== KONFIG ======
SDA_PIN = 21
SCL_PIN = 22
I2C_FREQ = 100_000
SLEEP_MS = 1 * 60 * 1000   # 5 Minuten
LCD_COLS, LCD_ROWS = 16, 2
LCD_ADDRESS_OVERRIDE = None   # z.B. 0x27, sonst Auto-Scan
SETTLE_MS = 150
SAMPLES = 3
# ====================

# WLAN
WIFI_SSID = "FRITZ!Box24"
WIFI_PASS = "02470515791265058806"

# WLAN-Zugangsdaten
SSID = "FRITZ!Box24"
PASSWORD = "02470515791265058806"

# firmware_url = "https://raw.githubusercontent.com/marioklein/Feriencountdown_OTA/"
# ota_updater = OTAUpdater(SSID, PASSWORD, firmware_url, "main.py")
# ota_updater.download_and_install_update_if_available()


# openSenseMap (IDs aus deinem oSeM-Account)
OSEM_BOX_ID      = "62ebd6ba49c9a7001beb9d3d"
OSEM_SENSOR_TEMP = "62ebd6ba49c9a7001beb9d40"
OSEM_SENSOR_PRES = "62ebd6ba49c9a7001beb9d3e"
OSEM_SENSOR_HUM  = "62ebd6ba49c9a7001beb9d3f"

# Optional: Zeitstempel mitschicken (True = createdAt mit UTC "Z")
ADD_CREATED_AT = False
# ===================

# --- Hilfsfunktionen ---
def wifi_connect(ssid, pw, timeout_s=15):
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, pw)
        t0 = ticks_ms()
        while not wlan.isconnected():
            if ticks_diff(ticks_ms(), t0) > timeout_s * 1000:
                return False
            sleep_ms(200)
    return True

def iso8601_z():
    # nutzt lokale Uhr (ohne NTP evtl. nicht korrekt) -> daher standardmäßig aus
    from time import localtime
    y, m, d, hh, mm, ss, _, _ = localtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (y, m, d, hh, mm, ss)


# MicroPython hat ticks_* in time oder utime je nach Build:
try:
    from time import ticks_ms, ticks_diff
except ImportError:
    from utime import ticks_ms, ticks_diff

# LCD-Treiber importieren (separate Datei!)
from lcd_i2c import I2cLcd

def make_i2c():
    try:
        return I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)
    except Exception:
        return I2C(1, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)

def pick_bme_addr(addrs):
    return 0x76 if 0x76 in addrs else (0x77 if 0x77 in addrs else (addrs[0] if addrs else None))

def pick_lcd_addr(addrs):
    if LCD_ADDRESS_OVERRIDE is not None:
        return LCD_ADDRESS_OVERRIDE
    candidates = [a for a in addrs if 0x20 <= a <= 0x27 or 0x38 <= a <= 0x3F]
    for pref in (0x27, 0x3F):
        if pref in candidates:
            return pref
    return candidates[0] if candidates else None

def _to_float(s):
    s = str(s).replace(',', '.').strip()
    cleaned = ''.join(ch for ch in s if ch.isdigit() or ch in '.-')
    if cleaned in ('', '.', '-'):
        raise ValueError("Kann Zahl nicht parsen: %r" % s)
    return float(cleaned)

# ---- Start ----
i2c = make_i2c()
scan = i2c.scan()
print("I2C:", [hex(a) for a in scan])
if not scan:
    print("❌ Kein I2C-Gerät – schlafe und versuche später erneut.")
    deepsleep(SLEEP_MS)

# LCD init
lcd_addr = pick_lcd_addr(scan)
lcd = None
if lcd_addr is not None:
    try:
        lcd = I2cLcd(i2c, lcd_addr, LCD_ROWS, LCD_COLS)
        lcd.move_to(0,0); lcd.putstr("BME280-Start...")
    except Exception as e:
        print("LCD-Init fehlgeschlagen:", e)

# BME-Treiber laden
try:
    import bme280_float as bme_drv
    BME = bme_drv.BME280
    use_float = True
    print("Treiber: bme280_float")
except ImportError:
    try:
        import bme280 as bme_drv
        BME = bme_drv.BME280
        use_float = False
        print("Treiber: bme280 (values-Strings)")
    except ImportError:
        msg = "❌ BME280-Treiber fehlt (bme280_float.py / bme280.py)."
        print(msg)
        if lcd:
            lcd.clear(); lcd.putstr("BME-Treiber fehlt")
        deepsleep(SLEEP_MS)

bme_addr = pick_bme_addr(scan)
if bme_addr is None:
    print("❌ BME280 nicht gefunden.")
    if lcd: lcd.clear(); lcd.putstr("BME nicht da")
    deepsleep(SLEEP_MS)

try:
    bme = BME(i2c=i2c, address=bme_addr)
except TypeError:
    bme = BME(i2c=i2c, addr=bme_addr)

# Stabilisieren & mitteln
sleep_ms(SETTLE_MS)

def _to_float(s):
    # robust: nur Ziffern, Punkt, Minus behalten; Komma->Punkt; Whitespace trimmen
    s = str(s).replace(',', '.').strip()
    cleaned = ''.join(ch for ch in s if (ch.isdigit() or ch in '.-'))
    if cleaned == '' or cleaned == '.' or cleaned == '-':
        raise ValueError("Kann Zahl nicht parsen aus: %r" % s)
    return float(cleaned)

def read_once():
    if hasattr(bme, "values"):
        # bme280.py liefert Strings wie ('24.5C','40.1%','1008.3hPa')
        t_str, p_str, h_str = bme.values
        try:
            t = _to_float(t_str)
            h = _to_float(h_str)
            p = _to_float(p_str)  # schon in hPa
        except ValueError as e:
            # Debughilfe: rohe Strings einmal anzeigen
            print("Rohwerte:", t_str, h_str, p_str)
            raise
        return t, h, p
    else:
        # bme280_float.py mit Float-Attributen
        t = float(bme.temperature)      # °C
        h = float(bme.humidity)         # %
        p = float(bme.pressure) / 100.0 # Pa -> hPa
        return t, h, p

t_sum = h_sum = p_sum = 0.0
for _ in range(SAMPLES):
    t, h, p = read_once()
    t_sum += t; h_sum += h; p_sum += p
    sleep_ms(30)

t = t_sum / SAMPLES
h = h_sum / SAMPLES
p = p_sum / SAMPLES

# ---- Ausgabe ----
print("{:>8}  {:>8}  {:>8}".format("Temp [°C]", "rF [%]", "p [hPa]"))
print("{:8.2f}  {:8.2f}  {:8.2f}".format(t, h, p))

# LCD-Ausgabe

def _fit(text, width):
    s = str(text)
    n = len(s)
    if n < width:
        return s + (" " * (width - n))
    return s[:width]

def lcd_line(col, row, text):
    if lcd:
        width = LCD_COLS - col if LCD_COLS > col else 0
        lcd.move_to(col, row)
        lcd.putstr(_fit(text, width))

if lcd:
    lcd.clear()
    lcd_line(0, 0, f"Temp:{t:5.1f}C")
    lcd_line(0, 1, f"rF:{h:7.1f}%")


# --- WLAN verbinden ---
ok = wifi_connect(WIFI_SSID, WIFI_PASS, timeout_s=20)
if not ok:
    print("WLAN-Timeout -> DeepSleep")
    sleep_ms(500); deepsleep(SLEEP_MS)

# --- Upload zu openSenseMap ---
try:
    import urequests as requests
except ImportError:
    import requests  # falls Port eine requests-Compat bietet

url = "https://api.opensensemap.org/boxes/%s/data" % OSEM_BOX_ID
ts = iso8601_z() if ADD_CREATED_AT else None

meas = [
    {"sensor": OSEM_SENSOR_TEMP, "value": round(t, 2)},
    {"sensor": OSEM_SENSOR_PRES, "value": round(p, 1)},
]
if h is not None:
    meas.append({"sensor": OSEM_SENSOR_HUM, "value": round(h, 1)})

if ts:
    for m in meas:
        m["createdAt"] = ts  # UTC, "Z"

headers = {"content-type": "application/json"}
try:
    resp = requests.post(url, data=json.dumps(meas), headers=headers)
    print("oSeM HTTP", resp.status_code, getattr(resp, "text", ""))
    # Erfolg: 201 (created) oder 200
    if lcd:
        lcd.move_to(0,1); lcd.putstr(_fit("Upload OK (%d)" % resp.status_code, 16))
        sleep_ms(3000)
        lcd.clear()
        lcd_line(0, 0, f"Temp:{t:5.1f}C")
        lcd_line(0, 1, f"rF:{h:7.1f}%")
        
    try:
        resp.close()
    except Exception:
        pass
except Exception as e:
    print("Upload-Fehler:", e)
    if lcd:
        lcd.move_to(0,1); lcd.putstr(_fit("Upload ERROR", 16))



# kurze Anzeigezeit, dann Schlaf
sleep_ms(3000)
print(f"\n💤 DeepSleep für {SLEEP_MS//1000} Sekunden …")
deepsleep(SLEEP_MS)

