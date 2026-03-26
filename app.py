"""
app.py — ENVEA QAL2 Analyser Web Application
Flask + SQLite | Déployable sur Render.com

Usage local  : python app.py
Render.com   : Suivre le README.md
"""
import os
import json
import sqlite3
import io
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, render_template, redirect, url_for,
    flash, jsonify, send_file, abort, session
)

from pdf_parser import parse_pdf
from analyzer import analyze, check_cofrac_status, check_norm_updates

# ─── Configuration ─────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "envea-qal2-secret-2025-change-me")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB max PDF

DB_PATH = os.environ.get("DB_PATH", "qal2_analyser.db")
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")  # optionnel

ALLOWED_EXTENSIONS = {"pdf"}

VERSION = "1.1.0"

# ─── Base de données ──────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS analyses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                dossier     TEXT,
                client      TEXT,
                labo        TEXT,
                installation TEXT,
                ville       TEXT,
                dates       TEXT,
                type_rapport TEXT DEFAULT 'QAL2',
                nb_essais   INTEGER,
                score_rapport REAL,
                score_ams   REAL,
                nb_valid    INTEGER DEFAULT 0,
                nb_invalid  INTEGER DEFAULT 0,
                nb_actions  INTEGER DEFAULT 0,
                cofrac_num  TEXT,
                notes       TEXT,
                parse_confidence INTEGER DEFAULT 0,
                data_json   TEXT NOT NULL,
                created_by  TEXT DEFAULT 'web'
            );

            CREATE TABLE IF NOT EXISTS norm_checks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at  TEXT NOT NULL,
                source      TEXT,
                result_json TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  TEXT
            );
        """)

# ─── Auth simple ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if ACCESS_PASSWORD and not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    if not ACCESS_PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if request.form.get("password") == ACCESS_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Mot de passe incorrect.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    search = request.args.get("q", "").strip()
    with get_db() as conn:
        if search:
            rows = conn.execute("""
                SELECT * FROM analyses
                WHERE client LIKE ? OR dossier LIKE ? OR labo LIKE ? OR ville LIKE ?
                ORDER BY created_at DESC LIMIT 50
            """, [f"%{search}%"] * 4).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM analyses ORDER BY created_at DESC LIMIT 30"
            ).fetchall()

        stats = conn.execute("""
            SELECT COUNT(*) as total,
                   AVG(score_rapport) as avg_rapport,
                   AVG(score_ams) as avg_ams,
                   SUM(nb_invalid) as total_invalid
            FROM analyses
        """).fetchone()

    return render_template("dashboard.html",
        analyses=rows, search=search, stats=stats,
        version=VERSION, has_claude=bool(CLAUDE_API_KEY)
    )

# ─── Upload & Analyse ─────────────────────────────────────────────────────────

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    # Récupérer le fichier PDF
    pdf_file = request.files.get("pdf")
    manual_data = request.form.get("manual_json")

    if not pdf_file and not manual_data:
        flash("Veuillez fournir un fichier PDF ou saisir des données manuellement.", "warning")
        return render_template("upload.html")

    parsed = {}

    if pdf_file and pdf_file.filename.lower().endswith(".pdf"):
        # Parsing PDF
        pdf_bytes = pdf_file.read()
        parsed = parse_pdf(pdf_bytes)
        if "error" in parsed and not parsed.get("pollutants"):
            flash(f"Erreur lors de la lecture du PDF : {parsed['error']}", "danger")
    elif manual_data:
        try:
            parsed = json.loads(manual_data)
        except Exception:
            flash("Données JSON invalides.", "danger")
            return render_template("upload.html")

    # Analyse normative
    analysis = analyze(parsed)

    # Enrichissement avec données formulaire éventuelles
    meta = analysis.get("meta", {})
    for field in ["client", "labo", "installation", "ville", "dates", "dossier"]:
        form_val = request.form.get(field, "").strip()
        if form_val:
            meta[field] = form_val

    # Sauvegarde en base
    analysis_id = _save_analysis(analysis, parsed)

    flash(f"Analyse enregistrée avec succès (ID #{analysis_id}).", "success")
    return redirect(url_for("show_analysis", analysis_id=analysis_id))


def _save_analysis(analysis: dict, parsed: dict) -> int:
    meta = analysis.get("meta", {})
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO analyses
            (created_at, dossier, client, labo, installation, ville, dates,
             type_rapport, nb_essais, score_rapport, score_ams,
             nb_valid, nb_invalid, nb_actions, cofrac_num,
             parse_confidence, data_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            datetime.now().isoformat(),
            meta.get("dossier"),
            meta.get("client"),
            meta.get("labo"),
            meta.get("installation"),
            meta.get("ville"),
            meta.get("dates"),
            meta.get("type_rapport", "QAL2"),
            meta.get("nb_essais"),
            analysis.get("score_rapport"),
            analysis.get("score_ams"),
            analysis.get("nb_valid", 0),
            analysis.get("nb_invalid", 0),
            analysis.get("nb_actions", 0),
            meta.get("labo_cofrac"),
            parsed.get("parse_confidence", 0),
            json.dumps(analysis, ensure_ascii=False),
        ])
        return cur.lastrowid

# ─── Affichage analyse ────────────────────────────────────────────────────────

@app.route("/analyse/<int:analysis_id>")
@login_required
def show_analysis(analysis_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id=?", [analysis_id]).fetchone()
    if not row:
        abort(404)
    analysis = json.loads(row["data_json"])
    return render_template("result.html",
        row=row, analysis=analysis,
        has_claude=bool(CLAUDE_API_KEY),
        version=VERSION
    )

@app.route("/analyse/<int:analysis_id>/delete", methods=["POST"])
@login_required
def delete_analysis(analysis_id):
    with get_db() as conn:
        conn.execute("DELETE FROM analyses WHERE id=?", [analysis_id])
    flash("Analyse supprimée.", "info")
    return redirect(url_for("dashboard"))

@app.route("/analyse/<int:analysis_id>/notes", methods=["POST"])
@login_required
def update_notes(analysis_id):
    notes = request.form.get("notes", "")
    with get_db() as conn:
        conn.execute("UPDATE analyses SET notes=? WHERE id=?", [notes, analysis_id])
    return jsonify({"ok": True})

# ─── Export HTML ──────────────────────────────────────────────────────────────

@app.route("/analyse/<int:analysis_id>/export")
@login_required
def export_html(analysis_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id=?", [analysis_id]).fetchone()
    if not row:
        abort(404)
    analysis = json.loads(row["data_json"])
    html = render_template("result.html",
        row=row, analysis=analysis,
        has_claude=False, version=VERSION,
        export_mode=True
    )
    return send_file(
        io.BytesIO(html.encode("utf-8")),
        mimetype="text/html",
        as_attachment=True,
        download_name=f"QAL2_{row['dossier'] or analysis_id}_{datetime.now().strftime('%Y%m%d')}.html"
    )

# ─── API Claude ───────────────────────────────────────────────────────────────

@app.route("/api/claude-analyse/<int:analysis_id>", methods=["POST"])
@login_required
def api_claude(analysis_id):
    if not CLAUDE_API_KEY:
        return jsonify({"error": "Clé API Claude non configurée (variable ANTHROPIC_API_KEY)"}), 400

    with get_db() as conn:
        row = conn.execute("SELECT data_json FROM analyses WHERE id=?", [analysis_id]).fetchone()
    if not row:
        return jsonify({"error": "Analyse non trouvée"}), 404

    analysis = json.loads(row["data_json"])
    meta = analysis.get("meta", {})
    channels = analysis.get("ams_channels", [])

    # Construire le prompt
    summary_lines = []
    for ch in channels:
        summary_lines.append(
            f"- {ch.get('name','?').upper()} : a={ch.get('a','?')}, b={ch.get('b','?')}, "
            f"R²={ch.get('r2','?')}, Sd={ch.get('sd','?')}, seuil={ch.get('kv_threshold','?')}, "
            f"résultat={'VALIDE' if ch.get('valid') else 'INVALIDE'}"
        )

    prompt = f"""Tu es un expert QAL2/SMEA selon NF EN 14181 et XP X43-132, au service d'ENVEA.

