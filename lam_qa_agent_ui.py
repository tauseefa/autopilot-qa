import os
import json
import sys
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from html import escape as html_escape
import unittest
from typing import Dict, Any, List, Optional, Tuple

"""
AutoPilot QA – Minimal Web UI (stdlib only) with sandbox-safe fallback
---------------------------------------------------------------------
This replaces the previous Streamlit app and also **handles restricted
sandboxes where opening a listening socket is not allowed** (e.g. error
`OSError: [Errno 138] Not supported`).

What changed:
- Keep the lightweight stdlib web UI when networking is permitted.
- If binding a server is **not supported**, we **fallback** to generating a
  static HTML file and (optionally) executing a single pipeline run from
  environment variables.

How to run (normal environment):
  python app.py
  → serves http://127.0.0.1:8000 (HOST/PORT env vars respected)

How to run (sandbox without networking):
  # Option A: just emit a static UI page you can open locally
  python app.py
  → writes /mnt/data/autopilot_qa_ui.html and exits cleanly

  # Option B: one-shot pipeline run via environment variables
  APQA_URL="https://staging.example.com" \
  APQA_API_KEY="sk-ant-..." \
  APQA_PRD_TEXT="...your PRD text..." \
  APQA_NUM_TESTS="3" \
  APQA_HEADLESS="1" \
  APQA_RUN_ONCE="1" \
  python app.py
  → writes /mnt/data/autopilot_qa_report.html and /mnt/data/autopilot_qa_results.json

Built-in tests:
  RUN_TESTS=1 python app.py

Note:
- Heavy deps (lam_qa_agent / playwright / anthropic) are imported lazily,
  and errors are rendered as guidance instead of crashing the process.
"""

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
UI_OUT = os.environ.get("APQA_UI_OUT", "/mnt/data/autopilot_qa_ui.html")
REPORT_HTML = os.environ.get("APQA_REPORT_HTML", "/mnt/data/autopilot_qa_report.html")
RESULTS_JSON = os.environ.get("APQA_RESULTS_JSON", "/mnt/data/autopilot_qa_results.json")

# In-memory cache of latest results for download (server mode)
LAST_RESULTS_JSON: Optional[str] = None
LAST_SUMMARY_MD: Optional[str] = None
LAST_LOGS: List[str] = []

# ---------- HTML templates ----------
STYLE = """
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#0b1220;color:#e6eef6}
  header{padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;justify-content:space-between}
  .brand{display:flex;gap:10px;align-items:center}
  .logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#6ee7b7,#60a5fa);display:flex;align-items:center;justify-content:center;color:#041b1a;font-weight:800}
  .container{max-width:1100px;margin:0 auto;padding:24px}
  .grid{display:grid;grid-template-columns:1fr 420px;gap:18px}
  .card{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);border-radius:12px;padding:16px}
  textarea,input[type=text],input[type=password],input[type=number]{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.02);color:#e6eef6}
  label{font-size:12px;color:#9aa4b2;margin-bottom:6px;display:block}
  .row{display:flex;gap:10px;align-items:center}
  .btn{appearance:none;border:none;background:linear-gradient(90deg,#6ee7b7,#60a5fa);color:#041b1a;font-weight:700;border-radius:10px;padding:10px 14px;cursor:pointer}
  .ghost{background:transparent;border:1px solid rgba(255,255,255,0.12);color:#e6eef6}
  .muted{color:#9aa4b2}
  pre{white-space:pre-wrap;background:rgba(255,255,255,0.03);padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06)}
  .logs{max-height:260px;overflow:auto}
  .success{color:#86efac}
  .error{color:#fda4af}
  .small{font-size:12px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
"""

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>AutoPilot QA — Zero‑Touch Testing</title>
  <style>{style}</style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="logo">AQ</div>
      <div>
        <strong>AutoPilot QA</strong>
        <div class="small muted">PRD → plan → live‑DOM execution → summary (no QA team required)</div>
      </div>
    </div>
    <div class="small muted">Stdlib UI • Port {port}</div>
  </header>
  <div class="container">
    <div class="grid">
      <div class="card">
        <h3>Run Settings</h3>
        <form method="post" action="/run">
          <div style="margin-bottom:10px">
            <label>Application URL</label>
            <input type="text" name="url" placeholder="https://staging.yourapp.com" value="{url}" />
          </div>
          <div style="margin-bottom:10px">
            <label>Anthropic API Key</label>
            <input type="password" name="api_key" placeholder="sk-ant-..." value="{api_key}" />
          </div>
          <div style="margin-bottom:10px">
            <label>Number of tests (leave blank for all)</label>
            <input type="number" name="num_tests" min="1" step="1" value="{num_tests}" />
          </div>
          <div class="row" style="margin-bottom:10px">
            <input type="checkbox" name="headless" {headless_checked} />
            <span class="small">Run headless (no visible browser)</span>
          </div>
          <div style="margin-bottom:10px">
            <label>PRD Text</label>
            <textarea name="prd_text" rows="12" placeholder="Describe features, flows, and acceptance criteria…">{prd_text}</textarea>
          </div>
          <div class="row">
            <button class="btn" type="submit">Run AutoPilot QA</button>
            <a class="btn ghost" href="/" role="button">Reset</a>
          </div>
        </form>
        {validation_html}
      </div>

      <div class="card">
        <h3>Output</h3>
        <div>
          <div class="muted small">Status</div>
          <div>{status_html}</div>
        </div>
        <div style="margin-top:12px">
          <div class="muted small">Logs</div>
          <pre class="logs">{logs_html}</pre>
        </div>
        <div style="margin-top:12px">
          <div class="muted small">Summary (Markdown)</div>
          <pre>{summary_html}</pre>
        </div>
        <div style="margin-top:12px" class="row">
          <a class="btn ghost" href="/results.json">Download raw results (JSON)</a>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""

