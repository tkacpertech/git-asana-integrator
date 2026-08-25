import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ASANA_API = "https://app.asana.com/api/1.0"
COUNTER_RE = re.compile(r"GitHub update #(\d+)")


def call_asana(url: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['ASANA_TOKEN']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Asana API error {exc.code}: {detail}", file=sys.stderr)
        raise


def next_update_number() -> int:
    highest = 0
    query = urllib.parse.urlencode({"opt_fields": "text", "limit": 100})
    url = f"{ASANA_API}/tasks/{os.environ['ASANA_TASK_GID']}/stories?{query}"
    while url:
        payload = call_asana(url)
        for story in payload["data"]:
            for match in COUNTER_RE.finditer(story.get("text") or ""):
                highest = max(highest, int(match.group(1)))
        url = (payload.get("next_page") or {}).get("uri")
    return highest + 1


def post_comment(html_body: str) -> None:
    call_asana(
        f"{ASANA_API}/tasks/{os.environ['ASANA_TASK_GID']}/stories",
        {"data": {"html_text": f"<body>{html_body}</body>"}},
    )


def commit_entry(number: int, event: dict, commit: dict) -> str:
    author = commit.get("author") or {}
    name = html.escape(author.get("name") or author.get("username") or "?")
    email = html.escape(author.get("email") or "")
    title = html.escape((commit.get("message") or "").splitlines()[0])
    branch = html.escape(event.get("ref", "").removeprefix("refs/heads/"))
    url = html.escape(commit.get("url", ""))
    return (
        f"<strong>GitHub update #{number:04d}</strong>\n"
        f"\U0001f464 {name} ({email})\n"
        f"\U0001f528 {title}\n"
        f"\U0001f33f {branch}\n"
        f"\U0001f517 <a href=\"{url}\">Commit</a>"
    )


def pr_entry(number: int, event: dict) -> str:
    pr = event["pull_request"]
    action = event.get("action")
    if action == "closed" and pr.get("merged"):
        status = "\U0001f7e3 Merged"
    elif action == "closed":
        status = "\U0001f534 Closed"
    else:
        status = "\U0001f7e2 Open"
    title = html.escape(pr.get("title") or "")
    url = html.escape(pr.get("html_url") or "")
    return (
        f"<strong>GitHub update #{number:04d}</strong>\n"
        f"<strong>PR #{pr['number']}</strong> — {title}\n"
        f"Status: {status}\n"
        f"\U0001f517 <a href=\"{url}\">Przejdź do Pull Request</a>"
    )


def main() -> None:
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    event_name = os.environ["GITHUB_EVENT_NAME"]

    number = next_update_number()

    if event_name == "push":
        commits = event.get("commits") or []
        if not commits:
            print("Push bez commitow (np. usuniecie brancha) - pomijam.")
            return
        for commit in commits:
            post_comment(commit_entry(number, event, commit))
            print(f"Dodano wpis #{number:04d} dla commita {commit.get('id', '')[:7]}")
            number += 1
    elif event_name == "pull_request":
        post_comment(pr_entry(number, event))
        print(f"Dodano wpis #{number:04d} dla PR #{event['pull_request']['number']}")
    else:
        print(f"Nieobslugiwane zdarzenie: {event_name}")
