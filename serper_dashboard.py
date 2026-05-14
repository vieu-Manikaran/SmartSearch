"""Local dashboard to run Serper pair searches and view results."""

from __future__ import annotations

import csv
import json
import os
import re
import threading
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, render_template_string, request, send_from_directory, url_for

from config import settings
from serper_search import find_linkedin_company_url, search_serper

app = Flask(__name__)

# Only one Company LinkedIn lookup at a time (Serper + CSV can be slow; avoids parallel load).
_company_linkedin_worker_lock = threading.Lock()

DEFAULT_PEOPLE = [
    "Juan Gomez-Sanchez",
    "Tim Plona",
    "Darren Argyle",
]

DEFAULT_ACCOUNTS = [
    "Standard Chartered",
    "Rabobank",
    "ASML",
]

PAIR_OPTIONS = {
    "person_person": '"Person" "Person"',
    "person_account": '"Person" "Account"',
    "account_account": '"Account" "Account"',
}

SEARCH_TYPE_OPTIONS = {
    "web": "Web (all sites)",
    "linkedin_posts": "LinkedIn posts only",
}

PAIR_INPUT_CONFIG = {
    "person_person": {
        "left_label": "Person list 1 (one per line)",
        "right_label": "Person list 2 (one per line)",
        "left_placeholder": "e.g. Juan Gomez-Sanchez",
        "right_placeholder": "e.g. Tim Plona\\nDarren Argyle",
    },
    "person_account": {
        "left_label": "People (one per line)",
        "right_label": "Accounts (one per line)",
        "left_placeholder": "e.g. Juan Gomez-Sanchez",
        "right_placeholder": "e.g. Standard Chartered",
    },
    "account_account": {
        "left_label": "Account list 1 (one per line)",
        "right_label": "Account list 2 (one per line)",
        "left_placeholder": "e.g. Standard Chartered",
        "right_placeholder": "e.g. Rabobank\\nASML",
    },
}

DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", re.I),
    re.compile(r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b", re.I),
    re.compile(r"\b\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago\b", re.I),
]

LINKEDIN_POSTS_PREFIX = re.compile(r"^site:linkedin\.com/posts\s+", re.I)

HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Serper Pair Search Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 1200px; }
    .row { display: flex; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
    .col { flex: 1; min-width: 200px; }
    label { display: block; font-weight: 600; margin-bottom: 6px; }
    select, textarea, input { width: 100%; padding: 8px; box-sizing: border-box; }
    textarea { min-height: 100px; }
    button { padding: 10px 14px; cursor: pointer; }
    .msg { margin: 14px 0; padding: 10px; background: #f5f5f5; border: 1px solid #ddd; }
    .warn { background: #fff8e6; border-color: #e6c200; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border: 1px solid #ddd; text-align: left; padding: 8px; vertical-align: top; }
    th { background: #fafafa; }
    .small { color: #666; font-size: 0.9em; }
    tr.row-empty td { background: #fff8f0; color: #553; }
    .filter-bar { margin-top: 16px; padding: 12px; background: #f9f9f9; border: 1px solid #e0e0e0; }
    .filter-bar .row { margin-bottom: 0; }
    tr.filtered-out { display: none; }
    .file-meta { color: #555; font-size: 0.85em; }
  </style>
</head>
<body>
  <h2>Serper Pair Search Dashboard</h2>
  <p><a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a> &mdash; upload a CSV (column <code>Company</code>) or enter one company; Serper finds each company&rsquo;s LinkedIn page.</p>
  <p class="small">Choose a pair and search type, then run query combinations. Each query writes one CSV file with the same columns as the table (one row per organic hit, or one &ldquo;no results&rdquo; row if Serper returned none).</p>

  <form method="post">
    <div class="row">
      <div class="col">
        <label for="pair_type">Pair Type (3 options)</label>
        <select name="pair_type" id="pair_type">
          {% for key, label in pair_options.items() %}
          <option value="{{ key }}" {% if key == selected_pair_type %}selected{% endif %}>{{ label }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col">
        <label for="search_type">Search Type (2 options)</label>
        <select name="search_type" id="search_type">
          {% for key, label in search_type_options.items() %}
          <option value="{{ key }}" {% if key == selected_search_type %}selected{% endif %}>{{ label }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col">
        <label for="num_results">Results per query</label>
        <input type="number" id="num_results" name="num_results" min="1" max="100" value="{{ num_results }}">
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label for="left_input" id="left_input_label">{{ left_input_label }}</label>
        <textarea name="left_input" id="left_input" placeholder="{{ left_input_placeholder }}">{{ left_input }}</textarea>
      </div>
      <div class="col">
        <label for="right_input" id="right_input_label">{{ right_input_label }}</label>
        <textarea name="right_input" id="right_input" placeholder="{{ right_input_placeholder }}">{{ right_input }}</textarea>
      </div>
    </div>
    <button type="submit">Run Search</button>
  </form>

  {% if message %}
  <div class="msg">{{ message }}</div>
  {% endif %}

  {% if no_result_queries %}
  <div class="msg warn">
    <strong>Combinations with no organic results ({{ no_result_queries|length }}):</strong>
    <ul style="margin:8px 0 0 18px;">
      {% for q in no_result_queries %}
      <li><code style="font-size:0.9em;">{{ q }}</code></li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if results %}
  <h3>Results</h3>

  {% if generated_files %}
  <p class="small"><strong>Download generated files</strong> &mdash; each file is the export for <em>one</em> search query. Rows are Serper organic results (link, title, date we could parse). If you see only a header or one row with &ldquo;No organic results&rdquo;, Serper returned no usable links for that query (quota, query too narrow, or blocking).</p>
  <ul>
    {% for file_item in generated_files %}
    <li>
      <a href="{{ file_item.download_url }}">{{ file_item.name }}</a>
      <span class="file-meta">({{ file_item.result_count }} hit{% if file_item.result_count != 1 %}s{% endif %})</span>
    </li>
    {% endfor %}
  </ul>
  {% endif %}

  <div class="filter-bar">
    <p class="small" style="margin-top:0;"><strong>Filter this run</strong> (client-side; does not re-call the API)</p>
    <div class="row">
      <div class="col">
        <label for="flt-person">Person</label>
        <select id="flt-person">
          <option value="">All people</option>
          {% for p in filter_people %}
          <option value="{{ p }}">{{ p }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col">
        <label for="flt-account">Account</label>
        <select id="flt-account">
          <option value="">All accounts</option>
          {% for a in filter_accounts %}
          <option value="{{ a }}">{{ a }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="col">
        <label for="flt-query">Query</label>
        <select id="flt-query">
          <option value="">All queries</option>
          {% for q in filter_queries %}
          <option value="{{ q }}">{{ q }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
  </div>

  <table id="results-table">
    <thead>
      <tr>
        <th>Query</th>
        <th>Heading</th>
        <th>Link</th>
        <th>Date</th>
        <th>Saved File</th>
        <th>Download</th>
      </tr>
    </thead>
    <tbody>
      {% for row in results %}
      <tr class="{% if row.is_empty %}row-empty{% endif %} result-row"
          data-people="{{ row.data_people | e }}"
          data-accounts="{{ row.data_accounts | e }}"
          data-query="{{ row.data_query | e }}">
        <td>{{ row.query }}</td>
        <td>{{ row.heading }}</td>
        <td>{% if row.link %}<a href="{{ row.link }}" target="_blank" rel="noopener noreferrer">{{ row.link }}</a>{% else %}&mdash;{% endif %}</td>
        <td>{{ row.date }}</td>
        <td class="small">{{ row.file_path_display }}</td>
        <td><a href="{{ row.download_url }}">Download CSV</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <script>
    (function () {
      var pairTypeEl = document.getElementById("pair_type");
      var leftLabelEl = document.getElementById("left_input_label");
      var rightLabelEl = document.getElementById("right_input_label");
      var leftInputEl = document.getElementById("left_input");
      var rightInputEl = document.getElementById("right_input");
      var personEl = document.getElementById("flt-person");
      var accountEl = document.getElementById("flt-account");
      var queryEl = document.getElementById("flt-query");
      var inputConfig = {{ pair_input_config_json | safe }};

      function norm(s) { return (s || "").trim(); }

      function applyPairInputConfig() {
        if (!pairTypeEl || !leftLabelEl || !rightLabelEl || !leftInputEl || !rightInputEl) return;
        var cfg = inputConfig[pairTypeEl.value] || inputConfig["person_account"];
        leftLabelEl.textContent = cfg.left_label;
        rightLabelEl.textContent = cfg.right_label;
        leftInputEl.placeholder = cfg.left_placeholder;
        rightInputEl.placeholder = cfg.right_placeholder;
      }

      if (pairTypeEl) {
        pairTypeEl.addEventListener("change", applyPairInputConfig);
        applyPairInputConfig();
      }

      if (!personEl || !accountEl || !queryEl) return;

      function applyFilters() {
        var p = norm(personEl.value);
        var a = norm(accountEl.value);
        var q = norm(queryEl.value);
        var rows = document.querySelectorAll("#results-table tbody tr.result-row");
        rows.forEach(function (tr) {
          var dPeople = tr.getAttribute("data-people") || "";
          var dAccounts = tr.getAttribute("data-accounts") || "";
          var dQuery = tr.getAttribute("data-query") || "";
          var ok = true;
          if (p) {
            var plist = dPeople ? dPeople.split("|||") : [];
            if (plist.indexOf(p) === -1) ok = false;
          }
          if (a) {
            var alist = dAccounts ? dAccounts.split("|||") : [];
            if (alist.indexOf(a) === -1) ok = false;
          }
          if (q && dQuery !== q) ok = false;
          tr.classList.toggle("filtered-out", !ok);
        });
      }

      personEl.addEventListener("change", applyFilters);
      accountEl.addEventListener("change", applyFilters);
      queryEl.addEventListener("change", applyFilters);
    })();
  </script>
  {% endif %}
</body>
</html>
"""

COMPANY_LINKEDIN_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Company LinkedIn finder</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 960px; }
    .small { color: #666; font-size: 0.9em; }
    label { display: block; font-weight: 600; margin-bottom: 6px; margin-top: 14px; }
    input[type="text"], input[type="file"] { width: 100%; max-width: 480px; padding: 8px; box-sizing: border-box; }
    button { padding: 10px 14px; cursor: pointer; margin-top: 16px; }
    .msg { margin: 14px 0; padding: 10px; background: #f5f5f5; border: 1px solid #ddd; }
    .warn { background: #fff8e6; border-color: #e6c200; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border: 1px solid #ddd; text-align: left; padding: 8px; vertical-align: top; }
    th { background: #fafafa; }
    tr.miss td { background: #fff8f0; }
    .nav { margin-bottom: 20px; }
    #client-loading { display: none; margin-top: 16px; padding: 12px 14px; background: #e8f4fc; border: 1px solid #9cc7e8; border-radius: 4px; }
    fieldset[disabled] { opacity: 0.65; }
    fieldset[disabled] label { color: #555; }
  </style>
</head>
<body>
  <p class="nav"><a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a></p>
  <h2>Company LinkedIn finder</h2>
  <p class="small">Upload a CSV with a header column named <strong>Company</strong> (one company per row), <em>or</em> enter a single company name. For each company we query Serper with the company name plus <code>site:linkedin.com</code>, scan the first 10 organic URLs, and take the first link containing <code>linkedin.com/company/</code>.</p>
  <p class="small">Only <strong>one</strong> lookup runs on the server at a time. If another request is running, this form stays disabled until it finishes (refresh the page).</p>

  {% if server_busy %}
  <div class="msg warn"><strong>Request in progress.</strong> Another company LinkedIn lookup is running. Please wait for it to complete, then refresh this page.</div>
  {% endif %}

  <div id="client-loading" style="display: none;" aria-live="polite">
    <strong>Loading.</strong> Searching via Serper&mdash;do not close this tab. Inputs are disabled until results return.
  </div>

  <form method="post" enctype="multipart/form-data" id="company-linkedin-form">
    <fieldset id="company-linkedin-fieldset" {% if server_busy %}disabled{% endif %}>
    <label for="csv_file">CSV file (optional)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_company">Single company name (optional)</label>
    <input type="text" name="single_company" id="single_company" value="{{ single_company }}" placeholder="e.g. ASML">

    <div><button type="submit" id="company-linkedin-submit">Find LinkedIn URLs</button></div>
    </fieldset>
  </form>

  <script>
    (function () {
      var form = document.getElementById("company-linkedin-form");
      var loadEl = document.getElementById("client-loading");
      var serverBusy = {{ server_busy_js }};
      if (!form || !loadEl) return;
      if (serverBusy) return;
      form.addEventListener("submit", function () {
        loadEl.style.display = "block";
        setTimeout(function () {
          var csv = document.getElementById("csv_file");
          var single = document.getElementById("single_company");
          var btn = document.getElementById("company-linkedin-submit");
          if (csv) csv.disabled = true;
          if (single) single.disabled = true;
          if (btn) btn.disabled = true;
        }, 0);
      });
    })();
  </script>

  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}

  {% if download_url %}
  <p><a href="{{ download_url }}">Download results as CSV</a></p>
  {% endif %}

  {% if rows %}
  <table>
    <thead>
      <tr>
        <th>Company</th>
        <th>Search query</th>
        <th>LinkedIn company URL</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr class="{% if not row.linkedin_url %}miss{% endif %}">
        <td>{{ row.company }}</td>
        <td class="small"><code>{{ row.search_query }}</code></td>
        <td>{% if row.linkedin_url %}<a href="{{ row.linkedin_url }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url }}</a>{% else %}&mdash;{% endif %}</td>
        <td>{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>
"""


def parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def strip_query_prefix(query: str) -> str:
    return LINKEDIN_POSTS_PREFIX.sub("", query.strip())


def quoted_terms(query: str) -> tuple[str, str]:
    inner = strip_query_prefix(query)
    parts = re.findall(r'"([^"]*)"', inner)
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def facet_lists_for_query(pair_type: str, query: str) -> tuple[list[str], list[str]]:
    a, b = quoted_terms(query)
    if pair_type == "person_person":
        return ([x for x in (a, b) if x], [])
    if pair_type == "person_account":
        return ([a] if a else [], [b] if b else [])
    if pair_type == "account_account":
        return ([], [x for x in (a, b) if x])
    return [], []


def parse_date(item: dict) -> str:
    for key in ("date", "publishedDate", "publishedAt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    snippet = item.get("snippet")
    if isinstance(snippet, str):
        for pattern in DATE_PATTERNS:
            match = pattern.search(snippet)
            if match:
                return match.group(0)
    return ""


def parse_date_sort_value(date_str: str) -> datetime:
    if not date_str:
        return datetime.min

    text = date_str.strip()
    now = datetime.now()

    relative_match = re.search(
        r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago",
        text,
        re.IGNORECASE,
    )
    if relative_match:
        quantity = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        if unit == "minute":
            return now - timedelta(minutes=quantity)
        if unit == "hour":
            return now - timedelta(hours=quantity)
        if unit == "day":
            return now - timedelta(days=quantity)
        if unit == "week":
            return now - timedelta(weeks=quantity)
        if unit == "month":
            return now - timedelta(days=30 * quantity)
        if unit == "year":
            return now - timedelta(days=365 * quantity)

    cleaned = text.replace(".", "")
    formats = [
        "%Y-%m-%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return datetime.min


def get_default_box_inputs(pair_type: str) -> tuple[str, str]:
    if pair_type == "person_person":
        left = DEFAULT_PEOPLE[:1]
        right = DEFAULT_PEOPLE[1:] if len(DEFAULT_PEOPLE) > 1 else DEFAULT_PEOPLE[:1]
        return "\n".join(left), "\n".join(right)
    if pair_type == "account_account":
        left = DEFAULT_ACCOUNTS[:1]
        right = DEFAULT_ACCOUNTS[1:] if len(DEFAULT_ACCOUNTS) > 1 else DEFAULT_ACCOUNTS[:1]
        return "\n".join(left), "\n".join(right)
    return "\n".join(DEFAULT_PEOPLE), "\n".join(DEFAULT_ACCOUNTS)


def build_queries(pair_type: str, search_type: str, left_values: Iterable[str], right_values: Iterable[str]) -> list[str]:
    prefix = "site:linkedin.com/posts " if search_type == "linkedin_posts" else ""
    queries: list[str] = []
    seen: set[str] = set()

    for left in left_values:
        for right in right_values:
            left_term = left.strip()
            right_term = right.strip()
            if not left_term or not right_term:
                continue
            if left_term.casefold() == right_term.casefold():
                continue
            query = f'{prefix}"{left_term}" "{right_term}"'
            if query not in seen:
                seen.add(query)
                queries.append(query)

    return queries


def save_query_results(output_dir: Path, query: str, rows: list[dict], had_organic: bool) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{slugify(query)[:120]}.csv"
    file_path = output_dir / filename

    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["query", "heading", "link", "date", "status"],
        )
        writer.writeheader()
        if not had_organic:
            writer.writerow(
                {
                    "query": query,
                    "heading": "",
                    "link": "",
                    "date": "",
                    "status": "no_organic_results",
                }
            )
        else:
            for row in rows:
                writer.writerow({**row, "status": "ok"})

    return str(file_path)


COMPANY_LINKEDIN_OUTPUT = Path("data/company_linkedin")


def _company_csv_column_key(fieldnames: list[str] | None) -> str | None:
    if not fieldnames:
        return None
    for name in fieldnames:
        if name and name.strip().casefold() == "company":
            return name
    return None


def parse_companies_from_csv_upload(storage) -> tuple[list[str], str | None]:
    """
    Read uploaded CSV; require a column header ``Company`` (case-insensitive).
    Returns (non-empty company names in row order, error_message or None).
    """
    if storage is None or not getattr(storage, "filename", None):
        return [], None
    raw = storage.read()
    if not raw:
        return [], "Uploaded file is empty."
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], "Could not decode CSV as UTF-8."
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        return [], "CSV has no header row."
    key = _company_csv_column_key(list(reader.fieldnames))
    if not key:
        return [], "CSV must include a header column named Company."
    companies: list[str] = []
    for row in reader:
        raw_cell = row.get(key, "")
        cell = raw_cell.strip() if isinstance(raw_cell, str) else str(raw_cell or "").strip()
        if cell:
            companies.append(cell)
    return companies, None


def save_company_linkedin_results(rows: list[dict[str, str]]) -> str:
    """Write one CSV under data/company_linkedin; return absolute path string."""
    COMPANY_LINKEDIN_OUTPUT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = COMPANY_LINKEDIN_OUTPUT / f"{timestamp}_company_linkedin.csv"
    fieldnames = ["Company", "LinkedIn_URL", "Search_Query", "Status"]
    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Company": row["company"],
                    "LinkedIn_URL": row.get("linkedin_url") or "",
                    "Search_Query": row["search_query"],
                    "Status": row["status"],
                }
            )
    return str(file_path)


def _result_sort_key(row: dict) -> tuple:
    if row.get("is_empty"):
        return (2, row.get("query", ""))
    dt = parse_date_sort_value(row.get("date", ""))
    if dt == datetime.min:
        return (1, row.get("query", ""))
    try:
        return (0, -dt.timestamp(), row.get("query", ""))
    except (OSError, OverflowError, ValueError):
        return (1, row.get("query", ""))


@app.route("/download/company-linkedin/<path:filename>", methods=["GET"])
def download_company_linkedin(filename: str):
    output_root = Path("data/company_linkedin").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/download/<path:filename>", methods=["GET"])
def download_file(filename: str):
    output_root = Path("data/serper_dashboard").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/", methods=["GET", "POST"])
def dashboard() -> str:
    selected_pair_type = "person_person"
    selected_search_type = "web"
    left_input, right_input = get_default_box_inputs(selected_pair_type)
    num_results = "10"
    results: list[dict[str, str]] = []
    generated_files: list[dict[str, str | int]] = []
    message = ""
    no_result_queries: list[str] = []
    filter_people: list[str] = []
    filter_accounts: list[str] = []
    filter_queries: list[str] = []

    if request.method == "POST":
        selected_pair_type = request.form.get("pair_type", selected_pair_type)
        selected_search_type = request.form.get("search_type", selected_search_type)
        default_left, default_right = get_default_box_inputs(selected_pair_type)
        left_input = request.form.get("left_input", default_left)
        right_input = request.form.get("right_input", default_right)
        num_results = request.form.get("num_results", "10")

        left_values = parse_lines(left_input)
        right_values = parse_lines(right_input)

        try:
            per_query = max(1, min(int(num_results), 100))
        except ValueError:
            per_query = 10
            num_results = "10"

        if not settings.serper_api_key:
            message = "Missing SERPER_API_KEY in environment."
        elif selected_pair_type not in PAIR_OPTIONS:
            message = "Invalid pair type selected."
        elif selected_search_type not in SEARCH_TYPE_OPTIONS:
            message = "Invalid search type selected."
        else:
            queries = build_queries(selected_pair_type, selected_search_type, left_values, right_values)
            if not queries:
                message = "No queries were generated. Check your people/accounts inputs."
            else:
                output_root = Path("data/serper_dashboard")
                total_rows = 0
                file_paths_seen: set[str] = set()
                fp_set: set[str] = set()
                fa_set: set[str] = set()

                for query in queries:
                    items = search_serper(
                        query=query,
                        api_key=settings.serper_api_key,
                        num=per_query,
                        date_restrict=None,
                    )

                    query_rows: list[dict[str, str]] = []
                    for item in items:
                        link = item.get("link") if isinstance(item.get("link"), str) else ""
                        heading = item.get("title") if isinstance(item.get("title"), str) else ""
                        if not link:
                            continue
                        query_rows.append(
                            {
                                "query": query,
                                "heading": heading,
                                "link": link,
                                "date": parse_date(item),
                            }
                        )

                    had_organic = len(query_rows) > 0
                    if not had_organic:
                        no_result_queries.append(query)

                    saved_file = save_query_results(output_root, query, query_rows, had_organic)
                    saved_file_name = Path(saved_file).name
                    if saved_file not in file_paths_seen:
                        generated_files.append(
                            {
                                "name": saved_file_name,
                                "download_url": f"/download/{saved_file_name}",
                                "result_count": len(query_rows),
                            }
                        )
                        file_paths_seen.add(saved_file)

                    people_f, accounts_f = facet_lists_for_query(selected_pair_type, query)
                    for x in people_f:
                        fp_set.add(x)
                    for x in accounts_f:
                        fa_set.add(x)

                    data_people = "|||".join(people_f)
                    data_accounts = "|||".join(accounts_f)
                    data_query = query

                    if not had_organic:
                        results.append(
                            {
                                "query": query,
                                "heading": "(No organic results for this query)",
                                "link": "",
                                "date": "",
                                "file_path": saved_file,
                                "file_path_display": Path(saved_file).name,
                                "download_url": f"/download/{saved_file_name}",
                                "is_empty": True,
                                "data_people": data_people,
                                "data_accounts": data_accounts,
                                "data_query": data_query,
                            }
                        )
                    else:
                        for row in query_rows:
                            results.append(
                                {
                                    **row,
                                    "file_path": saved_file,
                                    "file_path_display": Path(saved_file).name,
                                    "download_url": f"/download/{saved_file_name}",
                                    "is_empty": False,
                                    "data_people": data_people,
                                    "data_accounts": data_accounts,
                                    "data_query": data_query,
                                }
                            )
                        total_rows += len(query_rows)

                results.sort(key=_result_sort_key)

                filter_people = sorted(fp_set, key=str.lower)
                filter_accounts = sorted(fa_set, key=str.lower)
                filter_queries = list(queries)

                message = (
                    f"Ran {len(queries)} queries, {len(no_result_queries)} with no organic results, "
                    f"{total_rows} total hits. Files saved under data/serper_dashboard/."
                )

    return render_template_string(
        HTML_TEMPLATE,
        pair_options=PAIR_OPTIONS,
        search_type_options=SEARCH_TYPE_OPTIONS,
        selected_pair_type=selected_pair_type,
        selected_search_type=selected_search_type,
        left_input=left_input,
        right_input=right_input,
        left_input_label=PAIR_INPUT_CONFIG.get(selected_pair_type, PAIR_INPUT_CONFIG["person_account"])["left_label"],
        right_input_label=PAIR_INPUT_CONFIG.get(selected_pair_type, PAIR_INPUT_CONFIG["person_account"])["right_label"],
        left_input_placeholder=PAIR_INPUT_CONFIG.get(selected_pair_type, PAIR_INPUT_CONFIG["person_account"])["left_placeholder"],
        right_input_placeholder=PAIR_INPUT_CONFIG.get(selected_pair_type, PAIR_INPUT_CONFIG["person_account"])["right_placeholder"],
        pair_input_config_json=json.dumps(PAIR_INPUT_CONFIG),
        num_results=num_results,
        results=results,
        generated_files=generated_files,
        message=message,
        no_result_queries=no_result_queries,
        filter_people=filter_people,
        filter_accounts=filter_accounts,
        filter_queries=filter_queries,
    )


@app.route("/company-linkedin", methods=["GET", "POST"])
def company_linkedin_finder() -> str:
    single_company = ""
    rows: list[dict[str, str]] = []
    message = ""
    message_warn = False
    download_url = ""

    if request.method == "POST":
        single_company = request.form.get("single_company", "") or ""
        upload = request.files.get("csv_file")
        companies: list[str] = []
        csv_err: str | None = None

        if upload is not None and bool(upload.filename):
            companies, csv_err = parse_companies_from_csv_upload(upload)

        if csv_err:
            message = csv_err
            message_warn = True
        elif companies:
            pass
        elif single_company.strip():
            companies = [single_company.strip()]
        else:
            message = "Upload a CSV with a Company column, or enter one company name."
            message_warn = True

        if not message and not settings.serper_api_key:
            message = "Missing SERPER_API_KEY in environment."
            message_warn = True

        if not message and companies:
            acquired = _company_linkedin_worker_lock.acquire(blocking=False)
            if not acquired:
                message = (
                    "Another company LinkedIn lookup is already in progress on the server. "
                    "Please wait until it finishes, then refresh this page and try again."
                )
                message_warn = True
            else:
                try:
                    rows_run: list[dict[str, str]] = []
                    for name in companies:
                        search_query = f"{name} site:linkedin.com"
                        found_url = find_linkedin_company_url(
                            name,
                            settings.serper_api_key,
                            num=10,
                            date_restrict=None,
                        )
                        if found_url:
                            rows_run.append(
                                {
                                    "company": name,
                                    "search_query": search_query,
                                    "linkedin_url": found_url,
                                    "status": "found",
                                }
                            )
                        else:
                            rows_run.append(
                                {
                                    "company": name,
                                    "search_query": search_query,
                                    "linkedin_url": "",
                                    "status": "no_company_page_in_top_10",
                                }
                            )
                    rows = rows_run
                    saved_path = save_company_linkedin_results(rows)
                    download_url = url_for("download_company_linkedin", filename=Path(saved_path).name)
                    n = len(rows)
                    found_ct = sum(1 for r in rows if r.get("linkedin_url"))
                    message = (
                        f"Processed {n} compan{'ies' if n != 1 else 'y'}; "
                        f"{found_ct} LinkedIn company URL{'s' if found_ct != 1 else ''} found in the first 10 results."
                    )
                    message_warn = found_ct < n
                finally:
                    _company_linkedin_worker_lock.release()

    server_busy = _company_linkedin_worker_lock.locked()
    server_busy_js = "true" if server_busy else "false"

    return render_template_string(
        COMPANY_LINKEDIN_TEMPLATE,
        single_company=single_company,
        rows=rows,
        message=message,
        message_warn=message_warn,
        download_url=download_url,
        server_busy=server_busy,
        server_busy_js=server_busy_js,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5055"))
    app.run(host="0.0.0.0", port=port, debug=False)
