"""
Patient Access Management & Analytics — Backend
================================================
Single-file Flask + MongoDB backend.
Author : Senior Backend Engineer
Python : 3.10+
Run    : python run.py   (dev)
         gunicorn run:app (prod)
"""

from __future__ import annotations

import os
import re
import logging
import datetime
from functools import wraps
from typing import Any

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()                          # reads .env before anything else

from flask import Flask, request, jsonify, send_from_directory, g, Response
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
import bcrypt
import jwt

# ── App bootstrap ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("pam")

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "..", "frontend", "static"),
    static_url_path="/static",
    template_folder=os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"),
)
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGINS", "*")}})


# ── Config ────────────────────────────────────────────────────────────────────
class Config:
    MONGO_URI      : str  = os.getenv("MONGO_URI",      "mongodb://localhost:27017")
    DB_NAME        : str  = os.getenv("DB_NAME",         "patient_access_mgmt")
    JWT_SECRET     : str  = os.getenv("JWT_SECRET",      "super-secret-change-in-prod")
    JWT_ALGO       : str  = "HS256"
    TOKEN_TTL_DAYS : int  = int(os.getenv("TOKEN_TTL_DAYS", "1"))
    PORT           : int  = int(os.getenv("PORT",         "5000"))
    DEBUG          : bool = os.getenv("DEBUG", "false").lower() == "true"

cfg = Config()


# ── MongoDB ───────────────────────────────────────────────────────────────────
_client: MongoClient | None = None


def get_db():
    """Return the MongoDB database, lazily creating the connection."""
    global _client
    if _client is None:
        _client = MongoClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5_000)
        _bootstrap_db(_client[cfg.DB_NAME])
    return _client[cfg.DB_NAME]


def _bootstrap_db(db) -> None:
    """Idempotently create indexes and seed demo data on first run."""
    # ── indexes ───────────────────────────────────────────────────────────────
    db.users.create_index("email",        unique=True, sparse=True)
    db.users.create_index("validator_id", unique=True, sparse=True)
    db.users.create_index("username",     unique=True, sparse=True)
    db.patients.create_index("patient_id", unique=True)
    db.patients.create_index("email",      unique=True)
    db.patients.create_index("stage")
    db.patients.create_index([("name", ASCENDING)])
    db.reports.create_index("patient_id",  unique=True)
    db.reports.create_index("payment_status")
    db.reports.create_index([("created_at", DESCENDING)])

    # ── seed demo users ───────────────────────────────────────────────────────
    _upsert_patient_user(db, "patient1@example.com", "New Patient",      "MCP241000")
    _upsert_patient_user(db, "patient2@example.com", "Rajan Menon",      "MCP241001")
    _upsert_patient_user(db, "patient3@example.com", "Lakshmi Krishnan", "MCP242057")
    _upsert_patient_user(db, "patient4@example.com", "Karthik Raja",     "MCP249888")

    if not db.users.find_one({"validator_id": "VAL001"}):
        db.users.insert_one({
            "validator_id":  "VAL001",
            "role":          "validator",
            "password_hash": _hash("password123"),
            "created_at":    _now(),
        })

    if not db.users.find_one({"username": "admin"}):
        db.users.insert_one({
            "username":      "admin",
            "role":          "admin",
            "password_hash": _hash("admin123"),
            "created_at":    _now(),
        })

    # ── seed demo submissions ─────────────────────────────────────────────────
    _seed_submissions(db)
    log.info("Database bootstrapped successfully.")


def _upsert_patient_user(db, email: str, name: str, patient_id: str) -> None:
    if not db.users.find_one({"email": email}):
        db.users.insert_one({
            "email":         email,
            "name":          name,
            "patient_id":    patient_id,
            "role":          "patient",
            "password_hash": _hash("password123"),
            "created_at":    _now(),
        })