Voici les résultats de la campagne QAL2 pour {meta.get('client','?')} ({meta.get('installation','?')} - {meta.get('ville','?')}),
rapport {meta.get('dossier','?')}, réalisé par {meta.get('labo','?')} du {meta.get('dates','?')}.

Résultats par canal :
{chr(10).join(summary_lines)}

Score rapport qualité : {analysis.get('score_rapport','-')}/5
Score AMS : {analysis.get('score_ams','-')}/5
Canaux valides : {analysis.get('nb_valid',0)} | Non conformes : {analysis.get('nb_invalid',0)}

En tant qu'expert ENVEA, fournis une analyse narrative professionnelle (5-7 phrases) incluant :
1. Un résumé de la situation globale
2. Les points les plus critiques à traiter en priorité
3. Des recommandations concrètes pour l'exploitant et le fournisseur des AMS
4. L'impact sur la conformité réglementaire (surveillance continue ICPE)

Réponds en français, de manière directe et orientée terrain. Maximum 300 mots."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        ai_text = message.content[0].text

        # Sauvegarder dans l'analyse
        with get_db() as conn:
            row_data = conn.execute("SELECT data_json FROM analyses WHERE id=?", [analysis_id]).fetchone()
            data = json.loads(row_data["data_json"])
            data["claude_analysis"] = ai_text
            data["claude_analyzed_at"] = datetime.now().isoformat()
            conn.execute("UPDATE analyses SET data_json=? WHERE id=?",
                         [json.dumps(data, ensure_ascii=False), analysis_id])

        return jsonify({"ok": True, "analysis": ai_text})

    except Exception as e:
        return jsonify({"error": f"Erreur API Claude : {str(e)}"}), 500