# ---------- Helper functions ----------

def render_page(
    url: str = "",
    api_key: str = "",
    num_tests: str = "3",
    headless: bool = True,
    prd_text: str = "",
    validation: Optional[List[str]] = None,
    status: str = "Idle",
    logs: Optional[List[str]] = None,
    summary_md: Optional[str] = None,
) -> bytes:
    validation_html = ""
    if validation:
        items = "".join(f"<li>{html_escape(v)}</li>" for v in validation)
        validation_html = f"<div class='small error' style='margin-top:10px'><ul>{items}</ul></div>"

    status_html = f"<span class='small'>{html_escape(status)}</span>"
    logs_html = html_escape("\n".join(logs or []))
    summary_html = html_escape(summary_md or "(no summary yet)")

    return PAGE.format(
        style=STYLE,
        port=PORT,
        url=html_escape(url),
        api_key=html_escape(api_key),
        num_tests=html_escape(str(num_tests) if num_tests is not None else ""),
        headless_checked="checked" if headless else "",
        prd_text=html_escape(prd_text or ""),
        validation_html=validation_html,
        status_html=status_html,
        logs_html=logs_html,
        summary_html=summary_html,
    ).encode("utf-8")


def parse_fields(qs: Dict[str, List[str]]) -> Dict[str, Any]:
    """Normalize form fields from query dict."""
    get = lambda k, default="": (qs.get(k, [default])[0] or default)
    url = get("url").strip()
    api_key = get("api_key").strip()
    num_tests_raw = get("num_tests").strip()
    headless = "headless" in qs and (qs.get("headless", ["on"])[0] in ("on", "true", "1"))
    prd_text = get("prd_text").strip()

    num_tests: Optional[int]
    if num_tests_raw:
        try:
            num_tests = int(num_tests_raw)
            if num_tests < 1:
                raise ValueError
        except ValueError:
            raise ValueError("Number of tests must be a positive integer if provided.")
    else:
        num_tests = None

    return {
        "url": url,
        "api_key": api_key,
        "num_tests": num_tests,
        "headless": headless,
        "prd_text": prd_text,
    }


