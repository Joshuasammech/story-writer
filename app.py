"""
Story Writer Bot — Web Interface
Flask app with simple username/password login + SSE streaming.
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
from docx import Document
from docx.shared import Pt
from flask import (
    Flask, Response, redirect, request, send_file,
    session, stream_with_context, url_for,
)

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", "admin")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "changeme")

# ── Story prompt ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Write a punchy 150-200 word story from this report using ONLY this format:

---
# [One punchy title]

**The Impact:** What was achieved? What changed as a result? Include specific numbers or outcomes where available. 1-2 sentences.

**The Growth:** What did the person develop, learn, or transform within themselves to get here? 1-2 sentences.

**Years Saved:** How long does this kind of achievement conventionally take in the real world? State the typical timeframe (e.g. "Most people take X years to…"), then state how long it actually took, then one sentence on why that gap matters.
---

No extra sections. No bullet lists. Plain, direct English.\
"""

# ── Auth helpers ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated



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

@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password."
    with open(os.path.join(app.static_folder, "login.html"), encoding="utf-8") as f:
        html = f.read()
    if error:
        html = html.replace("<!--ERROR-->", f'<p class="error">{error}</p>')
    return html


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

            yield sse("status", "Writing story…")

            # Truncate to 2000 chars — enough context for a 250-word story
            MAX_CHARS = 2000
            if len(report_text) > MAX_CHARS:
                report_text = report_text[:MAX_CHARS] + "\n[truncated]"

            client = anthropic.Anthropic()
            user_message = f"Title: {title}\n\n{report_text}"

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            ) as stream_obj:
                writing_started = False
                for event in stream_obj:
                    if event.type == "content_block_start":
                        if event.content_block.type == "text" and not writing_started:
                            writing_started = True
                            yield sse("writing_start", "")
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
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
