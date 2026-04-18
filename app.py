"""
WhatsApp RSVP System - Main Application
-----------------------------------------
Run: python app.py
Webhook URL for Twilio: http://<your-ngrok-url>/sms/reply

Sandbox:    TWILIO_FROM_NUMBER=+14155238886  (Twilio Sandbox)
Production: TWILIO_FROM_NUMBER=+1xxxxxxxxxx  (approved WA Business number)
"""

import os
from flask import Flask, request, render_template, redirect, url_for, flash
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from database import init_db, get_db
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# ── Twilio credentials ────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]   # Sandbox: +14155238886

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# WhatsApp prefix helper
def wa(number: str) -> str:
    """Wrap a phone number with the whatsapp: prefix Twilio expects."""
    return f"whatsapp:{number}"


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    db = get_db()
    guests = db.execute(
        "SELECT * FROM guests ORDER BY name"
    ).fetchall()

    stats = {
        "total":        len(guests),
        "yes":          sum(1 for g in guests if g["rsvp"] == "yes"),
        "no":           sum(1 for g in guests if g["rsvp"] == "no"),
        "pending":      sum(1 for g in guests if g["rsvp"] is None),
        "total_guests": sum(g["guest_count"] or 0 for g in guests if g["rsvp"] == "yes"),
    }
    return render_template("dashboard.html", guests=guests, stats=stats)


# ── Manage guests ─────────────────────────────────────────────────────────────

@app.route("/guests/add", methods=["POST"])
def add_guest():
    name  = request.form["name"].strip()
    phone = request.form["phone"].strip()

    if not name or not phone:
        flash("שם ומספר טלפון הם שדות חובה", "error")
        return redirect(url_for("dashboard"))

    # Normalize Israeli number → E.164
    if phone.startswith("0"):
        phone = "+972" + phone[1:]

    db = get_db()
    try:
        db.execute(
            "INSERT INTO guests (name, phone) VALUES (?, ?)", (name, phone)
        )
        db.commit()
        flash(f"✅ {name} נוסף/ה בהצלחה", "success")
    except Exception as e:
        flash(f"שגיאה: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/guests/delete/<int:guest_id>", methods=["POST"])
def delete_guest(guest_id):
    db = get_db()
    db.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
    db.commit()
    flash("אורח/ת הוסר/ה", "success")
    return redirect(url_for("dashboard"))


# ── Send invitations ──────────────────────────────────────────────────────────

@app.route("/send", methods=["POST"])
def send_invitations():
    message_text = request.form.get("message_text", "").strip()
    send_to      = request.form.get("send_to", "all")
    image_url    = request.form.get("image_url", "").strip()

    if not message_text:
        flash("יש להזין טקסט להודעה", "error")
        return redirect(url_for("dashboard"))

    db = get_db()

    if send_to == "pending":
        guests = db.execute("SELECT * FROM guests WHERE rsvp IS NULL").fetchall()
    else:
        guests = db.execute("SELECT * FROM guests").fetchall()

    sent_count = 0
    for guest in guests:
        try:
            msg_params = dict(
                to=wa(guest["phone"]),
                from_=wa(TWILIO_FROM_NUMBER),
                body=message_text,
            )
            if image_url:
                msg_params["media_url"] = [image_url]

            twilio_client.messages.create(**msg_params)
            db.execute(
                "UPDATE guests SET last_sent = datetime('now') WHERE id = ?",
                (guest["id"],),
            )
            sent_count += 1
        except Exception as e:
            print(f"[ERROR] Failed to send to {guest['name']} ({guest['phone']}): {e}")

    db.commit()
    flash(f"💬 הודעות WhatsApp נשלחו ל-{sent_count} אנשים", "success")
    return redirect(url_for("dashboard"))


# ── Twilio webhook – incoming WhatsApp ───────────────────────────────────────

MAX_GUESTS = 20