def _seed_submissions(db) -> None:
    seed = [
        {
            "email": "patient2@example.com",
            "patient_id": "MCP241001",
            "name": "Rajan Menon",
            "stage": "submitted",
            "submitted_date": "24 Feb 2025",
            "hospital": "City Hospital, Bangalore",
            "hospital_phone": "080-23456789",
            "doctor": "Dr. Priya Sharma",
            "bill_amount": "₹48,500",
            "age": 45, "diseases": "Diabetes, Hypertension",
            "address": "12 MG Road, Bangalore",
            "aadhar": "123412341234", "pan": "ABCDE1234F",
            "phone": "9876543210", "marital_status": "Married", "income": 850000,
            "hospital_name": "City Hospital", "doctor_name": "Priya Sharma",
            "admission_date": "2025-02-10", "discharge_date": "2025-02-15",
            "department": "General Medicine", "ward_type": "General Ward",
            "payment_status": "Pending",
            "bill": {"total": 48500, "room_charges": 15000, "medicine_cost": 12000,
                     "doctor_fees": 8000, "lab_charges": 5000, "icu_charges": 0,
                     "ot_charges": 0, "other_charges": 8500, "insurance": 0, "discount": 500},
        },
        {
            "email": "patient3@example.com",
            "patient_id": "MCP242057",
            "name": "Lakshmi Krishnan",
            "stage": "review",
            "submitted_date": "23 Feb 2025", "review_date": "24 Feb 2025",
            "hospital": "Apollo Hospital, Chennai",
            "hospital_phone": "044-98765432",
            "doctor": "Dr. Suresh Kumar",
            "bill_amount": "₹1,25,000",
            "age": 32, "diseases": "Asthma, Allergy",
            "address": "34 Anna Nagar, Chennai",
            "aadhar": "432143214321", "pan": "XYZAB7890D",
            "phone": "9123456780", "marital_status": "Single", "income": 250000,
            "hospital_name": "Apollo Hospital", "doctor_name": "Suresh Kumar",
            "admission_date": "2025-02-14", "discharge_date": "2025-02-20",
            "department": "Pulmonology", "ward_type": "Private Room",
            "payment_status": "Pending",
            "bill": {"total": 125000, "room_charges": 35000, "medicine_cost": 25000,
                     "doctor_fees": 20000, "lab_charges": 15000, "icu_charges": 0,
                     "ot_charges": 20000, "other_charges": 10000, "insurance": 0, "discount": 0},
        },
        {
            "email": "patient4@example.com",
            "patient_id": "MCP249888",
            "name": "Karthik Raja",
            "stage": "approved",
            "submitted_date": "20 Feb 2025", "review_date": "21 Feb 2025",
            "approved_date": "22 Feb 2025",
            "hospital": "Fortis Hospital, Bangalore",
            "hospital_phone": "080-34567890",
            "doctor": "Dr. Meera Nair",
            "bill_amount": "₹95,000",
            "age": 55, "diseases": "Heart Disease",
            "address": "56 Church Street, Bangalore",
            "aadhar": "567856785678", "pan": "PQRST5678G",
            "phone": "9988776655", "marital_status": "Married", "income": 1800000,
            "hospital_name": "Fortis Hospital", "doctor_name": "Meera Nair",
            "admission_date": "2025-02-15", "discharge_date": "2025-02-22",
            "department": "Cardiology", "ward_type": "ICU",
            "payment_status": "Approved",
            "bill": {"total": 95000, "room_charges": 25000, "medicine_cost": 20000,
                     "doctor_fees": 15000, "lab_charges": 10000, "icu_charges": 15000,
                     "ot_charges": 0, "other_charges": 10000, "insurance": 0, "discount": 0},
        },
    ]
    for s in seed:
        if not db.patients.find_one({"email": s["email"]}):
            db.patients.insert_one({**s, "created_at": _now(), "updated_at": _now()})

    for patient in db.patients.find({"stage": {"$ne": "new"}}):
        if not db.reports.find_one({"patient_id": patient["patient_id"]}):
            db.reports.insert_one({
                "patient_id":         patient["patient_id"],
                "patient_email":      patient["email"],
                "name":               patient["name"],
                "age":                patient.get("age"),
                "diseases":           patient.get("diseases"),
                "address":            patient.get("address"),
                "aadhar":             patient.get("aadhar"),
                "pan":                patient.get("pan"),
                "phone":              patient.get("phone"),
                "marital_status":     patient.get("marital_status"),
                "income":             patient.get("income"),
                "hospital_name":      patient.get("hospital_name"),
                "hospital_phone":     patient.get("hospital_phone"),
                "doctor_name":        patient.get("doctor_name"),
                "admission_date":     patient.get("admission_date"),
                "discharge_date":     patient.get("discharge_date"),
                "department":         patient.get("department"),
                "ward_type":          patient.get("ward_type"),
                "payment_status":     patient.get("payment_status", "Pending"),
                "validator_decision": (
                    "approved" if patient["stage"] == "approved"
                    else "rejected" if patient["stage"] == "rejected"
                    else None
                ),
                "bill":           patient.get("bill", {}),
                "stage":          patient["stage"],
                "submitted_date": patient.get("submitted_date"),
                "approved_date":  patient.get("approved_date"),
                "created_at":     _now(),
                "updated_at":     _now(),
            })


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _check(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _make_token(payload: dict) -> str:
    payload["exp"] = _now() + datetime.timedelta(days=cfg.TOKEN_TTL_DAYS)
    payload["iat"] = _now()
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGO)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.JWT_ALGO])