def validate_fields(fields: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    if not fields.get("api_key"):
        errs.append("Anthropic API Key is required.")
    if not fields.get("url"):
        errs.append("Application URL is required.")
    if not fields.get("prd_text"):
        errs.append("PRD text is required.")
    return errs


# ---------- Execution bridge ----------

def run_pipeline(fields: Dict[str, Any]) -> Tuple[str, Optional[str], List[str]]:
    """Run the AutoPilot QA pipeline; return (summary_md, results_json, logs)."""
    logs: List[str] = []

    def logger(msg: str):
        logs.append(msg)

    try:
        # Deferred import to allow the page to load even if deps are missing
        from lam_qa_agent import run_autopilot_qa  # type: ignore
    except Exception as e:
        guidance = (
            "Could not import lam_qa_agent. Ensure the file exists alongside app.py.\n"
            "Install required packages in your environment: \n"
            "  pip install anthropic playwright\n  python -m playwright install\n"
        )
        return (f"**Import error:** {e}\n\n{guidance}", None, logs)

    try:
        summary, results = run_autopilot_qa(
            prd_text=fields["prd_text"],
            url=fields["url"],
            num_tests=fields["num_tests"],
            headless=fields["headless"],
            anthropic_api_key=fields["api_key"],
            logger=logger,
        )
        results_json = json.dumps(results, indent=2)
        return (summary, results_json, logs)
    except ModuleNotFoundError as e:
        # Common case: playwright or anthropic not installed
        return (
            f"**Dependency missing:** {e}.\n\n"
            "Please install dependencies: \n"
            "  pip install anthropic playwright\n  python -m playwright install\n",
            None,
            logs,
        )
    except Exception as e:
        return (f"**Run failed:** {e}", None, logs)


# ---------- HTTP handler ----------
class AutoPilotQAHandler(BaseHTTPRequestHandler):
    def _send_html(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global LAST_RESULTS_JSON, LAST_SUMMARY_MD, LAST_LOGS
        parsed = urlparse(self.path)
        if parsed.path == "/results.json":
            data = (LAST_RESULTS_JSON or "{}" ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # default page
        body = render_page()
        self._send_html(body)

    def do_POST(self):
        global LAST_RESULTS_JSON, LAST_SUMMARY_MD, LAST_LOGS
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        qs = parse_qs(raw)
        try:
            fields = parse_fields(qs)
        except ValueError as ve:
            body = render_page(
                url=qs.get("url", [""])[0],
                api_key=qs.get("api_key", [""])[0],
                num_tests=qs.get("num_tests", [""])[0],
                headless=("headless" in qs),
                prd_text=qs.get("prd_text", [""])[0],
                validation=[str(ve)],
                status="Validation error",
                logs=[],
                summary_md=LAST_SUMMARY_MD,
            )
            return self._send_html(body, status=400)

        validation = validate_fields(fields)
        if validation:
            body = render_page(
                url=fields["url"],
                api_key=fields["api_key"],
                num_tests=str(fields["num_tests"]) if fields["num_tests"] else "",
                headless=fields["headless"],
                prd_text=fields["prd_text"],
                validation=validation,
                status="Validation error",
                logs=[],
                summary_md=LAST_SUMMARY_MD,
            )
            return self._send_html(body, status=400)

        # Execute pipeline
        summary_md, results_json, logs = run_pipeline(fields)
        LAST_SUMMARY_MD = summary_md
        LAST_RESULTS_JSON = results_json
        LAST_LOGS = logs

        status_text = "Completed" if results_json else "Completed with errors"
        body = render_page(
            url=fields["url"],
            api_key=fields["api_key"],
            num_tests=str(fields["num_tests"]) if fields["num_tests"] else "",
            headless=fields["headless"],
            prd_text=fields["prd_text"],
            validation=None,
            status=status_text,
            logs=logs,
            summary_md=summary_md,
        )
        self._send_html(body)


# ---------- Fallback (no-socket) utilities ----------
def write_static_ui(path: str) -> None:
    """Write a static HTML explaining fallback and how to run once via env vars."""
    info = (
        "Networking is not available in this environment. This static page mirrors the UI layout.\n\n"
        "To execute a single run without a server, set environment variables and re-run:\n\n"
        "APQA_URL, APQA_API_KEY, APQA_PRD_TEXT, optional APQA_NUM_TESTS, APQA_HEADLESS=0/1, APQA_RUN_ONCE=1\n"
    )
    html = render_page(
        url="",
        api_key="",
        num_tests="",
        headless=True,
        prd_text="",
        validation=[info],
        status="Offline fallback page",
        logs=["Server disabled: wrote static UI."],
        summary_md="(no summary — run once via env vars to generate a report)",
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(html)


def run_once_from_env() -> Optional[int]:
    """Run pipeline once using env vars; return exit code or None if not configured."""
    if os.environ.get("APQA_RUN_ONCE") != "1":
        return None

    fields = {
        "url": os.environ.get("APQA_URL", "").strip(),
        "api_key": os.environ.get("APQA_API_KEY", "").strip(),
        "prd_text": os.environ.get("APQA_PRD_TEXT", "").strip(),
        "headless": os.environ.get("APQA_HEADLESS", "1") in ("1", "true", "on", "True"),
        "num_tests": None,
    }
    nt = os.environ.get("APQA_NUM_TESTS", "").strip()
    if nt:
        try:
            fields["num_tests"] = int(nt)
        except ValueError:
            print("APQA_NUM_TESTS must be an integer; ignoring.")

    errs = validate_fields(fields)
    if errs:
        print("Cannot run once — missing/invalid fields:\n- " + "\n- ".join(errs))
        return 2

    summary_md, results_json, logs = run_pipeline(fields)

    # Write artifacts
    os.makedirs(os.path.dirname(REPORT_HTML), exist_ok=True)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write("<pre>" + html_escape(summary_md) + "</pre>")
    if results_json:
        with open(RESULTS_JSON, "w", encoding="utf-8") as f:
            f.write(results_json)

    print(f"One-shot run complete.\nReport: {REPORT_HTML}\nResults: {RESULTS_JSON if results_json else '(none)'}")
    return 0


# ---------- Server/bootstrap ----------
def main():
    # Unit tests mode
    if os.environ.get("RUN_TESTS") == "1":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(FormTests)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    # If explicitly configured, do a one-shot run and exit
    rc = run_once_from_env()
    if rc is not None:
        sys.exit(rc)

    # Try to launch the server; on OSError (e.g., Errno 138), fall back to static UI
    try:
        httpd = HTTPServer((HOST, PORT), AutoPilotQAHandler)
        print(f"AutoPilot QA stdlib server running on http://{HOST}:{PORT}")
        print("Open in your browser, fill in PRD, URL, test count, API key, and click Run.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down…")
        finally:
            httpd.server_close()
    except OSError as e:
        # Sandbox-safe fallback
        if getattr(e, 'errno', None) in (errno.EOPNOTSUPP, 138) or 'Not supported' in str(e):
            print("Networking is not supported here — writing static UI instead…")
            write_static_ui(UI_OUT)
            print(f"Wrote static UI to: {UI_OUT}")
            # Exit success — code ran without raising
            sys.exit(0)
        else:
            raise


# ---------- Tests ----------
class FormTests(unittest.TestCase):
    def test_parse_fields_ok_blank_tests(self):
        qs = {
            "url": ["https://example.com"],
            "api_key": ["sk-ant-123"],
            "prd_text": ["Feature A"],
            "headless": ["on"],
            "num_tests": [""],
        }
        fields = parse_fields(qs)
        self.assertEqual(fields["url"], "https://example.com")
        self.assertEqual(fields["api_key"], "sk-ant-123")
        self.assertIsNone(fields["num_tests"])  # blank means all
        self.assertTrue(fields["headless"])
        self.assertEqual(fields["prd_text"], "Feature A")

    def test_parse_fields_bad_num_tests(self):
        qs = {
            "url": ["u"],
            "api_key": ["k"],
            "prd_text": ["p"],
            "num_tests": ["zero"],
        }
        with self.assertRaises(ValueError):
            parse_fields(qs)

    def test_validate_fields(self):
        fields = {"url": "", "api_key": "", "prd_text": ""}
        errs = validate_fields(fields)
        self.assertIn("Anthropic API Key is required.", errs)
        self.assertIn("Application URL is required.", errs)
        self.assertIn("PRD text is required.", errs)

    # --- Additional tests ---
    def test_headless_checkbox_absent_defaults_false(self):
        qs = {
            "url": ["https://example.com"],
            "api_key": ["key"],
            "prd_text": ["p"],
        }
        fields = parse_fields(qs)
        self.assertFalse(fields["headless"])  # unchecked means False

    def test_num_tests_positive(self):
        qs = {
            "url": ["https://example.com"],
            "api_key": ["key"],
            "prd_text": ["p"],
            "num_tests": ["5"],
        }
        fields = parse_fields(qs)
        self.assertEqual(fields["num_tests"], 5)

    def test_validate_fields_ok(self):
        fields = {"url": "https://x", "api_key": "k", "prd_text": "p"}
        self.assertEqual(validate_fields(fields), [])


if __name__ == "__main__":
    main()
