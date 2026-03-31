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
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
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
You are a transformation case study writer. Write a Leadership Transformation Case Study from the report provided. \
Write with warmth, depth, and precision — like someone who deeply understands people, not like a marketer. \
Every fact must come from the report. Never invent details.

Use EXACTLY this format (fill in content where shown in brackets):

---
# [Full Name]
### [One line capturing what this journey fundamentally is — e.g. "From 30 years of service to entrepreneurial identity"]

**[Key stat or number]** · **[Second key stat]** · **[Third key stat or outcome]**

---

## 01 | Participant Profile
[2–3 sentences: who they are, where this journey begins, and what makes it worth telling. Write with care — this is a real person.]

**[One bold sentence naming the core nature of this journey — what kind of transformation this is.]**

---

## 02 | The Journey So Far
[2–3 sentences on what has moved or been achieved. Reference specific outcomes, metrics, or changes from the report.]

> "[One sentence callout that captures the deeper significance of this progress — not a stat, a meaning.]"

---

## 03 | Starting Conditions
[One sentence framing what shaped them before this journey.]

**3.1 [Name this first shaping force]**
[1–2 sentences on what this force was and how it operated in their life.]

**3.2 [Name the second shaping force]**
[1–2 sentences.]

**3.3 [Name the third challenge or turning point]**
[1–2 sentences.]

---