# ─── API COFRAC ───────────────────────────────────────────────────────────────

@app.route("/api/cofrac-check", methods=["POST"])
@login_required
def api_cofrac():
    data = request.get_json() or {}
    result = check_cofrac_status(
        cofrac_number=data.get("cofrac_number"),
        labo_name=data.get("labo")
    )
    # Log
    with get_db() as conn:
        conn.execute("""
            INSERT INTO norm_checks (checked_at, source, result_json)
            VALUES (?,?,?)
        """, [datetime.now().isoformat(), "COFRAC", json.dumps(result)])
    return jsonify(result)

# ─── API Normes ───────────────────────────────────────────────────────────────

@app.route("/api/norms")
@login_required
def api_norms():
    updates = check_norm_updates()
    return jsonify({"norms": updates, "checked_at": datetime.now().isoformat()})

# ─── Page Normes ──────────────────────────────────────────────────────────────

@app.route("/norms")
@login_required
def norms_page():
    updates = check_norm_updates()
    with get_db() as conn:
        last_checks = conn.execute(
            "SELECT * FROM norm_checks ORDER BY checked_at DESC LIMIT 10"
        ).fetchall()
    return render_template("norms.html",
        norms=updates, last_checks=last_checks, version=VERSION
    )

# ─── Healthcheck Render ───────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": VERSION})

# ─── Error handlers ───────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page introuvable"), 404

@app.errorhandler(413)
def too_large(e):
    flash("Fichier trop volumineux (max 30 MB).", "danger")
    return redirect(url_for("upload"))

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message=str(e)), 500

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print(f"🚀 ENVEA QAL2 Analyser v{VERSION} → http://localhost:{port}")
    if CLAUDE_API_KEY:
        print("✅ Mode IA activé (Claude API configurée)")
    else:
        print("ℹ️  Mode règles normatives seul (pas de clé Claude API)")
    app.run(host="0.0.0.0", port=port, debug=debug)
