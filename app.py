"""
WhatsApp RSVP System - Main Application
-----------------------------------------
Run: python app.py
Webhook URL for Twilio: http://<your-ngrok-url>/sms/reply

Sandbox:    TWILIO_FROM_NUMBER=+14155238886  (Twilio Sandbox)
Production: TWILIO_FROM_NUMBER=+1xxxxxxxxxx  (approved WA Business number)
"""

import os
import logging
from flask import Flask, request, render_template, redirect, url_for, flash
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from database import init_db, get_db
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("rsvp.log", encoding="utf-8"),  # שמירה לקובץ
        logging.StreamHandler(),                              # גם לטרמינל
    ],
)
log = logging.getLogger(__name__)

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
        log.info(f"GUEST_ADDED | name={name} | phone={phone}")
        flash(f"✅ {name} נוסף/ה בהצלחה", "success")
    except Exception as e:
        flash(f"שגיאה: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/guests/delete/<int:guest_id>", methods=["POST"])
def delete_guest(guest_id):
    db = get_db()
    db.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
    db.commit()
    log.info(f"GUEST_DELETED | id={guest_id}")
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

            msg = twilio_client.messages.create(**msg_params)
            log.info(f"MSG_SENT | to={guest['name']} | phone={guest['phone']} | status={msg.status} | image={'yes' if image_url else 'no'}")
            print(f"[SENT] {guest['name']} | status: {msg.status} | image: {image_url or 'none'}")
            db.execute(
                "UPDATE guests SET last_sent = datetime('now') WHERE id = ?",
                (guest["id"],),
            )
            sent_count += 1
        except Exception as e:
            log.error(f"MSG_FAILED | to={guest['name']} | phone={guest['phone']} | error={e}")

    db.commit()
    log.info(f"SEND_BATCH_DONE | sent={sent_count} | target={send_to} | image={'yes' if image_url else 'no'}")
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
        log.warning(f"UNKNOWN_NUMBER | from={from_number}")
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
        log.info(f"GUEST_COUNT | name={guest['name']} | phone={from_number} | count={count}")
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
        log.info(f"RSVP_YES | name={guest['name']} | phone={from_number}")
        resp.message(f"תודה {guest['name']}! 😊\nכמה אורחים יגיעו? (ענה/י מספר בין 1 ל-{MAX_GUESTS})")

    elif body_lower in NO_WORDS:
        db.execute(
            """UPDATE guests
               SET rsvp = 'no', awaiting_count = 0, rsvp_time = datetime('now')
               WHERE id = ?""",
            (guest["id"],),
        )
        db.commit()
        log.info(f"RSVP_NO | name={guest['name']} | phone={from_number}")
        resp.message(f"תודה {guest['name']}, קיבלנו את עדכונך. נשמח לראותך בפעם אחרת 🙏")

    else:
        log.warning(f"INVALID_REPLY | name={guest['name']} | phone={from_number} | body={body}")
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
    log.info(f"RSVP_RESET | id={guest_id}")
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
    log.info("-" * 90)
    log.info("APP_START | server starting on port 5000")
    app.run(debug=True, port=5000)
