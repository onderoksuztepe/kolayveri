import os
import csv
import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
import psycopg2.extras


DB = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": os.getenv("KOLAYVERI_DB_PASSWORD", "Kv2026ChangeMe123"),
    "port": 5432,
}


def clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def dec(v, default=None):
    v = clean(v)
    if not v:
        return default
    v = v.replace(" ", "")
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return Decimal(v)
    except InvalidOperation:
        return default


def intval(v):
    d = dec(v)
    return int(d) if d is not None else None


def get(row, *names):
    for n in names:
        if n in row:
            return row.get(n)
    return None


def table_cols(cur, table):
    cur.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d.name for d in cur.description]


def first_existing(cols, names):
    for n in names:
        if n in cols:
            return n
    return None


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        reader = csv.DictReader(f, dialect=dialect)
        return list(reader), dialect.delimiter, reader.fieldnames


def get_or_create_customer(cur, name, apply):
    cols = table_cols(cur, "customers")
    name_col = first_existing(cols, ["customer_name", "name"])
    if not name_col:
        raise RuntimeError("customers tablosunda customer_name/name kolonu bulunamadı")

    cur.execute(f"SELECT id FROM customers WHERE {name_col} = %s LIMIT 1", (name,))
    row = cur.fetchone()
    if row:
        return row["id"], False

    if not apply:
        return -1, True

    cur.execute(f"INSERT INTO customers ({name_col}) VALUES (%s) RETURNING id", (name,))
    return cur.fetchone()["id"], True


def get_or_create_site(cur, customer_id, name, apply):
    cols = table_cols(cur, "sites")
    name_col = first_existing(cols, ["site_name", "name"])
    if not name_col:
        raise RuntimeError("sites tablosunda site_name/name kolonu bulunamadı")
    if "customer_id" not in cols:
        raise RuntimeError("sites tablosunda customer_id kolonu yok")

    if customer_id != -1:
        cur.execute(
            f"SELECT id FROM sites WHERE customer_id = %s AND {name_col} = %s LIMIT 1",
            (customer_id, name),
        )
        row = cur.fetchone()
        if row:
            return row["id"], False

    if not apply:
        return -1, True

    cur.execute(
        f"INSERT INTO sites (customer_id, {name_col}) VALUES (%s, %s) RETURNING id",
        (customer_id, name),
    )
    return cur.fetchone()["id"], True


def meter_data(row, site_id, customer_id, meter_cols):
    data = {}

    def add(col, val):
        if col in meter_cols:
            data[col] = val

    serial = clean(get(row, "Yeni sayaç seri no", "AMİ Sayaç no", "AMI Sayaç no"))
    if not serial:
        return None

    add("customer_id", customer_id)
    add("site_id", site_id)
    add("meter_serial", serial)
    add("sort_order", intval(get(row, "Sıra No")))
    add("muhatap", clean(get(row, "Muhatap")))
    add("floor_text", clean(get(row, "Kat")))
    add("block_text", clean(get(row, "Blok")))
    add("region_text", clean(get(row, "Bölge")))
    add("old_meter_year", clean(get(row, "Eski sayaç üretim tarihi")))
    add("old_meter_serial", clean(get(row, "Eski sayaç seri no")))
    add("old_meter_last_index", dec(get(row, "Eski sayaç son endeks")))
    add("device_no", clean(get(row, "Kolayveri cihaz no")))
    add("note_text", clean(get(row, "Not")))
    add("meter_name", clean(get(row, "Firma adı ve nereye ait")))
    add("current_transformer_ratio", clean(get(row, "Akım trafo oranı")))
    add("multiplier", dec(get(row, "Çarpan değeri"), Decimal("1")))
    add("status", "active")

    return data


def upsert_meter(cur, data, apply):
    cur.execute("SELECT id FROM meters WHERE meter_serial = %s LIMIT 1", (data["meter_serial"],))
    row = cur.fetchone()

    if row:
        if apply:
            cols = [k for k in data.keys() if k != "meter_serial"]
            set_sql = ", ".join([f"{c} = %s" for c in cols])
            vals = [data[c] for c in cols] + [row["id"]]
            cur.execute(f"UPDATE meters SET {set_sql} WHERE id = %s", vals)
        return "updated"

    if apply:
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_sql = ", ".join(cols)
        vals = [data[c] for c in cols]
        cur.execute(f"INSERT INTO meters ({col_sql}) VALUES ({placeholders})", vals)

    return "inserted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows, delimiter, headers = read_csv(args.file)

    print("CSV:", args.file, flush=True)
    print("Delimiter:", repr(delimiter), flush=True)
    print("Satır:", len(rows), flush=True)
    print("Mod:", "APPLY - DB'YE YAZAR" if args.apply else "DRY-RUN - DB'YE YAZMAZ", flush=True)
    print("Başlıklar:", headers, flush=True)

    conn = psycopg2.connect(**DB)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                meter_cols = table_cols(cur, "meters")
                print("meters kolonları:", meter_cols, flush=True)

                customer_id, c_new = get_or_create_customer(cur, args.customer, args.apply)
                site_id, s_new = get_or_create_site(cur, customer_id, args.site, args.apply)

                print("customer_id:", customer_id, "new:", c_new, flush=True)
                print("site_id:", site_id, "new:", s_new, flush=True)

                inserted = updated = skipped = 0

                for row in rows:
                    data = meter_data(row, site_id, customer_id, meter_cols)
                    if not data:
                        skipped += 1
                        continue

                    action = upsert_meter(cur, data, args.apply)
                    if action == "inserted":
                        inserted += 1
                    else:
                        updated += 1

                if not args.apply:
                    conn.rollback()

                print("ÖZET", flush=True)
                print("Inserted:", inserted, flush=True)
                print("Updated:", updated, flush=True)
                print("Skipped:", skipped, flush=True)
                print("Bitti.", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
