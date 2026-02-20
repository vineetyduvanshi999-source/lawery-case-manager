from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
import sqlite3, os, re
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime

# PDF Export
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

# PDF Text Extraction
import PyPDF2


app = Flask(__name__)
app.secret_key = "secretkey123"

# ---------------- UPLOAD CONFIG ----------------
UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------------- LOGIN REQUIRED ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


# ---------------- DATABASE INIT ----------------
def init_db():
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT,
            case_title TEXT,
            case_number TEXT,
            case_year TEXT,
            case_type TEXT,
            court TEXT,
            hearing_date TEXT,
            status TEXT,
            document TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lawyer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            note TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            phone TEXT,
            email TEXT,
            address TEXT,
            created_at TEXT
        )
    """)

    hashed = generate_password_hash("1234")
    cursor.execute("""
        INSERT OR IGNORE INTO lawyer (username, password)
        VALUES (?, ?)
    """, ("lawyer", hashed))

    conn.commit()
    conn.close()


def migrate_db():
    """
    Safe migration for old databases.
    """
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(cases)")
    cols = [c[1] for c in cursor.fetchall()]

    if "case_type" not in cols:
        cursor.execute("ALTER TABLE cases ADD COLUMN case_type TEXT DEFAULT ''")

    conn.commit()
    conn.close()


init_db()
migrate_db()


# ---------------- AUTH ----------------
@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/auth", methods=["POST"])
def authenticate():
    username = request.form.get("username")
    password = request.form.get("password")

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM lawyer WHERE username=?", (username,))
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user[2], password):
        session["logged_in"] = True
        return redirect("/")

    return redirect("/login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------- PAGES ----------------
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/add")
@login_required
def add_page():
    return render_template("add_case.html")


@app.route("/view")
@login_required
def view_page():
    return render_template("view_cases.html")


@app.route("/calendar")
@login_required
def calendar_page():
    return render_template("calendar.html")


@app.route("/clients")
@login_required
def clients_page():
    return render_template("clients.html")


@app.route("/edit/<int:id>")
@login_required
def edit_page(id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cases WHERE id=?", (id,))
    case = cursor.fetchone()
    conn.close()
    return render_template("edit_case.html", case=case)


@app.route("/case/<int:id>")
@login_required
def case_detail_page(id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM cases WHERE id=?", (id,))
    case = cursor.fetchone()

    if not case:
        conn.close()
        return "Case not found", 404

    cursor.execute("""
        SELECT note, created_at
        FROM notes
        WHERE case_id=?
        ORDER BY id DESC
    """, (id,))
    notes = cursor.fetchall()

    conn.close()
    return render_template("case_detail.html", case=case, notes=notes)


# =========================================================
#                    PDF HELPERS (PHASE 6)
# =========================================================

def extract_text_from_pdf(pdf_path):
    text = ""
    try:
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    text += "\n" + t
    except Exception:
        pass
    return text


def normalize_date_to_html(date_str):
    """
    Converts:
    20.01.2026  -> 2026-01-20
    20/01/2026  -> 2026-01-20
    20-01-2026  -> 2026-01-20
    """
    if not date_str:
        return ""

    date_str = date_str.strip()
    date_str = date_str.replace("/", ".").replace("-", ".")

    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def detect_next_hearing_date(text):
    """
    Strong logic for Delhi High Court style:
    'List on 20.01.2026'
    """
    if not text:
        return ""

    m = re.findall(r"List\s+on\s+(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4})", text, flags=re.IGNORECASE)
    if m:
        return normalize_date_to_html(m[-1])

    m2 = re.findall(
        r"(Next\s+date\s+of\s+hearing|Fixed\s+for)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4})",
        text,
        flags=re.IGNORECASE
    )
    if m2:
        return normalize_date_to_html(m2[-1][1])

    all_dates = re.findall(r"(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4})", text)
    if all_dates:
        return normalize_date_to_html(all_dates[-1])

    return ""


def detect_case_number_and_year(text):
    """
    Example:
    W.P.(C) 17864/2025
    """
    if not text:
        return ("", "", "")

    m = re.search(
        r"(W\.P\.\(C\)|CRL\.M\.C\.|CRL\.REV\.P\.|BAIL\s+APPLN\.|FAO|CM\(M\)|RFA|CS\(OS\)|CS\(COMM\)|LPA|MAT\.APP\.)\s*([0-9]+)\s*\/\s*([0-9]{4})",
        text,
        flags=re.IGNORECASE
    )
    if m:
        case_full = f"{m.group(1).upper()} {m.group(2)}/{m.group(3)}"
        return (case_full, m.group(2), m.group(3))

    m2 = re.search(r"([0-9]+)\s*\/\s*([0-9]{4})", text)
    if m2:
        return (m2.group(0), m2.group(1), m2.group(2))

    return ("", "", "")


def detect_case_title(text):
    """
    Petitioner vs Respondent detection
    """
    if not text:
        return ""

    pet = re.search(r"\n([A-Z0-9 &.,\-()\/]+)\s+\.{2,}Petitioner", text)
    res = re.search(r"\n([A-Z0-9 &.,\-()\/]+)\s+\.{2,}Respondent", text)

    petitioner = pet.group(1).strip() if pet else ""
    respondent = res.group(1).strip() if res else ""

    if petitioner and respondent:
        return f"{petitioner} vs {respondent}"

    return ""


def detect_court(text):
    if not text:
        return ""

    t = text.upper()

    if "HIGH COURT OF DELHI" in t:
        return "Delhi High Court"

    if "DISTRICT COURT" in t or "DWARKA COURT" in t:
        return "Dwarka District Court"

    return ""


def detect_case_type_from_pdf(text, court):
    """
    PHASE 6:
    Detects case type EXACTLY like dropdown values.

    For High Court: W.P.(C), CRL.M.C., BAIL APPLN., etc.
    For Dwarka: Bail Matters, CS DJ ADJ, etc. (limited)
    """
    if not text:
        return ""

    t = text.upper()

    # ---------------- HIGH COURT DETECTION ----------------
    if court == "Delhi High Court":
        # Find case type from patterns like:
        # W.P.(C) 1234/2025
        # CRL.M.C. 222/2024
        # BAIL APPLN. 10/2026

        hc_types = [
            "ADMIN.REPORT",
            "ARB.A.",
            "ARB. A. (COMM.)",
            "ARB.P.",
            "BAIL APPLN.",
            "CA",
            "CA (COMM.IPD-CR)",
            "C.A.(COMM.IPD-GI)",
            "C.A.(COMM.IPD-PAT)",
            "C.A.(COMM.IPD-PV)",
            "C.A.(COMM.IPD-TM)",
            "CAVEAT(CO.)",
            "CC(ARB.)",
            "CCP(CO.)",
            "CCP(REF)",
            "CEAC",
            "CEAR",
            "CHAT.A.C.",
            "CHAT.A.REF",
            "CMI",
            "CM(M)",
            "CM(M)-IPD",
            "C.O.",
            "CO.APP.",
            "CO.APPL.(C)",
            "CO.APPL.(M)",
            "CO.A(SB)",
            "C.O.(COMM.IPD-CR)",
            "C.O.(COMM.IPD-GI)",
            "C.O.(COMM.IPD-PAT)",
            "C.O.(COMM.IPD-TM)",
            "CO.EX.",
            "CONT.APP.(C)",
            "CONT.CAS(C)",
            "CONT.CAS.(CRL)",
            "CO.PET.",
            "C.REF.",
            "CRL.A.",
            "CRL.LIP.",
            "CRL.M.C.",
            "CRL.M.(CO)",
            "CRL.M.I.",
            "CRL.O.",
            "CRL.O.(CO.)",
            "CRL.REF.",
            "CRL.REV.P.",
            "CRL.REV.P.(MAT.)",
            "CRL.REV.P.(NDPS)",
            "CRL.REV.P.(NI)",
            "C.R.P.",
            "CRP-IPD",
            "C.RULE",
            "CS(COMM)",
            "CS(COMM) INFRA",
            "CS(OS)",
            "GP",
            "CUSAA",
            "CUS.A.C.",
            "CUS.A.R.",
            "CUSTOMA.",
            "DEATH SENTENCE REF.",
            "DEMO",
            "EDC",
            "EDR",
            "EFA(COMM)",
            "EFA(OS)",
            "EFA(OS) (COMM)",
            "EFA(OS)(IPD)",
            "EL.PET.",
            "ETR",
            "EX.F.A.",
            "EX.P.",
            "EX.S.A.",
            "FAO",
            "FAO (COMM)",
            "FAO-IPD",
            "FAO(OS)",
            "FAO(OS) (COMM)",
            "FAO(OS)(IPD)",
            "GCAC",
            "GCAR",
            "GTA",
            "GTC",
            "GTR",
            "I.A.",
            "I.P.A.",
            "ITA",
            "ITC",
            "ITR",
            "ITSA",
            "LA.APP.",
            "LPA",
            "MAC.APP.",
            "MAT.",
            "MAT.APP.",
            "MAT. APP.(FC.)",
            "MAT.CASE",
            "MAT.REF.",
            "MISC. APPEAL (FEMA)",
            "MISC. APPEAL(PMLA)",
            "OA",
            "OCJA",
            "O.M.P.",
            "O.M.P.(COMM)",
            "OMP (CONT.)",
            "O.MP. (E)",
            "O.M.P (E) (COMM.)",
            "O.M.P.(EFA)(COMM.)",
            "O.M.P. (ENF.)",
            "OMP (ENF.) (COMM.)",
            "O.M.P.(I)",
            "O.M.P.(I) (COMM.)",
            "O.M.P.(J) (COMM.)",
            "O.M.P.(MISC.)",
            "O.M.P.(MISC.)(COMM.)",
            "O.M.P.(T)",
            "O.M.P. (T) (COMM.)",
            "O.REF.",
            "RC.REV.",
            "RC.S.A.",
            "RERA APPEAL",
            "REVIEW PET.",
            "RFA",
            "RFA(COMM)",
            "RFA-IPD",
            "RFA(OS)",
            "RFA(OS)(COMM)",
            "RFA(OS)(IPD)",
            "RSA",
            "SCA",
            "SDR",
            "SERTA",
            "ST.APPL.",
            "STC",
            "ST.REF.",
            "SUR.T.REF.",
            "TEST.CAS.",
            "TR.P.(C)",
            "TR.P.(C.)",
            "TR.P.(CRL.)",
            "VAT APPEAL",
            "W.P.(C)",
            "W.P.(C)-IPD",
            "W.P.(CRL)",
            "WTA",
            "WTC",
            "WTR"
        ]

        # check in PDF for: "W.P.(C) 123/2025"
        for ct in hc_types:
            # make a safe regex
            ct_regex = re.escape(ct.upper())
            if re.search(ct_regex + r"\s*[0-9]+\s*\/\s*[0-9]{4}", t):
                return ct

        # fallback: if it contains just the case type without number
        for ct in hc_types:
            if ct.upper() in t:
                return ct

        return ""

    # ---------------- DWARKA DISTRICT COURT DETECTION ----------------
    if court == "Dwarka District Court":
        # District PDFs usually don't show same way,
        # so just detect a few common words.

        if "BAIL" in t:
            return "Bail Matters"

        if "MACT" in t:
            return "MACT"

        if "HMA" in t:
            return "HMA"

        if "CS" in t and "DJ" in t:
            return "CS DJ ADJ"

        return ""

    return ""


# =========================================================
#             PHASE 3: PDF UPLOAD (AUTO CREATE)
# =========================================================
@app.route("/add_case_pdf", methods=["POST"])
@login_required
def add_case_pdf():
    file = request.files.get("file")

    if not file:
        return jsonify({"error": "No PDF selected"}), 400

    filename = secure_filename(file.filename)

    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files allowed"}), 400

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    pdf_text = extract_text_from_pdf(filepath)

    next_date = detect_next_hearing_date(pdf_text)
    case_full, case_no, case_year = detect_case_number_and_year(pdf_text)
    case_title = detect_case_title(pdf_text)
    court = detect_court(pdf_text)

    # PHASE 6: Detect case type (dropdown style)
    case_type = detect_case_type_from_pdf(pdf_text, court)

    status = "Pending"

    # client guess
    client_name = ""
    pet = re.search(r"\n([A-Z0-9 &.,\-()\/]+)\s+\.{2,}Petitioner", pdf_text)
    if pet:
        client_name = pet.group(1).strip()
    else:
        client_name = "PDF Client"

    if case_title == "":
        case_title = "Case From PDF"

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO cases (
            client_name, case_title, case_number, case_year,
            case_type, court, hearing_date, status, document
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        client_name,
        case_title,
        case_no,
        case_year,
        case_type,
        court,
        next_date,
        status,
        filename
    ))

    case_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "message": "PDF uploaded and case created successfully!",
        "case_id": case_id,
        "next_date_detected": next_date,
        "case_number_detected": case_full,
        "court_detected": court,
        "case_type_detected": case_type
    })


# =========================================================
#      PHASE 4: PDF UPLOAD (AUTO UPDATE EXISTING CASE)
# =========================================================
@app.route("/update_case_pdf/<int:case_id>", methods=["POST"])
@login_required
def update_case_pdf(case_id):
    file = request.files.get("file")

    if not file:
        return jsonify({"error": "No PDF selected"}), 400

    filename = secure_filename(file.filename)

    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files allowed"}), 400

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    pdf_text = extract_text_from_pdf(filepath)
    next_date = detect_next_hearing_date(pdf_text)

    if next_date == "":
        return jsonify({"error": "Next hearing date not found in PDF!"}), 400

    # Detect court + case type also (optional update)
    court = detect_court(pdf_text)
    case_type = detect_case_type_from_pdf(pdf_text, court)

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    # update hearing_date + document + optional court + case_type
    cursor.execute("""
        UPDATE cases
        SET hearing_date=?,
            document=?,
            court=COALESCE(NULLIF(?,''), court),
            case_type=COALESCE(NULLIF(?,''), case_type)
        WHERE id=?
    """, (next_date, filename, court, case_type, case_id))

    conn.commit()
    conn.close()

    return jsonify({
        "message": "PDF uploaded! Hearing date updated successfully.",
        "next_date_detected": next_date,
        "court_detected": court,
        "case_type_detected": case_type
    })


# ---------------- CASE CRUD ----------------
@app.route("/add_case", methods=["POST"])
@login_required
def add_case():
    data = request.json

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO cases (
            client_name, case_title, case_number, case_year,
            case_type, court, hearing_date, status, document
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("client_name", ""),
        data.get("case_title", ""),
        data.get("case_number", ""),
        data.get("case_year", ""),
        data.get("case_type", ""),
        data.get("court", ""),
        data.get("hearing_date", ""),
        data.get("status", "Pending"),
        ""
    ))

    conn.commit()
    conn.close()

    return jsonify({"message": "Case added successfully"})


@app.route("/get_cases")
@login_required
def get_cases():
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, client_name, case_title, case_number, case_year,
               case_type, court, hearing_date, status, document
        FROM cases
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()
    conn.close()

    cases = []
    for row in rows:
        cases.append({
            "id": row[0],
            "client_name": row[1],
            "case_title": row[2],
            "case_number": row[3],
            "case_year": row[4],
            "case_type": row[5],
            "court": row[6],
            "hearing_date": row[7],
            "status": row[8],
            "document": row[9]
        })

    return jsonify(cases)


@app.route("/search_any/<query>")
@login_required
def search_any(query):
    q = "%" + query + "%"

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, client_name, case_title, case_number, case_year,
               case_type, court, hearing_date, status, document
        FROM cases
        WHERE client_name LIKE ?
           OR case_title LIKE ?
           OR case_number LIKE ?
           OR case_year LIKE ?
           OR case_type LIKE ?
           OR court LIKE ?
        ORDER BY id DESC
    """, (q, q, q, q, q, q))

    rows = cursor.fetchall()
    conn.close()

    cases = []
    for row in rows:
        cases.append({
            "id": row[0],
            "client_name": row[1],
            "case_title": row[2],
            "case_number": row[3],
            "case_year": row[4],
            "case_type": row[5],
            "court": row[6],
            "hearing_date": row[7],
            "status": row[8],
            "document": row[9]
        })

    return jsonify(cases)


