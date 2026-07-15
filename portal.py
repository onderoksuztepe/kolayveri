import os
import hmac
import time
import base64
import hashlib
from decimal import Decimal
from datetime import date, datetime

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles


APP_DIR = "/opt/kolayveri_ami"

DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": os.getenv("KOLAYVERI_DB_PASSWORD", "Kv2026ChangeMe123"),
    "port": 5432,
}

PORTAL_SECRET_KEY = os.getenv("PORTAL_SECRET_KEY", "change-me-now")

app = FastAPI(title="KolayVeri Portal")
templates = Jinja2Templates(directory=f"{APP_DIR}/templates")
app.mount("/static", StaticFiles(directory=f"{APP_DIR}/static"), name="static")


STATUS_LABELS = {
    "pending": "Veri Bekleniyor",
    "negative_consumption": "Endeks Hatası",
    "missing_first_index": "İlk Endeks Eksik",
    "missing_last_index": "Son Endeks Eksik",
    "calculated": "Hesaplandı",
}

STATUS_ACTIONS = {
    "pending": "Sayaçtan veri gelmiyor veya ilk/son endeks oluşmamış. AMI cihaz/sayaç eşleşmesi kontrol edilmeli.",
    "negative_consumption": "Son endeks ilk endeksten küçük. Sayaç değişimi, reset veya hatalı okuma kontrol edilmeli.",
    "missing_first_index": "Dönem ilk endeksi eksik. Manuel ilk endeks girilmeli veya önceki dönem kapanışı kontrol edilmeli.",
    "missing_last_index": "Son okuma yok. AMI okuma durumu kontrol edilmeli.",
    "calculated": "İşlem başarılı.",
}


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


def rows_to_dicts(rows):
    return [{k: json_safe(v) for k, v in dict(row).items()} for row in rows]


def db_fetch_all(sql, params=None):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def db_fetch_one(sql, params=None):
    rows = db_fetch_all(sql, params)
    return rows[0] if rows else None


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iterations_text, salt_b64, hash_b64 = password_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False

        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(hash_b64.encode())

        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def get_user_by_username(username: str):
    return db_fetch_one("""
        SELECT
            u.id,
            u.username,
            u.password_hash,
            u.role,
            u.site_id,
            u.is_active,
            u.billing_group_id,
            bg.group_name AS billing_group_name,
            CASE WHEN u.role = 'admin' THEN true ELSE false END AS is_admin
        FROM portal_users u
        LEFT JOIN billing_groups bg ON bg.id = u.billing_group_id
        WHERE u.username = %s
        LIMIT 1
    """, (username,))


