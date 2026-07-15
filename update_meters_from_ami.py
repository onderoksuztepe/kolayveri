#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AMI master listesini MongoDB'deki meters/readings koleksiyonlarıyla eşleştirip günceller.

Ne yapar?
- AMI CSV'deki sayaç numarasını doğru master kabul eder.
- Başındaki sıfırı düşmüş numerik sayaçları bulur ve meter_serial alanını AMI'deki orijinal hâle çeker.
- Tüm sayaçlarda name / multiplier / region / device_no gibi master bilgileri AMI CSV'ye göre günceller.
- İstenirse readings koleksiyonundaki sayaç numaralarını da aynı şekilde düzeltir.
- Her dokümana meter_serial_raw ve meter_key_normalized alanları ekler.
- Önce dry-run çalışır; --apply vermeden Mongo'ya yazmaz.

Örnek:
python update_meters_from_ami.py --ami-csv "/opt/kolayveri_ami/AMİ-Sayaçlar.csv"
python update_meters_from_ami.py --ami-csv "/opt/kolayveri_ami/AMİ-Sayaçlar.csv" --apply --update-readings
"""

import argparse
import csv
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from pymongo import MongoClient, UpdateOne
except ImportError as exc:
    raise SystemExit("pymongo bulunamadı. Kurulum: pip install pymongo") from exc


AMI_SERIAL_COL = "AMİ Sayaç no"
AMI_NAME_COL = "Firma adı ve nereye ait"
AMI_REGION_COL = "Bölge"
AMI_DEVICE_COL = "Kolayveri cihaz no"
AMI_CT_COL = "Akım trafo oranı"
AMI_MULTIPLIER_COL = "Çarpan değeri"

DEFAULT_MATCH_FIELDS = [
    "meter_serial",
    "meter_no",
    "serial",
    "sayac_no",
    "device_serial",
]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_key(value: Any) -> str:
    """Eşleştirme anahtarı. Orijinal sayaç numarasını değiştirmek için değil, aynı sayacı bulmak için kullanılır."""
    s = clean_text(value).upper()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[^0-9A-Z]", "", s)
    if s.isdigit():
        return s.lstrip("0") or "0"
    # Prefixli sayaçlarda sadece format temizliği yapıyoruz; AEL/MSY gibi prefix farkını otomatik düzeltmiyoruz.
    return s


def numeric_variants(raw_serial: str) -> List[Any]:
    """Mongo'da string/int olarak tutulmuş olabilecek numerik sayaç varyasyonları."""
    variants: List[Any] = []
    raw = clean_text(raw_serial)
    key = normalize_key(raw)
    for v in [raw, key]:
        if v and v not in variants:
            variants.append(v)
    if key.isdigit():
        try:
            variants.append(int(key))
        except Exception:
            pass
    return variants


def parse_float(value: Any) -> Optional[float]:
    s = clean_text(value)
    if s == "":
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s and s.count(".") > 0 else s.replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def expected_multiplier_from_ct(ct_ratio: str) -> Optional[float]:
    s = clean_text(ct_ratio).replace(" ", "")
    if not s or s in {"1", "1/1"}:
        return 1
    if "/" not in s:
        return None
    left, right = s.split("/", 1)
    try:
        a = float(left.replace(",", "."))
        b = float(right.replace(",", "."))
        if b == 0:
            return None
        val = a / b
        return int(val) if val.is_integer() else val
    except Exception:
        return None