def _ok(data: Any = None, msg: str = "success", status: int = 200):
    resp: dict = {"status": "ok", "message": msg}
    if data is not None:
        resp["data"] = data
    return jsonify(resp), status


def _err(msg: str, status: int = 400):
    return jsonify({"status": "error", "message": msg}), status


def _clean(doc: dict) -> dict:
    """Strip internal MongoDB fields and serialise datetimes."""
    if doc is None:
        return {}
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    for k, v in list(doc.items()):
        if isinstance(v, datetime.datetime):
            doc[k] = v.isoformat()
    return doc


# ── Validators ────────────────────────────────────────────────────────────────
AADHAR_RE = re.compile(r"^\d{12}$")
PAN_RE     = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
PHONE_RE   = re.compile(r"^\d{10}$")
EMAIL_RE   = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


def _validate_patient_fields(data: dict, partial: bool = False) -> list[str]:
    errors: list[str] = []
    if not partial:
        for f in ("name", "age", "marital_status", "address", "aadhar", "pan", "phone", "income"):
            if not data.get(f) and data.get(f) != 0:
                errors.append(f"`{f}` is required")

    aadhar = str(data.get("aadhar", ""))
    if aadhar and not AADHAR_RE.match(aadhar):
        errors.append("Aadhar must be exactly 12 digits")

    pan = str(data.get("pan", "")).upper()
    if pan and not PAN_RE.match(pan):
        errors.append("PAN must be in format ABCDE1234F")

    phone = str(data.get("phone", ""))
    if phone and not PHONE_RE.match(phone):
        errors.append("Phone must be exactly 10 digits")

    if "age" in data:
        try:
            if not (0 <= int(data["age"]) <= 120):
                errors.append("Age must be between 0 and 120")
        except (ValueError, TypeError):
            errors.append("Age must be a number")

    if "income" in data:
        try:
            if int(data["income"]) < 0:
                errors.append("Income must be non-negative")
        except (ValueError, TypeError):
            errors.append("Income must be a number")

    return errors


