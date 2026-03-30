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

# ── Story templates ────────────────────────────────────────────────────────────

TEMPLATES = {
    "contrast_frame": {
        "name": "Contrast Frame",
        "description": "Expectation vs result vs conventional timeline",
        "prompt": """\
You are a concise story writer. Read the report and write a short, punchy story \
— 150 to 250 words — built around three things:

1. THE EXPECTATION — What did the person expect or hope to achieve?
2. THE RESULT — What did they actually get? (better, worse, or different)
3. THE CONTRAST FRAME — How long does this kind of achievement conventionally \
take in the real world? Name a specific conventional timeframe, then contrast \
it with how long it actually took in this report.

FORMAT — use exactly this structure:

---
# [One punchy title]

**What they expected:** 1–2 sentences.

**What they got:** 1–2 sentences.

**The contrast frame:** State the conventional timeline (e.g. "Most people take \
X years to…"), then state the actual timeline, then one sentence on why that gap matters.
---

RULES: Under 250 words. No bullet lists. No extra sections. Plain, direct English.""",
    },
    "sales_win": {
        "name": "Sales Win",
        "description": "Challenge, approach, result with numbers",
        "prompt": """\
You are a concise story writer specialising in sales achievements. Read the \
report and write a short, punchy story — 150 to 250 words — built around:

1. THE CHALLENGE — What was the sales obstacle or goal?
2. THE APPROACH — What did they do differently or exceptionally well?
3. THE WIN — What was the result? Include specific numbers, revenue, or \
percentages if available. How does this compare to typical sales performance?

FORMAT — use exactly this structure:

---
# [One punchy title]

**The challenge:** 1–2 sentences.

**The approach:** 1–2 sentences.

**The win:** State the result with numbers, then one sentence on what made this \
performance stand out against the norm.
---

RULES: Under 250 words. No bullet lists. No extra sections. Plain, direct English.""",
    },
    "milestone": {
        "name": "Milestone Story",
        "description": "The journey, the obstacle, the breakthrough",
        "prompt": """\
You are a concise story writer. Read the report and write a short, punchy story \
— 150 to 250 words — built around:

1. THE STARTING POINT — Where did this person or team begin? What was the situation?
2. THE OBSTACLE — What stood in their way or made this hard?
3. THE BREAKTHROUGH — What was the milestone achieved, and why does it matter?

FORMAT — use exactly this structure:

---
# [One punchy title]

**Where they started:** 1–2 sentences.

**The obstacle:** 1–2 sentences.

**The breakthrough:** 1–2 sentences on the achievement and its significance.
---

RULES: Under 250 words. No bullet lists. No extra sections. Plain, direct English.""",
    },
    "personal_growth": {
        "name": "Personal Growth",
        "description": "Before, the shift, and what it means",
        "prompt": """\
You are a concise story writer. Read the report and write a short, punchy story \
— 150 to 250 words — focused on personal transformation:

1. BEFORE — What was this person's situation, mindset, or capability before?
2. THE SHIFT — What changed? What did they learn, do, or decide?
3. AFTER — What does their life, career, or confidence look like now? \
What does this growth mean for their future?

FORMAT — use exactly this structure:

---
# [One punchy title]

**Before:** 1–2 sentences.

**The shift:** 1–2 sentences.

**After:** 1–2 sentences on the transformation and what it unlocks.
---

RULES: Under 250 words. No bullet lists. No extra sections. Plain, direct English.""",
    },
    "client_impact": {
        "name": "Client Impact",
        "description": "Problem, solution, measurable transformation",
        "prompt": """\
You are a concise story writer. Read the report and write a short, punchy story \
— 150 to 250 words — about the impact delivered to a client or customer:

1. THE PROBLEM — What was the client struggling with or trying to solve?
2. THE SOLUTION — What was done to help them? Keep it specific.
3. THE TRANSFORMATION — What changed for the client? Include measurable \
outcomes (time saved, revenue gained, goals hit) if available.

FORMAT — use exactly this structure:

---
# [One punchy title]

**The problem:** 1–2 sentences.

**The solution:** 1–2 sentences.

**The transformation:** 1–2 sentences with measurable impact where possible.
---

RULES: Under 250 words. No bullet lists. No extra sections. Plain, direct English.""",
    },
}

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

@app.route("/templates")
@login_required
def get_templates():
    return {k: {"name": v["name"], "description": v["description"]} for k, v in TEMPLATES.items()}


@app.route("/")
@login_required
def index():
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    body = request.get_json(force=True)
    input_type   = body.get("type", "url")
    content      = body.get("content", "").strip()
    template_key = body.get("template", "contrast_frame")

    if not content:
        return {"error": "No content provided."}, 400

    template = TEMPLATES.get(template_key, TEMPLATES["contrast_frame"])
    system_prompt = template["prompt"]

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
                system=system_prompt,
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
