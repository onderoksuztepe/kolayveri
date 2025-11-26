from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

# ============================
#  KONFİG
# ============================

MONGO_URI = "mongodb+srv://onderoksuztepe_db:<OnderKolayveri2025>@kolayveri.t0lyzeu.mongodb.net/"
DB_NAME = "amimavialp"
METERS_COLL = "meters"
READINGS_COLL = "readings"
INVOICES_COLL = "invoices"  # ana sayaç faturalarını burada tutacağız

# Varsayılan birim fiyat (tek sayaç / test için fallback)
DEFAULT_UNIT_PRICE = 3.0

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
meters_col = db[METERS_COLL]
readings_col = db[READINGS_COLL]
invoices_col = db[INVOICES_COLL]

app = FastAPI(
    title="Kolayveri AMI API",
    version="2.0.0"
)

# Softr'den istek gelebilsin diye CORS açıyoruz
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # istersen Softr domainiyle kısıtlarsın
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================
#  HELPERS
# ============================

def mongo_to_dict(doc) -> Optional[Dict[str, Any]]:
    """Mongo dokümanını JSON friendly hale getir (_id hariç)."""
    if not doc:
        return None
    d = dict(doc)
    d.pop("_id", None)
    return d


def get_effective_period_kwh(m: Dict[str, Any]) -> Optional[float]:
    """
    Bir sayaç için dönem tüketimini belirler:
    1) manual_period_consumption_kwh varsa onu kullan,
    2) yoksa period_consumption_kwh kullan,
    3) o da yoksa last_reading - october_last_value * multiplier ile hesaplamaya çalış.
    """
    manual = m.get("manual_period_consumption_kwh")
    if manual is not None:
        try:
            return float(manual)
        except Exception:
            pass

    pc = m.get("period_consumption_kwh")
    if pc is not None:
        try:
            return float(pc)
        except Exception:
            pass

    last_reading = m.get("last_reading")
    october_last = m.get("october_last_value")

    if last_reading and (october_last is not None):
        try:
            last_val = float(last_reading.get("value"))
            oct_val = float(october_last)
            multiplier = float(m.get("multiplier", 1))
            raw_delta = last_val - oct_val
            return raw_delta * multiplier
        except Exception:
            return None

    return None


# ============================
#  ENDPOINTS
# ============================

@app.get("/health")
def health_check():
    return {"status": "ok", "time": datetime.utcnow()}


@app.get("/meters", response_model=List[dict])
def list_meters(status: Optional[str] = None):
    """
    Tüm sayaçları listeler.
    ?status=ok gibi filtre de verebilirsin.
    """
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status

    docs = meters_col.find(query, {"_id": 0})
    return list(docs)


