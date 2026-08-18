"""Local dashboard to run Serper pair searches and view results."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Iterable

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)

from config import settings
from email_enrichment_jobs import (
    get_job_public_status,
    resume_pending_jobs_on_startup,
    retry_email_job,
    submit_email_enrichment_job,
)
from email_enrichment_store import count_pending_jobs, write_results_csv
from email_provider import EmailEnrichmentError, email_providers_configured, enrich_contacts
from fullenrich_client import FullEnrichError, is_valid_linkedin_url
from molster_client import MolsterError
from seeqe_email_callback import post_email_to_seeqe
from linkedin_jobs import (
    is_email_job_running,
    is_rapidapi_job_running,
    is_serper_job_running,
    job_snapshot,
    progress_display,
    release_email_worker,
    release_rapidapi_worker,
    release_serper_worker,
    start_company_enrich_job,
    start_company_job,
    start_person_job,
    start_urn_resolve_job,
    start_vendor_file_graph_job,
    start_vendor_file_job,
    try_acquire_email_worker,
    try_acquire_rapidapi_worker,
    try_acquire_serper_worker,
    validate_email,
)
from mailer import smtp_configured
from rapidapi_linkedin_company import (
    extract_numeric_company_id,
    is_valid_linkedin_company_url,
    lookup_company,
)
from rapidapi_person_deep import normalize_linkedin_profile_url, resolve_vanity_url
from person_linkedin_finder import find_person_linkedin
from vendor_file.graph import graph_configured
from vendor_file.graph_pipeline import new_graph_request_id
from vendor_file.pipeline import contact_need_flags, new_request_id, parse_input_csv

app = Flask(__name__)
logger = logging.getLogger(__name__)

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
  <p>
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a> &mdash; CSV or single company &rarr; company LinkedIn page.<br>
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a> &mdash; CSV or single person + company &rarr; person LinkedIn profile.<br>
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a> &mdash; CSV or single URN profile URL &rarr; vanity LinkedIn URL via RapidAPI.<br>
    <a href="{{ url_for('email_finder') }}">Email finder</a> &mdash; CSV or single person + LinkedIn URL &rarr; work email via Molster, with FullEnrich fallback.<br>
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a> &mdash; CSV with company name + LinkedIn URL &rarr; employee count and numeric LinkedIn ID via RapidAPI.<br>
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a> &mdash; CSV of stakeholders + your email &rarr; vendor-ready email/phone request file via RapidAPI, emailed when done.<br>
    <a href="{{ url_for('vendor_file_graph_finder') }}">Vendor email file (graph)</a> &mdash; same CSV and 27 columns, filled only from Seeqe Postgres (no RapidAPI).
  </p>
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

LINKEDIN_FINDER_STYLES = """
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 960px; }
    .small { color: #666; font-size: 0.9em; }
    label { display: block; font-weight: 600; margin-bottom: 6px; margin-top: 14px; }
    input[type="text"], input[type="email"], input[type="file"] { width: 100%; max-width: 480px; padding: 8px; box-sizing: border-box; }
    button { padding: 10px 14px; cursor: pointer; margin-top: 16px; }
    .msg { margin: 14px 0; padding: 10px; background: #f5f5f5; border: 1px solid #ddd; }
    .warn { background: #fff8e6; border-color: #e6c200; }
    .ok { background: #eef8ee; border-color: #9c9; }
    .nav { margin-bottom: 20px; }
    fieldset[disabled] { opacity: 0.65; }
    fieldset[disabled] label { color: #555; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border: 1px solid #ddd; text-align: left; padding: 8px; vertical-align: top; }
    th { background: #fafafa; }
    tr.miss td { background: #fff8f0; }
"""

COMPANY_RESULTS_TABLE = """
  {% if rows %}
  <h3>Result</h3>
  <table>
    <thead>
      <tr><th>Company</th><th>Search query</th><th>LinkedIn company URL</th><th>Status</th></tr>
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
"""

PERSON_RESULTS_TABLE = """
  {% if rows %}
  <h3>Result</h3>
  <table>
    <thead>
      <tr><th>Person</th><th>Company</th><th>Search query</th><th>LinkedIn profile URL</th><th>Status</th></tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr class="{% if not row.linkedin_url %}miss{% endif %}">
        <td>{{ row.person }}</td>
        <td>{{ row.company }}</td>
        <td class="small"><code>{{ row.search_query }}</code></td>
        <td>{% if row.linkedin_url %}<a href="{{ row.linkedin_url }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url }}</a>{% else %}&mdash;{% endif %}</td>
        <td>{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
"""

EMAIL_RESULTS_TABLE = """
  {% if rows %}
  <h3>Result</h3>
  <table>
    <thead>
      <tr><th>Person</th><th>Company</th><th>LinkedIn URL</th><th>Work email</th><th>Email status</th><th>Source</th><th>All work emails</th><th>Job title</th><th>Status</th></tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr class="{% if not row.work_email %}miss{% endif %}">
        <td>{{ row.person }}</td>
        <td>{{ row.company or "—" }}</td>
        <td><a href="{{ row.linkedin_url }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url }}</a></td>
        <td>{{ row.work_email or "—" }}</td>
        <td>{{ row.email_status or "—" }}</td>
        <td>{{ row.email_source or "—" }}</td>
        <td class="small">{{ row.all_work_emails or "—" }}</td>
        <td>{{ row.job_title or "—" }}</td>
        <td>{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
"""

URN_RESOLVE_RESULTS_TABLE = """
  {% if rows %}
  <h3>Result</h3>
  <table>
    <thead>
      <tr><th>LinkedIn URL (input)</th><th>LinkedIn_URL_Resolved</th><th>Status</th></tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr class="{% if not row.linkedin_url_resolved %}miss{% endif %}">
        <td><a href="{{ row.linkedin_url }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url }}</a></td>
        <td>{% if row.linkedin_url_resolved %}<a href="{{ row.linkedin_url_resolved }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url_resolved }}</a>{% else %}&mdash;{% endif %}</td>
        <td>{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
"""

COMPANY_ENRICH_RESULTS_TABLE = """
  {% if rows %}
  <h3>Result</h3>
  <table>
    <thead>
      <tr><th>Company</th><th>LinkedIn URL</th><th>Employee count</th><th>LinkedIn company ID</th><th>Status</th></tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr class="{% if not row.employee_count %}miss{% endif %}">
        <td>{{ row.company or "—" }}</td>
        <td>{% if row.linkedin_url %}<a href="{{ row.linkedin_url }}" target="_blank" rel="noopener noreferrer">{{ row.linkedin_url }}</a>{% else %}—{% endif %}</td>
        <td>{{ row.employee_count or "—" }}</td>
        <td>{{ row.linkedin_company_id or "—" }}</td>
        <td>{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
"""

LINKEDIN_PROGRESS_BLOCK = """
  <div id="shared-job-progress" class="msg warn" style="{% if prog.job_running %}display:block{% else %}display:none{% endif %}" aria-live="polite">
    <strong>{{ prog.progress_note }}</strong>
    <p id="shared-job-progress-line" style="margin:8px 0 0 0;">{{ prog.progress_line }}</p>
    {% if prog.job_email_masked %}
    <p class="small" style="margin:4px 0 0 0;">Results will be emailed to {{ prog.job_email_masked }}</p>
    {% endif %}
  </div>
  {% if not prog.job_running and prog.last_summary %}
  <div class="msg ok"><strong>Last job completed.</strong> {{ prog.last_summary }}
    {% if prog.last_error %}<br><span class="small">Note: {{ prog.last_error }}</span>{% endif %}
  </div>
  {% endif %}
"""

LINKEDIN_PROGRESS_POLL_SCRIPT = """
  <script>
    (function () {
      var poll = {{ poll_js }};
      if (!poll) return;
      var box = document.getElementById("shared-job-progress");
      var line = document.getElementById("shared-job-progress-line");
      var fieldset = document.querySelector("form fieldset");
      function setDisabled(d) { if (fieldset) fieldset.disabled = d; }
      function tick() {
        fetch("{{ url_for('linkedin_finder_status', scope=prog.scope) }}", { cache: "no-store" })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.running) {
              if (box) box.style.display = "block";
              if (line) line.textContent = data.progress_line || ("Processing " + data.current + " / " + data.total);
              setDisabled(true);
              setTimeout(tick, 2500);
            } else {
              window.location.reload();
            }
          })
          .catch(function () { setTimeout(tick, 4000); });
      }
      setDisabled(true);
      tick();
    })();
  </script>
"""

COMPANY_LINKEDIN_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Company LinkedIn finder</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """</style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('email_finder') }}">Email finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a>
  </p>
  <h2>Company LinkedIn finder</h2>
  <p class="small">One company name: result appears on this page immediately (no email). CSV with <strong>2+ companies</strong>: email required; results are sent when the job finishes. Only <strong>one</strong> job at a time (company or person).</p>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{{ url_for('company_linkedin_finder') }}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required only for CSV with multiple companies)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com">

    <label for="csv_file">CSV file (optional)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_company">Single company name (optional)</label>
    <input type="text" name="single_company" id="single_company" placeholder="e.g. ASML">

    <div><button type="submit">Find LinkedIn URLs</button></div>
    </fieldset>
  </form>
"""
    + COMPANY_RESULTS_TABLE
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

PERSON_LINKEDIN_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Person LinkedIn finder</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """</style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('email_finder') }}">Email finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a>
  </p>
  <h2>Person LinkedIn finder</h2>
  <p class="small">One person + company: result on this page (no email). CSV with <strong>2+ rows</strong>: email required; results emailed when done. Only <strong>one</strong> job at a time.</p>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{{ url_for('person_linkedin_finder') }}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required only for CSV with multiple rows)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com">

    <label for="csv_file">CSV file (optional)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_person">Person name (optional)</label>
    <input type="text" name="single_person" id="single_person" placeholder="e.g. Juan Gomez-Sanchez">

    <label for="single_company">Company name (optional)</label>
    <input type="text" name="single_company" id="single_company" placeholder="e.g. Standard Chartered">

    <div><button type="submit">Find LinkedIn URLs</button></div>
    </fieldset>
  </form>
"""
    + PERSON_RESULTS_TABLE
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

EMAIL_FINDER_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Email finder</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """
    pre.example { background: #f7f7f7; border: 1px solid #ddd; padding: 12px; overflow-x: auto; font-size: 0.85em; }
    .csv-spec { margin: 16px 0; padding: 12px; background: #f9f9f9; border: 1px solid #e0e0e0; }
    .csv-spec h3 { margin: 0 0 10px 0; font-size: 1em; }
    .csv-spec table { margin-top: 8px; font-size: 0.9em; }
    .csv-spec th { width: 28%; }
    .req { color: #a33; font-weight: 600; }
    .opt { color: #666; }
    </style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a>
  </p>
  <h2>Email finder (Molster → FullEnrich)</h2>
  <p class="small">Find work emails from LinkedIn URLs. Each row is looked up in Molster first (batches of 100; ~5k emails / 5 hours), then misses fall back to FullEnrich. One person: result on this page. CSV upload: enter your email and submit — we queue the job, process in resumable batches, and email the CSV when done. Jobs survive restarts and resume from the last checkpoint.</p>
  <p class="small"><strong>CSV limit:</strong> upload <strong>at most 500 records</strong>. Files with more than 500 data rows are rejected — split the list and submit separate jobs.</p>

  <div class="csv-spec">
    <h3>Expected CSV column names</h3>
    <p class="small">Header names are <strong>case-insensitive</strong>. Use one accepted name per role below. Any extra columns you include are kept unchanged in the results file.</p>
    <table>
      <thead>
        <tr><th>Role</th><th>Required?</th><th>Accepted header names (use one)</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>Person name</td>
          <td class="req">Required</td>
          <td><code>Person</code>, <code>Person Name</code>, <code>Name</code>, <code>Full Name</code>, <code>Contact Name</code></td>
        </tr>
        <tr>
          <td>LinkedIn profile URL</td>
          <td class="req">Required</td>
          <td><code>LinkedIn_URL</code>, <code>LinkedIn URL</code>, <code>LinkedIn</code>, <code>LinkedIn Profile URL</code>, <code>Profile_URL</code>, <code>Profile URL</code></td>
        </tr>
        <tr>
          <td>Company name</td>
          <td class="opt">Optional</td>
          <td><code>Company</code>, <code>Company Name</code>, <code>Account</code>, <code>Account Name</code>, <code>Organization</code>, <code>Employer</code>, <code>Current Company</code></td>
        </tr>
        <tr>
          <td>Other columns</td>
          <td class="opt">Optional</td>
          <td>Any other headers (e.g. <code>Domain</code>, <code>Cohort</code>, <code>Email</code>) — passed through to the output as-is</td>
        </tr>
      </tbody>
    </table>
    <p class="small" style="margin-top:12px;"><strong>Results file:</strong> your original columns first, then these appended columns: <code>Work_Email</code>, <code>Email_Status</code>, <code>All_Work_Emails</code>, <code>Job_Title</code>, <code>Enrichment_Status</code>, <code>Email_Source</code>, <code>Molster_Risk_Score</code>, <code>Molster_Last_Validated_At</code>.</p>
  </div>

  <p class="small"><strong>Example CSV:</strong></p>
  <pre class="example">Person,Company,LinkedIn_URL
Jane Doe,Acme Inc,https://www.linkedin.com/in/jane-doe/
John Smith,Vistra,https://www.linkedin.com/in/john-smith/</pre>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{{ url_for('email_finder') }}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required only for CSV with multiple rows)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com">

    <label for="csv_file">CSV file (optional, max 500 records)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_person">Person name (optional)</label>
    <input type="text" name="single_person" id="single_person" placeholder="e.g. Jane Doe">

    <label for="single_company">Company name (optional)</label>
    <input type="text" name="single_company" id="single_company" placeholder="e.g. Acme Inc">

    <label for="single_linkedin_url">LinkedIn profile URL (required for single lookup)</label>
    <input type="url" name="single_linkedin_url" id="single_linkedin_url" placeholder="https://www.linkedin.com/in/...">

    <div><button type="submit">Find verified work email</button></div>
    </fieldset>
  </form>
"""
    + EMAIL_RESULTS_TABLE
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

URN_RESOLVE_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>LinkedIn URN resolver</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """
    pre.example { background: #f7f7f7; border: 1px solid #ddd; padding: 12px; overflow-x: auto; font-size: 0.85em; }
    .csv-spec { margin: 16px 0; padding: 12px; background: #f9f9f9; border: 1px solid #e0e0e0; }
    .csv-spec h3 { margin: 0 0 10px 0; font-size: 1em; }
    .csv-spec table { margin-top: 8px; font-size: 0.9em; }
    .csv-spec th { width: 28%; }
    .req { color: #a33; font-weight: 600; }
    .opt { color: #666; }
    </style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('email_finder') }}">Email finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a>
  </p>
  <h2>LinkedIn URN resolver</h2>
  <p class="small">Convert opaque LinkedIn member URLs (URN-style <code>/in/ACwAAA...</code>) to normal vanity profile URLs via RapidAPI <code>person_deep</code> (not Serper). One URL: result on this page. CSV with <strong>2+ rows</strong>: email required; results emailed when done. Only <strong>one</strong> RapidAPI job at a time (this page shares the lock with company employee count and vendor email file).</p>

  <div class="csv-spec">
    <h3>Expected CSV column names</h3>
    <p class="small">Header names are <strong>case-insensitive</strong>. Any extra columns are kept unchanged in the results file.</p>
    <table>
      <thead>
        <tr><th>Role</th><th>Required?</th><th>Accepted header names (use one)</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>LinkedIn profile URL (URN or full URL)</td>
          <td class="req">Required</td>
          <td><code>LinkedIn_URL</code>, <code>LinkedIn URL</code>, <code>LinkedIn</code>, <code>li_url</code>, <code>Linkedin Bio</code>, <code>Profile URL</code>, <code>profileUrl</code></td>
        </tr>
        <tr>
          <td>Other columns</td>
          <td class="opt">Optional</td>
          <td>Any other headers — passed through to the output as-is</td>
        </tr>
      </tbody>
    </table>
    <p class="small" style="margin-top:12px;"><strong>Results file:</strong> your original columns first, then <code>LinkedIn_URL_Resolved</code> and <code>Resolve_Status</code>.</p>
  </div>

  <p class="small"><strong>Example CSV:</strong></p>
  <pre class="example">LinkedIn_URL,Cohort
https://www.linkedin.com/in/ACwAAAD2vwIBIoSEhsYMRkMjeC5cZgXDzQBQ4TQ,west
ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58,east</pre>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{{ url_for('urn_resolve_finder') }}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required only for CSV with multiple rows)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com">

    <label for="csv_file">CSV file (optional)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_linkedin_url">Single LinkedIn profile URL or URN (optional)</label>
    <input type="text" name="single_linkedin_url" id="single_linkedin_url" placeholder="https://www.linkedin.com/in/ACwAAA... or ACwAAA...">

    <div><button type="submit">Resolve LinkedIn URLs</button></div>
    </fieldset>
  </form>
"""
    + URN_RESOLVE_RESULTS_TABLE
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

COMPANY_ENRICH_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Company employee count</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """
    pre.example { background: #f7f7f7; border: 1px solid #ddd; padding: 12px; overflow-x: auto; font-size: 0.85em; }
    .csv-spec { margin: 16px 0; padding: 12px; background: #f9f9f9; border: 1px solid #e0e0e0; }
    .csv-spec h3 { margin: 0 0 10px 0; font-size: 1em; }
    .csv-spec table { margin-top: 8px; font-size: 0.9em; }
    .csv-spec th { width: 28%; }
    .req { color: #a33; font-weight: 600; }
    .opt { color: #666; }
    </style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('email_finder') }}">Email finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a>
  </p>
  <h2>Company employee count</h2>
  <p class="small">Look up LinkedIn <strong>employee count</strong> and the <strong>numeric company ID</strong> (org slug) via RapidAPI <code>/company</code>. One company: result on this page. CSV with <strong>2+ rows</strong>: email required; results emailed when done. Only <strong>one</strong> RapidAPI job at a time (this page shares the lock with the URN resolver and vendor email file).</p>
  <p class="small"><strong>CSV limit:</strong> upload <strong>at most 500 records</strong>.</p>

  <div class="csv-spec">
    <h3>Expected CSV column names</h3>
    <p class="small">Header names are <strong>case-insensitive</strong>. Any extra columns are kept unchanged in the results file.</p>
    <table>
      <thead>
        <tr><th>Role</th><th>Required?</th><th>Accepted header names (use one)</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>Company name</td>
          <td class="req">Required</td>
          <td><code>Company</code>, <code>Company Name</code>, <code>Account</code>, <code>Account Name</code>, <code>Organization</code></td>
        </tr>
        <tr>
          <td>LinkedIn company URL</td>
          <td class="req">Required</td>
          <td><code>LinkedIn_URL</code>, <code>LinkedIn URL</code>, <code>LinkedIn</code>, <code>Company LinkedIn</code>, <code>Company URL</code></td>
        </tr>
        <tr>
          <td>Numeric LinkedIn ID slug</td>
          <td class="opt">Optional</td>
          <td><code>LinkedIn_Company_ID</code>, <code>LinkedIn Company ID</code>, <code>companyId</code>, <code>Company ID</code>, <code>org_id</code>. Also detected when the URL is already <code>/company/1035/</code>.</td>
        </tr>
      </tbody>
    </table>
    <p class="small" style="margin-top:12px;"><strong>Results file:</strong> your original columns first, then <code>Employee_Count</code>, <code>LinkedIn_Company_ID</code>, and <code>Company_Enrich_Status</code>. If a row already has a numeric ID slug, that value is copied into <code>LinkedIn_Company_ID</code>.</p>
  </div>

  <p class="small"><strong>Example CSV:</strong></p>
  <pre class="example">Company,LinkedIn_URL
Microsoft,https://www.linkedin.com/company/microsoft/
IBM,https://www.linkedin.com/company/1035/</pre>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{{ url_for('company_enrich_finder') }}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required for CSV with multiple rows)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com">

    <label for="csv_file">CSV file (optional, max 500 records)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv">

    <label for="single_company">Company name (optional)</label>
    <input type="text" name="single_company" id="single_company" placeholder="e.g. Microsoft">

    <label for="single_linkedin_url">LinkedIn company URL (required for single lookup)</label>
    <input type="text" name="single_linkedin_url" id="single_linkedin_url" placeholder="https://www.linkedin.com/company/...">

    <div><button type="submit">Lookup employee count and LinkedIn ID</button></div>
    </fieldset>
  </form>
"""
    + COMPANY_ENRICH_RESULTS_TABLE
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

VENDOR_FILE_TEMPLATE = (
    """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{% if graph_mode %}Vendor email file (graph){% else %}Vendor email file{% endif %}</title>
  <style>"""
    + LINKEDIN_FINDER_STYLES
    + """
    pre.example { background: #f7f7f7; border: 1px solid #ddd; padding: 12px; overflow-x: auto; font-size: 0.85em; }
    .csv-spec { margin: 16px 0; padding: 12px; background: #f9f9f9; border: 1px solid #e0e0e0; }
    .csv-spec h3 { margin: 0 0 10px 0; font-size: 1em; }
    .csv-spec table { margin-top: 8px; font-size: 0.9em; }
    .csv-spec th { width: 28%; }
    .req { color: #a33; font-weight: 600; }
    .opt { color: #666; }
    .need-options { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 6px; }
    .need-options input[type="radio"] {
      position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none;
    }
    .need-options label.need {
      display: inline-block; margin: 0; padding: 10px 18px;
      border: 1px solid #ccc; border-radius: 6px; cursor: pointer;
      font-weight: 600; background: #fff; user-select: none;
    }
    .need-options input[type="radio"]:focus-visible + label.need {
      outline: 2px solid #1a73e8; outline-offset: 2px;
    }
    .need-options input[type="radio"]:checked + label.need {
      border-color: #1a73e8; background: #e8f0fe; color: #174ea6;
      box-shadow: inset 0 0 0 1px #1a73e8;
    }
    </style>
</head>
<body>
  <p class="nav">
    <a href="{{ url_for('dashboard') }}">&larr; Serper Pair Search Dashboard</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('email_finder') }}">Email finder</a>
    &nbsp;|&nbsp;
    <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a>
    {% if graph_mode %}
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_finder') }}">Vendor email file (RapidAPI)</a>
    {% else %}
    &nbsp;|&nbsp;
    <a href="{{ url_for('vendor_file_graph_finder') }}">Vendor email file (graph)</a>
    {% endif %}
  </p>
  {% if graph_mode %}
  <h2>Vendor email file (graph)</h2>
  <p class="small">Same stakeholder CSV and 27 vendor columns as RapidAPI, filled only from Seeqe Postgres. Person, experience, company, email_domains, and historical employee count come from the graph. No RapidAPI calls. Email required; results are emailed when done. Shares the RapidAPI job lock so the two vendor workflows cannot run at the same time.</p>
  <p class="small"><strong>CSV limit:</strong> upload <strong>at most 500 records</strong>.</p>
  <p class="small">UID prefix is <code>VNG-</code>. Last Profile Refresh Date is <code>MAX(experience.updated_at)</code>. Company website is the first clean domain in <code>company.email_domains</code> (not <code>website_url</code>). Historical headcount is the graph year matching the target start year (19xx/20xx only). If they have left the target, board/advisor present roles are skipped when another present employer exists; if board/advisor is the only current role, it is kept.</p>
  {% else %}
  <h2>Vendor email file</h2>
  <p class="small">Turn a stakeholder CSV into the vendor email/phone request file. RapidAPI fills names, titles, and company fields from the <strong>target</strong> company (not assumed current employer). The Seeqe graph fills <strong>Stakeholder / Target / Current Company Vieu IDs</strong> when the LinkedIn URL is already in the graph. Email required; results are emailed when done. Only <strong>one</strong> RapidAPI job at a time (shares the lock with the URN resolver and company employee count).</p>
  <p class="small"><strong>CSV limit:</strong> upload <strong>at most 500 records</strong>.</p>
  <p class="small">Sales Nav <strong>lead</strong> URLs cannot be converted. Use <code>/in/{slug}</code> for people and <code>/company/{slug}</code> for companies. Historical headcount at start date is left blank. Vieu IDs that are not in the graph stay blank.</p>
  {% endif %}

  <div class="csv-spec">
    <h3>Expected CSV column names</h3>
    <p class="small">Header names are <strong>case-insensitive</strong>. Extra columns are ignored.</p>
    <table>
      <thead>
        <tr><th>Role</th><th>Required?</th><th>Accepted header names (use one)</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>Stakeholder name</td>
          <td class="req">Required</td>
          <td><code>Stakeholder Name</code>, <code>Name</code>, <code>Full Name</code>, <code>Person Name</code>, <code>Contact Name</code></td>
        </tr>
        <tr>
          <td>Person LinkedIn</td>
          <td class="req">Required</td>
          <td><code>Profile Linkedin</code>, <code>Person LinkedIn</code>, <code>LinkedIn</code>, <code>LinkedIn URL</code>, <code>Profile URL</code></td>
        </tr>
        <tr>
          <td>Target company name</td>
          <td class="req">Required</td>
          <td><code>Target Company Name</code>, <code>Company Name</code>, <code>Company</code>, <code>Account Name</code></td>
        </tr>
        <tr>
          <td>Target company LinkedIn</td>
          <td class="req">Required</td>
          <td><code>Target Company Linkedin</code>, <code>Company LinkedIn</code>, <code>Company LinkedIn URL</code>, <code>Account LinkedIn</code></td>
        </tr>
        <tr>
          <td>Email / phone required</td>
          <td class="opt">Set on this page</td>
          <td>Use the Email / Phone / Both buttons below. Optional CSV columns <code>Email required</code> and <code>Phone required</code> can still override a single row.</td>
        </tr>
      </tbody>
    </table>
    <p class="small" style="margin-top:12px;"><strong>Emailed files:</strong> <code>{UID}_vendor.csv</code> (send this to the vendor), plus <code>{UID}_rejects.csv</code> and <code>{UID}_qa.csv</code> when they have rows. One UID per upload, same value on every vendor row.</p>
  </div>

  <p class="small"><strong>Example CSV:</strong></p>
  <pre class="example">Stakeholder Name,Profile Linkedin,Target Company Name,Target Company Linkedin
Jane Doe,https://www.linkedin.com/in/jane-doe/,Acme Inc,https://www.linkedin.com/company/acme
José García,https://www.linkedin.com/in/jose-garcia/,Walmart,https://www.linkedin.com/company/walmart</pre>
"""
    + LINKEDIN_PROGRESS_BLOCK
    + """
  {% if message %}
  <div class="msg {% if message_warn %}warn{% endif %}">{{ message }}</div>
  {% endif %}
  <form method="post" enctype="multipart/form-data" action="{% if graph_mode %}{{ url_for('vendor_file_graph_finder') }}{% else %}{{ url_for('vendor_file_finder') }}{% endif %}">
    <fieldset {% if prog.server_busy %}disabled{% endif %}>
    <label for="email">Your email (required)</label>
    <input type="email" name="email" id="email" placeholder="you@company.com" required>

    <label for="csv_file">CSV file (required, max 500 records)</label>
    <input type="file" name="csv_file" id="csv_file" accept=".csv,text/csv" required>

    <label>Need from vendor</label>
    <div class="need-options" role="radiogroup" aria-label="Need from vendor">
      <input type="radio" name="contact_need" id="need_email" value="email"{% if contact_need == 'email' %} checked{% endif %}>
      <label class="need" for="need_email">Email</label>
      <input type="radio" name="contact_need" id="need_phone" value="phone"{% if contact_need == 'phone' %} checked{% endif %}>
      <label class="need" for="need_phone">Phone</label>
      <input type="radio" name="contact_need" id="need_both" value="both"{% if contact_need != 'email' and contact_need != 'phone' %} checked{% endif %}>
      <label class="need" for="need_both">Both</label>
    </div>
    <p class="small">Sets <code>Email required</code> and <code>Phone required</code> on every vendor row.</p>

    <div><button type="submit">Build vendor file</button></div>
    </fieldset>
  </form>
"""
    + LINKEDIN_PROGRESS_POLL_SCRIPT
    + """
</body>
</html>
"""
)

THANK_YOU_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Request received</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 720px; }
    .msg { padding: 16px; background: #eef8ee; border: 1px solid #9c9; }
    .small { color: #666; font-size: 0.9em; }
    code { background: #f4f4f4; padding: 2px 6px; }
  </style>
</head>
<body>
  <h2>Thank you</h2>
  <div class="msg">
    <p>We received your <strong>{{ job_label }}</strong> request.</p>
    {% if job_id %}
    <p>Job ID: <code>{{ job_id }}</code>{% if queue_position and queue_position > 1 %} — queued (position {{ queue_position }}){% endif %}</p>
    {% endif %}
    <p>Once processing is complete, we will email the results CSV to <strong>{{ email }}</strong>.</p>
    <p class="small">You can close this tab. Jobs are saved to disk and resume automatically if the server restarts. Large cohorts may take hours; you do not need to resubmit.</p>
    {% if job_id %}
    <p class="small"><a href="{{ url_for('email_job_status', job_id=job_id) }}">Check job status</a></p>
    {% endif %}
  </div>
  <p><a href="{{ url_for('company_linkedin_finder') }}">Company LinkedIn finder</a> &nbsp;|&nbsp;
     <a href="{{ url_for('person_linkedin_finder') }}">Person LinkedIn finder</a> &nbsp;|&nbsp;
     <a href="{{ url_for('urn_resolve_finder') }}">LinkedIn URN resolver</a> &nbsp;|&nbsp;
     <a href="{{ url_for('email_finder') }}">Email finder</a> &nbsp;|&nbsp;
     <a href="{{ url_for('company_enrich_finder') }}">Company employee count</a> &nbsp;|&nbsp;
     <a href="{{ url_for('vendor_file_finder') }}">Vendor email file</a> &nbsp;|&nbsp;
     <a href="{{ url_for('vendor_file_graph_finder') }}">Vendor email file (graph)</a></p>
</body>
</html>
"""

EMAIL_JOB_STATUS_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Email job status</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 720px; }
    .msg { padding: 16px; background: #f5f5f5; border: 1px solid #ddd; }
    .small { color: #666; font-size: 0.9em; }
    code { background: #f4f4f4; padding: 2px 6px; }
  </style>
</head>
<body>
  <h2>Email enrichment job</h2>
  <div class="msg">
    <p><strong>Job ID:</strong> <code>{{ job.job_id }}</code></p>
    <p><strong>Status:</strong> {{ job.status }}</p>
    <p><strong>Progress:</strong> {{ job.processed }} / {{ job.total }} contacts</p>
    <p><strong>Results email:</strong> {{ job.recipient_email }}</p>
    {% if job.status == 'completed' and job.email_sent %}
    <p class="small"><strong>Email sent:</strong> yes — check inbox (and spam) for the results CSV.</p>
    {% elif job.status == 'completed' and not job.email_sent %}
    <p class="small warn"><strong>Email not sent.</strong> {% if job.error %}{{ job.error }}{% else %}SMTP delivery failed.{% endif %}</p>
    {% elif job.error %}
    <p class="small"><strong>Note:</strong> {{ job.error }}</p>
    {% endif %}
    {% if job.summary %}
    <p class="small">{{ job.summary }}</p>
    {% endif %}
    <p class="small">Updated: {{ job.updated_at }}</p>
    {% if job.status in ['failed', 'interrupted'] %}
    <form method="post" action="{{ url_for('email_job_retry', job_id=job.job_id) }}" style="margin-top:16px;">
      <button type="submit">Retry this job</button>
    </form>
    {% endif %}
  </div>
  <p><a href="{{ url_for('email_finder') }}">&larr; Email finder</a></p>
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


def _decode_uploaded_csv_bytes(raw: bytes) -> tuple[str | None, str | None]:
    """Try common encodings (Excel exports often use Windows-1252, not UTF-8)."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, (
        "Could not read the uploaded file as text. Save the CSV as UTF-8 in Excel "
        "(Save As → CSV UTF-8) or use the single-company field without a file."
    )


def _normalize_csv_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _csv_dict_reader(text: str) -> csv.DictReader:
    return csv.DictReader(StringIO(_normalize_csv_text(text), newline=""))


def _read_uploaded_csv(text: str) -> tuple[list[str] | None, list[dict[str, str]], str | None]:
    """Return (fieldnames, data rows, error_message)."""
    try:
        reader = _csv_dict_reader(text)
        if reader.fieldnames is None:
            return None, [], "CSV has no header row."
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
        return fieldnames, rows, None
    except csv.Error as exc:
        return None, [], (
            f"Could not parse CSV ({exc}). Save the file as CSV UTF-8 from Excel, "
            "or check for unquoted line breaks inside cells."
        )


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
    text, decode_err = _decode_uploaded_csv_bytes(raw)
    if decode_err:
        return [], decode_err
    fieldnames, rows, parse_err = _read_uploaded_csv(text)
    if parse_err:
        return [], parse_err
    if not fieldnames:
        return [], "CSV has no header row."
    key = _company_csv_column_key(fieldnames)
    if not key:
        return [], "CSV must include a header column named Company."
    companies: list[str] = []
    for row in rows:
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


PERSON_LINKEDIN_OUTPUT = Path("data/person_linkedin")


def _csv_column_key(fieldnames: list[str] | None, *aliases: str) -> str | None:
    if not fieldnames:
        return None
    want = {a.casefold() for a in aliases}
    for name in fieldnames:
        if name and name.strip().casefold() in want:
            return name
    return None


def parse_person_company_from_csv_upload(storage) -> tuple[list[tuple[str, str]], str | None]:
    """
    Read uploaded CSV; require ``Person`` and ``Company`` headers (case-insensitive).
    ``Person Name`` is accepted as an alias for Person.
    """
    if storage is None or not getattr(storage, "filename", None):
        return [], None
    raw = storage.read()
    if not raw:
        return [], "Uploaded file is empty."
    text, decode_err = _decode_uploaded_csv_bytes(raw)
    if decode_err:
        return [], decode_err
    fields, data_rows, parse_err = _read_uploaded_csv(text)
    if parse_err:
        return [], parse_err
    if not fields:
        return [], "CSV has no header row."
    person_key = _csv_column_key(fields, "person", "person name")
    company_key = _csv_column_key(fields, "company")
    if not person_key or not company_key:
        return [], "CSV must include header columns named Person and Company."
    pairs: list[tuple[str, str]] = []
    for row in data_rows:
        raw_person = row.get(person_key, "")
        raw_company = row.get(company_key, "")
        person = raw_person.strip() if isinstance(raw_person, str) else str(raw_person or "").strip()
        company = raw_company.strip() if isinstance(raw_company, str) else str(raw_company or "").strip()
        if person and company:
            pairs.append((person, company))
    return pairs, None


def save_person_linkedin_results(rows: list[dict[str, str]]) -> str:
    """Write one CSV under data/person_linkedin; return path string."""
    PERSON_LINKEDIN_OUTPUT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = PERSON_LINKEDIN_OUTPUT / f"{timestamp}_person_linkedin.csv"
    fieldnames = ["Person", "Company", "LinkedIn_URL", "Search_Query", "Status", "Match_Score", "Source"]
    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Person": row["person"],
                    "Company": row["company"],
                    "LinkedIn_URL": row.get("linkedin_url") or "",
                    "Search_Query": row["search_query"],
                    "Status": row["status"],
                    "Match_Score": row.get("match_score", ""),
                    "Source": row.get("source", ""),
                }
            )
    return str(file_path)


EMAIL_ENRICHMENT_OUTPUT = Path("data/email_enrichment")
EMAIL_ENRICHMENT_MAX_ROWS = 500


def _clean_csv_cell(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def parse_email_enrichment_from_csv_upload(storage) -> tuple[list[dict[str, str]], str | None]:
    """
    Read uploaded CSV; require Person (or Name) and LinkedIn_URL columns.
    Company is optional. All original columns are preserved for the output CSV.
    """
    if storage is None or not getattr(storage, "filename", None):
        return [], None
    raw = storage.read()
    if not raw:
        return [], "Uploaded file is empty."
    text, decode_err = _decode_uploaded_csv_bytes(raw)
    if decode_err:
        return [], decode_err
    fields, data_rows, parse_err = _read_uploaded_csv(text)
    if parse_err:
        return [], parse_err
    if not fields:
        return [], "CSV has no header row."
    if len(data_rows) > EMAIL_ENRICHMENT_MAX_ROWS:
        return [], (
            f"CSV has {len(data_rows)} records. Upload at most "
            f"{EMAIL_ENRICHMENT_MAX_ROWS} records per file. Split the list and submit separate jobs."
        )
    person_key = _csv_column_key(
        fields, "person", "person name", "name", "full name", "contact name"
    )
    linkedin_key = _csv_column_key(
        fields,
        "linkedin_url",
        "linkedin url",
        "linkedin",
        "linkedin profile url",
        "profile_url",
        "profile url",
    )
    company_key = _csv_column_key(
        fields,
        "company",
        "company name",
        "account",
        "account name",
        "organization",
        "employer",
        "current company",
    )
    if not person_key:
        return [], (
            "CSV must include a person-name column. Accepted headers: "
            "Person, Person Name, Name, Full Name, Contact Name."
        )
    if not linkedin_key:
        return [], (
            "CSV must include a LinkedIn URL column. Accepted headers: "
            "LinkedIn_URL, LinkedIn URL, LinkedIn, LinkedIn Profile URL, Profile_URL, Profile URL."
        )

    rows: list[dict[str, str]] = []
    for row_num, row in enumerate(data_rows, start=2):
        raw_person = row.get(person_key, "")
        raw_linkedin = row.get(linkedin_key, "")
        raw_company = row.get(company_key, "") if company_key else ""
        person = _clean_csv_cell(raw_person)
        linkedin_url = _clean_csv_cell(raw_linkedin)
        company = _clean_csv_cell(raw_company)
        if not person and not linkedin_url:
            continue
        if not person:
            return [], f"Row {row_num}: Person name is required."
        if not linkedin_url:
            return [], f"Row {row_num}: LinkedIn_URL is required."
        if not is_valid_linkedin_url(linkedin_url):
            return [], f"Row {row_num}: LinkedIn_URL must be a LinkedIn profile URL."
        original = {field: _clean_csv_cell(row.get(field, "")) for field in fields}
        rows.append(
            {
                "person": person,
                "company": company,
                "linkedin_url": linkedin_url,
                "row_index": str(len(rows)),
                "original": original,
                "_fieldnames": fields,
            }
        )
    if not rows:
        return [], "CSV has no data rows."
    if len(rows) > EMAIL_ENRICHMENT_MAX_ROWS:
        return [], (
            f"CSV has {len(rows)} records. Upload at most "
            f"{EMAIL_ENRICHMENT_MAX_ROWS} records per file. Split the list and submit separate jobs."
        )
    return rows, None


def save_email_enrichment_results(rows: list[dict[str, str]]) -> str:
    """Write one CSV under data/email_enrichment; return path string."""
    EMAIL_ENRICHMENT_OUTPUT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = EMAIL_ENRICHMENT_OUTPUT / f"{timestamp}_email_enrichment.csv"
    write_results_csv(file_path, rows)
    return str(file_path)


URN_RESOLVE_OUTPUT = Path("data/linkedin_urn_resolve")
URN_RESOLVE_EXTRA_COLUMNS = ["LinkedIn_URL_Resolved", "Resolve_Status"]


def write_urn_resolve_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_fieldnames: list[str] = []
    if rows and rows[0].get("_fieldnames"):
        original_fieldnames = list(rows[0]["_fieldnames"])
    elif rows and rows[0].get("original"):
        original_fieldnames = list(rows[0]["original"].keys())

    fieldnames = original_fieldnames + [
        col for col in URN_RESOLVE_EXTRA_COLUMNS if col not in original_fieldnames
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row.get("original") or {})
            out["LinkedIn_URL_Resolved"] = row.get("linkedin_url_resolved") or ""
            out["Resolve_Status"] = row.get("status") or ""
            writer.writerow(out)


def save_urn_resolve_results(rows: list[dict]) -> str:
    """Write one CSV under data/linkedin_urn_resolve; return path string."""
    URN_RESOLVE_OUTPUT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = URN_RESOLVE_OUTPUT / f"{timestamp}_linkedin_urn_resolve.csv"
    write_urn_resolve_csv(file_path, rows)
    return str(file_path)


def parse_urn_resolve_from_csv_upload(storage) -> tuple[list[dict[str, str]], str | None]:
    """Read uploaded CSV; require a LinkedIn URL column. Preserve all original columns."""
    if storage is None or not getattr(storage, "filename", None):
        return [], None
    raw = storage.read()
    if not raw:
        return [], "Uploaded file is empty."
    text, decode_err = _decode_uploaded_csv_bytes(raw)
    if decode_err:
        return [], decode_err
    fields, data_rows, parse_err = _read_uploaded_csv(text)
    if parse_err:
        return [], parse_err
    if not fields:
        return [], "CSV has no header row."
    linkedin_key = _csv_column_key(
        fields,
        "linkedin_url",
        "linkedin url",
        "linkedin",
        "linkedin profile url",
        "profile_url",
        "profile url",
        "li_url",
        "linkedin bio",
        "profileurl",
        "target linkedin",
    )
    if not linkedin_key:
        return [], (
            "CSV must include a LinkedIn URL column. Accepted headers: "
            "LinkedIn_URL, LinkedIn URL, LinkedIn, li_url, Linkedin Bio, Profile URL, profileUrl."
        )

    rows: list[dict[str, str]] = []
    for row_num, row in enumerate(data_rows, start=2):
        raw_linkedin = row.get(linkedin_key, "")
        linkedin_url = normalize_linkedin_profile_url(_clean_csv_cell(raw_linkedin))
        if not linkedin_url:
            continue
        if not is_valid_linkedin_url(linkedin_url):
            return [], f"Row {row_num}: LinkedIn URL must be a LinkedIn profile URL (linkedin.com/in/...)."
        original = {field: _clean_csv_cell(row.get(field, "")) for field in fields}
        rows.append(
            {
                "linkedin_url": linkedin_url,
                "row_index": str(len(rows)),
                "original": original,
                "_fieldnames": fields,
            }
        )
    if not rows:
        return [], "CSV has no data rows with a LinkedIn profile URL."
    return rows, None


COMPANY_ENRICH_OUTPUT = Path("data/company_enrich")
COMPANY_ENRICH_MAX_ROWS = 500
COMPANY_ENRICH_EXTRA_COLUMNS = ["Employee_Count", "LinkedIn_Company_ID", "Company_Enrich_Status"]
COMPANY_ID_COLUMN_ALIASES = (
    "linkedin_company_id",
    "linkedin company id",
    "linkedin companyid",
    "companyid",
    "company_id",
    "company id",
    "linkedin_id",
    "linkedin id",
    "org_id",
    "org id",
    "numeric_id",
    "numeric linkedin id",
    "linkedin numeric id",
)


def write_company_enrich_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_fieldnames: list[str] = []
    if rows and rows[0].get("_fieldnames"):
        original_fieldnames = list(rows[0]["_fieldnames"])
    elif rows and rows[0].get("original"):
        original_fieldnames = list(rows[0]["original"].keys())
    else:
        original_fieldnames = ["Company", "LinkedIn_URL"]

    fieldnames = original_fieldnames + [
        col for col in COMPANY_ENRICH_EXTRA_COLUMNS if col not in original_fieldnames
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row.get("original") or {})
            if not out:
                out = {
                    "Company": row.get("company") or "",
                    "LinkedIn_URL": row.get("linkedin_url") or "",
                }
            out["Employee_Count"] = row.get("employee_count") or ""
            out["LinkedIn_Company_ID"] = row.get("linkedin_company_id") or ""
            out["Company_Enrich_Status"] = row.get("status") or ""
            writer.writerow(out)


def save_company_enrich_results(rows: list[dict]) -> str:
    """Write one CSV under data/company_enrich; return path string."""
    COMPANY_ENRICH_OUTPUT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = COMPANY_ENRICH_OUTPUT / f"{timestamp}_company_enrich.csv"
    write_company_enrich_csv(file_path, rows)
    return str(file_path)


def parse_company_enrich_from_csv_upload(storage) -> tuple[list[dict[str, str]], str | None]:
    """Read uploaded CSV; require company name and LinkedIn company URL. Preserve original columns."""
    if storage is None or not getattr(storage, "filename", None):
        return [], None
    raw = storage.read()
    if not raw:
        return [], "Uploaded file is empty."
    text, decode_err = _decode_uploaded_csv_bytes(raw)
    if decode_err:
        return [], decode_err
    fields, data_rows, parse_err = _read_uploaded_csv(text)
    if parse_err:
        return [], parse_err
    if not fields:
        return [], "CSV has no header row."
    if len(data_rows) > COMPANY_ENRICH_MAX_ROWS:
        return [], (
            f"CSV has {len(data_rows)} records. Upload at most "
            f"{COMPANY_ENRICH_MAX_ROWS} records per file. Split the list and submit separate jobs."
        )
    company_key = _csv_column_key(
        fields,
        "company",
        "company name",
        "account",
        "account name",
        "organization",
        "employer",
    )
    linkedin_key = _csv_column_key(
        fields,
        "linkedin_url",
        "linkedin url",
        "linkedin",
        "company linkedin",
        "company linkedin url",
        "linkedin company url",
        "company_url",
        "company url",
        "li_url",
    )
    id_key = _csv_column_key(fields, *COMPANY_ID_COLUMN_ALIASES)
    if not company_key:
        return [], (
            "CSV must include a company-name column. Accepted headers: "
            "Company, Company Name, Account, Account Name, Organization."
        )
    if not linkedin_key:
        return [], (
            "CSV must include a LinkedIn company URL column. Accepted headers: "
            "LinkedIn_URL, LinkedIn URL, LinkedIn, Company LinkedIn, Company URL."
        )

    rows: list[dict[str, str]] = []
    for row_num, row in enumerate(data_rows, start=2):
        company = _clean_csv_cell(row.get(company_key, ""))
        linkedin_raw = _clean_csv_cell(row.get(linkedin_key, ""))
        existing_id = extract_numeric_company_id(
            _clean_csv_cell(row.get(id_key, "")) if id_key else ""
        ) or extract_numeric_company_id(linkedin_raw)
        if not company and not linkedin_raw:
            continue
        if not company:
            return [], f"Row {row_num}: Company name is required."
        if not linkedin_raw:
            return [], f"Row {row_num}: LinkedIn company URL is required."
        if not is_valid_linkedin_company_url(linkedin_raw) and not existing_id:
            return [], (
                f"Row {row_num}: LinkedIn_URL must be a LinkedIn company page "
                "(linkedin.com/company/...) or a numeric company ID."
            )
        original = {field: _clean_csv_cell(row.get(field, "")) for field in fields}
        rows.append(
            {
                "company": company,
                "linkedin_url": linkedin_raw,
                "existing_company_id": existing_id,
                "row_index": str(len(rows)),
                "original": original,
                "_fieldnames": fields,
            }
        )
    if not rows:
        return [], "CSV has no data rows."
    if len(rows) > COMPANY_ENRICH_MAX_ROWS:
        return [], (
            f"CSV has {len(rows)} records. Upload at most "
            f"{COMPANY_ENRICH_MAX_ROWS} records per file. Split the list and submit separate jobs."
        )
    return rows, None


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


@app.route("/download/person-linkedin/<path:filename>", methods=["GET"])
def download_person_linkedin(filename: str):
    output_root = Path("data/person_linkedin").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/download/email-enrichment/<path:filename>", methods=["GET"])
def download_email_enrichment(filename: str):
    output_root = Path("data/email_enrichment").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/download/company-enrich/<path:filename>", methods=["GET"])
def download_company_enrich(filename: str):
    output_root = Path("data/company_enrich").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/download/vendor-file/<path:filename>", methods=["GET"])
def download_vendor_file(filename: str):
    output_root = Path("data/vendor_file").resolve()
    target_path = (output_root / filename).resolve()

    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/download/vendor-file-graph/<path:filename>", methods=["GET"])
def download_vendor_file_graph(filename: str):
    output_root = Path("data/vendor_file_graph").resolve()
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


def _finder_page_context(scope: str = "all") -> dict:
    prog = progress_display(scope=scope)
    return {
        "prog": prog,
        "poll_js": "true" if prog["job_running"] else "false",
        "message": "",
        "message_warn": False,
        "rows": [],
    }


def _parse_company_submission() -> tuple[list[str], str, str, str | None]:
    """Returns (companies, email, mode single|bulk, error_message)."""
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    companies: list[str] = []
    csv_err: str | None = None
    single = (request.form.get("single_company") or "").strip()
    if upload is not None and bool(upload.filename):
        companies, csv_err = parse_companies_from_csv_upload(upload)
    if companies:
        mode = "single" if len(companies) == 1 else "bulk"
        if mode == "bulk":
            err = validate_email(email)
            if err:
                return [], email, mode, err
        return companies, email, mode, None
    if single:
        return [single], email, "single", None
    if csv_err:
        return [], email, "single", csv_err
    return [], email, "single", "Upload a CSV with a Company column, or enter one company name."


def _parse_person_submission() -> tuple[list[tuple[str, str]], str, str, str | None]:
    """Returns (pairs, email, mode single|bulk, error_message)."""
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    pairs: list[tuple[str, str]] = []
    csv_err: str | None = None
    single_person = (request.form.get("single_person") or "").strip()
    single_company = (request.form.get("single_company") or "").strip()
    if upload is not None and bool(upload.filename):
        pairs, csv_err = parse_person_company_from_csv_upload(upload)
    if pairs:
        mode = "single" if len(pairs) == 1 else "bulk"
        if mode == "bulk":
            err = validate_email(email)
            if err:
                return [], email, mode, err
        return pairs, email, mode, None
    if single_person and single_company:
        return [(single_person, single_company)], email, "single", None
    if single_person or single_company:
        return [], email, "single", "For a single lookup, enter both person name and company name."
    if csv_err:
        return [], email, "single", csv_err
    return [], email, "single", "Upload a CSV with Person and Company columns, or enter one person and company."


def _lookup_single_company(name: str) -> tuple[dict[str, str] | None, str | None]:
    if is_serper_job_running():
        return None, "A Serper LinkedIn finder job is already running. See progress above."
    if not try_acquire_serper_worker():
        return None, "Another Serper lookup is in progress. Please wait."
    try:
        api_key = settings.serper_api_key or ""
        search_query = f"{name} site:linkedin.com"
        found_url = find_linkedin_company_url(name, api_key, num=10, date_restrict=None)
        return (
            {
                "company": name,
                "search_query": search_query,
                "linkedin_url": found_url or "",
                "status": "found" if found_url else "no_company_page_in_top_10",
            },
            None,
        )
    finally:
        release_serper_worker()


def _lookup_single_person(person: str, company: str) -> tuple[dict[str, str] | None, str | None]:
    if is_serper_job_running():
        return None, "A Serper LinkedIn finder job is already running. See progress above."
    if not try_acquire_serper_worker():
        return None, "Another Serper lookup is in progress. Please wait."
    try:
        api_key = settings.serper_api_key or ""
        match = find_person_linkedin(person, company, serper_api_key=api_key)
        return (
            {
                "person": person,
                "company": company,
                "search_query": match.search_query,
                "linkedin_url": match.url or "",
                "status": match.status,
                "match_score": str(match.score),
                "source": match.source,
            },
            None,
        )
    finally:
        release_serper_worker()


@app.route("/linkedin-finder/status", methods=["GET"])
def linkedin_finder_status():
    scope = (request.args.get("scope") or "all").strip().lower()
    if scope not in {"serper", "rapidapi", "email", "all"}:
        scope = "all"
    prog = progress_display(scope=scope)
    snap_key = scope if scope in {"serper", "rapidapi", "email"} else None
    if snap_key == "serper":
        snap = job_snapshot("serper")
    elif snap_key == "rapidapi":
        snap = job_snapshot("rapidapi")
    elif snap_key == "email":
        snap = job_snapshot("email")
    else:
        snap = job_snapshot()
        if "running" not in snap:
            for key in ("serper", "rapidapi", "email"):
                candidate = snap.get(key) or {}
                if candidate.get("running"):
                    snap = candidate
                    break
            else:
                snap = snap.get("serper") or {}
    return jsonify(
        {
            "running": prog["job_running"],
            "job_type": snap.get("job_type") or "",
            "current": snap.get("current") or 0,
            "total": snap.get("total") or 0,
            "current_item": snap.get("current_item") or "",
            "progress_line": prog.get("progress_line") or "",
            "error": snap.get("error"),
            "scope": scope,
        }
    )


@app.route("/linkedin-finder/thanks", methods=["GET"])
def linkedin_finder_thanks():
    email = (request.args.get("email") or "").strip()
    job_type = request.args.get("type") or "company"
    job_id = (request.args.get("job_id") or "").strip()
    queue_position = request.args.get("queue_position", type=int)
    if job_type == "company":
        job_label = "company LinkedIn"
    elif job_type == "email":
        job_label = "email enrichment"
    elif job_type == "urn_resolve":
        job_label = "LinkedIn URN resolver"
    elif job_type == "company_enrich":
        job_label = "company employee count"
    elif job_type == "vendor_file":
        job_label = "vendor email file"
    elif job_type == "vendor_file_graph":
        job_label = "vendor email file (graph)"
    else:
        job_label = "person LinkedIn"
    return render_template_string(
        THANK_YOU_TEMPLATE,
        email=email,
        job_label=job_label,
        job_id=job_id,
        queue_position=queue_position,
    )


@app.route("/email-finder/job/<job_id>", methods=["GET"])
def email_job_status(job_id: str):
    job = get_job_public_status(job_id)
    if not job:
        abort(404)
    return render_template_string(EMAIL_JOB_STATUS_TEMPLATE, job=job)


@app.route("/email-finder/job/<job_id>/retry", methods=["POST"])
def email_job_retry(job_id: str):
    ok, err = retry_email_job(job_id)
    if not ok:
        abort(400, description=err or "Could not retry job.")
    return redirect(url_for("email_job_status", job_id=job_id))


@app.route("/company-linkedin", methods=["GET", "POST"])
def company_linkedin_finder():
    ctx = _finder_page_context("serper")

    if request.method == "POST":
        if is_serper_job_running():
            ctx["message"] = "A Serper LinkedIn finder job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)

        companies, email, mode, err = _parse_company_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)
        if not settings.serper_api_key:
            ctx["message"] = "Missing SERPER_API_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)

        if mode == "single":
            row, lookup_err = _lookup_single_company(companies[0])
            if lookup_err:
                ctx["message"] = lookup_err
                ctx["message_warn"] = True
            elif row:
                ctx["rows"] = [row]
                ctx["message"] = "Lookup complete."
                ctx["message_warn"] = not row.get("linkedin_url")
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)

        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)

        started, start_err = start_company_job(companies, email, save_company_linkedin_results)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="company"))

    return render_template_string(COMPANY_LINKEDIN_TEMPLATE, **ctx)


@app.route("/person-linkedin", methods=["GET", "POST"])
def person_linkedin_finder():
    ctx = _finder_page_context("serper")

    if request.method == "POST":
        if is_serper_job_running():
            ctx["message"] = "A Serper LinkedIn finder job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)

        pairs, email, mode, err = _parse_person_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)
        if not settings.serper_api_key:
            ctx["message"] = "Missing SERPER_API_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)

        if mode == "single":
            person, company = pairs[0]
            row, lookup_err = _lookup_single_person(person, company)
            if lookup_err:
                ctx["message"] = lookup_err
                ctx["message_warn"] = True
            elif row:
                ctx["rows"] = [row]
                ctx["message"] = "Lookup complete."
                ctx["message_warn"] = not row.get("linkedin_url")
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)

        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)

        started, start_err = start_person_job(pairs, email, save_person_linkedin_results)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="person"))

    return render_template_string(PERSON_LINKEDIN_TEMPLATE, **ctx)


def _parse_email_submission() -> tuple[list[dict[str, str]], str, str, str | None]:
    """Returns (rows, email, mode single|bulk, error_message)."""
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    rows: list[dict[str, str]] = []
    csv_err: str | None = None
    single_person = (request.form.get("single_person") or "").strip()
    single_company = (request.form.get("single_company") or "").strip()
    single_linkedin = (request.form.get("single_linkedin_url") or "").strip()

    if upload is not None and bool(upload.filename):
        rows, csv_err = parse_email_enrichment_from_csv_upload(upload)
    if rows:
        mode = "single" if len(rows) == 1 else "bulk"
        if mode == "bulk":
            err = validate_email(email)
            if err:
                return [], email, mode, err
        return rows, email, mode, None
    if single_person and single_linkedin:
        if not is_valid_linkedin_url(single_linkedin):
            return [], email, "single", "Enter a valid LinkedIn profile URL (linkedin.com/in/...)."
        return (
            [
                {
                    "person": single_person,
                    "company": single_company,
                    "linkedin_url": single_linkedin,
                    "row_index": "0",
                }
            ],
            email,
            "single",
            None,
        )
    if single_person or single_company or single_linkedin:
        return [], email, "single", "For a single lookup, enter person name and LinkedIn profile URL."
    if csv_err:
        return [], email, "single", csv_err
    return (
        [],
        email,
        "single",
        "Upload a CSV (see expected column names above) or enter one person and LinkedIn URL.",
    )


def _lookup_single_email(row: dict[str, str]) -> tuple[dict[str, str] | None, str | None]:
    if is_email_job_running():
        return None, "An email enrichment job is already running. See progress above."
    if not try_acquire_email_worker():
        return None, "Another email lookup is in progress. Please wait."
    try:
        results = enrich_contacts([row], wait_for_molster_quota=False)
        if not results:
            return None, "No result returned from email enrichment."
        result = results[0]
        post_email_to_seeqe(result)
        return result, None
    except (EmailEnrichmentError, FullEnrichError, MolsterError) as exc:
        return None, str(exc)
    finally:
        release_email_worker()


def _parse_urn_submission() -> tuple[list[dict[str, str]], str, str, str | None]:
    """Returns (rows, email, mode single|bulk, error_message)."""
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    rows: list[dict[str, str]] = []
    csv_err: str | None = None
    single_linkedin = (request.form.get("single_linkedin_url") or "").strip()

    if upload is not None and bool(upload.filename):
        rows, csv_err = parse_urn_resolve_from_csv_upload(upload)
    if rows:
        mode = "single" if len(rows) == 1 else "bulk"
        if mode == "bulk":
            err = validate_email(email)
            if err:
                return [], email, mode, err
        return rows, email, mode, None
    if single_linkedin:
        linkedin_url = normalize_linkedin_profile_url(single_linkedin)
        if not is_valid_linkedin_url(linkedin_url):
            return [], email, "single", "Enter a valid LinkedIn profile URL or URN (linkedin.com/in/...)."
        return (
            [
                {
                    "linkedin_url": linkedin_url,
                    "row_index": "0",
                    "original": {"LinkedIn_URL": linkedin_url},
                    "_fieldnames": ["LinkedIn_URL"],
                }
            ],
            email,
            "single",
            None,
        )
    if csv_err:
        return [], email, "single", csv_err
    return (
        [],
        email,
        "single",
        "Upload a CSV with a LinkedIn URL column, or enter one profile URL / URN.",
    )


def _lookup_single_urn(row: dict[str, str]) -> tuple[dict[str, str] | None, str | None]:
    if is_rapidapi_job_running():
        return None, "A RapidAPI URN resolver job is already running. See progress above."
    if not try_acquire_rapidapi_worker():
        return None, "Another RapidAPI lookup is in progress. Please wait."
    try:
        if not settings.rapidapi_key:
            return None, "Missing RAPIDAPI_KEY in environment."
        resolved = resolve_vanity_url(str(row.get("linkedin_url") or ""))
        out = dict(row)
        out.update(
            {
                "linkedin_url": resolved["linkedin_url_input"],
                "linkedin_url_resolved": resolved["linkedin_url_resolved"],
                "public_identifier": resolved["public_identifier"],
                "status": resolved["status"],
            }
        )
        return out, None
    finally:
        release_rapidapi_worker()


@app.route("/linkedin-urn-resolve", methods=["GET", "POST"])
def urn_resolve_finder():
    ctx = _finder_page_context("rapidapi")

    if request.method == "POST":
        if is_rapidapi_job_running():
            ctx["message"] = "A RapidAPI URN resolver job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)

        rows, email, mode, err = _parse_urn_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)
        if not settings.rapidapi_key:
            ctx["message"] = "Missing RAPIDAPI_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)

        if mode == "single":
            row, lookup_err = _lookup_single_urn(rows[0])
            if lookup_err:
                ctx["message"] = lookup_err
                ctx["message_warn"] = True
            elif row:
                ctx["rows"] = [row]
                ctx["message"] = "Lookup complete."
                ctx["message_warn"] = not row.get("linkedin_url_resolved")
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)

        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)

        started, start_err = start_urn_resolve_job(rows, email, save_urn_resolve_results)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="urn_resolve"))

    return render_template_string(URN_RESOLVE_TEMPLATE, **ctx)


@app.route("/email-finder", methods=["GET", "POST"])
def email_finder():
    ctx = _finder_page_context("email")

    if request.method == "POST":
        rows, email, mode, err = _parse_email_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)
        if not email_providers_configured():
            ctx["message"] = "Missing MOLSTER_API_KEY and FULLENRICH_API_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)

        if mode == "single":
            row, lookup_err = _lookup_single_email(rows[0])
            if lookup_err:
                ctx["message"] = lookup_err
                ctx["message_warn"] = True
            elif row:
                ctx["rows"] = [row]
                ctx["message"] = "Lookup complete."
                ctx["message_warn"] = not row.get("work_email")
            return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)

        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)

        started, start_err, job_id = submit_email_enrichment_job(rows, email)
        if not started:
            ctx["message"] = start_err or "Could not queue job."
            ctx["message_warn"] = True
            return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)

        queue_position = count_pending_jobs()
        return redirect(
            url_for(
                "linkedin_finder_thanks",
                email=email,
                type="email",
                job_id=job_id,
                queue_position=queue_position,
            )
        )

    return render_template_string(EMAIL_FINDER_TEMPLATE, **ctx)


def _parse_company_enrich_submission() -> tuple[list[dict[str, str]], str, str, str | None]:
    """Returns (rows, email, mode single|bulk, error_message)."""
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    rows: list[dict[str, str]] = []
    csv_err: str | None = None
    single_company = (request.form.get("single_company") or "").strip()
    single_linkedin = (request.form.get("single_linkedin_url") or "").strip()

    if upload is not None and bool(upload.filename):
        rows, csv_err = parse_company_enrich_from_csv_upload(upload)
    if rows:
        mode = "single" if len(rows) == 1 else "bulk"
        if mode == "bulk":
            err = validate_email(email)
            if err:
                return [], email, mode, err
        return rows, email, mode, None
    if single_company and single_linkedin:
        existing_id = extract_numeric_company_id(single_linkedin)
        if not is_valid_linkedin_company_url(single_linkedin) and not existing_id:
            return [], email, "single", "Enter a valid LinkedIn company URL (linkedin.com/company/...) or numeric ID."
        return (
            [
                {
                    "company": single_company,
                    "linkedin_url": single_linkedin,
                    "existing_company_id": existing_id,
                    "row_index": "0",
                    "original": {
                        "Company": single_company,
                        "LinkedIn_URL": single_linkedin,
                    },
                    "_fieldnames": ["Company", "LinkedIn_URL"],
                }
            ],
            email,
            "single",
            None,
        )
    if single_company or single_linkedin:
        return [], email, "single", "For a single lookup, enter company name and LinkedIn company URL."
    if csv_err:
        return [], email, "single", csv_err
    return (
        [],
        email,
        "single",
        "Upload a CSV with Company and LinkedIn URL columns, or enter one company and LinkedIn URL.",
    )


def _lookup_single_company_enrich(row: dict[str, str]) -> tuple[dict[str, str] | None, str | None]:
    if is_rapidapi_job_running():
        return None, "A RapidAPI job is already running. See progress above."
    if not try_acquire_rapidapi_worker():
        return None, "Another RapidAPI lookup is in progress. Please wait."
    try:
        if not settings.rapidapi_key:
            return None, "Missing RAPIDAPI_KEY in environment."
        looked_up = lookup_company(
            str(row.get("linkedin_url") or ""),
            existing_company_id=str(row.get("existing_company_id") or ""),
        )
        out = dict(row)
        out.update(looked_up)
        return out, None
    finally:
        release_rapidapi_worker()


@app.route("/company-enrich", methods=["GET", "POST"])
def company_enrich_finder():
    ctx = _finder_page_context("rapidapi")

    if request.method == "POST":
        if is_rapidapi_job_running():
            ctx["message"] = "A RapidAPI job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)

        rows, email, mode, err = _parse_company_enrich_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)
        if not settings.rapidapi_key:
            ctx["message"] = "Missing RAPIDAPI_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)

        if mode == "single":
            row, lookup_err = _lookup_single_company_enrich(rows[0])
            if lookup_err:
                ctx["message"] = lookup_err
                ctx["message_warn"] = True
            elif row:
                ctx["rows"] = [row]
                ctx["message"] = "Lookup complete."
                ctx["message_warn"] = not row.get("employee_count")
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)

        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)

        started, start_err = start_company_enrich_job(rows, email, save_company_enrich_results)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="company_enrich"))

    return render_template_string(COMPANY_ENRICH_TEMPLATE, **ctx)


def _parse_vendor_file_submission() -> tuple[list[dict], str, str | None]:
    email = (request.form.get("email") or "").strip()
    upload = request.files.get("csv_file")
    if upload is None or not bool(upload.filename):
        return [], email, "Upload a CSV with stakeholder name, person LinkedIn, target company name, and target company LinkedIn."
    err = validate_email(email)
    if err:
        return [], email, err
    email_required, phone_required = contact_need_flags(request.form.get("contact_need") or "")
    try:
        rows = parse_input_csv(
            upload.read(),
            email_required_default=email_required,
            phone_required_default=phone_required,
        )
    except UnicodeDecodeError:
        return [], email, "CSV must be UTF-8."
    except ValueError as exc:
        return [], email, str(exc)
    return rows, email, None


@app.route("/vendor-file", methods=["GET", "POST"])
def vendor_file_finder():
    ctx = _finder_page_context("rapidapi")
    need = (request.form.get("contact_need") or "both").strip().lower()
    ctx["contact_need"] = need if need in {"email", "phone", "both"} else "both"
    ctx["graph_mode"] = False

    if request.method == "POST":
        if is_rapidapi_job_running():
            ctx["message"] = "A RapidAPI job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        rows, email, err = _parse_vendor_file_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)
        if not settings.rapidapi_key:
            ctx["message"] = "Missing RAPIDAPI_KEY in environment."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)
        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        uid = new_request_id()
        started, start_err = start_vendor_file_job(rows, email, uid)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="vendor_file"))

    return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)


@app.route("/vendor-file-graph", methods=["GET", "POST"])
def vendor_file_graph_finder():
    ctx = _finder_page_context("rapidapi")
    need = (request.form.get("contact_need") or "both").strip().lower()
    ctx["contact_need"] = need if need in {"email", "phone", "both"} else "both"
    ctx["graph_mode"] = True

    if request.method == "POST":
        if is_rapidapi_job_running():
            ctx["message"] = "A RapidAPI / vendor job is already running. See progress on this page."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        rows, email, err = _parse_vendor_file_submission()
        if err:
            ctx["message"] = err
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)
        if not graph_configured():
            ctx["message"] = "Missing POSTGRES_* in environment (graph is not configured)."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)
        if not smtp_configured():
            ctx["message"] = "Email is not configured on the server (SMTP settings)."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        uid = new_graph_request_id()
        started, start_err = start_vendor_file_graph_job(rows, email, uid)
        if not started:
            ctx["message"] = start_err or "Could not start job."
            ctx["message_warn"] = True
            return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)

        return redirect(url_for("linkedin_finder_thanks", email=email, type="vendor_file_graph"))

    return render_template_string(VENDOR_FILE_TEMPLATE, **ctx)


resume_pending_jobs_on_startup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.getenv("PORT", "5055"))
    app.run(host="0.0.0.0", port=port, debug=False)
