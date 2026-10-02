import os
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
from reword import reword_feedback

# A draft is held ONLY in the submitter's signed session cookie until they
# approve it — see the big comment on the /submit route below for why, and
# db.py's module docstring for what that guarantees. Flask's default
# session cookie has a real size ceiling (~4KB once signed), so feedback
# text is capped comfortably under that.
MAX_FEEDBACK_CHARS = 1200


def format_dt(value):
    if not value:
        return ""
    dt = db.parse_dt(value)
    return dt.strftime("%b %-d, %Y")


def describe_window_from_now(release_at_iso: str) -> str:
    delta = db.parse_dt(release_at_iso) - db.now_dt()
    hours = max(1, round(delta.total_seconds() / 3600))
    if hours <= 36:
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    days = round(hours / 24)
    return f"about {days} day{'s' if days != 1 else ''}"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.jinja_env.filters["fmtdate"] = format_dt
    db.init_db()
    register_routes(app)
    return app


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in first.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def register_routes(app):
    @app.before_request
    def load_logged_in_user():
        user_id = session.get("user_id")
        g.user = db.get_user(user_id) if user_id else None

    @app.context_processor
    def inject_user():
        unread_count = 0
        if g.get("user"):
            received = db.list_received_feedback(g.user["id"])
            unread_count = sum(1 for f in received if not f["read_at"])
        return {"current_user": g.get("user"), "unread_count": unread_count}

    # --- auth ---------------------------------------------------------

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            error = None
            if not name:
                error = "Name is required."
            elif not email or "@" not in email:
                error = "A valid email is required."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif db.get_user_by_email(email):
                error = "An account with that email already exists."

            if error:
                flash(error, "error")
                return render_template("signup.html", name=name, email=email)

            user_id = db.create_user(name, email, generate_password_hash(password))
            session.clear()
            session["user_id"] = user_id
            flash("Welcome! Your account is ready.", "success")
            return redirect(url_for("dashboard"))

        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = db.get_user_by_email(email)
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Incorrect email or password.", "error")
                return render_template("login.html", email=email)
            session.clear()
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/faq")
    def faq():
        return render_template("faq.html")

    # --- dashboard / inbox ----------------------------------------------

    @app.route("/")
    def index():
        return redirect(url_for("dashboard") if g.user else url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        received = db.list_received_feedback(g.user["id"])
        unread_count = sum(1 for f in received if not f["read_at"])
        return render_template("dashboard.html", received=received, unread_count=unread_count)

    @app.route("/feedback/<int:feedback_id>/mark-read", methods=["POST"])
    @login_required
    def mark_read(feedback_id):
        fb = db.get_feedback(feedback_id)
        if fb is None or fb["recipient_id"] != g.user["id"]:
            abort(403)
        if not fb["read_at"]:
            db.mark_feedback_read(feedback_id)
        return ("", 204)

    # --- submitting feedback ----------------------------------------------
    #
    # TRUE anonymity, including against anyone with full database access,
    # requires that the database never stores who wrote a piece of
    # feedback — not while it's a draft, not after it's sent. But the
    # submitter still needs to review, regenerate, and hand-edit the
    # reworded version before approving it, and that only works if
    # *something* remembers the draft between requests.
    #
    # The fix: the draft lives only in the submitter's own signed session
    # cookie (Flask's default session backend) — never in a SQL row. The
    # server can read it back on each request because the browser sends it
    # back, but nothing is written to disk until the submitter hits
    # "Approve & send", at which point db.create_feedback_anonymous() is
    # called — a function that structurally has no parameter for a
    # submitter identity. Discard, or just closing the browser without
    # approving, leaves no trace anywhere. This is also why there's no
    # "my submissions" history: keeping one would mean storing exactly the
    # link this whole design exists to avoid.

    @app.route("/submit", methods=["GET", "POST"])
    @login_required
    def submit():
        recipients = db.list_other_users(g.user["id"])

        if request.method == "POST":
            recipient_id = request.form.get("recipient_id", type=int)
            category = request.form.get("category", db.CATEGORY_SUGGESTION)
            text = request.form.get("text", "").strip()

            error = None
            recipient = db.get_user(recipient_id) if recipient_id else None
            if recipient is None or recipient["id"] == g.user["id"]:
                error = "Please choose who this feedback is for."
            elif category not in (db.CATEGORY_COMPLAINT, db.CATEGORY_SUGGESTION):
                error = "Please choose a valid category."
            elif len(text) < 10:
                error = "Please write a bit more detail (at least 10 characters)."
            elif len(text) > MAX_FEEDBACK_CHARS:
                error = (
                    f"Please keep it under {MAX_FEEDBACK_CHARS} characters "
                    "(split longer feedback into more than one submission)."
                )

            if error:
                flash(error, "error")
                return render_template(
                    "submit.html", recipients=recipients, category=category, text=text,
                    recipient_id=recipient_id, max_chars=MAX_FEEDBACK_CHARS,
                )

            reworded, source = reword_feedback(text, category)
            session["draft"] = {
                "recipient_id": recipient["id"],
                "recipient_name": recipient["name"],
                "category": category,
                "original_text": text,
                "reworded_text": reworded,
                "reword_source": source,
                "edited": False,
            }
            return redirect(url_for("review"))

        return render_template("submit.html", recipients=recipients, max_chars=MAX_FEEDBACK_CHARS)

    def _draft_or_redirect():
        draft = session.get("draft")
        if not draft:
            flash("That draft is gone (drafts only live for your current session).", "error")
            return None
        return draft

    @app.route("/review", methods=["GET"])
    @login_required
    def review():
        draft = _draft_or_redirect()
        if draft is None:
            return redirect(url_for("submit"))
        return render_template("review.html", draft=draft)

    @app.route("/review/regenerate", methods=["POST"])
    @login_required
    def regenerate():
        draft = _draft_or_redirect()
        if draft is None:
            return redirect(url_for("submit"))
        reworded, source = reword_feedback(draft["original_text"], draft["category"])
        draft["reworded_text"] = reworded
        draft["reword_source"] = source
        draft["edited"] = False
        session["draft"] = draft
        flash("Generated a new version.", "success")
        return redirect(url_for("review"))

    @app.route("/review/edit", methods=["POST"])
    @login_required
    def edit_reworded():
        draft = _draft_or_redirect()
        if draft is None:
            return redirect(url_for("submit"))
        new_text = request.form.get("reworded_text", "").strip()
        if len(new_text) < 5:
            flash("Edited text is too short.", "error")
            return redirect(url_for("review"))
        draft["reworded_text"] = new_text
        draft["edited"] = True
        session["draft"] = draft
        flash("Saved your edits.", "success")
        return redirect(url_for("review"))

    @app.route("/review/approve", methods=["POST"])
    @login_required
    def approve():
        draft = _draft_or_redirect()
        if draft is None:
            return redirect(url_for("submit"))

        _, release_at = db.create_feedback_anonymous(
            recipient_id=draft["recipient_id"],
            category=draft["category"],
            reworded_text=draft["reworded_text"],
            edited_by_submitter=draft["edited"],
            reword_source=draft["reword_source"],
        )
        recipient_name = draft["recipient_name"]
        session.pop("draft", None)

        flash(
            f"Sent anonymously to {recipient_name}. It'll appear in their inbox in "
            f"{describe_window_from_now(release_at)}, not immediately — that's "
            "intentional, so arrival timing doesn't give you away. Nothing about "
            "who sent it was stored, including here: there's no record of this "
            "submission left in your account.",
            "success",
        )
        return redirect(url_for("dashboard"))

    @app.route("/review/discard", methods=["POST"])
    @login_required
    def discard():
        session.pop("draft", None)
        return redirect(url_for("submit"))


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