# ── Auth decorator ─────────────────────────────────────────────────────────────
def require_auth(*allowed_roles: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return _err("Missing or malformed Authorization header", 401)
            token = auth_header.split(" ", 1)[1]
            try:
                payload = _decode_token(token)
            except jwt.ExpiredSignatureError:
                return _err("Token has expired — please log in again", 401)
            except jwt.InvalidTokenError:
                return _err("Invalid token", 401)
            if allowed_roles and payload.get("role") not in allowed_roles:
                return _err("Insufficient permissions", 403)
            g.user = payload
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═════════════════════════════════════════════════════════════════════════════

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    """Serve the main SPA entry point."""
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
    return send_from_directory(templates_dir, "index.html")


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    try:
        get_db().command("ping")
        db_ok = True
    except Exception as exc:
        log.warning("DB ping failed: %s", exc)
        db_ok = False
    return _ok({"db": "connected" if db_ok else "error", "server": "running"})


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH  /api/auth/*
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/patient/login", methods=["POST"])
def patient_login():
    """POST { email, password } → JWT + user object"""
    body     = request.get_json(silent=True) or {}
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not password:
        return _err("email and password are required")

    db   = get_db()
    user = db.users.find_one({"email": email, "role": "patient"})
    if not user or not _check(password, user["password_hash"]):
        return _err("Invalid email or password", 401)

    token = _make_token({
        "sub":        email,
        "role":       "patient",
        "patient_id": user.get("patient_id"),
    })
    return _ok({
        "token": token,
        "user":  {"email": email, "name": user.get("name"),
                  "patient_id": user.get("patient_id"), "role": "patient"},
    })


@app.route("/api/auth/patient/register", methods=["POST"])
def patient_register():
    """POST { email, password, name } → JWT"""
    body     = request.get_json(silent=True) or {}
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    name     = (body.get("name") or "").strip()

    if not email or not password:
        return _err("email and password are required")
    if not EMAIL_RE.match(email):
        return _err("Invalid email format")
    if len(password) < 6:
        return _err("Password must be at least 6 characters")

    db = get_db()
    try:
        db.users.insert_one({
            "email":         email,
            "name":          name or email.split("@")[0],
            "role":          "patient",
            "patient_id":    None,
            "password_hash": _hash(password),
            "created_at":    _now(),
        })
    except DuplicateKeyError:
        return _err("Email already registered", 409)

    token = _make_token({"sub": email, "role": "patient"})
    return _ok({"token": token, "message": "Registration successful"}, status=201)


@app.route("/api/auth/validator/login", methods=["POST"])
def validator_login():
    """POST { validator_id, password } → JWT"""
    body         = request.get_json(silent=True) or {}
    validator_id = (body.get("validator_id") or "").strip()
    password     = body.get("password") or ""

    if not validator_id or not password:
        return _err("validator_id and password are required")

    db   = get_db()
    user = db.users.find_one({"validator_id": validator_id, "role": "validator"})
    if not user or not _check(password, user["password_hash"]):
        return _err("Invalid validator credentials", 401)

    token = _make_token({"sub": validator_id, "role": "validator"})
    return _ok({"token": token, "user": {"validator_id": validator_id, "role": "validator"}})


@app.route("/api/auth/admin/login", methods=["POST"])
def admin_login():
    """POST { username, password } → JWT"""
    body     = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return _err("username and password are required")

    db   = get_db()
    user = db.users.find_one({"username": username, "role": "admin"})
    if not user or not _check(password, user["password_hash"]):
        return _err("Invalid admin credentials", 401)

    token = _make_token({"sub": username, "role": "admin"})
    return _ok({"token": token, "user": {"username": username, "role": "admin"}})


# ══════════════════════════════════════════════════════════════════════════════
#  PATIENTS  /api/patients/*
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/patients/me", methods=["GET"])
@require_auth("patient")
def get_my_profile():
    """Returns the logged-in patient's profile and current application stage."""
    db      = get_db()
    email   = g.user["sub"]
    patient = db.patients.find_one({"email": email})
    if not patient:
        return _ok({"stage": "new", "email": email})
    return _ok(_clean(patient))


@app.route("/api/patients/submit", methods=["POST"])
@require_auth("patient")
def submit_application():
    """Submit a new healthcare access application."""
    db    = get_db()
    email = g.user["sub"]

    if db.patients.find_one({"email": email, "stage": {"$ne": "new"}}):
        return _err("Application already submitted. Use PATCH /api/patients/me to update.", 409)

    body   = request.get_json(silent=True) or {}
    errors = _validate_patient_fields(body)
    if errors:
        return _err("; ".join(errors))

    # Build bill
    bill_fields = ["room_charges", "medicine_cost", "doctor_fees", "lab_charges",
                   "icu_charges", "ot_charges", "other_charges", "discount"]
    bill = {f: int(body.get(f, 0) or 0) for f in bill_fields}
    bill["insurance"] = 0
    subtotal    = sum(bill[f] for f in bill_fields if f != "discount")
    bill["total"] = max(0, subtotal - bill["discount"])

    patient_id = (body.get("patient_id") or "").strip() or \
                 f"MCP{_now().strftime('%y')}{os.urandom(3).hex().upper()[:5]}"
    now_str = _now().strftime("%d %b %Y")

    doc = {
        "email":          email,
        "patient_id":     patient_id,
        "name":           body["name"].strip(),
        "age":            int(body["age"]),
        "marital_status": body["marital_status"],
        "diseases":       body.get("diseases", ""),
        "address":        body["address"].strip(),
        "aadhar":         body["aadhar"],
        "pan":            body["pan"].upper(),
        "phone":          body["phone"],
        "income":         int(body["income"]),
        "hospital_name":  body.get("hospital_name", ""),
        "hospital":       body.get("hospital_name", ""),
        "hospital_phone": body.get("hospital_phone", ""),
        "doctor_name":    body.get("doctor_name", ""),
        "doctor":         f"Dr. {body.get('doctor_name', '')}",
        "admission_date": body.get("admission_date"),
        "discharge_date": body.get("discharge_date"),
        "department":     body.get("department", "General Medicine"),
        "ward_type":      body.get("ward_type", "General Ward"),
        "payment_status": body.get("payment_status", "Pending"),
        "bill":           bill,
        "bill_amount":    f"₹{bill['total']:,}",
        "stage":          "submitted",
        "submitted_date": now_str,
        "created_at":     _now(),
        "updated_at":     _now(),
    }

    try:
        db.patients.insert_one(doc)
    except DuplicateKeyError:
        return _err("Patient ID conflict. Please regenerate the ID.", 409)

    db.users.update_one(
        {"email": email},
        {"$set": {"patient_id": patient_id, "name": doc["name"]}},
    )

    # Mirror into validator reports queue
    report_doc = {k: v for k, v in doc.items()}
    report_doc.update({
        "patient_email":      email,
        "validator_decision": None,
    })
    report_doc.pop("_id", None)
    db.reports.insert_one(report_doc)

    return _ok(
        {"patient_id": patient_id, "stage": "submitted", "submitted_date": now_str},
        status=201,
    )


@app.route("/api/patients/me", methods=["PATCH"])
@require_auth("patient")
def update_my_profile():
    """Update own personal details (not bill / stage)."""
    db    = get_db()
    email = g.user["sub"]
    body  = request.get_json(silent=True) or {}

    editable = {"name", "age", "marital_status", "diseases", "address", "phone", "income"}
    updates  = {k: v for k, v in body.items() if k in editable}
    if not updates:
        return _err("No editable fields provided")

    errors = _validate_patient_fields(updates, partial=True)
    if errors:
        return _err("; ".join(errors))

    if "age"    in updates: updates["age"]    = int(updates["age"])
    if "income" in updates: updates["income"] = int(updates["income"])
    if "pan"    in updates: updates["pan"]    = updates["pan"].upper()
    updates["updated_at"] = _now()

    result = db.patients.find_one_and_update(
        {"email": email}, {"$set": updates}, return_document=True
    )
    if not result:
        return _err("No application found for this patient", 404)

    db.reports.update_one({"patient_email": email}, {"$set": updates})
    return _ok(_clean(result))


# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATOR  /api/validator/*
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/validator/reports", methods=["GET"])
@require_auth("validator")
def get_validator_reports():
    """Paginated report queue with optional name/ID search."""
    db     = get_db()
    search = request.args.get("search", "").strip()
    page   = max(1, int(request.args.get("page", 1)))
    limit  = min(100, int(request.args.get("limit", 20)))
    skip   = (page - 1) * limit

    query: dict = {}
    if search:
        query["$or"] = [
            {"name":       {"$regex": search, "$options": "i"}},
            {"patient_id": {"$regex": search, "$options": "i"}},
        ]

    total   = db.reports.count_documents(query)
    reports = list(
        db.reports
          .find(query, {"_id": 0, "password_hash": 0})
          .sort("created_at", DESCENDING)
          .skip(skip)
          .limit(limit)
    )
    for r in reports:
        for k, v in r.items():
            if isinstance(v, datetime.datetime):
                r[k] = v.isoformat()

    return _ok({
        "reports": reports,
        "total":   total,
        "page":    page,
        "limit":   limit,
        "pages":   (total + limit - 1) // limit,
    })


@app.route("/api/validator/reports/<patient_id>", methods=["GET"])
@require_auth("validator")
def get_report_detail(patient_id: str):
    """Full report detail for a single patient."""
    db     = get_db()
    report = db.reports.find_one({"patient_id": patient_id}, {"_id": 0, "password_hash": 0})
    if not report:
        return _err("Report not found", 404)
    return _ok(_clean(report))


@app.route("/api/validator/reports/<patient_id>/decision", methods=["POST"])
@require_auth("validator")
def make_decision(patient_id: str):
    """
    POST { decision: "approved" | "rejected" }
    Approve or reject a patient application. Idempotent guard prevents double decisions.
    """
    db       = get_db()
    body     = request.get_json(silent=True) or {}
    decision = (body.get("decision") or "").lower()

    if decision not in ("approved", "rejected"):
        return _err("`decision` must be 'approved' or 'rejected'")

    report = db.reports.find_one({"patient_id": patient_id})
    if not report:
        return _err("Report not found", 404)
    if report.get("validator_decision"):
        return _err(f"Decision already recorded: {report['validator_decision']}", 409)

    now_str = _now().strftime("%d %b %Y")
    now_dt  = _now()

    r_updates = {
        "validator_decision": decision,
        "payment_status":     "Approved" if decision == "approved" else "Rejected",
        "stage":              decision,
        "updated_at":         now_dt,
    }
    p_updates = {
        "stage":          decision,
        "payment_status": "Approved" if decision == "approved" else "Rejected",
        "review_date":    now_str,
        "updated_at":     now_dt,
    }
    if decision == "approved":
        r_updates["approved_date"] = p_updates["approved_date"] = now_str
    else:
        r_updates["rejected_date"] = p_updates["rejected_date"] = now_str

    db.reports.update_one({"patient_id": patient_id}, {"$set": r_updates})
    db.patients.update_one({"patient_id": patient_id}, {"$set": p_updates})

    # Mock email notification
    patient_email = report.get("patient_email", "unknown")
    log.info("MOCK EMAIL → %s  |  Application %s  |  Patient %s",
             patient_email, decision.upper(), patient_id)

    return _ok({"patient_id": patient_id, "decision": decision, "date": now_str})


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN  /api/admin/*
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/reports", methods=["GET"])
@require_auth("admin")
def admin_reports():
    """
    Paginated reports (default: Approved only) with aggregate stats.
    Query params: search, status, page, limit
    """
    db     = get_db()
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "Approved")
    page   = max(1, int(request.args.get("page", 1)))
    limit  = min(200, int(request.args.get("limit", 50)))
    skip   = (page - 1) * limit

    query: dict = {}
    if status:
        query["payment_status"] = {"$regex": f"^{status}$", "$options": "i"}
    if search:
        query["$or"] = [
            {"name":       {"$regex": search, "$options": "i"}},
            {"patient_id": {"$regex": search, "$options": "i"}},
        ]

    total   = db.reports.count_documents(query)
    reports = list(
        db.reports
          .find(query, {"_id": 0, "password_hash": 0, "aadhar": 0})
          .sort("approved_date", DESCENDING)
          .skip(skip)
          .limit(limit)
    )
    for r in reports:
        for k, v in r.items():
            if isinstance(v, datetime.datetime):
                r[k] = v.isoformat()
        bill_total           = (r.get("bill") or {}).get("total", 0)
        r["insurance_amount"] = round(bill_total * 0.5)
        r["patient_pays"]     = round(bill_total * 0.5)

    # Aggregate stats (always over ALL approved records, ignoring pagination)
    agg = list(db.reports.aggregate([
        {"$match": {"payment_status": {"$regex": "^Approved$", "$options": "i"}}},
        {"$group": {"_id": None,
                    "total_approved": {"$sum": 1},
                    "total_bill":     {"$sum": "$bill.total"}}},
    ]))
    stats = agg[0] if agg else {"total_approved": 0, "total_bill": 0}
    stats.pop("_id", None)
    stats["total_insurance"] = round(stats.get("total_bill", 0) * 0.5)
    stats["patient_pays"]    = round(stats.get("total_bill", 0) * 0.5)

    return _ok({
        "reports": reports,
        "stats":   stats,
        "total":   total,
        "page":    page,
        "limit":   limit,
        "pages":   (total + limit - 1) // limit,
    })


@app.route("/api/admin/patients/<patient_email>", methods=["PATCH"])
@require_auth("admin")
def admin_edit_patient(patient_email: str):
    """
    Edit personal details of an approved patient.
    Editable: name, age, marital_status, diseases, address, aadhar, pan, phone, income
    """
    db      = get_db()
    patient = db.patients.find_one({"email": patient_email})
    if not patient:
        return _err("Patient not found", 404)
    if patient.get("stage") != "approved":
        return _err("Only approved patients can be edited via admin", 403)

    body     = request.get_json(silent=True) or {}
    editable = {"name", "age", "marital_status", "diseases",
                "address", "aadhar", "pan", "phone", "income"}
    updates  = {k: v for k, v in body.items() if k in editable}
    if not updates:
        return _err("No editable fields provided")

    errors = _validate_patient_fields(updates, partial=True)
    if errors:
        return _err("; ".join(errors))

    if "age"    in updates: updates["age"]    = int(updates["age"])
    if "income" in updates: updates["income"] = int(updates["income"])
    if "pan"    in updates: updates["pan"]    = updates["pan"].upper()
    updates["updated_at"] = _now()

    db.patients.update_one({"email": patient_email}, {"$set": updates})
    db.reports.update_one({"patient_email": patient_email}, {"$set": updates})

    updated = db.patients.find_one({"email": patient_email},
                                   {"_id": 0, "password_hash": 0})
    return _ok(_clean(updated))


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYTICS  /api/analytics/*
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/analytics/summary", methods=["GET"])
@require_auth("admin", "validator")
def analytics_summary():
    """
    KPI dashboard data: status counts, bill totals, monthly trend,
    status distribution, department breakdown.
    """
    db = get_db()

    # Status counts
    status_map: dict = {}
    for r in db.reports.aggregate([{"$group": {"_id": "$payment_status", "count": {"$sum": 1}}}]):
        status_map[(r["_id"] or "unknown").lower()] = r["count"]

    total    = sum(status_map.values())
    approved = status_map.get("approved", 0)
    rejected = status_map.get("rejected", 0)
    pending  = total - approved - rejected

    # Bill stats
    bill_agg = list(db.reports.aggregate([
        {"$group": {"_id": None,
                    "total_bill": {"$sum": "$bill.total"},
                    "avg_bill":   {"$avg": "$bill.total"}}},
    ]))
    total_bill = int(bill_agg[0]["total_bill"]) if bill_agg else 0
    avg_bill   = int(bill_agg[0]["avg_bill"])   if bill_agg else 0

    # Monthly trend — current calendar year
    year = _now().year
    submitted_by_month = [0] * 12
    approved_by_month  = [0] * 12
    for r in db.reports.aggregate([
        {"$match": {"created_at": {
            "$gte": datetime.datetime(year, 1, 1),
            "$lt":  datetime.datetime(year + 1, 1, 1),
        }}},
        {"$group": {"_id": {"month": {"$month": "$created_at"},
                             "status": "$payment_status"},
                    "count": {"$sum": 1}}},
    ]):
        m = r["_id"]["month"] - 1
        submitted_by_month[m] += r["count"]
        if (r["_id"]["status"] or "").lower() == "approved":
            approved_by_month[m] += r["count"]

    # Department distribution
    dept_data = {
        r["_id"] or "Other": r["count"]
        for r in db.reports.aggregate([
            {"$group": {"_id": "$department", "count": {"$sum": 1}}},
            {"$sort":  {"count": -1}},
            {"$limit": 8},
        ])
    }

    return _ok({
        "kpis": {
            "total_patients": total,
            "approved":       approved,
            "pending":        pending,
            "rejected":       rejected,
            "total_bill":     total_bill,
            "avg_bill":       avg_bill,
        },
        "monthly_trend": {
            "submitted": submitted_by_month,
            "approved":  approved_by_month,
        },
        "status_distribution": {
            "approved": approved,
            "pending":  pending,
            "rejected": rejected,
        },
        "department_distribution": dept_data,
    })


@app.route("/api/analytics/export/csv", methods=["GET"])
@require_auth("admin", "validator")
def export_analytics_csv():
    """Download CSV of all patient reports."""
    db = get_db()
    rows = ["Patient Name,Patient ID,Department,Bill Amount,Status,Date"]
    for r in db.reports.find({}, {"_id": 0, "password_hash": 0, "aadhar": 0}):
        bill = (r.get("bill") or {}).get("total", 0)
        rows.append(
            f'"{r.get("name","Unknown")}",{r.get("patient_id","--")},'
            f'{r.get("department","General")},{bill},'
            f'{r.get("payment_status","Pending")},{r.get("submitted_date","")}'
        )
    return Response(
        "\n".join(rows),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=analytics_report.csv"},
    )


@app.route("/api/admin/export/csv", methods=["GET"])
@require_auth("admin")
def export_approved_csv():
    """Download CSV of approved reports with insurance split."""
    db = get_db()
    rows = ["Patient Name,Patient ID,Bill Amount (INR),Insurance 50% (INR),Patient Pays (INR),Approved Date"]
    for r in db.reports.find(
        {"payment_status": {"$regex": "^Approved$", "$options": "i"}},
        {"_id": 0, "password_hash": 0, "aadhar": 0},
    ):
        total     = (r.get("bill") or {}).get("total", 0)
        insurance = round(total * 0.5)
        rows.append(
            f'"{r.get("name","Unknown")}",{r.get("patient_id","--")},'
            f'{total},{insurance},{insurance},{r.get("approved_date","")}'
        )
    return Response(
        "\n".join(rows),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=approved_reports.csv"},
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(_):
    return _err("Route not found", 404)

@app.errorhandler(405)
def method_not_allowed(_):
    return _err("Method not allowed", 405)

@app.errorhandler(500)
def internal_error(exc):
    log.exception("Unhandled exception: %s", exc)
    return _err("Internal server error", 500)

@app.errorhandler(PyMongoError)
def mongo_error(exc):
    log.exception("MongoDB error: %s", exc)
    return _err("Database error — please retry", 503)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("Starting PAM server on port %s  |  debug=%s", cfg.PORT, cfg.DEBUG)
    app.run(host="0.0.0.0", port=cfg.PORT, debug=cfg.DEBUG)
