import requests

API_URL = "http://127.0.0.1:8000/api/meters/last-vs-previous"

def normalize_serial(serial):
    if serial is None:
        return None
    s = str(serial).strip()
    if s.upper().startswith("MSY"):
        s = s[3:]
    return s

rows = requests.get(API_URL, timeout=120).json()

print("API kayıt sayısı:", len(rows))
print()
print("İlk 100 API seri no:")
for i, item in enumerate(rows[:100], start=1):
    print(i, item.get("meter_serial"), "=>", normalize_serial(item.get("meter_serial")), "|", item.get("name"))