@app.get("/meters/{meter_serial}", response_model=dict)
def get_meter(meter_serial: str):
    """
    Tek bir sayaç detayı.
    """
    doc = meters_col.find_one({"meter_serial": meter_serial}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Meter not found")
    return doc


@app.get("/meters/{meter_serial}/readings", response_model=List[dict])
def get_meter_readings(meter_serial: str, limit: int = 50):
    """
    Bir sayacın son N okumasını döner.
    """
    cursor = readings_col.find(
        {"meter_serial": meter_serial},
        {"_id": 0}
    ).sort("time", -1).limit(limit)

    return list(cursor)


@app.get("/billing/{meter_serial}", response_model=dict)
def get_billing_summary(
    meter_serial: str,
    unit_price: Optional[float] = None
):
    """
    Tek sayaç için basit fatura özeti.
    - Dönem tüketimini bulur (manuel override > hesaplanmış > fallback).
    - Birim fiyatı parametreden veya dokümandan alır.
    - Vergi yok, sadece kWh * birim fiyat.
    """
    meter = meters_col.find_one({"meter_serial": meter_serial})
    if not meter:
        raise HTTPException(status_code=404, detail="Meter not found")

    m = mongo_to_dict(meter)
    period_kwh = get_effective_period_kwh(m)

    if period_kwh is None:
        raise HTTPException(
            status_code=400,
            detail="Bu sayaç için dönem tüketimi hesaplanamamış."
        )

    if unit_price is not None:
        up = float(unit_price)
    else:
        up = float(m.get("unit_price", DEFAULT_UNIT_PRICE))

    energy_amount = period_kwh * up

    def rnd(x: float) -> float:
        return float(f"{x:.4f}")

    summary = {
        "meter_serial": m.get("meter_serial"),
        "name": m.get("name"),
        "status": m.get("status"),
        "multiplier": m.get("multiplier"),
        "period": {
            "consumption_kwh": rnd(period_kwh),
        },
        "tariff": {
            "unit_price": up,
        },
        "amounts": {
            "energy_amount": rnd(energy_amount),
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    return summary


@app.get("/billing-group/{group_id}", response_model=dict)
def get_group_billing(
    group_id: str,
    period: Optional[str] = None
):
    """
    Grup bazlı fatura özeti (ANA SAYAÇ YOK, SADECE SÜZME).
    - invoices: group_id + period → invoice_consumption_kwh, invoice_amount_net
    - meters: group_id = ... olanların hepsi süzme sayaç
    - Her sayaç için effective period kWh hesaplanır.
    - Süzme toplam kWh ile fatura tüketimi arasındaki fark = loss_kwh.
    - Birim fiyat = invoice_amount_net / invoice_consumption_kwh.
    - Kayıp kWh süzme sayaçlara tüketim oranında paylaştırılır.
    """

    # 1) Fatura bul
    inv_query: Dict[str, Any] = {"group_id": group_id}
    if period:
        inv_query["period"] = period

    invoice = invoices_col.find_one(inv_query, sort=[("period", -1)])
    if not invoice:
        raise HTTPException(
            status_code=404,
            detail="Bu grup için invoices koleksiyonunda fatura bulunamadı."
        )

    inv = mongo_to_dict(invoice)
    inv_consumption = inv.get("invoice_consumption_kwh")
    inv_amount_net = inv.get("invoice_amount_net")

    if inv_consumption is None or inv_amount_net is None:
        raise HTTPException(
            status_code=400,
            detail="Faturada invoice_consumption_kwh veya invoice_amount_net eksik."
        )

    inv_consumption = float(inv_consumption)
    inv_amount_net = float(inv_amount_net)

    if inv_consumption <= 0:
        raise HTTPException(
            status_code=400,
            detail="Fatura tüketimi (invoice_consumption_kwh) sıfır veya negatif olamaz."
        )

    unit_price = inv_amount_net / inv_consumption

    # 2) Grup sayaçlarını getir (tamamı süzme)
    meters = list(meters_col.find({"group_id": group_id}))
    if not meters:
        raise HTTPException(
            status_code=404,
            detail="Bu grup için meters koleksiyonunda sayaç bulunamadı."
        )

    subs_detail = []
    subs_total_kwh = 0.0

    for m in meters:
        md = mongo_to_dict(m)
        base_kwh = get_effective_period_kwh(md)
        if base_kwh is None:
            base_kwh = 0.0

        base_kwh = float(base_kwh)
        subs_total_kwh += base_kwh

        subs_detail.append({
            "meter_serial": md.get("meter_serial"),
            "name": md.get("name"),
            "status": md.get("status"),
            "base_kwh": float(f"{base_kwh:.4f}")
        })

    # 3) Kayıp hesabı
    loss_kwh = inv_consumption - subs_total_kwh

    if loss_kwh <= 0 or subs_total_kwh <= 0:
        for s in subs_detail:
            bk = s["base_kwh"]
            s["loss_share_kwh"] = 0.0
            s["billed_kwh"] = bk
            s["billed_amount"] = float(f"{bk * unit_price:.4f}")
    else:
        for s in subs_detail:
            bk = s["base_kwh"]
            share = loss_kwh * (bk / subs_total_kwh) if subs_total_kwh > 0 else 0.0
            billed_kwh = bk + share
            billed_amount = billed_kwh * unit_price

            s["loss_share_kwh"] = float(f"{share:.4f}")
            s["billed_kwh"] = float(f"{billed_kwh:.4f}")
            s["billed_amount"] = float(f"{billed_amount:.4f}")

    summary = {
        "group_id": group_id,
        "period": inv.get("period"),
        "unit_price": float(f"{unit_price:.6f}"),
        "loss_kwh": float(f"{loss_kwh:.4f}"),
        "invoice": {
            "consumption_kwh": inv_consumption,
            "amount_net": inv_amount_net,
        },
        "subs": subs_detail,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    return summary


@app.get("/api/billing/summary", response_model=dict)
def api_billing_summary(group_id: str, period: Optional[str] = None):
    """
    Softr için özet endpoint.
    Object döner (root-level).
    """
    summary = get_group_billing(group_id=group_id, period=period)

    return {
        "group_id": summary.get("group_id"),
        "period": summary.get("period"),
        "unit_price": summary.get("unit_price"),
        "loss_kwh": summary.get("loss_kwh"),
        "invoice_consumption_kwh": summary.get("invoice", {}).get("consumption_kwh"),
        "invoice_amount_net": summary.get("invoice", {}).get("amount_net"),
        "generated_at": summary.get("generated_at"),
    }


@app.get("/api/billing/subs", response_model=List[dict])
def api_billing_subs(group_id: str, period: Optional[str] = None):
    """
    Softr Table için flat liste endpoint'i.
    Root-level array döner. Her eleman bir sayaç satırı.
    """
    summary = get_group_billing(group_id=group_id, period=period)

    subs = summary.get("subs", [])
    result: List[Dict[str, Any]] = []

    for s in subs:
        row = dict(s)  # meter_serial, name, status, base_kwh, loss_share_kwh, billed_kwh, billed_amount

        # Grup & fatura bilgilerini de satıra ekleyelim
        row["group_id"] = summary.get("group_id")
        row["period"] = summary.get("period")
        row["unit_price"] = summary.get("unit_price")
        row["invoice_consumption_kwh"] = summary.get("invoice", {}).get("consumption_kwh")
        row["invoice_amount_net"] = summary.get("invoice", {}).get("amount_net")
        row["loss_kwh_total"] = summary.get("loss_kwh")

        result.append(row)

    return result

@app.get("/api/meters/last-readings", response_model=List[dict])
def api_last_readings(
    group_id: Optional[str] = None,
    status: Optional[str] = None,
):
    """
    Tüm sayaçların son okumalarını toplu döner.
    - meters.last_reading.time
    - meters.last_reading.value
    """
    query: Dict[str, Any] = {}

    if group_id:
        query["group_id"] = group_id
    if status:
        query["status"] = status

    meters = list(meters_col.find(query))

    result: List[Dict[str, Any]] = []

    for m in meters:
        md = mongo_to_dict(m)
        lr = md.get("last_reading") or {}

        t = lr.get("time")
        v = lr.get("value")

        # time mongodb'de datetime ise stringe çevirelim
        if isinstance(t, datetime):
            t_str = t.isoformat() + "Z"
        else:
            t_str = t  # zaten string olabilir

        try:
            v_val = float(v) if v is not None else None
        except Exception:
            v_val = None

        result.append(
            {
                "meter_serial": md.get("meter_serial"),
                "name": md.get("name"),
                "group_id": md.get("group_id"),
                "status": md.get("status"),
                "last_read_time": t_str,
                "last_read_value": v_val,
                "multiplier": md.get("multiplier"),
            }
        )

    return result
@app.get("/api/meters/last-vs-previous", response_model=List[dict])
def api_last_vs_previous(
    group_id: Optional[str] = None,
    status: Optional[str] = None,
):
    """
    Son endeks ile bir önceki ay sonu endeksini karşılaştırır.
    Şu anda 'previous' olarak meters.october_last_value alanını kullanıyoruz.
    """
    query: Dict[str, Any] = {}
    if group_id:
        query["group_id"] = group_id
    if status:
        query["status"] = status

    meters = list(meters_col.find(query))
    result: List[Dict[str, Any]] = []

    for m in meters:
        md = mongo_to_dict(m)
        lr = md.get("last_reading") or {}
        t = lr.get("time")
        v = lr.get("value")

        # time -> string
        if isinstance(t, datetime):
            t_str = t.isoformat() + "Z"
        else:
            t_str = t

        # son endeks
        try:
            last_val = float(v) if v is not None else None
        except Exception:
            last_val = None

        # önceki ay son endeksi (şimdilik october_last_value)
        prev_raw = md.get("october_last_value")
        try:
            prev_val = float(prev_raw) if prev_raw is not None else None
        except Exception:
            prev_val = None

        multiplier = md.get("multiplier", 1)
        try:
            mul = float(multiplier)
        except Exception:
            mul = 1.0

        if last_val is not None and prev_val is not None:
            raw_delta = last_val - prev_val
            energy_delta = raw_delta * mul
        else:
            raw_delta = None
            energy_delta = None

        result.append(
            {
                "meter_serial": md.get("meter_serial"),
                "name": md.get("name"),
                "group_id": md.get("group_id"),
                "status": md.get("status"),
                "multiplier": mul,
                "previous_end_value": prev_val,
                "last_read_time": t_str,
                "last_read_value": last_val,
                "raw_delta": raw_delta,            # sayaç endeks farkı
                "delta_kwh": energy_delta,         # çarpanlı kWh farkı
            }
        )

    return result
@app.get("/api/meter-readings", response_model=List[dict])
def api_meter_readings(
    group_id: Optional[str] = None,
    meter_serial: Optional[str] = None,
    status: Optional[str] = None,
):
    """
    Geçmişe ait TÜM okumaları döner.
    - readings_col: her kayıt bir okuma satırı
    - meters_col ile join edip name, group_id, status, multiplier ekliyoruz.

    Filtreler:
    - group_id: sadece ilgili gruptaki sayaçların okumaları
    - meter_serial: sadece tek sayaç
    - status: meters.status filtresi (ör. ok)
    """

    # Önce ilgili sayaçları bul (join için)
    meter_query: Dict[str, Any] = {}
    if group_id:
        meter_query["group_id"] = group_id
    if status:
        meter_query["status"] = status
    if meter_serial:
        meter_query["meter_serial"] = meter_serial

    meters = list(meters_col.find(meter_query))
    if not meters:
        return []  # ilgili sayaç yoksa boş liste

    meter_map: Dict[str, Dict[str, Any]] = {}
    serials: List[str] = []

    for m in meters:
        md = mongo_to_dict(m)
        serial = md.get("meter_serial")
        if not serial:
            continue
        serials.append(serial)
        meter_map[serial] = md

    # Şimdi tüm okumaları çek
    read_query: Dict[str, Any] = {"meter_serial": {"$in": serials}}

    cursor = readings_col.find(read_query).sort("time", 1)  # zaman artan sıralı

    result: List[Dict[str, Any]] = []

    for r in cursor:
        rd = mongo_to_dict(r)
        serial = rd.get("meter_serial")

        mi = meter_map.get(serial, {})
        t = rd.get("time")
        v = rd.get("value")

        # time -> string
        if isinstance(t, datetime):
            t_str = t.isoformat() + "Z"
        else:
            t_str = t

        try:
            index_val = float(v) if v is not None else None
        except Exception:
            index_val = None

        try:
            mul = float(mi.get("multiplier", 1))
        except Exception:
            mul = 1.0

        if index_val is not None:
            energy_kwh = index_val * mul
        else:
            energy_kwh = None

        result.append(
            {
                "meter_serial": serial,
                "name": mi.get("name"),
                "group_id": mi.get("group_id"),
                "status": mi.get("status"),
                "multiplier": mul,
                "read_time": t_str,
                "index_value": index_val,   # sayaç üzerindeki endeks
                "energy_kwh": energy_kwh,   # çarpanlı kWh (anlık endeks*katsayı)
            }
        )

    return result