def sign_payload(payload: str) -> str:
    return hmac.new(
        PORTAL_SECRET_KEY.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()


def make_session(user: dict) -> str:
    exp = int(time.time()) + 60 * 60 * 12
    site_id = user.get("site_id") if user.get("site_id") is not None else ""
    payload = f"{user['username']}|{user['role']}|{site_id}|{exp}"
    sig = sign_payload(payload)
    token = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(token.encode()).decode()


def parse_session(token: str | None):
    if not token:
        return None

    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        username, role, site_id_text, exp_text, sig = decoded.split("|")
        payload = f"{username}|{role}|{site_id_text}|{exp_text}"

        if not hmac.compare_digest(sig, sign_payload(payload)):
            return None

        if int(exp_text) < int(time.time()):
            return None

        db_user = get_user_by_username(username)
        if not db_user or not db_user.get("is_active"):
            return None

        user = dict(db_user)
        user["is_admin"] = bool(user.get("is_admin") or user.get("role") == "admin")

        try:
            if user.get("site_id") is not None:
                user["site_id"] = int(user["site_id"])
        except Exception:
            pass

        return user

    except Exception:
        return None


def require_user(request: Request):
    return parse_session(request.cookies.get("kv_session"))





def _user_billing_group_id(user):
    if not user:
        return None
    try:
        return user.get("billing_group_id")
    except Exception:
        return None


def _is_group_user(user):
    return _user_billing_group_id(user) is not None



@app.middleware("http")
async def restrict_group_user_pages(request: Request, call_next):
    path = request.url.path

    if path.startswith("/static"):
        return await call_next(request)

    user = require_user(request)

    if user and _is_group_user(user):
        allowed = (
            "/meters",
            "/billing-allocation",
            "/billing-allocation.csv",
            "/logout",
            "/login",
        )

        if path == "/" or not any(path.startswith(a) for a in allowed):
            return RedirectResponse("/meters", status_code=302)

    return await call_next(request)


def _status_tr(status):
    if status == "open":
        return "Açık"
    if status == "closed":
        return "Kapalı"
    return status or ""


def active_period_code():
    row = db_fetch_one("""
        SELECT period_code
        FROM periods
        WHERE status = 'open'
        ORDER BY start_date DESC
        LIMIT 1
    """)
    return row["period_code"] if row else None


def table_columns(table_name):
    rows = db_fetch_all("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
    """, (table_name,))
    return {r["column_name"] for r in rows}


def get_sites_for_user(user):
    site_cols = table_columns("sites")
    customer_cols = table_columns("customers")

    site_name_col = "site_name" if "site_name" in site_cols else "name"
    customer_name_col = "customer_name" if "customer_name" in customer_cols else "name"

    if user["is_admin"]:
        sql = f"""
            SELECT
                s.id AS site_id,
                s.{site_name_col} AS site_name,
                c.{customer_name_col} AS customer_name
            FROM sites s
            LEFT JOIN customers c ON c.id = s.customer_id
            ORDER BY c.{customer_name_col}, s.{site_name_col}
        """
        return db_fetch_all(sql)

    sql = f"""
        SELECT
            s.id AS site_id,
            s.{site_name_col} AS site_name,
            c.{customer_name_col} AS customer_name
        FROM sites s
        LEFT JOIN customers c ON c.id = s.customer_id
        WHERE s.id = %s
        LIMIT 1
    """
    return db_fetch_all(sql, (user["site_id"],))


def get_selected_site(request: Request, user):
    sites = get_sites_for_user(user)
    if not sites:
        return None, [], None

    if not user["is_admin"]:
        selected = sites[0]
        return int(selected["site_id"]), sites, selected

    cookie_site_id = request.cookies.get("kv_site_id")
    selected = None

    if cookie_site_id:
        try:
            cookie_site_id_int = int(cookie_site_id)
            selected = next((s for s in sites if int(s["site_id"]) == cookie_site_id_int), None)
        except Exception:
            selected = None

    if not selected:
        selected = sites[0]

    return int(selected["site_id"]), sites, selected


def enrich_control_rows(rows):
    for row in rows:
        status = row.get("calculation_status")
        row["calculation_status_tr"] = (
            row.get("calculation_status_tr")
            or STATUS_LABELS.get(status)
            or status
            or "Kontrol Gerekli"
        )
        row["suggested_action"] = (
            row.get("suggested_action")
            or row.get("error_message")
            or STATUS_ACTIONS.get(status)
            or "Kontrol gerekli."
        )
    return rows


def make_summary(period, rows):
    total_active = 0.0
    last_calc = None

    for r in rows:
        if r.get("active_consumption") is not None:
            total_active += float(r.get("active_consumption") or 0)

        if r.get("calculated_at"):
            last_calc = r.get("calculated_at")

    return {
        "period_code": period,
        "total_meters": len(rows),
        "calculated_meters": sum(1 for r in rows if r.get("calculation_status") == "calculated"),
        "pending_meters": sum(1 for r in rows if r.get("calculation_status") == "pending"),
        "negative_consumption_meters": sum(1 for r in rows if r.get("calculation_status") == "negative_consumption"),
        "missing_first_index": sum(1 for r in rows if r.get("first_active_index") is None),
        "missing_last_index": sum(1 for r in rows if r.get("last_active_index") is None),
        "total_active_consumption": total_active,
        "last_calculation_at": last_calc or "-",
    }


@app.get("/")
def root(request: Request):
    if require_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
    })


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = get_user_by_username(username)

    if user and user.get("is_active") and verify_password(password, user["password_hash"]):
        next_path = "/meters" if user.get("billing_group_id") else "/dashboard"
        response = RedirectResponse(next_path, status_code=302)
        response.set_cookie(
            "kv_session",
            make_session(user),
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return response

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "Kullanıcı adı veya şifre hatalı.",
    })


@app.post("/select-site")
def select_site(request: Request, site_id: int = Form(...), next_url: str = Form("/dashboard")):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not user["is_admin"]:
        return RedirectResponse("/dashboard", status_code=302)

    allowed_sites = get_sites_for_user(user)
    allowed_site_ids = {int(s["site_id"]) for s in allowed_sites}
    if site_id not in allowed_site_ids:
        return RedirectResponse("/dashboard", status_code=302)

    if not next_url.startswith("/"):
        next_url = "/dashboard"

    response = RedirectResponse(next_url, status_code=302)
    response.set_cookie(
        "kv_site_id",
        str(site_id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("kv_session")
    response.delete_cookie("kv_site_id")
    return response


@app.get("/dashboard")
def dashboard(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if _is_group_user(user):
        return RedirectResponse("/meters", status_code=302)

    if _is_group_user(user):
        return RedirectResponse("/meters", status_code=302)

    period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    all_rows = db_fetch_all("""
        SELECT *
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
        ORDER BY sort_order NULLS LAST
    """, (period, selected_site_id))
    all_rows = enrich_control_rows(all_rows)

    control_rows = [r for r in all_rows if r.get("calculation_status") != "calculated"]
    summary = make_summary(period, all_rows)

    reactive_all_rows = _reactive_rows(period, selected_site_id)
    reactive_summary = {
        "all": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="all")),
        "over_limit": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="over_limit")),
        "inductive_over": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="inductive_over")),
        "capacitive_over": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="capacitive_over")),
        "missing": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="missing")),
        "low_consumption": len(_reactive_filter_rows([dict(r) for r in reactive_all_rows], filter_type="low_consumption")),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "summary": summary,
        "reactive_summary": reactive_summary,
        "control_rows": control_rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })




def _is_admin_user(user):
    try:
        return bool(user.get("is_admin"))
    except Exception:
        return bool(getattr(user, "is_admin", False))


def _next_period_code(period_code):
    year, month = map(int, period_code.split("-"))
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def _period_management_summary(source_code):
    source_rows = db_fetch_all("""
        SELECT id, period_code, start_date, end_date, status
        FROM periods
        WHERE period_code = %s
        LIMIT 1
    """, (source_code,))

    if not source_rows:
        return None

    source = source_rows[0]
    target_code = _next_period_code(source_code)

    target_rows = db_fetch_all("""
        SELECT id, period_code, start_date, end_date, status
        FROM periods
        WHERE period_code = %s
        LIMIT 1
    """, (target_code,))

    target = target_rows[0] if target_rows else None

    rows = db_fetch_all("""
        SELECT
            i.meter_id,
            m.sort_order,
            m.meter_serial,
            m.meter_name,
            i.last_active_index,
            i.last_inductive_index,
            i.last_capacitive_index
        FROM meter_period_indexes i
        JOIN meters m ON m.id = i.meter_id
        WHERE i.period_id = %s
        ORDER BY m.sort_order NULLS LAST, m.meter_serial
    """, (source["id"],))

    total = len(rows)
    active_ready = len([r for r in rows if r.get("last_active_index") is not None])
    inductive_ready = len([r for r in rows if r.get("last_inductive_index") is not None])
    capacitive_ready = len([r for r in rows if r.get("last_capacitive_index") is not None])
    reactive_ready = len([
        r for r in rows
        if r.get("last_inductive_index") is not None
        and r.get("last_capacitive_index") is not None
    ])

    missing_rows = []
    ready_all = 0
    active_and_reactive_missing = 0
    reactive_only_missing = 0
    active_only_missing = 0

    for r in rows:
        active_missing_flag = r.get("last_active_index") is None
        reactive_missing_flag = (
            r.get("last_inductive_index") is None
            or r.get("last_capacitive_index") is None
        )

        if not active_missing_flag and not reactive_missing_flag:
            ready_all += 1
        elif active_missing_flag and reactive_missing_flag:
            active_and_reactive_missing += 1
        elif reactive_missing_flag and not active_missing_flag:
            reactive_only_missing += 1
        elif active_missing_flag and not reactive_missing_flag:
            active_only_missing += 1

        missing = []
        if active_missing_flag:
            missing.append("Aktif")
        if r.get("last_inductive_index") is None:
            missing.append("Endüktif")
        if r.get("last_capacitive_index") is None:
            missing.append("Kapasitif")

        if missing:
            rr = dict(r)
            rr["missing_text"] = ", ".join(missing)
            if active_missing_flag and reactive_missing_flag:
                rr["missing_group"] = "Aktif + Reaktif Eksik"
            elif reactive_missing_flag:
                rr["missing_group"] = "Sadece Reaktif Eksik"
            elif active_missing_flag:
                rr["missing_group"] = "Sadece Aktif Eksik"
            else:
                rr["missing_group"] = "Kontrol"
            missing_rows.append(rr)

    return {
        "source": source,
        "target_code": target_code,
        "target": target,
        "total": total,
        "active_ready": active_ready,
        "inductive_ready": inductive_ready,
        "capacitive_ready": capacitive_ready,
        "reactive_ready": reactive_ready,
        "active_missing": total - active_ready,
        "reactive_missing": total - reactive_ready,
        "ready_all": ready_all,
        "active_and_reactive_missing": active_and_reactive_missing,
        "reactive_only_missing": reactive_only_missing,
        "active_only_missing": active_only_missing,
        "missing_rows": missing_rows,
    }


@app.get("/period-management")
def period_management(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not _is_admin_user(user):
        return RedirectResponse("/dashboard", status_code=302)

    period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    periods = db_fetch_all("""
        SELECT id, period_code, start_date, end_date, status
        FROM periods
        ORDER BY period_code
    """)

    rollover = _period_management_summary(period)

    return templates.TemplateResponse("period_management.html", {
        "request": request,
        "user": user,
        "period": period,
        "periods": periods,
        "rollover": rollover,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })




def _parse_decimal_or_none(value):
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    value = value.replace(".", "").replace(",", ".") if "," in value else value
    from decimal import Decimal
    return Decimal(value)


def _recalculate_meter_period(cur, meter_id, period_id):
    from decimal import Decimal

    cur.execute("""
        SELECT
            m.multiplier,
            i.first_active_index,
            i.last_active_index,
            i.first_inductive_index,
            i.last_inductive_index,
            i.first_capacitive_index,
            i.last_capacitive_index
        FROM meters m
        JOIN meter_period_indexes i ON i.meter_id = m.id
        WHERE m.id = %s
          AND i.period_id = %s
    """, (meter_id, period_id))
    r = cur.fetchone()

    if not r:
        return

    multiplier = r.get("multiplier") or Decimal("1")

    fa = r.get("first_active_index")
    la = r.get("last_active_index")
    fi = r.get("first_inductive_index")
    li = r.get("last_inductive_index")
    fc = r.get("first_capacitive_index")
    lc = r.get("last_capacitive_index")

    active_consumption = None
    calculation_status = "pending"
    error_message = None

    if fa is None or la is None:
        calculation_status = "pending"
        error_message = "Aktif ilk veya son endeks eksik"
    else:
        active_consumption = (la - fa) * multiplier
        if active_consumption < 0:
            calculation_status = "negative_consumption"
            error_message = "Negatif aktif tüketim"
        else:
            calculation_status = "calculated"
            error_message = None

    inductive_consumption = None
    capacitive_consumption = None
    inductive_ratio_pct = None
    capacitive_ratio_pct = None
    reactive_status = None

    if fi is None or li is None or fc is None or lc is None:
        reactive_status = "missing_reactive_index"
    else:
        inductive_consumption = (li - fi) * multiplier
        capacitive_consumption = (lc - fc) * multiplier

        if inductive_consumption < 0 or capacitive_consumption < 0:
            reactive_status = "negative_reactive_index"
        else:
            reactive_status = "calculated"

            if active_consumption is not None and active_consumption > 0:
                inductive_ratio_pct = (inductive_consumption / active_consumption) * Decimal("100")
                capacitive_ratio_pct = (capacitive_consumption / active_consumption) * Decimal("100")

    cur.execute("""
        UPDATE meter_period_calculations
        SET active_consumption = %s,
            inductive_consumption = %s,
            capacitive_consumption = %s,
            inductive_ratio_pct = %s,
            capacitive_ratio_pct = %s,
            reactive_status = %s,
            calculation_status = %s,
            error_message = %s,
            calculated_at = NOW()
        WHERE meter_id = %s
          AND period_id = %s
    """, (
        active_consumption,
        inductive_consumption,
        capacitive_consumption,
        inductive_ratio_pct,
        capacitive_ratio_pct,
        reactive_status,
        calculation_status,
        error_message,
        meter_id,
        period_id,
    ))

    if cur.rowcount == 0:
        cur.execute("""
            INSERT INTO meter_period_calculations (
                meter_id,
                period_id,
                active_consumption,
                inductive_consumption,
                capacitive_consumption,
                inductive_ratio_pct,
                capacitive_ratio_pct,
                reactive_status,
                calculation_status,
                error_message,
                calculated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            meter_id,
            period_id,
            active_consumption,
            inductive_consumption,
            capacitive_consumption,
            inductive_ratio_pct,
            capacitive_ratio_pct,
            reactive_status,
            calculation_status,
            error_message,
        ))


def _index_entry_rows(period, selected_site_id, only_missing=True):
    rows = db_fetch_all("""
        SELECT
            p.period_code,
            m.id AS meter_id,
            m.site_id,
            m.sort_order,
            m.meter_serial,
            m.meter_name,
            m.device_no,
            m.multiplier,
            i.first_active_index,
            i.last_active_index,
            i.first_inductive_index,
            i.last_inductive_index,
            i.first_capacitive_index,
            i.last_capacitive_index,
            c.active_consumption,
            c.inductive_consumption,
            c.capacitive_consumption,
            c.inductive_ratio_pct,
            c.capacitive_ratio_pct,
            c.calculation_status,
            c.reactive_status
        FROM meter_period_indexes i
        JOIN periods p ON p.id = i.period_id
        JOIN meters m ON m.id = i.meter_id
        LEFT JOIN meter_period_calculations c
          ON c.meter_id = i.meter_id
         AND c.period_id = i.period_id
        WHERE p.period_code = %s
          AND m.site_id = %s
        ORDER BY m.sort_order NULLS LAST, m.meter_serial
    """, (period, selected_site_id))

    result = []
    for r in rows:
        missing = []
        if r.get("first_active_index") is None:
            missing.append("İlk Aktif")
        if r.get("last_active_index") is None:
            missing.append("Son Aktif")
        if r.get("first_inductive_index") is None:
            missing.append("İlk Endüktif")
        if r.get("last_inductive_index") is None:
            missing.append("Son Endüktif")
        if r.get("first_capacitive_index") is None:
            missing.append("İlk Kapasitif")
        if r.get("last_capacitive_index") is None:
            missing.append("Son Kapasitif")

        rr = dict(r)
        rr["missing_text"] = ", ".join(missing) if missing else "Tamam"

        if only_missing and not missing:
            continue

        result.append(rr)

    return result


@app.get("/index-entry")
def index_entry(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    active_period = active_period_code()
    period = request.query_params.get("period") or active_period
    show = request.query_params.get("show") or "missing"
    saved = request.query_params.get("saved") or ""

    selected_site_id, sites, selected_site = get_selected_site(request, user)

    available_periods = db_fetch_all("""
        SELECT period_code, status
        FROM periods
        ORDER BY period_code DESC
    """)

    rows = _index_entry_rows(
        period,
        selected_site_id,
        only_missing=(show != "all")
    )

    return templates.TemplateResponse("index_entry.html", {
        "request": request,
        "user": user,
        "period": period,
        "active_period": active_period,
        "available_periods": available_periods,
        "show": show,
        "saved": saved,
        "rows": rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })


@app.post("/index-entry")
def index_entry_save(
    request: Request,
    period: str = Form(...),
    meter_id: int = Form(...),
    show: str = Form("missing"),
    first_active_index: str = Form(""),
    last_active_index: str = Form(""),
    first_inductive_index: str = Form(""),
    last_inductive_index: str = Form(""),
    first_capacitive_index: str = Form(""),
    last_capacitive_index: str = Form(""),
):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    selected_site_id, sites, selected_site = get_selected_site(request, user)

    import os
    import psycopg2
    import psycopg2.extras

    password = os.environ.get("KOLAYVERI_DB_PASSWORD")
    conn = psycopg2.connect(
        host="127.0.0.1",
        dbname="kolayveri_db",
        user="kolayveri_user",
        password=password,
    )

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id
                FROM meters
                WHERE id = %s
                  AND site_id = %s
                LIMIT 1
            """, (meter_id, selected_site_id))
            meter_auth = cur.fetchone()

            if not meter_auth:
                raise Exception("Bu sayaca erişim yetkiniz yok")

            cur.execute("SELECT id FROM periods WHERE period_code = %s", (period,))
            p = cur.fetchone()
            if not p:
                raise Exception("Dönem bulunamadı")

            period_id = p["id"]

            cur.execute("""
                UPDATE meter_period_indexes
                SET first_active_index = %s,
                    last_active_index = %s,
                    first_inductive_index = %s,
                    last_inductive_index = %s,
                    first_capacitive_index = %s,
                    last_capacitive_index = %s,
                    first_source = COALESCE(first_source, 'manual'),
                    last_source = 'manual',
                    updated_at = NOW()
                WHERE meter_id = %s
                  AND period_id = %s
            """, (
                _parse_decimal_or_none(first_active_index),
                _parse_decimal_or_none(last_active_index),
                _parse_decimal_or_none(first_inductive_index),
                _parse_decimal_or_none(last_inductive_index),
                _parse_decimal_or_none(first_capacitive_index),
                _parse_decimal_or_none(last_capacitive_index),
                meter_id,
                period_id,
            ))

            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO meter_period_indexes (
                        meter_id,
                        period_id,
                        first_active_index,
                        last_active_index,
                        first_inductive_index,
                        last_inductive_index,
                        first_capacitive_index,
                        last_capacitive_index,
                        first_source,
                        last_source,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual', 'manual', NOW())
                """, (
                    meter_id,
                    period_id,
                    _parse_decimal_or_none(first_active_index),
                    _parse_decimal_or_none(last_active_index),
                    _parse_decimal_or_none(first_inductive_index),
                    _parse_decimal_or_none(last_inductive_index),
                    _parse_decimal_or_none(first_capacitive_index),
                    _parse_decimal_or_none(last_capacitive_index),
                ))

            _recalculate_meter_period(cur, meter_id, period_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return RedirectResponse(
        f"/index-entry?period={period}&show={show}&saved=1",
        status_code=303
    )




def _parse_billing_decimal(value):
    if value is None:
        return None

    value = str(value).strip()
    if value == "":
        return None

    # Türkçe format desteği:
    # 35.187,50 -> 35187.50
    # 35.187 -> 35187 kabul edilir
    # 35187.50 -> 35187.50 kabul edilir
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    else:
        parts = value.split(".")
        if len(parts) > 2:
            value = "".join(parts)
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            value = "".join(parts)

    from decimal import Decimal
    return Decimal(value)


def _fmt_tr_number(value, digits=2):
    if value is None:
        return ""
    try:
        return f"{float(value):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


def _to_decimal(value):
    if value is None:
        return None
    from decimal import Decimal
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _get_period_by_code(period_code):
    rows = db_fetch_all("""
        SELECT id, period_code, start_date, end_date, status
        FROM periods
        WHERE period_code = %s
        LIMIT 1
    """, (period_code,))
    return rows[0] if rows else None


def _default_billing_period_code():
    rows = db_fetch_all("""
        SELECT period_code
        FROM periods
        WHERE status = 'closed'
        ORDER BY period_code DESC
        LIMIT 1
    """)
    if rows:
        return rows[0]["period_code"]
    return active_period_code()


def _get_billing_input(site_id, period_id):
    rows = db_fetch_all("""
        SELECT *
        FROM period_billing_inputs
        WHERE site_id = %s
          AND period_id = %s
        LIMIT 1
    """, (site_id, period_id))
    return rows[0] if rows else None



def _billing_group_calculation(period_code, selected_site_id, billing_group_id):
    from decimal import Decimal

    period_row = _get_period_by_code(period_code)
    if not period_row:
        return None

    group_rows = db_fetch_all("""
        SELECT
            bg.id AS billing_group_id,
            bg.group_name,
            bg.loss_method,
            bgi.item_type,
            bgi.sort_order AS group_sort_order,
            m.id AS meter_id,
            m.meter_serial,
            m.meter_name
        FROM billing_groups bg
        JOIN billing_group_items bgi ON bgi.billing_group_id = bg.id
        JOIN meters m ON m.id = bgi.meter_id
        WHERE bg.id = %s
          AND bg.site_id = %s
        ORDER BY bgi.sort_order NULLS LAST, m.meter_serial
    """, (billing_group_id, selected_site_id))

    if not group_rows:
        return _billing_calculation(period_code, selected_site_id, None)

    billing_input = _get_billing_input(selected_site_id, period_row["id"])
    unit_price = _to_decimal(billing_input.get("unit_price")) if billing_input else None

    meter_serials = [r["meter_serial"] for r in group_rows]
    main_serials = [r["meter_serial"] for r in group_rows if r["item_type"] == "main_meter"]
    sub_serials = [r["meter_serial"] for r in group_rows if r["item_type"] != "main_meter"]

    rows = db_fetch_all("""
        SELECT
            period_code,
            site_id,
            sort_order,
            meter_serial,
            meter_name,
            device_no,
            multiplier,
            first_active_index,
            last_active_index,
            active_consumption,
            calculation_status,
            calculation_status_tr,
            suggested_action
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
          AND meter_serial = ANY(%s)
        ORDER BY sort_order NULLS LAST, meter_serial
    """, (period_code, selected_site_id, meter_serials))

    row_by_serial = {r["meter_serial"]: dict(r) for r in rows}

    main_meter_consumption = None
    main_row = None

    for serial in main_serials:
        r = row_by_serial.get(serial)
        if r:
            main_row = r
            main_meter_consumption = _to_decimal(r.get("active_consumption"))
            break

    result_rows = []
    subtotal_consumption = Decimal("0")
    calculated_meter_count = 0

    for serial in sub_serials:
        r = row_by_serial.get(serial)
        if not r:
            continue

        rr = dict(r)
        c = _to_decimal(rr.get("active_consumption"))
        rr["active_consumption"] = c

        if c is not None and c > 0:
            subtotal_consumption += c
            calculated_meter_count += 1
            rr["invoice_consumption"] = c
            rr["invoice_amount"] = c * unit_price if unit_price is not None else None
            rr["share_pct"] = None
            rr["loss_share"] = None
            rr["chart_consumption"] = c
        else:
            rr["invoice_consumption"] = None
            rr["invoice_amount"] = None
            rr["share_pct"] = None
            rr["loss_share"] = None
            rr["chart_consumption"] = Decimal("0")

        result_rows.append(rr)

    loss_consumption = None
    loss_ratio_pct = None

    if main_meter_consumption is not None:
        loss_consumption = main_meter_consumption - subtotal_consumption

        if main_meter_consumption > 0:
            loss_ratio_pct = (loss_consumption / main_meter_consumption) * Decimal("100")

        synthetic = {
            "period_code": period_code,
            "site_id": selected_site_id,
            "sort_order": 9999,
            "meter_serial": "",
            "meter_name": f"{group_rows[0]['group_name']} - Diğer Tesisatlar ve Kayıp",
            "device_no": "",
            "multiplier": Decimal("1"),
            "first_active_index": None,
            "last_active_index": None,
            "active_consumption": loss_consumption,
            "calculation_status": "calculated",
            "calculation_status_tr": "Hesaplandı",
            "suggested_action": "",
            "share_pct": None,
            "loss_share": None,
            "invoice_consumption": loss_consumption,
            "invoice_amount": loss_consumption * unit_price if unit_price is not None else None,
            "chart_consumption": loss_consumption if loss_consumption is not None else Decimal("0"),
        }

        result_rows.append(synthetic)

    total_bill_amount = Decimal("0")
    has_amount = False

    for r in result_rows:
        amount = _to_decimal(r.get("invoice_amount"))
        if amount is not None:
            total_bill_amount += amount
            has_amount = True

    if not has_amount:
        total_bill_amount = None

    top10 = [
        dict(r) for r in result_rows
        if _to_decimal(r.get("chart_consumption")) is not None
        and _to_decimal(r.get("chart_consumption")) > 0
    ]
    top10 = sorted(top10, key=lambda x: _to_decimal(x.get("chart_consumption")) or Decimal("0"), reverse=True)[:10]

    max_consumption = _to_decimal(top10[0].get("chart_consumption")) if top10 else None

    for r in top10:
        chart_consumption = _to_decimal(r.get("chart_consumption")) or Decimal("0")
        if max_consumption and max_consumption > 0:
            r["bar_pct"] = float((chart_consumption / max_consumption) * Decimal("100"))
        else:
            r["bar_pct"] = 0

    return {
        "period_row": period_row,
        "billing_input": billing_input,
        "billing_group_id": billing_group_id,
        "billing_group_name": group_rows[0]["group_name"],
        "group_mode": True,
        "main_meter_row": main_row,
        "main_meter_consumption": main_meter_consumption,
        "unit_price": unit_price,
        "note": billing_input.get("note") if billing_input else "",
        "rows": result_rows,
        "top10": top10,
        "subtotal_consumption": subtotal_consumption,
        "calculated_meter_count": calculated_meter_count,
        "loss_consumption": loss_consumption,
        "loss_ratio_pct": loss_ratio_pct,
        "total_bill_amount": total_bill_amount,
        "fmt": _fmt_tr_number,
    }


def _billing_calculation(period_code, selected_site_id, billing_group_id=None):
    from decimal import Decimal

    if billing_group_id:
        return _billing_group_calculation(period_code, selected_site_id, billing_group_id)

    period_row = _get_period_by_code(period_code)
    if not period_row:
        return None

    billing_input = _get_billing_input(selected_site_id, period_row["id"])

    main_meter_consumption = None
    unit_price = None
    note = ""

    if billing_input:
        main_meter_consumption = _to_decimal(billing_input.get("main_meter_consumption"))
        unit_price = _to_decimal(billing_input.get("unit_price"))
        note = billing_input.get("note") or ""

    rows = db_fetch_all("""
        SELECT
            period_code,
            site_id,
            sort_order,
            meter_serial,
            meter_name,
            device_no,
            multiplier,
            first_active_index,
            last_active_index,
            active_consumption,
            calculation_status,
            calculation_status_tr,
            suggested_action
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
        ORDER BY sort_order NULLS LAST, meter_serial
    """, (period_code, selected_site_id))

    subtotal_consumption = Decimal("0")
    calculated_meter_count = 0

    for r in rows:
        c = _to_decimal(r.get("active_consumption"))
        if c is not None and c > 0:
            subtotal_consumption += c
            calculated_meter_count += 1

    loss_consumption = None
    loss_ratio_pct = None
    total_bill_amount = None

    if main_meter_consumption is not None:
        loss_consumption = main_meter_consumption - subtotal_consumption
        if main_meter_consumption > 0:
            loss_ratio_pct = (loss_consumption / main_meter_consumption) * Decimal("100")

    result_rows = []

    for r in rows:
        rr = dict(r)
        c = _to_decimal(rr.get("active_consumption"))
        rr["active_consumption"] = c

        loss_share = None
        invoice_consumption = None
        invoice_amount = None
        share_pct = None

        if c is not None and c > 0:
            if subtotal_consumption > 0:
                share_pct = (c / subtotal_consumption) * Decimal("100")

            if loss_consumption is not None and subtotal_consumption > 0:
                loss_share = (c / subtotal_consumption) * loss_consumption
                invoice_consumption = c + loss_share
            else:
                invoice_consumption = c

            if unit_price is not None and invoice_consumption is not None:
                invoice_amount = invoice_consumption * unit_price

        rr["share_pct"] = share_pct
        rr["loss_share"] = loss_share
        rr["invoice_consumption"] = invoice_consumption
        rr["invoice_amount"] = invoice_amount

        result_rows.append(rr)

        if invoice_amount is not None:
            total_bill_amount = (total_bill_amount or Decimal("0")) + invoice_amount

    def _chart_consumption(row):
        invoice_c = _to_decimal(row.get("invoice_consumption"))
        active_c = _to_decimal(row.get("active_consumption"))
        return invoice_c or active_c or Decimal("0")

    top10 = [
        dict(r) for r in result_rows
        if _chart_consumption(r) > 0
    ]
    top10 = sorted(top10, key=lambda x: _chart_consumption(x), reverse=True)[:10]

    max_consumption = _chart_consumption(top10[0]) if top10 else None

    for r in top10:
        chart_consumption = _chart_consumption(r)
        r["chart_consumption"] = chart_consumption

        if max_consumption and max_consumption > 0:
            r["bar_pct"] = float((chart_consumption / max_consumption) * Decimal("100"))
        else:
            r["bar_pct"] = 0

    return {
        "period_row": period_row,
        "billing_input": billing_input,
        "main_meter_consumption": main_meter_consumption,
        "unit_price": unit_price,
        "note": note,
        "rows": result_rows,
        "top10": top10,
        "subtotal_consumption": subtotal_consumption,
        "calculated_meter_count": calculated_meter_count,
        "loss_consumption": loss_consumption,
        "loss_ratio_pct": loss_ratio_pct,
        "total_bill_amount": total_bill_amount,
        "fmt": _fmt_tr_number,
    }


@app.get("/billing-allocation")
def billing_allocation(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    active_period = active_period_code()
    default_period = _default_billing_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    period = request.query_params.get("period") or default_period or active_period
    saved = request.query_params.get("saved") or ""

    available_periods = db_fetch_all("""
        SELECT period_code, status
        FROM periods
        ORDER BY period_code DESC
    """)

    calc = _billing_calculation(period, selected_site_id, _user_billing_group_id(user))

    return templates.TemplateResponse("billing_allocation.html", {
        "request": request,
        "user": user,
        "period": period,
        "active_period": active_period,
        "available_periods": available_periods,
        "calc": calc,
        "saved": saved,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })


@app.post("/billing-allocation")
def billing_allocation_save(
    request: Request,
    period: str = Form(...),
    main_meter_consumption: str = Form(""),
    unit_price: str = Form(""),
    note: str = Form(""),
):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import os
    import psycopg2
    import psycopg2.extras

    selected_site_id, sites, selected_site = get_selected_site(request, user)

    main_value = _parse_billing_decimal(main_meter_consumption)
    price_value = _parse_billing_decimal(unit_price)

    password = os.environ.get("KOLAYVERI_DB_PASSWORD")
    conn = psycopg2.connect(
        host="127.0.0.1",
        dbname="kolayveri_db",
        user="kolayveri_user",
        password=password,
    )

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id
                FROM periods
                WHERE period_code = %s
                LIMIT 1
            """, (period,))
            p = cur.fetchone()

            if not p:
                raise Exception("Dönem bulunamadı")

            period_id = p["id"]

            updated_by = ""
            try:
                updated_by = user.get("username") or user.get("email") or ""
            except Exception:
                updated_by = ""

            cur.execute("""
                INSERT INTO period_billing_inputs (
                    site_id,
                    period_id,
                    main_meter_consumption,
                    unit_price,
                    note,
                    updated_by,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (site_id, period_id)
                DO UPDATE SET
                    main_meter_consumption = EXCLUDED.main_meter_consumption,
                    unit_price = EXCLUDED.unit_price,
                    note = EXCLUDED.note,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
            """, (
                selected_site_id,
                period_id,
                main_value,
                price_value,
                note,
                updated_by,
            ))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return RedirectResponse(
        f"/billing-allocation?period={period}&saved=1",
        status_code=303
    )


@app.get("/billing-allocation.csv")
def billing_allocation_csv(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import csv
    import io
    from starlette.responses import Response

    active_period = active_period_code()
    default_period = _default_billing_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    period = request.query_params.get("period") or default_period or active_period
    calc = _billing_calculation(period, selected_site_id, _user_billing_group_id(user))

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Dönem",
        "Sıra",
        "Sayaç No",
        "Sayaç Adı",
        "Cihaz No",
        "Sayaç Tüketimi kWh",
        "Pay %",
        "Kayıp Payı kWh",
        "Faturalandırılacak kWh",
        "Birim Fiyat",
        "Fatura Tutarı",
        "Durum",
    ])

    if calc:
        for r in calc["rows"]:
            writer.writerow([
                period,
                r.get("sort_order") or "",
                r.get("meter_serial") or "",
                r.get("meter_name") or "",
                r.get("device_no") or "",
                _fmt_tr_number(r.get("active_consumption"), 3),
                _fmt_tr_number(r.get("share_pct"), 4),
                _fmt_tr_number(r.get("loss_share"), 3),
                _fmt_tr_number(r.get("invoice_consumption"), 3),
                _fmt_tr_number(calc.get("unit_price"), 6),
                _fmt_tr_number(r.get("invoice_amount"), 2),
                r.get("calculation_status_tr") or r.get("calculation_status") or "",
            ])

    content = "\ufeff" + output.getvalue()
    filename = f"faturalar_{period}.csv"

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )





def _period_label_tr(period_code):
    months = {
        "01": "Ocak",
        "02": "Şubat",
        "03": "Mart",
        "04": "Nisan",
        "05": "Mayıs",
        "06": "Haziran",
        "07": "Temmuz",
        "08": "Ağustos",
        "09": "Eylül",
        "10": "Ekim",
        "11": "Kasım",
        "12": "Aralık",
    }

    try:
        year, month = str(period_code).split("-")
        return f"{months.get(month, month)} {year}"
    except Exception:
        return str(period_code)


def _monthly_consumption_matrix(selected_site_id, q=""):
    q = (q or "").strip().lower()

    periods = db_fetch_all("""
        SELECT period_code, status
        FROM periods
        ORDER BY period_code ASC
    """)

    period_codes = [p["period_code"] for p in periods]

    meters = db_fetch_all("""
        SELECT
            id AS meter_id,
            sort_order,
            meter_serial,
            meter_name,
            device_no,
            multiplier
        FROM meters
        WHERE site_id = %s
        ORDER BY sort_order NULLS LAST, meter_serial
    """, (selected_site_id,))

    consumption_rows = db_fetch_all("""
        SELECT
            m.id AS meter_id,
            p.period_code,
            c.active_consumption
        FROM meters m
        JOIN meter_period_calculations c ON c.meter_id = m.id
        JOIN periods p ON p.id = c.period_id
        WHERE m.site_id = %s
    """, (selected_site_id,))

    consumption_map = {}
    for r in consumption_rows:
        consumption_map[(r["meter_id"], r["period_code"])] = r.get("active_consumption")

    result = []
    for m in meters:
        if q:
            haystack = " ".join([
                str(m.get("meter_serial") or ""),
                str(m.get("meter_name") or ""),
                str(m.get("device_no") or ""),
            ]).lower()

            if q not in haystack:
                continue

        rr = dict(m)
        rr["period_values"] = []

        for pc in period_codes:
            val = consumption_map.get((m["meter_id"], pc))
            rr["period_values"].append({
                "period_code": pc,
                "value": val,
                "value_tr": _fmt_tr_number(val, 0) if val is not None else "",
            })

        result.append(rr)

    period_headers = [
        {
            "period_code": p["period_code"],
            "label": _period_label_tr(p["period_code"]),
            "status": p["status"],
        }
        for p in periods
    ]

    return period_headers, result



@app.get("/monthly-consumption-tracking")
def monthly_consumption_tracking(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    active_period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    q = request.query_params.get("q", "")

    period_headers, rows = _monthly_consumption_matrix(selected_site_id, q=q)

    return templates.TemplateResponse("monthly_consumption_tracking.html", {
        "request": request,
        "user": user,
        "active_period": active_period,
        "q": q,
        "period_headers": period_headers,
        "rows": rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })




@app.get("/monthly-consumption-tracking.csv")
def monthly_consumption_tracking_csv(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import csv
    import io
    from starlette.responses import Response

    selected_site_id, sites, selected_site = get_selected_site(request, user)
    q = request.query_params.get("q", "")

    period_headers, rows = _monthly_consumption_matrix(selected_site_id, q=q)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    header = [
        "Sıra No",
        "Sayaç Seri No",
        "Firma Adı ve Nereye Ait",
        "Cihaz No",
        "Çarpan Değeri",
    ]

    for p in period_headers:
        header.append(p["label"])

    writer.writerow(header)

    for r in rows:
        line = [
            r.get("sort_order") or "",
            r.get("meter_serial") or "",
            r.get("meter_name") or "",
            r.get("device_no") or "",
            _fmt_tr_number(r.get("multiplier"), 2) if r.get("multiplier") is not None else "",
        ]

        for pv in r.get("period_values", []):
            line.append(_fmt_tr_number(pv.get("value"), 2) if pv.get("value") is not None else "")

        writer.writerow(line)

    content = "\ufeff" + output.getvalue()
    filename = "aylik_tuketim_takip.csv"

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )




def _period_code_from_header(label):
    label = str(label or "").strip()

    if re.match(r"^\d{4}-\d{2}$", label):
        return label

    months = {
        "ocak": "01",
        "şubat": "02",
        "subat": "02",
        "mart": "03",
        "nisan": "04",
        "mayıs": "05",
        "mayis": "05",
        "haziran": "06",
        "temmuz": "07",
        "ağustos": "08",
        "agustos": "08",
        "eylül": "09",
        "eylul": "09",
        "ekim": "10",
        "kasım": "11",
        "kasim": "11",
        "aralık": "12",
        "aralik": "12",
    }

    normalized = label.lower().replace(".", "").replace("_", " ").replace("-", " ")
    parts = normalized.split()

    year = None
    month = None

    for part in parts:
        if re.match(r"^\d{4}$", part):
            year = part
        if part in months:
            month = months[part]

    if year and month:
        return f"{year}-{month}"

    return None


def _parse_import_decimal(value):
    if value is None:
        return None

    value = str(value).strip()
    if value == "":
        return None

    value = value.replace("kWh", "").replace("kwh", "").replace("TL", "").strip()

    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    else:
        parts = value.split(".")
        if len(parts) > 2:
            value = "".join(parts)
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            value = "".join(parts)

    from decimal import Decimal
    try:
        return Decimal(value)
    except Exception:
        return None


def _period_dates_for_code(period_code):
    import calendar
    from datetime import date

    year, month = map(int, period_code.split("-"))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _ensure_period(cur, period_code, status="closed"):
    start_date, end_date = _period_dates_for_code(period_code)

    cur.execute("""
        SELECT id
        FROM periods
        WHERE period_code = %s
        LIMIT 1
    """, (period_code,))
    row = cur.fetchone()

    if row:
        return row["id"]

    cur.execute("""
        INSERT INTO periods (period_code, start_date, end_date, status)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (period_code, start_date, end_date, status))

    return cur.fetchone()["id"]


def _find_meter_for_import(cur, selected_site_id, meter_serial):
    meter_serial = str(meter_serial or "").strip()

    if not meter_serial:
        return None

    serial_candidates = [meter_serial]

    if meter_serial.startswith("MSY"):
        serial_candidates.append(meter_serial[3:])
    elif meter_serial.isdigit():
        serial_candidates.append("MSY" + meter_serial)

    cur.execute("""
        SELECT id
        FROM meters
        WHERE site_id = %s
          AND meter_serial = ANY(%s)
        LIMIT 1
    """, (selected_site_id, serial_candidates))

    row = cur.fetchone()
    return row["id"] if row else None


def _upsert_historical_calc(
    cur,
    meter_id,
    period_id,
    active_consumption=None,
    inductive_consumption=None,
    capacitive_consumption=None,
):
    from decimal import Decimal

    cur.execute("""
        INSERT INTO meter_period_indexes (
            meter_id,
            period_id,
            first_source,
            last_source,
            updated_at
        )
        VALUES (%s, %s, 'historical_import', 'historical_import', NOW())
        ON CONFLICT DO NOTHING
    """, (meter_id, period_id))

    cur.execute("""
        SELECT
            active_consumption,
            inductive_consumption,
            capacitive_consumption
        FROM meter_period_calculations
        WHERE meter_id = %s
          AND period_id = %s
        LIMIT 1
    """, (meter_id, period_id))

    old = cur.fetchone() or {}

    if active_consumption is None:
        active_consumption = old.get("active_consumption")

    if inductive_consumption is None:
        inductive_consumption = old.get("inductive_consumption")

    if capacitive_consumption is None:
        capacitive_consumption = old.get("capacitive_consumption")

    inductive_ratio_pct = None
    capacitive_ratio_pct = None
    reactive_status = None

    if inductive_consumption is not None or capacitive_consumption is not None:
        reactive_status = "historical"

    if active_consumption is not None and active_consumption > 0:
        if inductive_consumption is not None:
            inductive_ratio_pct = (inductive_consumption / active_consumption) * Decimal("100")
        if capacitive_consumption is not None:
            capacitive_ratio_pct = (capacitive_consumption / active_consumption) * Decimal("100")

    calculation_status = "calculated" if active_consumption is not None else "pending"

    cur.execute("""
        INSERT INTO meter_period_calculations (
            meter_id,
            period_id,
            active_consumption,
            inductive_consumption,
            capacitive_consumption,
            inductive_ratio_pct,
            capacitive_ratio_pct,
            reactive_status,
            calculation_status,
            error_message,
            calculated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NOW())
        ON CONFLICT (meter_id, period_id)
        DO UPDATE SET
            active_consumption = EXCLUDED.active_consumption,
            inductive_consumption = EXCLUDED.inductive_consumption,
            capacitive_consumption = EXCLUDED.capacitive_consumption,
            inductive_ratio_pct = EXCLUDED.inductive_ratio_pct,
            capacitive_ratio_pct = EXCLUDED.capacitive_ratio_pct,
            reactive_status = EXCLUDED.reactive_status,
            calculation_status = EXCLUDED.calculation_status,
            error_message = NULL,
            calculated_at = NOW()
    """, (
        meter_id,
        period_id,
        active_consumption,
        inductive_consumption,
        capacitive_consumption,
        inductive_ratio_pct,
        capacitive_ratio_pct,
        reactive_status,
        calculation_status,
    ))


def _decode_uploaded_csv(file_bytes):
    for enc in ["utf-8-sig", "utf-8", "cp1254", "iso-8859-9"]:
        try:
            return file_bytes.decode(enc)
        except Exception:
            pass
    return file_bytes.decode("utf-8", errors="replace")


@app.get("/admin-data-import")
def admin_data_import(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not _is_admin_user(user):
        return RedirectResponse("/dashboard", status_code=302)

    selected_site_id, sites, selected_site = get_selected_site(request, user)

    imported = request.query_params.get("imported", "")
    skipped = request.query_params.get("skipped", "")
    mode = request.query_params.get("mode", "")

    return templates.TemplateResponse("admin_data_import.html", {
        "request": request,
        "user": user,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
        "imported": imported,
        "skipped": skipped,
        "mode": mode,
    })


@app.post("/admin-data-import")
def admin_data_import_upload(
    request: Request,
    import_type: str = Form(...),
    file: UploadFile = File(...),
):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not _is_admin_user(user):
        return RedirectResponse("/dashboard", status_code=302)

    import csv
    import io
    import os
    import psycopg2
    import psycopg2.extras

    selected_site_id, sites, selected_site = get_selected_site(request, user)

    file_bytes = file.file.read()
    text = _decode_uploaded_csv(file_bytes)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    password = os.environ.get("KOLAYVERI_DB_PASSWORD")
    conn = psycopg2.connect(
        host="127.0.0.1",
        dbname="kolayveri_db",
        user="kolayveri_user",
        password=password,
    )

    imported = 0
    skipped = 0

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            headers = reader.fieldnames or []
            normalized_headers = {h.lower().strip(): h for h in headers}

            def col(*names):
                for n in names:
                    key = n.lower().strip()
                    if key in normalized_headers:
                        return normalized_headers[key]
                return None

            if import_type == "monthly_matrix":
                serial_col = col("sayaç seri no", "sayac seri no", "sayaç no", "sayac no", "meter_serial")
                name_col = col("firma adı ve nereye ait", "firma adi ve nereye ait", "sayaç adı", "sayac adi", "meter_name")
                multiplier_col = col("çarpan değeri", "carpan degeri", "çarpan", "carpan", "multiplier")

                if not serial_col:
                    raise Exception("Sayaç seri no kolonu bulunamadı")

                period_columns = []
                for h in headers:
                    pc = _period_code_from_header(h)
                    if pc:
                        period_columns.append((h, pc))

                if not period_columns:
                    raise Exception("Ay/dönem kolonu bulunamadı. Örn: Eylül 2025 veya 2025-09")

                for row in reader:
                    meter_serial = row.get(serial_col)
                    meter_id = _find_meter_for_import(cur, selected_site_id, meter_serial)

                    if not meter_id:
                        skipped += 1
                        continue

                    for h, period_code in period_columns:
                        val = _parse_import_decimal(row.get(h))
                        if val is None:
                            continue

                        period_id = _ensure_period(cur, period_code, status="closed")
                        _upsert_historical_calc(
                            cur,
                            meter_id,
                            period_id,
                            active_consumption=val,
                        )
                        imported += 1

            elif import_type == "period_detail":
                period_col = col("period_code", "dönem", "donem", "period")
                serial_col = col("meter_serial", "sayaç seri no", "sayac seri no", "sayaç no", "sayac no")
                active_col = col("active_consumption", "tüketim", "tuketim", "tüketim_kwh", "tuketim_kwh", "aktif tüketim", "aktif tuketim")
                ind_col = col("inductive_consumption", "endüktif", "enduktif", "endüktif_kvarh", "enduktif_kvarh")
                cap_col = col("capacitive_consumption", "kapasitif", "kapasitif_kvarh")
                main_col = col("main_meter_consumption", "ana sayaç tüketimi", "ana sayac tuketimi")
                price_col = col("unit_price", "birim fiyat", "birim_fiyat")

                if not period_col or not serial_col:
                    raise Exception("period_code/dönem ve sayaç seri no kolonları zorunlu")

                for row in reader:
                    period_code = str(row.get(period_col) or "").strip()
                    meter_serial = row.get(serial_col)

                    if not period_code:
                        skipped += 1
                        continue

                    meter_id = _find_meter_for_import(cur, selected_site_id, meter_serial)
                    if not meter_id:
                        skipped += 1
                        continue

                    period_id = _ensure_period(cur, period_code, status="closed")

                    active = _parse_import_decimal(row.get(active_col)) if active_col else None
                    ind = _parse_import_decimal(row.get(ind_col)) if ind_col else None
                    cap = _parse_import_decimal(row.get(cap_col)) if cap_col else None

                    _upsert_historical_calc(
                        cur,
                        meter_id,
                        period_id,
                        active_consumption=active,
                        inductive_consumption=ind,
                        capacitive_consumption=cap,
                    )

                    main_value = _parse_import_decimal(row.get(main_col)) if main_col else None
                    price_value = _parse_import_decimal(row.get(price_col)) if price_col else None

                    if main_value is not None or price_value is not None:
                        cur.execute("""
                            INSERT INTO period_billing_inputs (
                                site_id,
                                period_id,
                                main_meter_consumption,
                                unit_price,
                                note,
                                updated_by,
                                created_at,
                                updated_at
                            )
                            VALUES (%s, %s, %s, %s, 'historical_import', %s, NOW(), NOW())
                            ON CONFLICT (site_id, period_id)
                            DO UPDATE SET
                                main_meter_consumption = COALESCE(EXCLUDED.main_meter_consumption, period_billing_inputs.main_meter_consumption),
                                unit_price = COALESCE(EXCLUDED.unit_price, period_billing_inputs.unit_price),
                                note = 'historical_import',
                                updated_by = EXCLUDED.updated_by,
                                updated_at = NOW()
                        """, (
                            selected_site_id,
                            period_id,
                            main_value,
                            price_value,
                            user.get("username") or "admin",
                        ))

                    imported += 1

            else:
                raise Exception("Bilinmeyen import tipi")

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return RedirectResponse(
        f"/admin-data-import?mode={import_type}&imported={imported}&skipped={skipped}",
        status_code=303
    )


@app.get("/control-list")
def control_list(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    rows = db_fetch_all("""
        SELECT *
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
          AND calculation_status <> 'calculated'
        ORDER BY sort_order NULLS LAST
    """, (period, selected_site_id))
    rows = enrich_control_rows(rows)

    return templates.TemplateResponse("control_list.html", {
        "request": request,
        "user": user,
        "period": period,
        "rows": rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })


@app.get("/period-consumption")
def period_consumption(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    active_period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    period = request.query_params.get("period") or active_period

    available_periods = db_fetch_all("""
        SELECT period_code, status
        FROM periods
        ORDER BY period_code DESC
    """)

    rows = db_fetch_all("""
        SELECT *
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
        ORDER BY sort_order NULLS LAST
    """, (period, selected_site_id))
    rows = enrich_control_rows(rows)
    summary = make_summary(period, rows)

    return templates.TemplateResponse("period_consumption.html", {
        "request": request,
        "user": user,
        "period": period,
        "active_period": active_period,
        "available_periods": available_periods,
        "summary": summary,
        "rows": rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })


@app.get("/period-consumption.csv")
def period_consumption_csv(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import csv
    import io
    from starlette.responses import Response

    active_period = active_period_code()
    period = request.query_params.get("period") or active_period
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    rows = db_fetch_all("""
        SELECT *
        FROM period_meter_control_list
        WHERE period_code = %s
          AND site_id = %s
        ORDER BY sort_order NULLS LAST
    """, (period, selected_site_id))
    rows = enrich_control_rows(rows)

    def fmt_decimal(value, digits=2):
        if value is None:
            return ""
        try:
            return f"{float(value):.{digits}f}".replace(".", ",")
        except Exception:
            return str(value)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Dönem",
        "Tesis",
        "Sıra",
        "Sayaç No",
        "Sayaç Adı",
        "Cihaz No",
        "Çarpan",
        "İlk Endeks",
        "Son Endeks",
        "Tüketim kWh",
        "Durum",
        "Önerilen Aksiyon",
    ])

    site_name = ""
    if selected_site:
        site_name = selected_site.get("site_name") or selected_site.get("name") or ""

    for r in rows:
        writer.writerow([
            period,
            site_name,
            r.get("sort_order") or "",
            r.get("meter_serial") or "",
            r.get("meter_name") or "",
            r.get("device_no") or "",
            fmt_decimal(r.get("multiplier"), 2),
            fmt_decimal(r.get("first_active_index"), 3),
            fmt_decimal(r.get("last_active_index"), 3),
            fmt_decimal(r.get("active_consumption"), 2),
            r.get("calculation_status_tr") or r.get("calculation_status") or "",
            r.get("suggested_action") or "",
        ])

    content = "\ufeff" + output.getvalue()
    filename = f"donem_tuketimleri_{period}.csv"

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )



def _reactive_filter_rows(rows, q="", filter_type="all"):
    q = (q or "").strip().lower()
    filter_type = filter_type or "all"

    filtered = []

    for r in rows:
        active = r.get("active_consumption") or 0
        try:
            low_consumption = active is not None and active < 5
        except Exception:
            low_consumption = False

        r["low_consumption_flag"] = low_consumption

        if low_consumption and r.get("limit_status") not in ("Endeks Eksik",):
            r["operational_status"] = "Düşük Tüketim / Kontrol"
        else:
            r["operational_status"] = r.get("limit_status") or "Kontrol"

        if q:
            haystack = " ".join([
                str(r.get("meter_serial") or ""),
                str(r.get("meter_name") or ""),
                str(r.get("device_no") or ""),
            ]).lower()
            if q not in haystack:
                continue

        limit_status = r.get("limit_status")
        reactive_status = r.get("reactive_status")

        if filter_type == "all":
            pass
        elif filter_type == "over_limit":
            if limit_status not in ("Endüktif Limit Aşımı", "Kapasitif Limit Aşımı"):
                continue
        elif filter_type == "inductive_over":
            if limit_status != "Endüktif Limit Aşımı":
                continue
        elif filter_type == "capacitive_over":
            if limit_status != "Kapasitif Limit Aşımı":
                continue
        elif filter_type == "missing":
            if limit_status != "Endeks Eksik" and reactive_status != "missing_reactive_index":
                continue
        elif filter_type == "low_consumption":
            if not low_consumption:
                continue
        elif filter_type == "control":
            if limit_status != "Kontrol" and not low_consumption:
                continue

        filtered.append(r)

    return filtered


def _reactive_filter_options():
    return [
        ("all", "Tümü"),
        ("over_limit", "Limit Aşımı"),
        ("inductive_over", "Endüktif Aşım"),
        ("capacitive_over", "Kapasitif Aşım"),
        ("missing", "Endeks Eksik"),
        ("low_consumption", "Düşük Tüketim / Kontrol"),
        ("control", "Kontrol"),
    ]


def _reactive_rows(period, selected_site_id):
    return db_fetch_all("""
        SELECT
            p.period_code,
            m.site_id,
            m.sort_order,
            m.meter_serial,
            m.meter_name,
            m.device_no,
            m.multiplier,

            i.first_active_index,
            i.last_active_index,
            c.active_consumption,

            i.first_inductive_index,
            i.last_inductive_index,
            c.inductive_consumption,
            c.inductive_ratio_pct,

            i.first_capacitive_index,
            i.last_capacitive_index,
            c.capacitive_consumption,
            c.capacitive_ratio_pct,

            c.reactive_status,
            CASE
                WHEN c.reactive_status = 'calculated' THEN 'Hesaplandı'
                WHEN c.reactive_status = 'missing_reactive_index' THEN 'Reaktif Endeks Eksik'
                WHEN c.reactive_status = 'negative_reactive_index' THEN 'Reaktif Endeks Hatalı'
                WHEN c.reactive_status IS NULL THEN 'Bekliyor'
                ELSE c.reactive_status
            END AS reactive_status_tr,

            CASE
                WHEN c.inductive_ratio_pct IS NOT NULL AND c.capacitive_ratio_pct IS NOT NULL
                     AND c.inductive_ratio_pct <= 20 AND c.capacitive_ratio_pct <= 15
                    THEN 'Limit Altı'
                WHEN c.inductive_ratio_pct IS NOT NULL AND c.inductive_ratio_pct > 20
                    THEN 'Endüktif Limit Aşımı'
                WHEN c.capacitive_ratio_pct IS NOT NULL AND c.capacitive_ratio_pct > 15
                    THEN 'Kapasitif Limit Aşımı'
                WHEN c.reactive_status = 'missing_reactive_index'
                    THEN 'Endeks Eksik'
                ELSE 'Kontrol'
            END AS limit_status

        FROM meter_period_calculations c
        JOIN meter_period_indexes i
          ON i.meter_id = c.meter_id
         AND i.period_id = c.period_id
        JOIN periods p
          ON p.id = c.period_id
        JOIN meters m
          ON m.id = c.meter_id
        WHERE p.period_code = %s
          AND m.site_id = %s
        ORDER BY m.sort_order NULLS LAST, m.meter_serial
    """, (period, selected_site_id))



def _reactive_filter_rows(rows, q="", filter_type="all"):
    q = (q or "").strip().lower()
    filter_type = filter_type or "all"
    filtered = []

    for r in rows:
        active = r.get("active_consumption") or 0
        low_consumption = False
        try:
            low_consumption = active is not None and active < 5
        except Exception:
            pass

        r["low_consumption_flag"] = low_consumption

        if low_consumption and r.get("limit_status") != "Endeks Eksik":
            r["operational_status"] = "Düşük Tüketim / Kontrol"
        else:
            r["operational_status"] = r.get("limit_status") or "Kontrol"

        if q:
            haystack = " ".join([
                str(r.get("meter_serial") or ""),
                str(r.get("meter_name") or ""),
                str(r.get("device_no") or ""),
            ]).lower()
            if q not in haystack:
                continue

        limit_status = r.get("limit_status")
        reactive_status = r.get("reactive_status")

        if filter_type == "all":
            pass
        elif filter_type == "over_limit":
            if limit_status not in ("Endüktif Limit Aşımı", "Kapasitif Limit Aşımı"):
                continue
        elif filter_type == "inductive_over":
            if limit_status != "Endüktif Limit Aşımı":
                continue
        elif filter_type == "capacitive_over":
            if limit_status != "Kapasitif Limit Aşımı":
                continue
        elif filter_type == "missing":
            if limit_status != "Endeks Eksik" and reactive_status != "missing_reactive_index":
                continue
        elif filter_type == "low_consumption":
            if not low_consumption:
                continue
        elif filter_type == "control":
            if limit_status != "Kontrol" and not low_consumption:
                continue

        filtered.append(r)

    return filtered


def _reactive_filter_options():
    return [
        ("all", "Tümü"),
        ("over_limit", "Limit Aşımı"),
        ("inductive_over", "Endüktif Aşım"),
        ("capacitive_over", "Kapasitif Aşım"),
        ("missing", "Endeks Eksik"),
        ("low_consumption", "Düşük Tüketim / Kontrol"),
        ("control", "Kontrol"),
    ]


def _reactive_rows(period, selected_site_id):
    return db_fetch_all("""
        SELECT
            p.period_code,
            m.site_id,
            m.sort_order,
            m.meter_serial,
            m.meter_name,
            m.device_no,
            m.multiplier,
            c.active_consumption,
            c.inductive_consumption,
            c.inductive_ratio_pct,
            c.capacitive_consumption,
            c.capacitive_ratio_pct,
            c.reactive_status,
            CASE
                WHEN c.reactive_status = 'calculated' THEN 'Hesaplandı'
                WHEN c.reactive_status = 'missing_reactive_index' THEN 'Reaktif Endeks Eksik'
                WHEN c.reactive_status = 'negative_reactive_index' THEN 'Reaktif Endeks Hatalı'
                WHEN c.reactive_status IS NULL THEN 'Bekliyor'
                ELSE c.reactive_status
            END AS reactive_status_tr,
            CASE
                WHEN c.inductive_ratio_pct IS NOT NULL AND c.capacitive_ratio_pct IS NOT NULL
                     AND c.inductive_ratio_pct <= 20 AND c.capacitive_ratio_pct <= 15
                    THEN 'Limit Altı'
                WHEN c.inductive_ratio_pct IS NOT NULL AND c.inductive_ratio_pct > 20
                    THEN 'Endüktif Limit Aşımı'
                WHEN c.capacitive_ratio_pct IS NOT NULL AND c.capacitive_ratio_pct > 15
                    THEN 'Kapasitif Limit Aşımı'
                WHEN c.reactive_status = 'missing_reactive_index'
                    THEN 'Endeks Eksik'
                ELSE 'Kontrol'
            END AS limit_status
        FROM meter_period_calculations c
        JOIN periods p ON p.id = c.period_id
        JOIN meters m ON m.id = c.meter_id
        WHERE p.period_code = %s
          AND m.site_id = %s
        ORDER BY m.sort_order NULLS LAST, m.meter_serial
    """, (period, selected_site_id))


@app.get("/reactive-consumption")
def reactive_consumption(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    active_period = active_period_code()
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    period = request.query_params.get("period") or active_period

    available_periods = db_fetch_all("""
        SELECT period_code, status
        FROM periods
        ORDER BY period_code DESC
    """)

    q = request.query_params.get("q", "")
    filter_type = request.query_params.get("filter", "all")

    all_rows = _reactive_rows(period, selected_site_id)
    rows = _reactive_filter_rows(all_rows, q=q, filter_type=filter_type)

    summary = {
        "total_meters": len(rows),
        "total_before_filter": len(all_rows),
        "total_active": sum([(r.get("active_consumption") or 0) for r in rows]),
        "total_inductive": sum([(r.get("inductive_consumption") or 0) for r in rows]),
        "total_capacitive": sum([(r.get("capacitive_consumption") or 0) for r in rows]),
        "over_limit": len([r for r in rows if r.get("limit_status") in ("Endüktif Limit Aşımı", "Kapasitif Limit Aşımı")]),
        "missing": len([r for r in rows if r.get("limit_status") == "Endeks Eksik"]),
        "low_consumption": len([r for r in rows if r.get("low_consumption_flag")]),
    }

    return templates.TemplateResponse("reactive_consumption.html", {
        "request": request,
        "user": user,
        "period": period,
        "active_period": active_period,
        "available_periods": available_periods,
        "summary": summary,
        "rows": rows,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
        "q": q,
        "filter_type": filter_type,
        "filter_options": _reactive_filter_options(),
    })


@app.get("/reactive-consumption.csv")
def reactive_consumption_csv(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import csv
    import io
    from starlette.responses import Response

    active_period = active_period_code()
    period = request.query_params.get("period") or active_period
    selected_site_id, sites, selected_site = get_selected_site(request, user)

    q = request.query_params.get("q", "")
    filter_type = request.query_params.get("filter", "all")

    rows = _reactive_rows(period, selected_site_id)
    rows = _reactive_filter_rows(rows, q=q, filter_type=filter_type)

    def fmt(value, digits=2):
        if value is None:
            return ""
        try:
            return f"{float(value):.{digits}f}".replace(".", ",")
        except Exception:
            return str(value)

    site_name = ""
    if selected_site:
        site_name = selected_site.get("site_name") or selected_site.get("name") or ""

    filter_label = dict(_reactive_filter_options()).get(filter_type, filter_type)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Dönem", "Tesis", "Filtre", "Arama", "Sıra", "Sayaç No", "Sayaç Adı",
        "Cihaz No", "Çarpan", "Aktif Tüketim kWh",
        "Endüktif Tüketim kVArh", "Endüktif Oran %",
        "Kapasitif Tüketim kVArh", "Kapasitif Oran %",
        "Reaktif Durum", "Limit Durumu", "Operasyonel Durum",
    ])

    for r in rows:
        writer.writerow([
            period,
            site_name,
            filter_label,
            q,
            r.get("sort_order") or "",
            r.get("meter_serial") or "",
            r.get("meter_name") or "",
            r.get("device_no") or "",
            fmt(r.get("multiplier"), 2),
            fmt(r.get("active_consumption"), 2),
            fmt(r.get("inductive_consumption"), 2),
            fmt(r.get("inductive_ratio_pct"), 2),
            fmt(r.get("capacitive_consumption"), 2),
            fmt(r.get("capacitive_ratio_pct"), 2),
            r.get("reactive_status_tr") or "",
            r.get("limit_status") or "",
            r.get("operational_status") or "",
        ])

    content = "\ufeff" + output.getvalue()
    filename = f"reaktif_tuketim_{period}.csv"

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/meters")
def meters(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    selected_site_id, sites, selected_site = get_selected_site(request, user)
    billing_group_id = _user_billing_group_id(user)

    q = (request.query_params.get("q") or "").strip()
    region = (request.query_params.get("region") or "").strip()
    status = (request.query_params.get("status") or "").strip()

    if billing_group_id:
        regions = db_fetch_all("""
            SELECT DISTINCT m.region_text AS region
            FROM meters m
            JOIN billing_group_items bgi ON bgi.meter_id = m.id
            WHERE m.site_id = %s
              AND bgi.billing_group_id = %s
              AND m.region_text IS NOT NULL
              AND m.region_text <> ''
            ORDER BY m.region_text
        """, (selected_site_id, billing_group_id))

        statuses = db_fetch_all("""
            SELECT DISTINCT m.status
            FROM meters m
            JOIN billing_group_items bgi ON bgi.meter_id = m.id
            WHERE m.site_id = %s
              AND bgi.billing_group_id = %s
              AND m.status IS NOT NULL
              AND m.status <> ''
            ORDER BY m.status
        """, (selected_site_id, billing_group_id))

        where = ["m.site_id = %s", "bgi.billing_group_id = %s"]
        params = [selected_site_id, billing_group_id]

        if q:
            like = f"%{q}%"
            where.append("""(
                m.meter_serial ILIKE %s OR
                m.meter_name ILIKE %s OR
                m.device_no ILIKE %s OR
                m.region_text ILIKE %s OR
                m.block_text ILIKE %s OR
                m.floor_text ILIKE %s OR
                m.muhatap ILIKE %s
            )""")
            params.extend([like, like, like, like, like, like, like])

        if region:
            where.append("m.region_text = %s")
            params.append(region)

        if status:
            where.append("m.status = %s")
            params.append(status)

        sql = f"""
            SELECT
                m.sort_order,
                m.meter_serial,
                m.meter_name,
                m.device_no,
                m.multiplier,
                m.status,
                CASE
                    WHEN m.status = 'active' THEN 'Aktif'
                    WHEN m.status = 'inactive' THEN 'Pasif'
                    WHEN m.status IS NULL OR m.status = '' THEN '-'
                    ELSE m.status
                END AS status_label,
                m.region_text,
                m.block_text,
                m.floor_text,
                m.muhatap,
                m.current_transformer_ratio,
                bgi.item_type,
                bgi.item_name,
                bgi.sort_order AS group_sort_order
            FROM meters m
            JOIN billing_group_items bgi ON bgi.meter_id = m.id
            WHERE {' AND '.join(where)}
            ORDER BY bgi.sort_order NULLS LAST, m.sort_order NULLS LAST, m.meter_serial
        """

        rows = db_fetch_all(sql, tuple(params))

    else:
        regions = db_fetch_all("""
            SELECT DISTINCT region_text AS region
            FROM meters
            WHERE site_id = %s
              AND region_text IS NOT NULL
              AND region_text <> ''
            ORDER BY region_text
        """, (selected_site_id,))

        statuses = db_fetch_all("""
            SELECT DISTINCT status
            FROM meters
            WHERE site_id = %s
              AND status IS NOT NULL
              AND status <> ''
            ORDER BY status
        """, (selected_site_id,))

        where = ["site_id = %s"]
        params = [selected_site_id]

        if q:
            like = f"%{q}%"
            where.append("""(
                meter_serial ILIKE %s OR
                meter_name ILIKE %s OR
                device_no ILIKE %s OR
                region_text ILIKE %s OR
                block_text ILIKE %s OR
                floor_text ILIKE %s OR
                muhatap ILIKE %s
            )""")
            params.extend([like, like, like, like, like, like, like])

        if region:
            where.append("region_text = %s")
            params.append(region)

        if status:
            where.append("status = %s")
            params.append(status)

        sql = f"""
            SELECT
                sort_order,
                meter_serial,
                meter_name,
                device_no,
                multiplier,
                status,
                CASE
                    WHEN status = 'active' THEN 'Aktif'
                    WHEN status = 'inactive' THEN 'Pasif'
                    WHEN status IS NULL OR status = '' THEN '-'
                    ELSE status
                END AS status_label,
                region_text,
                block_text,
                floor_text,
                muhatap,
                current_transformer_ratio
            FROM meters
            WHERE {' AND '.join(where)}
            ORDER BY sort_order NULLS LAST, meter_serial
        """

        rows = db_fetch_all(sql, tuple(params))

    return templates.TemplateResponse("meters.html", {
        "request": request,
        "user": user,
        "rows": rows,
        "regions": regions,
        "statuses": statuses,
        "q": q,
        "region": region,
        "status": status,
        "sites": sites,
        "selected_site": selected_site,
        "selected_site_id": selected_site_id,
    })