@app.route("/delete/<int:id>", methods=["DELETE"])
@login_required
def delete_case(id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cases WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Case deleted"})


@app.route("/update", methods=["PUT"])
@login_required
def update_case():
    data = request.json

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE cases
        SET client_name=?,
            case_title=?,
            case_number=?,
            case_year=?,
            case_type=?,
            court=?,
            hearing_date=?,
            status=?
        WHERE id=?
    """, (
        data.get("client_name", ""),
        data.get("case_title", ""),
        data.get("case_number", ""),
        data.get("case_year", ""),
        data.get("case_type", ""),
        data.get("court", ""),
        data.get("hearing_date", ""),
        data.get("status", "Pending"),
        data.get("id")
    ))

    conn.commit()
    conn.close()
    return jsonify({"message": "Case updated"})


# ---------------- UPLOAD / DOWNLOAD ----------------
@app.route("/upload/<int:case_id>", methods=["POST"])
@login_required
def upload_file(case_id):
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file selected"})

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE cases SET document=? WHERE id=?", (filename, case_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Uploaded", "file": filename})


@app.route("/download/<filename>")
@login_required
def download_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)


# ---------------- NOTES ----------------
@app.route("/add_note/<int:case_id>", methods=["POST"])
@login_required
def add_note(case_id):
    data = request.json
    note = data.get("note")

    if not note or note.strip() == "":
        return jsonify({"error": "Empty note"}), 400

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO notes (case_id, note, created_at) VALUES (?, ?, datetime('now'))",
        (case_id, note)
    )
    conn.commit()
    conn.close()

    return jsonify({"message": "Note added"})


@app.route("/get_notes/<int:case_id>")
@login_required
def get_notes(case_id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT note, created_at FROM notes WHERE case_id=? ORDER BY id DESC",
        (case_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    notes = [{"note": r[0], "time": r[1]} for r in rows]
    return jsonify(notes)


# ---------------- PDF EXPORT (ALL CASES) ----------------
@app.route("/export_pdf")
@login_required
def export_pdf():
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, client_name, case_title, case_number, case_year,
               case_type, court, hearing_date, status
        FROM cases
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()
    conn.close()

    pdf_path = "cases_report.pdf"
    doc = SimpleDocTemplate(pdf_path, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    logo_path = os.path.join("static", "logo.png")
    if os.path.exists(logo_path):
        elements.append(Image(logo_path, width=1.2*inch, height=1.2*inch))

    elements.append(Paragraph("<b>VIPUL KUMAR - Lawyer Case Report</b>", styles["Title"]))
    elements.append(Paragraph("Generated: " + datetime.now().strftime("%d-%m-%Y %I:%M %p"), styles["Normal"]))
    elements.append(Spacer(1, 12))

    data = [["ID", "Client", "Case Title", "Case No", "Year", "Case Type", "Court", "Hearing", "Status"]]
    for r in rows:
        data.append([
            str(r[0]),
            r[1],
            r[2],
            r[3] or "",
            r[4] or "",
            r[5] or "",
            r[6] or "",
            r[7] or "",
            r[8] or ""
        ])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
    ]))

    elements.append(table)
    doc.build(elements)

    return send_from_directory(".", pdf_path, as_attachment=True)


# ---------------- PDF EXPORT (SINGLE CASE) ----------------
@app.route("/export_case_pdf/<int:case_id>")
@login_required
def export_case_pdf(case_id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cases WHERE id=?", (case_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return "Case not found", 404

    pdf_path = f"case_{case_id}.pdf"
    doc = SimpleDocTemplate(pdf_path, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    logo_path = os.path.join("static", "logo.png")
    if os.path.exists(logo_path):
        elements.append(Image(logo_path, width=1.2*inch, height=1.2*inch))

    elements.append(Paragraph("<b>VIPUL KUMAR - Case Details</b>", styles["Title"]))
    elements.append(Paragraph("Generated: " + datetime.now().strftime("%d-%m-%Y %I:%M %p"), styles["Normal"]))
    elements.append(Spacer(1, 14))

    data = [
        ["Field", "Details"],
        ["Case ID", str(row[0])],
        ["Client Name", row[1]],
        ["Case Title", row[2]],
        ["Case Number", row[3] or ""],
        ["Case Year", row[4] or ""],
        ["Case Type", row[5] or ""],
        ["Court", row[6] or ""],
        ["Hearing Date", row[7] or ""],
        ["Status", row[8] or ""],
        ["Document", row[9] or "Not Uploaded"]
    ]

    table = Table(data, colWidths=[150, 350])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 12),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
    ]))

    elements.append(table)
    doc.build(elements)

    return send_from_directory(".", pdf_path, as_attachment=True)


# ---------------- CALENDAR EVENTS ----------------
@app.route("/calendar_events")
@login_required
def calendar_events():
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, client_name, case_title, hearing_date
        FROM cases
        WHERE hearing_date IS NOT NULL AND hearing_date != ''
    """)
    rows = cursor.fetchall()
    conn.close()

    events = []
    for r in rows:
        events.append({
            "id": r[0],
            "title": f"{r[1]} - {r[2]}",
            "start": r[3]
        })

    return jsonify(events)


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run()