def load_ami_rows(path: str, skip_suspicious_multiplier: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in [AMI_SERIAL_COL, AMI_NAME_COL, AMI_MULTIPLIER_COL] if c not in reader.fieldnames]
        if missing:
            raise SystemExit(f"AMI CSV kolonları eksik: {missing}. Gelen kolonlar: {reader.fieldnames}")

        seen = set()
        for line_no, row in enumerate(reader, start=2):
            raw_serial = clean_text(row.get(AMI_SERIAL_COL))
            if not raw_serial:
                warnings.append({"line": line_no, "serial": "", "issue": "EMPTY_SERIAL"})
                continue
            if raw_serial in seen:
                warnings.append({"line": line_no, "serial": raw_serial, "issue": "DUPLICATE_SERIAL"})
                continue
            seen.add(raw_serial)

            multiplier = parse_float(row.get(AMI_MULTIPLIER_COL))
            ct = clean_text(row.get(AMI_CT_COL))
            expected = expected_multiplier_from_ct(ct)
            suspicious = False
            if multiplier is not None and expected is not None and abs(float(multiplier) - float(expected)) > 0.0001:
                suspicious = True
                warnings.append({
                    "line": line_no,
                    "serial": raw_serial,
                    "issue": "SUSPICIOUS_MULTIPLIER",
                    "ct_ratio": ct,
                    "csv_multiplier": multiplier,
                    "expected_multiplier": expected,
                })

            rows.append({
                "ami_serial": raw_serial,
                "meter_key_normalized": normalize_key(raw_serial),
                "name": clean_text(row.get(AMI_NAME_COL)),
                "region": clean_text(row.get(AMI_REGION_COL)),
                "device_no": clean_text(row.get(AMI_DEVICE_COL)),
                "current_transformer_ratio": ct,
                "multiplier": None if (skip_suspicious_multiplier and suspicious) else multiplier,
                "csv_multiplier": multiplier,
                "expected_multiplier_from_ct": expected,
                "suspicious_multiplier": suspicious,
            })

    return rows, warnings


def build_match_filter(ami_serial: str, match_fields: List[str]) -> Dict[str, Any]:
    variants = numeric_variants(ami_serial)
    clauses = []
    for field in match_fields:
        clauses.append({field: {"$in": variants}})
    # Daha önce normalize alan eklenmişse oradan da bul.
    clauses.append({"meter_key_normalized": normalize_key(ami_serial)})
    clauses.append({"meter_serial_raw": ami_serial})
    return {"$or": clauses}


