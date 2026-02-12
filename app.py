from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
import sqlite3, os
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

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

    # CASES
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT,
            case_title TEXT,
            court TEXT,
            hearing_date TEXT,
            status TEXT,
            document TEXT
        )
    """)

    # LAWYER LOGIN
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lawyer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    """)

    # NOTES
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            note TEXT,
            created_at TEXT
        )
    """)

    # CLIENTS
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

    # DEFAULT USER
    hashed = generate_password_hash("1234")
    cursor.execute("""
        INSERT OR IGNORE INTO lawyer (username, password)
        VALUES (?, ?)
    """, ("lawyer", hashed))

    conn.commit()
    conn.close()


init_db()


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


# ---------------- CASE CRUD ----------------
@app.route("/add_case", methods=["POST"])
@login_required
def add_case():
    data = request.json

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO cases (client_name, case_title, court, hearing_date, status, document)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["client_name"],
        data["case_title"],
        data["court"],
        data["hearing_date"],
        data["status"],
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
    cursor.execute("SELECT * FROM cases ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    cases = []
    for row in rows:
        cases.append({
            "id": row[0],
            "client_name": row[1],
            "case_title": row[2],
            "court": row[3],
            "hearing_date": row[4],
            "status": row[5],
            "document": row[6]
        })
    return jsonify(cases)


@app.route("/search/<name>")
@login_required
def search_case(name):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM cases WHERE client_name LIKE ? ORDER BY id DESC",
        ("%" + name + "%",)
    )
    rows = cursor.fetchall()
    conn.close()

    cases = []
    for row in rows:
        cases.append({
            "id": row[0],
            "client_name": row[1],
            "case_title": row[2],
            "court": row[3],
            "hearing_date": row[4],
            "status": row[5],
            "document": row[6]
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
        SET client_name=?, case_title=?, court=?, hearing_date=?, status=?
        WHERE id=?
    """, (
        data["client_name"],
        data["case_title"],
        data["court"],
        data["hearing_date"],
        data["status"],
        data["id"]
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


# ---------------- CLIENTS ----------------
@app.route("/get_clients")
@login_required
def get_clients():
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, phone, email, address, created_at FROM clients ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    clients = []
    for r in rows:
        clients.append({
            "id": r[0],
            "name": r[1],
            "phone": r[2],
            "email": r[3],
            "address": r[4],
            "created_at": r[5]
        })

    return jsonify(clients)


@app.route("/add_client", methods=["POST"])
@login_required
def add_client():
    data = request.json

    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO clients (name, phone, email, address, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (
        data["name"],
        data["phone"],
        data["email"],
        data["address"]
    ))
    conn.commit()
    conn.close()

    return jsonify({"message": "Client saved"})

@app.route("/delete_client/<int:client_id>", methods=["DELETE"])
@login_required
def delete_client(client_id):
    conn = sqlite3.connect("cases.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM clients WHERE id=?", (client_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Client deleted successfully"})


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
    cursor.execute("SELECT id, client_name, case_title, court, hearing_date, status FROM cases ORDER BY id DESC")
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

    data = [["ID", "Client", "Case Title", "Court", "Hearing", "Status"]]
    for r in rows:
        data.append([str(r[0]), r[1], r[2], r[3], r[4] or "", r[5]])

    table = Table(data, repeatRows=1)

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 11),

        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
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
        ["Court", row[3]],
        ["Hearing Date", row[4] or ""],
        ["Status", row[5]],
        ["Document", row[6] or "Not Uploaded"]
    ]

    table = Table(data, colWidths=[140, 360])
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


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