@app.route("/sms/reply", methods=["POST"])
def sms_reply():
    raw_from = request.form.get("From", "").strip()
    # Twilio sends "whatsapp:+972501234567" – strip the prefix for DB lookup
    from_number = raw_from.replace("whatsapp:", "")
    body        = request.form.get("Body", "").strip()

    db = get_db()
    guest = db.execute(
        "SELECT * FROM guests WHERE phone = ?", (from_number,)
    ).fetchone()

    resp = MessagingResponse()

    if not guest:
        resp.message("מספר זה אינו רשום במערכת. פנה/י לבעל האירוע.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── Step 2: waiting for guest count ──────────────────────────────────────
    if guest["awaiting_count"]:
        try:
            count = int(body.strip())
            if count < 1 or count > MAX_GUESTS:
                raise ValueError
        except ValueError:
            resp.message(f"אנא ענה/י במספר בין 1 ל-{MAX_GUESTS}.")
            return str(resp), 200, {"Content-Type": "text/xml"}

        db.execute(
            """UPDATE guests
               SET guest_count = ?, awaiting_count = 0, rsvp_time = datetime('now')
               WHERE id = ?""",
            (count, guest["id"]),
        )
        db.commit()
        guests_word = "אורח" if count == 1 else "אורחים"
        resp.message(f"מעולה! רשמנו {count} {guests_word} על שמך. נתראה באירוע! 🎉")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── Step 1: yes / no ─────────────────────────────────────────────────────
    body_lower = body.lower()
    YES_WORDS = {"כן", "yes", "y", "אכן", "בטח", "בטוח"}
    NO_WORDS  = {"לא", "no",  "n"}

    if body_lower in YES_WORDS:
        # Mark yes, then ask for guest count
        db.execute(
            "UPDATE guests SET rsvp = 'yes', awaiting_count = 1 WHERE id = ?",
            (guest["id"],),
        )
        db.commit()
        resp.message(f"תודה {guest['name']}! 😊\nכמה אורחים יגיעו? (ענה/י מספר בין 1 ל-{MAX_GUESTS})")

    elif body_lower in NO_WORDS:
        db.execute(
            """UPDATE guests
               SET rsvp = 'no', awaiting_count = 0, rsvp_time = datetime('now')
               WHERE id = ?""",
            (guest["id"],),
        )
        db.commit()
        resp.message(f"תודה {guest['name']}, קיבלנו את עדכונך. נשמח לראותך בפעם אחרת 🙏")

    else:
        resp.message("אנא ענה/י *כן* אם תגיע/י, או *לא* אם לא תגיע/י.")

    return str(resp), 200, {"Content-Type": "text/xml"}


# ── Reset RSVP ────────────────────────────────────────────────────────────────

@app.route("/guests/reset/<int:guest_id>", methods=["POST"])
def reset_rsvp(guest_id):
    db = get_db()
    db.execute(
        "UPDATE guests SET rsvp = NULL, rsvp_time = NULL, guest_count = NULL, awaiting_count = 0 WHERE id = ?",
        (guest_id,),
    )
    db.commit()
    flash("תשובה אופסה", "success")
    return redirect(url_for("dashboard"))


# ── Export CSV ────────────────────────────────────────────────────────────────

@app.route("/export")
def export_csv():
    import csv
    import io
    from flask import Response

    db = get_db()
    guests = db.execute("SELECT name, phone, rsvp, guest_count, rsvp_time FROM guests ORDER BY name").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["שם", "טלפון", "RSVP", "מספר אורחים", "זמן תגובה"])
    for g in guests:
        rsvp_display = {"yes": "מגיע/ה", "no": "לא מגיע/ה"}.get(g["rsvp"], "ממתין/ה")
        writer.writerow([g["name"], g["phone"], rsvp_display, g["guest_count"] or "", g["rsvp_time"] or ""])

    output.seek(0)
    return Response(
        "\ufeff" + output.getvalue(),   # BOM for Excel Hebrew support
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=rsvp_list.csv"},
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