def export_report(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    keys = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ami-csv", required=True, help="AMI sayaç master CSV dosyası")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    parser.add_argument("--db", default=os.getenv("MONGO_DB", "kolayveri"))
    parser.add_argument("--meters-coll", default="meters")
    parser.add_argument("--readings-coll", default="readings")
    parser.add_argument("--apply", action="store_true", help="Verilmezse sadece dry-run yapar")
    parser.add_argument("--upsert-missing", action="store_true", help="Mongo'da olmayan AMI sayaçlarını meters'a ekler")
    parser.add_argument("--update-readings", action="store_true", help="readings koleksiyonundaki sayaç no alanlarını da düzeltir")
    parser.add_argument("--skip-suspicious-multiplier", action="store_true", help="Trafo oranına göre şüpheli görünen çarpanları güncellemez")
    parser.add_argument("--report", default="ami_mongo_update_report.csv")
    parser.add_argument("--match-fields", default=",".join(DEFAULT_MATCH_FIELDS), help="Virgüllü alan listesi")
    args = parser.parse_args()

    match_fields = [x.strip() for x in args.match_fields.split(",") if x.strip()]
    ami_rows, warnings = load_ami_rows(args.ami_csv, args.skip_suspicious_multiplier)

    client = MongoClient(args.mongo_uri)
    db = client[args.db]
    meters = db[args.meters_coll]
    readings = db[args.readings_coll]
    now = datetime.now(timezone.utc)

    report: List[Dict[str, Any]] = []

    print(f"AMI kayıt sayısı: {len(ami_rows)}")
    print(f"Mongo DB: {args.db}")
    print(f"Mod: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Readings güncelle: {args.update_readings}")
    print(f"Eksik sayaç upsert: {args.upsert_missing}")
    print("-" * 60)

    for item in ami_rows:
        ami_serial = item["ami_serial"]
        key = item["meter_key_normalized"]
        filt = build_match_filter(ami_serial, match_fields)
        existing = list(meters.find(filt, {"_id": 1, "meter_serial": 1, "name": 1, "multiplier": 1, "group_id": 1, "device_no": 1}).limit(5))

        set_doc: Dict[str, Any] = {
            "meter_serial": ami_serial,
            "meter_serial_raw": ami_serial,
            "meter_key_normalized": key,
            "name": item["name"],
            "region": item["region"],
            "device_no": item["device_no"],
            "current_transformer_ratio": item["current_transformer_ratio"],
            "ami_master_updated_at": now,
        }
        # group_id geçmişte cihaz no gibi kullanıldıysa ikisini de eşitlemek için set ediyoruz.
        if item["device_no"]:
            set_doc["group_id"] = item["device_no"]
        if item["multiplier"] is not None:
            set_doc["multiplier"] = item["multiplier"]

        status = ""
        modified_meter = 0
        matched_meter = len(existing)

        if existing:
            status = "MATCHED_UPDATE"
            if args.apply:
                result = meters.update_many(filt, {"$set": set_doc})
                modified_meter = result.modified_count
        else:
            status = "NOT_FOUND"
            if args.upsert_missing and args.apply:
                meters.insert_one({**set_doc, "created_at": now, "source": "AMI_MASTER"})
                modified_meter = 1
                matched_meter = 0
                status = "INSERTED"

        readings_modified = 0
        if args.update_readings:
            # Özellikle baştaki sıfır düşmüş numerik sayaçları ve exact eşleşenleri normalize ediyoruz.
            variants = numeric_variants(ami_serial)
            read_filter = {"$or": []}
            for field in match_fields:
                read_filter["$or"].append({field: {"$in": variants}})
            read_filter["$or"].append({"meter_key_normalized": key})

            read_set = {
                "meter_serial": ami_serial,
                "meter_serial_raw": ami_serial,
                "meter_key_normalized": key,
                "ami_master_updated_at": now,
            }
            if args.apply:
                rres = readings.update_many(read_filter, {"$set": read_set})
                readings_modified = rres.modified_count
            else:
                readings_modified = readings.count_documents(read_filter)

        report.append({
            "status": status,
            "ami_serial": ami_serial,
            "meter_key_normalized": key,
            "matched_meter_docs": matched_meter,
            "modified_meter_docs": modified_meter,
            "candidate_existing_meter_serials": " | ".join([str(x.get("meter_serial")) for x in existing]),
            "old_names": " | ".join([str(x.get("name")) for x in existing]),
            "new_name": item["name"],
            "old_multipliers": " | ".join([str(x.get("multiplier")) for x in existing]),
            "new_multiplier": item["multiplier"],
            "csv_multiplier": item["csv_multiplier"],
            "expected_multiplier_from_ct": item["expected_multiplier_from_ct"],
            "suspicious_multiplier": item["suspicious_multiplier"],
            "region": item["region"],
            "device_no": item["device_no"],
            "readings_matched_or_modified": readings_modified,
        })

    if args.apply:
        print("Index oluşturuluyor/kontrol ediliyor...")
        meters.create_index("meter_serial")
        meters.create_index("meter_key_normalized")
        readings.create_index("meter_serial")
        readings.create_index("meter_key_normalized")

    export_report(args.report, report)

    print("Rapor yazıldı:", args.report)
    print("Özet:")
    for k in sorted(set(r["status"] for r in report)):
        print(f"  {k}: {sum(1 for r in report if r['status'] == k)}")
    suspicious = [r for r in report if r.get("suspicious_multiplier")]
    if suspicious:
        print("\nUYARI: Trafo oranına göre şüpheli çarpan görünen kayıtlar var:")
        for r in suspicious:
            print(f"  {r['ami_serial']} | CSV çarpan: {r['csv_multiplier']} | beklenen: {r['expected_multiplier_from_ct']}")
        print("Bunlar hata olmayabilir; ama fatura hesabına geçmeden elle doğrulayın.")

    if not args.apply:
        print("\nBu sadece DRY-RUN idi. Gerçek güncelleme için --apply ekleyin.")


if __name__ == "__main__":
    main()
