import os
import random
import sqlite3
from datetime import datetime
from flask import Flask, g, render_template, request, redirect, url_for, flash, session

APP_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH          = os.path.join(APP_DIR, "tipps.db")
ADMIN_PASSWORT   = os.environ.get("ADMIN_PASSWORT",   "admin123")
MANAGER_PASSWORT = os.environ.get("MANAGER_PASSWORT", "manager")

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "wm-tipp-geheim")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS spiele (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            team_heim TEXT NOT NULL,
            team_gast TEXT NOT NULL,
            anstoss   TEXT NOT NULL,
            tore_heim INTEGER,
            tore_gast INTEGER
        );
        CREATE TABLE IF NOT EXISTS klassen (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS tipps (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            spiel_id  INTEGER NOT NULL,
            spieler   TEXT NOT NULL,
            klasse    TEXT,
            tipp_heim INTEGER NOT NULL,
            tipp_gast INTEGER NOT NULL,
            abgegeben TEXT NOT NULL,
            UNIQUE(spiel_id, spieler),
            FOREIGN KEY(spiel_id) REFERENCES spiele(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS admin_logins (
            name          TEXT PRIMARY KEY,
            rolle         TEXT NOT NULL,
            gesperrt      INTEGER NOT NULL DEFAULT 0,
            letzter_login TEXT
        );
        CREATE TABLE IF NOT EXISTS spieler (
            name   TEXT PRIMARY KEY,
            klasse TEXT,
            pin    TEXT NOT NULL UNIQUE
        );
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tipps)").fetchall()]
    if "klasse" not in cols:
        conn.execute("ALTER TABLE tipps ADD COLUMN klasse TEXT")
    conn.commit()
    conn.close()


# ── PIN helper ────────────────────────────────────────────────────────────────

def generiere_pin(db):
    """Generate a unique 6-digit PIN."""
    for _ in range(100):
        pin = str(random.randint(100000, 999999))
        if not db.execute("SELECT 1 FROM spieler WHERE pin = ?", (pin,)).fetchone():
            return pin
    raise RuntimeError("Kein freier PIN gefunden.")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_rolle():
    return session.get("rolle")


def require_admin():
    if get_rolle() == "admin":
        return True
    flash("Diese Seite ist nur für den Haupt-Admin zugänglich.", "error")
    return False


def require_staff():
    if get_rolle() in ("admin", "manager"):
        return True
    flash("Bitte melde dich an.", "error")
    return False


# ── Scoring ───────────────────────────────────────────────────────────────────

def punkte_fuer_tipp(tipp_heim, tipp_gast, tore_heim, tore_gast):
    if tore_heim is None or tore_gast is None:
        return 0
    if tipp_heim == tore_heim and tipp_gast == tore_gast:
        return 1
    return 0


def parse_anstoss(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ── Tipp-Speicher-Hilfsfunktion ───────────────────────────────────────────────

def speichere_tipps(db, spiele, spieler_name, klasse):
    """Save submitted tips for `spieler_name`. Returns count saved."""
    jetzt = datetime.now().isoformat(timespec="seconds")
    gespeichert = 0
    for s in spiele:
        h  = request.form.get(f"tipp_heim_{s['id']}")
        g_ = request.form.get(f"tipp_gast_{s['id']}")
        if not h or not g_:
            continue
        try:
            th, tg = int(h), int(g_)
        except ValueError:
            continue
        if not (0 <= th <= 99 and 0 <= tg <= 99):
            continue
        db.execute(
            """
            INSERT INTO tipps (spiel_id, spieler, klasse, tipp_heim, tipp_gast, abgegeben)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(spiel_id, spieler) DO UPDATE SET
                klasse=excluded.klasse, tipp_heim=excluded.tipp_heim,
                tipp_gast=excluded.tipp_gast, abgegeben=excluded.abgegeben
            """,
            (s["id"], spieler_name, klasse or None, th, tg, jetzt),
        )
        gespeichert += 1
    return gespeichert


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    db      = get_db()
    spiele  = db.execute("SELECT * FROM spiele ORDER BY id ASC").fetchall()
    klassen = db.execute("SELECT * FROM klassen ORDER BY name ASC").fetchall()

    spieler_name   = session.get("spieler_name")
    spieler_klasse = session.get("spieler_klasse")

    offene  = [s for s in spiele if s["tore_heim"] is None]
    beendet = [s for s in spiele if s["tore_heim"] is not None]

    # Welche offenen Spiele hat dieser Spieler noch NICHT getippt?
    if spieler_name:
        getippt_ids = {
            r["spiel_id"] for r in db.execute(
                "SELECT spiel_id FROM tipps WHERE spieler = ?", (spieler_name,)
            ).fetchall()
        }
        ungetippt = [s for s in offene if s["id"] not in getippt_ids]
    else:
        ungetippt = offene

    if request.method == "POST":
        aktion = request.form.get("aktion", "neu")

        # ── Erstmalige Registrierung ──────────────────────────────────────────
        if aktion == "neu":
            vorname  = (request.form.get("vorname")  or "").strip()
            nachname = (request.form.get("nachname") or "").strip()
            klasse   = (request.form.get("klasse")   or "").strip()
            name     = " ".join(p for p in (vorname, nachname) if p)

            if not vorname or not nachname:
                flash("Bitte gib Vor- und Nachname ein.", "error")
                return redirect(url_for("index"))
            if klassen and not klasse:
                flash("Bitte wähle deine Klasse aus.", "error")
                return redirect(url_for("index"))

            # Name bereits vergeben?
            if db.execute("SELECT 1 FROM spieler WHERE name = ?", (name,)).fetchone():
                flash("Dieser Name ist bereits vergeben – bitte melde dich mit deiner PIN an.", "error")
                return redirect(url_for("index"))

            pin = generiere_pin(db)
            db.execute("INSERT INTO spieler (name, klasse, pin) VALUES (?, ?, ?)", (name, klasse or None, pin))
            gespeichert = speichere_tipps(db, offene, name, klasse)
            db.commit()

            session["spieler_name"]   = name
            session["spieler_klasse"] = klasse
            session["spieler_pin"]    = pin
            return redirect(url_for("meine_pin"))

        # ── Nachtipp (eingeloggt, neue Spiele) ───────────────────────────────
        elif aktion == "nachtipp":
            if not spieler_name:
                flash("Bitte erst anmelden.", "error")
                return redirect(url_for("index"))
            gespeichert = speichere_tipps(db, ungetippt, spieler_name, spieler_klasse)
            db.commit()
            if gespeichert:
                flash(f"{gespeichert} Tipp(s) gespeichert!", "ok")
            else:
                flash("Keine Tipps gespeichert.", "error")
            return redirect(url_for("index"))

    return render_template(
        "index.html",
        spiele=spiele,
        klassen=klassen,
        offene=offene,
        ungetippt=ungetippt,
        beendet=beendet,
        spieler_name=spieler_name,
        spieler_klasse=spieler_klasse,
    )


@app.route("/pin-login", methods=["POST"])
def pin_login():
    pin = (request.form.get("pin") or "").strip()
    if not pin:
        flash("Bitte gib deine PIN ein.", "error")
        return redirect(url_for("index"))
    db = get_db()
    sp = db.execute("SELECT * FROM spieler WHERE pin = ?", (pin,)).fetchone()
    if not sp:
        flash("Ungültige PIN. Bitte nochmal versuchen.", "error")
        return redirect(url_for("index"))
    session["spieler_name"]   = sp["name"]
    session["spieler_klasse"] = sp["klasse"]
    session["spieler_pin"]    = sp["pin"]
    flash(f"Willkommen zurück, {sp['name']}!", "ok")
    return redirect(url_for("index"))


@app.route("/abmelden", methods=["POST"])
def abmelden():
    session.pop("spieler_name",   None)
    session.pop("spieler_klasse", None)
    session.pop("spieler_pin",    None)
    return redirect(url_for("index"))


@app.route("/meine-pin")
def meine_pin():
    name = session.get("spieler_name")
    pin  = session.get("spieler_pin")
    if not name or not pin:
        return redirect(url_for("index"))
    return render_template("meine_pin.html", spieler_name=name, pin=pin)


@app.route("/klassen-rangliste")
def klassen_rangliste():
    db = get_db()
    klassen = db.execute("SELECT name FROM klassen ORDER BY name ASC").fetchall()
    rows = db.execute(
        """
        SELECT t.spieler, t.klasse, t.tipp_heim, t.tipp_gast, s.tore_heim, s.tore_gast
        FROM tipps t JOIN spiele s ON s.id = t.spiel_id
        WHERE t.klasse IS NOT NULL
        """
    ).fetchall()

    kp, km = {}, {}
    for r in rows:
        k = r["klasse"]
        kp[k] = kp.get(k, 0) + punkte_fuer_tipp(
            r["tipp_heim"], r["tipp_gast"], r["tore_heim"], r["tore_gast"]
        )
        km.setdefault(k, set()).add(r["spieler"])

    alle = set(list(kp.keys()) + [kl["name"] for kl in klassen])
    ergebnis = sorted(
        [{"klasse": k, "punkte": kp.get(k, 0), "mitglieder": len(km.get(k, set()))} for k in alle],
        key=lambda x: (-x["punkte"], x["klasse"].lower()),
    )
    return render_template("klassen_rangliste.html", ergebnis=ergebnis)


# ── Rangliste (staff only) ────────────────────────────────────────────────────

@app.route("/rangliste")
def rangliste():
    if not require_staff():
        return redirect(url_for("admin"))
    db = get_db()
    rows = db.execute(
        """
        SELECT t.spieler, t.klasse, t.tipp_heim, t.tipp_gast,
               s.tore_heim, s.tore_gast
        FROM tipps t JOIN spiele s ON s.id = t.spiel_id
        """
    ).fetchall()

    stats = {}
    for r in rows:
        e = stats.setdefault(
            r["spieler"],
            {"spieler": r["spieler"], "klasse": r["klasse"] or "–",
             "punkte": 0, "tipps": 0, "treffer": 0},
        )
        e["tipps"] += 1
        if r["tore_heim"] is not None:
            p = punkte_fuer_tipp(r["tipp_heim"], r["tipp_gast"], r["tore_heim"], r["tore_gast"])
            e["punkte"] += p
            if p:
                e["treffer"] += 1

    data = sorted(
        stats.values(),
        key=lambda x: (-x["punkte"], -x["treffer"], x["spieler"].lower()),
    )
    return render_template("rangliste.html", rangliste=data)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not get_rolle():
        if request.method == "POST" and "passwort" in request.form:
            name     = (request.form.get("admin_name") or "").strip()
            passwort = request.form.get("passwort", "")
            if not name:
                flash("Bitte gib deinen Namen ein.", "error")
                return render_template("admin_login.html")

            if passwort == ADMIN_PASSWORT:
                rolle = "admin"
            elif passwort == MANAGER_PASSWORT:
                rolle = "manager"
            else:
                flash("Falsches Passwort.", "error")
                return render_template("admin_login.html")

            db = get_db()
            eintrag = db.execute("SELECT * FROM admin_logins WHERE name = ?", (name,)).fetchone()
            if eintrag and eintrag["gesperrt"]:
                flash(f'Das Konto "{name}" wurde gesperrt.', "error")
                return render_template("admin_login.html")

            db.execute(
                """
                INSERT INTO admin_logins (name, rolle, letzter_login)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    rolle=excluded.rolle, letzter_login=excluded.letzter_login
                """,
                (name, rolle, datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            session["rolle"]      = rolle
            session["admin_name"] = name
            return redirect(url_for("admin"))
        return render_template("admin_login.html")

    db    = get_db()
    rolle = get_rolle()

    if request.method == "POST":
        aktion = request.form.get("aktion")

        if aktion in ("spieler_loeschen", "konto_sperren", "konto_entsperren", "admin_entfernen"):
            if not require_admin():
                return redirect(url_for("admin"))

            if aktion == "spieler_loeschen":
                sp = (request.form.get("spieler") or "").strip()
                if sp:
                    db.execute("DELETE FROM tipps WHERE spieler = ?", (sp,))
                    db.execute("DELETE FROM spieler WHERE name = ?", (sp,))
                    db.commit()
                    flash(f'Konto "{sp}" gelöscht.', "ok")

            elif aktion == "konto_sperren":
                name = (request.form.get("login_name") or "").strip()
                if name == session.get("admin_name"):
                    flash("Du kannst dich nicht selbst sperren.", "error")
                elif name:
                    db.execute("UPDATE admin_logins SET gesperrt=1 WHERE name=?", (name,))
                    db.commit()
                    flash(f'"{name}" wurde gesperrt.', "ok")

            elif aktion == "konto_entsperren":
                name = (request.form.get("login_name") or "").strip()
                if name:
                    db.execute("UPDATE admin_logins SET gesperrt=0 WHERE name=?", (name,))
                    db.commit()
                    flash(f'"{name}" wurde entsperrt.', "ok")

            elif aktion == "admin_entfernen":
                name = (request.form.get("login_name") or "").strip()
                if name == session.get("admin_name"):
                    flash("Du kannst dich nicht selbst entfernen.", "error")
                elif name:
                    db.execute("DELETE FROM admin_logins WHERE name=?", (name,))
                    db.commit()
                    flash(f'"{name}" wurde entfernt.', "ok")

        elif aktion == "spiel_anlegen":
            th = (request.form.get("team_heim") or "").strip()
            tg = (request.form.get("team_gast") or "").strip()
            if not th or not tg:
                flash("Bitte beide Teams angeben.", "error")
            else:
                db.execute(
                    "INSERT INTO spiele (team_heim, team_gast, anstoss) VALUES (?,?,?)",
                    (th, tg, datetime.now().isoformat(timespec="seconds")),
                )
                db.commit()
                flash(f"Spiel angelegt: {th} – {tg}", "ok")

        elif aktion == "ergebnis_eintragen":
            try:
                sid = int(request.form.get("spiel_id", ""))
                th  = int(request.form.get("tore_heim", ""))
                tg  = int(request.form.get("tore_gast", ""))
            except ValueError:
                flash("Ungültige Eingabe.", "error")
            else:
                db.execute("UPDATE spiele SET tore_heim=?, tore_gast=? WHERE id=?", (th, tg, sid))
                db.commit()
                flash("Ergebnis gespeichert.", "ok")

        elif aktion == "spiel_loeschen":
            try:
                sid = int(request.form.get("spiel_id", ""))
            except ValueError:
                flash("Ungültige ID.", "error")
            else:
                db.execute("DELETE FROM spiele WHERE id=?", (sid,))
                db.commit()
                flash("Spiel gelöscht.", "ok")

        elif aktion == "klasse_anlegen":
            name = (request.form.get("klasse_name") or "").strip()
            if not name:
                flash("Bitte einen Klassennamen eingeben.", "error")
            else:
                try:
                    db.execute("INSERT INTO klassen (name) VALUES (?)", (name,))
                    db.commit()
                    flash(f'Klasse "{name}" angelegt.', "ok")
                except sqlite3.IntegrityError:
                    flash(f'Klasse "{name}" existiert bereits.', "error")

        elif aktion == "klasse_loeschen":
            try:
                kid = int(request.form.get("klasse_id", ""))
            except ValueError:
                flash("Ungültige Klassen-ID.", "error")
            else:
                db.execute("DELETE FROM klassen WHERE id=?", (kid,))
                db.commit()
                flash("Klasse gelöscht.", "ok")

        return redirect(url_for("admin"))

    # ── Render ──
    spiele  = db.execute("SELECT * FROM spiele ORDER BY id ASC").fetchall()
    klassen = db.execute("SELECT * FROM klassen ORDER BY name ASC").fetchall()

    spieler_liste = alle_tipps = admin_liste = None

    if rolle == "admin":
        spieler_rows = db.execute(
            """
            SELECT t.spieler, t.klasse, COUNT(*) AS anzahl, MAX(t.abgegeben) AS letzter,
                   s.pin
            FROM tipps t
            LEFT JOIN spieler s ON s.name = t.spieler
            GROUP BY t.spieler ORDER BY letzter DESC
            """
        ).fetchall()
        punkte_rows = db.execute(
            """
            SELECT t.spieler, t.tipp_heim, t.tipp_gast, s.tore_heim, s.tore_gast
            FROM tipps t JOIN spiele s ON s.id = t.spiel_id
            """
        ).fetchall()
        pp = {}
        for r in punkte_rows:
            pp[r["spieler"]] = pp.get(r["spieler"], 0) + punkte_fuer_tipp(
                r["tipp_heim"], r["tipp_gast"], r["tore_heim"], r["tore_gast"]
            )
        spieler_liste = [dict(sp) | {"punkte": pp.get(sp["spieler"], 0)} for sp in spieler_rows]

        alle_tipps = db.execute(
            """
            SELECT t.spieler, t.klasse, t.tipp_heim, t.tipp_gast, t.abgegeben,
                   s.team_heim, s.team_gast, s.tore_heim, s.tore_gast
            FROM tipps t JOIN spiele s ON s.id = t.spiel_id
            ORDER BY t.abgegeben DESC
            """
        ).fetchall()

        admin_liste = db.execute(
            "SELECT * FROM admin_logins ORDER BY letzter_login DESC"
        ).fetchall()

    return render_template(
        "admin.html",
        spiele=spiele,
        klassen=klassen,
        spieler_liste=spieler_liste,
        alle_tipps=alle_tipps,
        admin_liste=admin_liste,
        rolle=rolle,
        admin_name=session.get("admin_name"),
    )


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("rolle", None)
    session.pop("admin_name", None)
    flash("Abgemeldet.", "ok")
    return redirect(url_for("index"))


# ── Template filter ───────────────────────────────────────────────────────────

@app.template_filter("datum")
def datum_filter(s):
    dt = parse_anstoss(s) if isinstance(s, str) else s
    if dt is None:
        return s or "–"
    return dt.strftime("%a, %d.%m.%Y %H:%M")


