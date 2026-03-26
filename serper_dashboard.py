"""Local dashboard to run Serper pair searches and view results."""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, render_template_string, request, send_from_directory

from config import settings
from serper_search import search_serper

app = Flask(__name__)

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

DATE_PATTERNS = [
    # 2026-03-24
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    # Mar 24, 2026 / March 24, 2026
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", re.I),
    # 24 Mar 2026
    re.compile(r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b", re.I),
    # 2 days ago
    re.compile(r"\b\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago\b", re.I),
]

HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Serper Pair Search Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 1200px; }
    .row { display: flex; gap: 16px; margin-bottom: 14px; }
    .col { flex: 1; min-width: 260px; }
    label { display: block; font-weight: 600; margin-bottom: 6px; }
    select, textarea, input { width: 100%; padding: 8px; box-sizing: border-box; }
    textarea { min-height: 100px; }
    button { padding: 10px 14px; cursor: pointer; }
    .msg { margin: 14px 0; padding: 10px; background: #f5f5f5; border: 1px solid #ddd; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border: 1px solid #ddd; text-align: left; padding: 8px; vertical-align: top; }
    th { background: #fafafa; }
    .small { color: #666; font-size: 0.9em; }
  </style>
</head>
<body>
  <h2>Serper Pair Search Dashboard</h2>
  <p class="small">Choose a pair and search type, then run query combinations. Each query writes to a separate CSV file.</p>

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
        <label for="people_input">People (one per line)</label>
        <textarea name="people_input" id="people_input">{{ people_input }}</textarea>
      </div>
      <div class="col">
        <label for="accounts_input">Accounts (one per line)</label>
        <textarea name="accounts_input" id="accounts_input">{{ accounts_input }}</textarea>
      </div>
    </div>
    <button type="submit">Run Search</button>
  </form>

  {% if message %}
  <div class="msg">{{ message }}</div>
  {% endif %}

  {% if results %}
  <h3>Results</h3>
  {% if generated_files %}
  <p class="small">Download generated files:</p>
  <ul>
    {% for file_item in generated_files %}
    <li><a href="{{ file_item.download_url }}">{{ file_item.name }}</a></li>
    {% endfor %}
  </ul>
  {% endif %}
  <table>
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
      <tr>
        <td>{{ row.query }}</td>
        <td>{{ row.heading }}</td>
        <td><a href="{{ row.link }}" target="_blank" rel="noopener noreferrer">{{ row.link }}</a></td>
        <td>{{ row.date }}</td>
        <td>{{ row.file_path }}</td>
        <td><a href="{{ row.download_url }}">Download CSV</a></td>
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


def build_queries(pair_type: str, search_type: str, people: Iterable[str], accounts: Iterable[str]) -> list[str]:
    prefix = "site:linkedin.com/posts " if search_type == "linkedin_posts" else ""
    queries: list[str] = []
    people = list(people)
    accounts = list(accounts)

    if pair_type == "person_person":
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                queries.append(f'{prefix}"{people[i]}" "{people[j]}"')
    elif pair_type == "person_account":
        for person in people:
            for account in accounts:
                queries.append(f'{prefix}"{person}" "{account}"')
    elif pair_type == "account_account":
        for i in range(len(accounts)):
            for j in range(i + 1, len(accounts)):
                queries.append(f'{prefix}"{accounts[i]}" "{accounts[j]}"')

    return queries


def save_query_results(output_dir: Path, query: str, rows: list[dict]) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{slugify(query)[:120]}.csv"
    file_path = output_dir / filename

    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["query", "heading", "link", "date"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(file_path)


@app.route("/download/<path:filename>", methods=["GET"])
def download_file(filename: str):
    output_root = Path("data/serper_dashboard").resolve()
    target_path = (output_root / filename).resolve()

    # Ensure the file is inside our output folder.
    if output_root not in target_path.parents and target_path != output_root:
        abort(404)
    if not target_path.exists() or not target_path.is_file():
        abort(404)

    return send_from_directory(output_root, filename, as_attachment=True)


@app.route("/", methods=["GET", "POST"])
def dashboard() -> str:
    selected_pair_type = "person_person"
    selected_search_type = "web"
    people_input = "\n".join(DEFAULT_PEOPLE)
    accounts_input = "\n".join(DEFAULT_ACCOUNTS)
    num_results = "10"
    results: list[dict[str, str]] = []
    generated_files: list[dict[str, str]] = []
    message = ""

    if request.method == "POST":
        selected_pair_type = request.form.get("pair_type", selected_pair_type)
        selected_search_type = request.form.get("search_type", selected_search_type)
        people_input = request.form.get("people_input", people_input)
        accounts_input = request.form.get("accounts_input", accounts_input)
        num_results = request.form.get("num_results", "10")

        people = parse_lines(people_input)
        accounts = parse_lines(accounts_input)

        try:
            per_query = max(1, min(int(num_results), 100))
        except ValueError:
            per_query = 10
            num_results = "10"

        if not settings.serper_api_key:
            message = "Missing SERPER_API_KEY in .env."
        elif selected_pair_type not in PAIR_OPTIONS:
            message = "Invalid pair type selected."
        elif selected_search_type not in SEARCH_TYPE_OPTIONS:
            message = "Invalid search type selected."
        else:
            queries = build_queries(selected_pair_type, selected_search_type, people, accounts)
            if not queries:
                message = "No queries were generated. Check your people/accounts inputs."
            else:
                output_root = Path("data/serper_dashboard")
                total_rows = 0
                file_paths_seen: set[str] = set()
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

                    saved_file = save_query_results(output_root, query, query_rows)
                    saved_file_name = Path(saved_file).name
                    if saved_file not in file_paths_seen:
                        generated_files.append(
                            {
                                "name": saved_file_name,
                                "download_url": f"/download/{saved_file_name}",
                            }
                        )
                        file_paths_seen.add(saved_file)
                    for row in query_rows:
                        results.append(
                            {
                                **row,
                                "file_path": saved_file,
                                "download_url": f"/download/{saved_file_name}",
                            }
                        )
                    total_rows += len(query_rows)

                results.sort(
                    key=lambda r: parse_date_sort_value(r.get("date", "")),
                    reverse=True,
                )

                message = (
                    f"Ran {len(queries)} queries, fetched {total_rows} results. "
                    f"Files saved under {Path('data/serper_dashboard')}."
                )

    return render_template_string(
        HTML_TEMPLATE,
        pair_options=PAIR_OPTIONS,
        search_type_options=SEARCH_TYPE_OPTIONS,
        selected_pair_type=selected_pair_type,
        selected_search_type=selected_search_type,
        people_input=people_input,
        accounts_input=accounts_input,
        num_results=num_results,
        results=results,
        generated_files=generated_files,
        message=message,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5055"))
    app.run(host="0.0.0.0", port=port, debug=False)
