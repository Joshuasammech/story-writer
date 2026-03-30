"""
Story Writer Bot — Web Interface
Flask app with Google OAuth (soexcellence.com domain restriction) + SSE streaming.
"""

import io
import json
import os
import re
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

import anthropic
import requests
from authlib.integrations.flask_client import OAuth
from docx import Document
from docx.shared import Pt
from flask import (
    Flask, Response, redirect, request, send_file,
    session, stream_with_context, url_for,
)

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

ALLOWED_DOMAIN = "soexcellence.com"

# ── Google OAuth ───────────────────────────────────────────────────────────────

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ── Auth helpers ───────────────────────────────────────────────────────────────

AUTH_ENABLED = bool(os.environ.get("GOOGLE_CLIENT_ID"))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and not session.get("user"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a concise story writer. Your job is to read a report and write a short,
punchy story — 150 to 250 words maximum — built around three things:

1. THE EXPECTATION — What did the person expect or hope to achieve?
2. THE RESULT — What did they actually get? (better, worse, or different)
3. THE CONTRAST FRAME — How long does this kind of achievement conventionally
   take in the real world? Draw on your knowledge of industry norms and
   historical benchmarks to name a specific conventional timeframe, then
   contrast it with how long it actually took in this report.

FORMAT — always use exactly this structure, nothing more:

---
# [One punchy title]

**What they expected:** 1–2 sentences.

**What they got:** 1–2 sentences.

**The contrast frame:** State the conventional timeline for this type of
achievement (e.g. "Most people take X years to…"), then state the actual
timeline, then one sentence on why that gap matters.
---

RULES:
- Total output must be under 250 words.
- No bullet lists, no extra sections, no headers beyond the three above.
- Write in plain, direct English. No jargon or hype.
- If the report lacks enough detail for a section, write one honest sentence
  saying what information is missing.
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_doc_id(url_or_id: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    return match.group(1) if match else url_or_id.strip()


def fetch_google_doc(url_or_id: str) -> tuple[str, str]:
    doc_id = extract_doc_id(url_or_id)
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    resp = requests.get(export_url, timeout=20)
    if resp.status_code == 403:
        raise RuntimeError(
            'Access denied. Share the doc as "Anyone with the link can view".'
        )
    if resp.status_code == 404:
        raise RuntimeError("Document not found. Check that the URL is correct.")
    if not resp.ok:
        raise RuntimeError(f"Failed to fetch document (HTTP {resp.status_code}).")
    text = resp.text.strip()
    lines = text.splitlines()
    title = next((l.strip() for l in lines if l.strip()), "Untitled Document")
    return title, text


def sse(event: str, data: str) -> str:
    payload = json.dumps({"data": data})
    return f"event: {event}\ndata: {payload}\n\n"


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    with open(os.path.join(app.static_folder, "login.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    user_info = token.get("userinfo")
    email = user_info.get("email", "")

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        return (
            f'<h2 style="font-family:sans-serif;text-align:center;margin-top:80px">'
            f'Access denied. Only @{ALLOWED_DOMAIN} accounts are allowed.</h2>'
            f'<p style="text-align:center"><a href="/login">Back to login</a></p>'
        ), 403

    session["user"] = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ── App routes ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/me")
def me():
    user = session.get("user")
    if not user:
        return {}, 204
    return user


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    body = request.get_json(force=True)
    input_type = body.get("type", "url")
    content    = body.get("content", "").strip()

    if not content:
        return {"error": "No content provided."}, 400

    def stream():
        try:
            if input_type == "url":
                yield sse("status", "Fetching Google Doc…")
                try:
                    title, report_text = fetch_google_doc(content)
                except RuntimeError as e:
                    yield sse("error", str(e))
                    return
                if not report_text:
                    yield sse("error", "The document appears to be empty.")
                    return
                yield sse("status", f'Fetched: "{title}" ({len(report_text):,} chars)')
            else:
                title = "Pasted Report"
                report_text = content
                yield sse("status", f"Using pasted text ({len(report_text):,} chars)")

            yield sse("status", "Claude is thinking…")

            client = anthropic.Anthropic()
            user_message = (
                f"Document title: {title}\n\n"
                f"--- REPORT CONTENT START ---\n{report_text}\n--- REPORT CONTENT END ---\n\n"
                "Please write the story. Follow the format exactly."
            )

            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            ) as stream_obj:
                thinking_started = False
                writing_started  = False
                for event in stream_obj:
                    if event.type == "content_block_start":
                        if event.content_block.type == "thinking" and not thinking_started:
                            thinking_started = True
                            yield sse("thinking_start", "")
                        elif event.content_block.type == "text" and not writing_started:
                            writing_started = True
                            yield sse("writing_start", "")
                    elif event.type == "content_block_delta":
                        if event.delta.type == "thinking_delta":
                            yield sse("thinking", event.delta.thinking)
                        elif event.delta.type == "text_delta":
                            yield sse("token", event.delta.text)

            yield sse("done", "")

        except Exception as e:
            yield sse("error", str(e))

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/docx", methods=["POST"])
@login_required
def download_docx():
    body    = request.get_json(force=True)
    content = body.get("content", "").strip()
    if not content:
        return {"error": "No content."}, 400

    doc   = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for line in content.splitlines():
        line = line.strip()
        if not line or line == "---":
            doc.add_paragraph()
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("**") and ":**" in line:
            label, _, rest = line.partition(":**")
            p = doc.add_paragraph()
            p.add_run(label.lstrip("*") + ": ").bold = True
            p.add_run(rest.strip())
        else:
            doc.add_paragraph(line)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name="story.docx",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
