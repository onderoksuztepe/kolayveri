import json
import re
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

src = Path("ami_to_mongo.py").read_text()

def find_string(name):
    m = re.search(rf'^{name}\s*=\s*[\"\']([^\"\']+)[\"\']', src, re.M)
    return m.group(1) if m else None

AMI_API_BASE = find_string("AMI_API_BASE")
AMI_TOKEN = find_string("AMI_TOKEN") or find_string("AMI_API_TOKEN") or find_string("TOKEN")

if not AMI_API_BASE:
    raise SystemExit("AMI_API_BASE bulunamadı.")
if not AMI_TOKEN:
    raise SystemExit("AMI_TOKEN bulunamadı.")

headers = {
    "Authorization": f"Bearer {AMI_TOKEN}"
}

meter_serial = "MSY300267184"

end = datetime.now(timezone.utc)
start = end - timedelta(days=2)

url = f"{AMI_API_BASE}/meters/{meter_serial}/read/data/"
params = {
    "readed_at__gte": start.isoformat().replace("+00:00", "Z"),
    "readed_at__lte": end.isoformat().replace("+00:00", "Z"),
}

r = requests.get(url, headers=headers, params=params, timeout=60)
print("HTTP:", r.status_code)
print("URL:", r.url.replace(AMI_TOKEN, "***TOKEN***"))
r.raise_for_status()

data = r.json()
print("Ana tip:", type(data).__name__)

if isinstance(data, dict):
    print("Ana keys:", list(data.keys()))
    sample = data.get("results") or data.get("data") or data
elif isinstance(data, list):
    sample = data
else:
    sample = data

if isinstance(sample, list):
    print("Kayıt sayısı:", len(sample))
    if sample:
        first = sample[0]
        print("İlk kayıt keys:", list(first.keys()) if isinstance(first, dict) else type(first).__name__)

        read_data = None
        if isinstance(first, dict):
            raw_data = first.get("raw_data") or first.get("rawData") or {}
            if isinstance(raw_data, dict):
                read_data = raw_data.get("readData")
            if read_data is None:
                read_data = first.get("readData")

        print("readData tipi:", type(read_data).__name__)
        print("readData örnek:")
        print(json.dumps(read_data, ensure_ascii=False, indent=2)[:5000])

        text = json.dumps(first, ensure_ascii=False)
        print("\nOBIS arama:")
        for code in ["1.8.0", "5.8.0", "8.8.0", "3.8.0"]:
            print(code, "VAR" if code in text else "YOK")
else:
    text = json.dumps(sample, ensure_ascii=False, indent=2)
    print(text[:5000])
    print("\nOBIS arama:")
    for code in ["1.8.0", "5.8.0", "8.8.0", "3.8.0"]:
        print(code, "VAR" if code in text else "YOK")
