import base64
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FIELDS = "summary,status,parent,issuetype"
MAX_PARENT_DEPTH = 4
NOT_FOUND = 404


class JiraAuthError(Exception):
    pass


def get_project(key):
    return key.split("-")[0]


def get_site(config, key):
    return config["projects"].get(get_project(key))


def get_browse_url(config, key):
    site = get_site(config, key)
    if not site:
        return None
    return f"{site}/browse/{key}"


def get_credentials(config, key):
    site = get_site(config, key)
    credentials = config["sites"].get(site, {})
    if not credentials.get("email") or not credentials.get("token"):
        return None
    return credentials


def build_request(site, credentials, key):
    auth = base64.b64encode(f"{credentials['email']}:{credentials['token']}".encode()).decode()
    return urllib.request.Request(
        f"{site}/rest/api/3/issue/{key}?fields={FIELDS}",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )


def to_issue(raw):
    fields = raw["fields"]
    parent = fields.get("parent")
    return {
        "key": raw["key"],
        "summary": fields["summary"],
        "status": fields["status"]["name"],
        "statusCategory": fields["status"]["statusCategory"]["key"],
        "type": fields["issuetype"]["name"],
        "parent": parent["key"] if parent else None,
    }


def fetch_issue(config, key):
    site = get_site(config, key)
    request = build_request(site, get_credentials(config, key), key)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return to_issue(json.loads(response.read()))
    except urllib.error.HTTPError as error:
        if error.code == NOT_FOUND:
            return None
        raise JiraAuthError(f"jira {site} returned {error.code} for {key}") from error


def fetch_level(config, keys):
    fetchable = [key for key in keys if get_credentials(config, key)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [issue for issue in pool.map(lambda key: fetch_issue(config, key), fetchable) if issue]


def fetch_issues(config, keys):
    issues = {}
    pending = set(keys)
    for _ in range(MAX_PARENT_DEPTH):
        for issue in fetch_level(config, pending):
            issues[issue["key"]] = issue
        pending = {i["parent"] for i in issues.values() if i["parent"] and i["parent"] not in issues}
        if not pending:
            break
    return issues


def get_root(issues, key):
    current = key
    for _ in range(MAX_PARENT_DEPTH):
        parent = issues.get(current, {}).get("parent")
        if not parent or parent not in issues:
            return current
        current = parent
    return current