## 04 | What's Being Built
[2–3 sentences on their current work, role, or emergence. What exists now that didn't before? Be specific about what they are actively creating.]

---

## 05 | Key Breakthrough Themes

**5.1 [Name this first inner shift]**
[1–2 sentences on what changed — cognitively, emotionally, or behaviourally.]

**5.2 [Name the second shift]**
[1–2 sentences.]

**5.3 [Name the third shift]**
[1–2 sentences.]

---

## 06 | The Identity Shift
[2–3 sentences. Where do they stand today, internally? What is the new operating system they are building? What does the next chapter look like?]

---

**CORE INSIGHT**
[3–4 sentences. The defining statement of this person's transformation. Not just what they did — what it means. Make the reader feel the weight of it.]
---

Rules:
- Do not add extra sections or change section numbers/names
- No hollow phrases: "hard work", "passion", "incredible journey", "amazing"
- Write in third person throughout
- Keep total output between 450–650 words\
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
    for attempt in range(2):
        try:
            resp = requests.get(export_url, timeout=25)
            break
        except requests.exceptions.ConnectionError:
            if attempt == 1:
                raise RuntimeError(
                    "Could not reach Google Docs. Check your internet connection or try again."
                )
        except requests.exceptions.Timeout:
            if attempt == 1:
                raise RuntimeError("Google Docs request timed out. Try again.")
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

            yield sse("status", "Writing case study…")

            # Provide up to 4000 chars of context for the richer template
            MAX_CHARS = 4000
            if len(report_text) > MAX_CHARS:
                report_text = report_text[:MAX_CHARS] + "\n[truncated]"

            client = anthropic.Anthropic()
            user_message = f"Title: {title}\n\n{report_text}"

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1800,
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


# ── DOCX helpers ───────────────────────────────────────────────────────────────

def _shade_paragraph(paragraph, hex_color: str):
    """Apply background shading to a paragraph."""
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


def _set_para_border(paragraph, sides=("bottom",), color="CCCCCC", size=6, space=1):
    """Add border lines to a paragraph."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    for side in sides:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), str(space))
        el.set(qn("w:color"), color)
        pBdr.append(el)
    pPr.append(pBdr)


def _add_section_header(doc, text: str):
    """Add a styled section header like '01 | Participant Profile'."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(0)
    _shade_paragraph(p, "E8EDF2")
    run = p.add_run(f"  {text}")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x1A, 0x3A, 0x5C)
    # Bottom border
    _set_para_border(p, sides=("bottom",), color="2563EB", size=6)


def _add_callout(doc, text: str):
    """Add a styled blockquote callout box."""
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    _shade_paragraph(p, "EFF6FF")
    _set_para_border(p, sides=("left",), color="2563EB", size=16)
    run = p.add_run(text.strip('"').strip())
    run.italic = True
    run.font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
    run.font.size = Pt(11)


def _add_core_insight(doc, text: str):
    """Add the CORE INSIGHT block."""
    # Header row
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(0)
    _shade_paragraph(p, "1A3A5C")
    run = p.add_run("  CORE INSIGHT")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # Body
    p2 = doc.add_paragraph()
    p2.paragraph_format.space_before = Pt(0)
    p2.paragraph_format.space_after = Pt(6)
    _shade_paragraph(p2, "EFF6FF")
    _set_para_border(p2, sides=("left", "right", "bottom"), color="1A3A5C", size=6)
    run2 = p2.add_run(f"  {text}")
    run2.font.size = Pt(11)
    run2.font.color.rgb = RGBColor(0x1A, 0x1A, 0x18)


@app.route("/download/docx", methods=["POST"])
@login_required
def download_docx():
    body    = request.get_json(force=True)
    content = body.get("content", "").strip()
    if not content:
        return {"error": "No content."}, 400

    doc = Document()

    # Default style
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    # Page margins
    for section in doc.sections:
        section.top_margin    = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin   = Inches(1.1)
        section.right_margin  = Inches(1.1)

    lines = content.splitlines()
    in_core_insight = False
    core_insight_lines = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1

        # Flush core insight when we hit --- or end
        if in_core_insight:
            if line == "---" or line == "":
                if core_insight_lines:
                    _add_core_insight(doc, " ".join(core_insight_lines))
                    core_insight_lines = []
                    in_core_insight = False
                if line == "---":
                    continue
            else:
                core_insight_lines.append(line)
            continue

        if not line or line == "---":
            doc.add_paragraph()
            continue

        # H1 — person's name
        if line.startswith("# "):
            p = doc.add_heading(line[2:], level=1)
            p.runs[0].font.color.rgb = RGBColor(0x1A, 0x3A, 0x5C)

        # H3 — tagline/subtitle
        elif line.startswith("### "):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            run = p.add_run(line[4:])
            run.italic = True
            run.font.size = Pt(12)
            run.font.color.rgb = RGBColor(0x6B, 0x6B, 0x67)

        # H2 — numbered section header
        elif line.startswith("## "):
            _add_section_header(doc, line[3:])

        # Stats line: **stat1** · **stat2** · **stat3**
        elif re.match(r"^\*\*[^*]+\*\*\s*·", line):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(8)
            parts = re.split(r"(\*\*[^*]+\*\*)", line)
            for part in parts:
                if part.startswith("**") and part.endswith("**"):
                    run = p.add_run(part[2:-2])
                    run.bold = True
                    run.font.color.rgb = RGBColor(0x1A, 0x3A, 0x5C)
                elif part.strip():
                    p.add_run(part)

        # Blockquote callout
        elif line.startswith("> "):
            _add_callout(doc, line[2:])

        # CORE INSIGHT marker
        elif line == "**CORE INSIGHT**":
            in_core_insight = True

        # Bold subsection header e.g. **3.1 Title** or **5.2 Title**
        elif re.match(r"^\*\*\d+\.\d+\s", line) and line.endswith("**"):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            run = p.add_run(line.strip("*"))
            run.bold = True
            run.font.color.rgb = RGBColor(0x1A, 0x3A, 0x5C)

        # Bold label with colon e.g. **The Impact:** text
        elif line.startswith("**") and ":**" in line:
            label, _, rest = line.partition(":**")
            p = doc.add_paragraph()
            p.add_run(label.lstrip("*") + ": ").bold = True
            p.add_run(rest.strip())

        # Plain bold line
        elif line.startswith("**") and line.endswith("**") and len(line) > 4:
            p = doc.add_paragraph()
            run = p.add_run(line.strip("*"))
            run.bold = True

        else:
            doc.add_paragraph(line)

    # Flush any remaining core insight at EOF
    if in_core_insight and core_insight_lines:
        _add_core_insight(doc, " ".join(core_insight_lines))

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name="case_study.docx",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
