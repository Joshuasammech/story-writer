#!/usr/bin/env python3
"""
Story Writer Bot
================
Fetches a report from Google Docs and writes a case report story
with contrast frames — showing how long achievements conventionally
take vs. how long they actually took.

Usage:
    python story_writer.py <google_doc_url_or_id> [--output output.md]

Authentication:
    Set GOOGLE_SERVICE_ACCOUNT_JSON to your service account JSON file path,
    OR share the Google Doc with your service account's email address.
    Alternatively, set GOOGLE_APPLICATION_CREDENTIALS env var.
"""

import argparse
import os
import re
import sys

import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ─── Google Docs helpers ──────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]


def extract_doc_id(url_or_id: str) -> str:
    """Extract the document ID from a Google Docs URL or return raw ID."""
    # Matches /d/<id>/ pattern in Google Docs URLs
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Assume it's already a plain doc ID
    return url_or_id.strip()


def build_docs_service():
    """Build an authenticated Google Docs API service."""
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if not sa_path:
        print(
            "ERROR: Set GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_APPLICATION_CREDENTIALS) "
            "to the path of your service account JSON file.",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return build("docs", "v1", credentials=creds)


def fetch_doc_text(service, doc_id: str) -> tuple[str, str]:
    """Return (title, plain_text) for the given Google Doc."""
    try:
        doc = service.documents().get(documentId=doc_id).execute()
    except HttpError as e:
        print(f"ERROR fetching document: {e}", file=sys.stderr)
        sys.exit(1)

    title = doc.get("title", "Untitled Document")
    body = doc.get("body", {}).get("content", [])

    lines: list[str] = []
    for element in body:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                lines.append(text_run.get("content", ""))

    return title, "".join(lines).strip()


# ─── Claude story generation ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert business storyteller and case report writer. Your specialty is
transforming raw reports and data into compelling case reports that highlight human
achievement through "contrast frames."

A CONTRAST FRAME is the backbone of every story you write. It works like this:
1. Identify the core achievement or success described in the report.
2. Draw on your knowledge of industry conventions, domain norms, and historical
   benchmarks to state how long that achievement *conventionally* takes (e.g.,
   "Most companies take 3–5 years to reach $1 M ARR," or "Clinical trials typically
   span 7–10 years before approval").
3. Reveal the actual timeline from the report.
4. Let the gap between conventional expectation and actual achievement drive the
   emotional weight of the story.

CASE REPORT FORMAT — always structure your output as follows:

---
# [Compelling Title — action-oriented, outcome-focused]

## Executive Summary
2–3 sentences: who, what they achieved, and the contrast frame headline
(e.g., "In an industry where X takes Y years, [Subject] did it in Z months.").

## Background & Context
Who is the subject? What domain/industry? What problem were they solving?

## The Challenge
What obstacles, constraints, or odds were they working against?

## Contrast Frame: Conventional Timeline vs. Reality
| Milestone | Industry Benchmark | Actual Time Achieved |
|---|---|---|
| … | … | … |

Add a short narrative (3–5 sentences) explaining *why* the conventional timeline
exists and what makes the subject's pace remarkable.

## Approach & Methodology
How did they do it? Key decisions, strategies, turning points.

## Results & Impact
Quantified outcomes wherever possible. Use bullet points.

## Key Takeaways
3–5 lessons that others in the field can apply.

## Conclusion
A forward-looking paragraph: what this achievement signals for the industry or field.
---

TONE: Precise, authoritative, and inspiring. Avoid hype. Let the facts speak.
IMPORTANT: If the report does not contain enough data for a certain section, note
"[Insufficient data in source report]" rather than fabricating details.
"""


def generate_story(report_title: str, report_text: str) -> str:
    """Send the report to Claude and stream back a case report story."""
    client = anthropic.Anthropic()

    user_message = (
        f"Document title: {report_title}\n\n"
        f"--- REPORT CONTENT START ---\n{report_text}\n--- REPORT CONTENT END ---\n\n"
        "Please write a full case report story with contrast frames based on the "
        "report above. Follow the case report format exactly."
    )

    print("Generating story...\n", flush=True)

    full_text = ""
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for event in stream:
            if event.type == "content_block_start":
                if event.content_block.type == "thinking":
                    print("[Claude is thinking...]\n", flush=True)
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    print(event.delta.text, end="", flush=True)
                    full_text += event.delta.text

    print()  # newline after stream ends
    return full_text


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Google Doc report into a case report story with contrast frames."
    )
    parser.add_argument(
        "doc",
        help="Google Doc URL or document ID",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Save the story to this file (e.g. story.md). Prints to stdout if omitted.",
        default=None,
    )
    args = parser.parse_args()

    # 1. Fetch the Google Doc
    doc_id = extract_doc_id(args.doc)
    print(f"Fetching Google Doc: {doc_id}")
    service = build_docs_service()
    title, text = fetch_doc_text(service, doc_id)
    print(f"  Title  : {title}")
    print(f"  Length : {len(text):,} characters\n")

    if not text:
        print("ERROR: The document appears to be empty.", file=sys.stderr)
        sys.exit(1)

    # 2. Generate the story
    story = generate_story(title, text)

    # 3. Save or print
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(story)
        print(f"\nStory saved to: {args.output}")
    else:
        # Already printed via streaming; nothing more needed
        pass


if __name__ == "__main__":
    main()
