import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jira

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
MERGED_LOOKBACK_DAYS = 14
STALE_AFTER_DAYS = 365
TICKET_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
NO_TICKET = "no ticket"
INDEX_HTML = Path(__file__).with_name("index.html")
CONFIG_DIR = Path.home() / ".config" / "pr-board"
CONFIG_PATH = CONFIG_DIR / "config.json"
OVERRIDES_PATH = CONFIG_DIR / "overrides.json"
EMPTY_CONFIG = {"orgs": [], "sites": {}, "projects": {}}
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
JSON_TYPE = "application/json"
MAX_BODY_BYTES = 4096
FORBIDDEN = {"error": "forbidden"}
BOARD_ERROR = "failed to load the board, see ~/.local/state/pr-board.log"
SAME_ORIGIN = "same-origin"
OVERRIDES_LOCK = threading.Lock()

QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft state createdAt updatedAt mergedAt
        headRefName baseRefName mergeable mergeStateStatus reviewDecision
        repository { name }
        latestReviews(first: 20) { nodes { state author { login } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""


class Flag(str, Enum):
    MERGED = "merged"
    DRAFT = "draft"
    CONFLICTS = "conflicts"
    BEHIND = "behind"
    CHECKING = "checking"
    CI_FAILING = "ci_failing"
    CI_RUNNING = "ci_running"
    READY = "ready"
    APPROVED = "approved"
    REAPPROVE = "reapprove"
    CHANGES = "changes"
    REVIEW = "review"


def run_search_page(query, cursor):
    args = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={query}"]
    if cursor:
        args += ["-f", f"after={cursor}"]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["data"]["search"]


def run_search(query):
    nodes = []
    cursor = None
    while True:
        page = run_search_page(query, cursor)
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


def get_org_filter(config):
    return " ".join(f"org:{org}" for org in config.get("orgs", []))


def fetch_prs(config):
    since = (date.today() - timedelta(days=MERGED_LOOKBACK_DAYS)).isoformat()
    orgs = get_org_filter(config)
    queries = [
        f"is:pr is:open author:@me {orgs}",
        f"is:pr is:merged author:@me {orgs} merged:>={since}",
    ]
    with ThreadPoolExecutor() as pool:
        return [pr for prs in pool.map(run_search, queries) for pr in prs]


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_overrides(overrides):
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = OVERRIDES_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(overrides, indent=2, sort_keys=True))
    os.replace(temp_path, OVERRIDES_PATH)


def get_pr_id(pr):
    return f"{pr['repository']['name']}#{pr['number']}"


def get_ticket(pr, overrides):
    override = overrides.get(get_pr_id(pr))
    if override:
        return override
    for text in (pr["title"], pr["headRefName"]):
        match = TICKET_PATTERN.search(text)
        if match:
            return match.group(1)
    return NO_TICKET


def get_ci_state(pr):
    commits = pr["commits"]["nodes"]
    if not commits or not commits[0]["commit"]["statusCheckRollup"]:
        return None
    return commits[0]["commit"]["statusCheckRollup"]["state"]


def get_reviewers_by_state(pr, state):
    return [r["author"]["login"] for r in pr["latestReviews"]["nodes"] if r["state"] == state and r["author"]]


def get_review_flag(pr):
    if pr["reviewDecision"] == "APPROVED":
        return Flag.APPROVED
    if pr["reviewDecision"] == "CHANGES_REQUESTED":
        return Flag.CHANGES
    if get_reviewers_by_state(pr, "DISMISSED"):
        return Flag.REAPPROVE
    return Flag.REVIEW


def get_merge_flags(pr):
    if pr["mergeable"] == "CONFLICTING":
        return [Flag.CONFLICTS]
    if pr["mergeable"] == "UNKNOWN":
        return [Flag.CHECKING]
    if pr["mergeStateStatus"] == "BEHIND":
        return [Flag.BEHIND]
    return []


def get_ci_flags(pr):
    state = get_ci_state(pr)
    if state in ("FAILURE", "ERROR"):
        return [Flag.CI_FAILING]
    if state in ("PENDING", "EXPECTED"):
        return [Flag.CI_RUNNING]
    return []


def get_flags(pr):
    if pr["state"] == "MERGED":
        return [Flag.MERGED]
    review = get_review_flag(pr)
    if review == Flag.APPROVED and pr["mergeStateStatus"] in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return [Flag.READY]
    draft = [Flag.DRAFT] if pr["isDraft"] else []
    return draft + get_merge_flags(pr) + get_ci_flags(pr) + [review]


def is_stale(pr):
    created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created > timedelta(days=STALE_AFTER_DAYS)


def to_row(pr):
    return {
        "id": get_pr_id(pr),
        "repo": pr["repository"]["name"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "base": pr["baseRefName"],
        "updatedAt": pr["updatedAt"],
        "stale": pr["state"] != "MERGED" and is_stale(pr),
        "flags": [f.value for f in get_flags(pr)],
        "dismissed": get_reviewers_by_state(pr, "DISMISSED"),
    }


def group_by_ticket(prs, overrides):
    groups = {}
    for pr in prs:
        groups.setdefault(get_ticket(pr, overrides), []).append(to_row(pr))
    return groups


def get_issue_summary(issues, key):
    issue = issues.get(key)
    if not issue:
        return None
    return issue["summary"]


def to_ticket(config, issues, ticket, rows):
    root = jira.get_root(issues, ticket)
    return {
        "ticket": ticket,
        "url": jira.get_browse_url(config, ticket),
        "jira": issues.get(ticket),
        "root": root,
        "rootUrl": jira.get_browse_url(config, root),
        "rootSummary": get_issue_summary(issues, root),
        "prs": rows,
    }


def fetch_jira(config, keys):
    try:
        return jira.fetch_issues(config, keys), None
    except (jira.JiraAuthError, OSError) as error:
        return {}, str(error)


def build_board():
    config = read_json(CONFIG_PATH, EMPTY_CONFIG)
    groups = group_by_ticket(fetch_prs(config), read_json(OVERRIDES_PATH, {}))
    issues, jira_error = fetch_jira(config, [key for key in groups if key != NO_TICKET])
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mergedLookbackDays": MERGED_LOOKBACK_DAYS,
        "jiraError": jira_error,
        "tickets": [to_ticket(config, issues, ticket, rows) for ticket, rows in groups.items()],
    }


def assign_ticket(pr_id, ticket):
    with OVERRIDES_LOCK:
        overrides = read_json(OVERRIDES_PATH, {})
        overrides.pop(pr_id, None)
        if ticket:
            overrides[pr_id] = ticket
        write_overrides(overrides)


def is_valid_assignment(payload):
    if not isinstance(payload, dict):
        return False
    ticket = payload.get("ticket", "")
    if not isinstance(payload.get("pr"), str) or not isinstance(ticket, str):
        return False
    return ticket == "" or bool(TICKET_PATTERN.fullmatch(ticket))


def parse_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.is_trusted_host():
            self.send_json(403, FORBIDDEN)
            return
        if self.path != "/api/board":
            self.send_body(200, "text/html; charset=utf-8", INDEX_HTML.read_bytes())
            return
        if not self.is_same_origin_fetch():
            self.send_json(403, FORBIDDEN)
            return
        self.send_board()

    def do_POST(self):
        if not self.is_trusted_post():
            self.send_json(403, FORBIDDEN)
            return
        if self.path != "/api/assign":
            self.send_json(404, {})
            return
        payload = self.read_payload()
        if not is_valid_assignment(payload):
            self.send_json(400, {"error": "ticket must look like ABC-123"})
            return
        assign_ticket(payload["pr"], payload.get("ticket", ""))
        self.send_json(200, {})

    def is_trusted_host(self):
        return self.headers.get("Host") in ALLOWED_HOSTS

    def is_same_origin_fetch(self):
        site = self.headers.get("Sec-Fetch-Site")
        return site is None or site == SAME_ORIGIN

    def is_trusted_post(self):
        content_type = self.headers.get("Content-Type", "")
        return (
            self.is_trusted_host()
            and self.headers.get("Origin") in ALLOWED_ORIGINS
            and content_type.split(";")[0].strip() == JSON_TYPE
        )

    def get_body_length(self):
        value = self.headers.get("Content-Length", "")
        if not value.isascii() or not value.isdigit():
            return None
        return int(value)

    def read_payload(self):
        length = self.get_body_length()
        if length is None or length > MAX_BODY_BYTES:
            return None
        return parse_json(self.rfile.read(length))

    def send_board(self):
        try:
            board = build_board()
        except subprocess.CalledProcessError as error:
            self.send_board_error(error.stderr)
            return
        except (OSError, ValueError, KeyError) as error:
            self.send_board_error(repr(error))
            return
        self.send_json(200, board)

    def send_board_error(self, detail):
        print(detail, file=sys.stderr)
        self.send_json(502, {"error": BOARD_ERROR})

    def send_json(self, status, data):
        self.send_body(status, JSON_TYPE, json.dumps(data).encode())

    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"pr board on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
