import fnmatch
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

ASANA_API = "https://app.asana.com/api/1.0"

ICON_AUTHOR = "\U0001f464"
ICON_COMMIT = "\U0001f528"
ICON_BRANCH = "\U0001f33f"
ICON_STATUS = "\U0001f4e6"  # tylko jako kotwica dla wpisow ze starszej wersji
ICON_LINK = "\U0001f517"

STATUS_OPEN = "⚪ Not merged"
STATUS_MERGED = "\U0001f7e3 Merged"
STATUS_CLOSED = "\U0001f534 Closed"

NUMBER_RE = re.compile(r"GitHub update #(\d+)")
SHA_RE = re.compile(r"/commit/([0-9a-f]{7,40})")
PR_RE = re.compile(r"/pull/(\d+)")
BRANCH_RE = re.compile(rf"{ICON_BRANCH}\s*([^\n<]*)")
TITLE_RE = re.compile(rf"{ICON_COMMIT}\s*([^\n<]*)")
STATUS_LINE_RE = re.compile(
    rf"^(?:{STATUS_OPEN[0]}|{STATUS_MERGED[0]}|{STATUS_CLOSED[0]}|{ICON_STATUS})[^\n]*$",
    re.MULTILINE,
)
BODY_RE = re.compile(r"\A\s*<body>(.*)</body>\s*\Z", re.DOTALL)
SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")

MERGE_PREFIXES = (
    "Merge pull request #",
    "Merge branch ",
    "Merge remote-tracking branch ",
)
RETRY_CODES = frozenset({429, 500, 502, 503, 504})


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Brak wymaganej zmiennej srodowiskowej {name}.")
    return value


def call_asana(url: str, payload: dict | None = None, method: str | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {require_env('ASANA_TOKEN')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method or ("POST" if payload is not None else "GET"),
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code in RETRY_CODES and attempt < 2:
                print(f"Asana API {exc.code}, ponawiam...", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            print(f"Asana API error {exc.code}: {detail}", file=sys.stderr)
            raise
        except urllib.error.URLError as exc:
            if attempt < 2:
                print(f"Blad polaczenia z Asana ({exc.reason}), ponawiam...", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


@dataclass
class Entry:
    """Pojedynczy wpis 'GitHub update #NNNN' zapisany jako komentarz w Asanie."""

    gid: str
    number: int
    body: str
    sha: str = ""
    branch: str = ""
    title: str = ""
    pr_number: int | None = None

    @classmethod
    def parse(cls, gid: str, number: int, html_text: str) -> "Entry":
        body = BODY_RE.sub(lambda m: m.group(1), html_text)
        sha = SHA_RE.search(body)
        branch = BRANCH_RE.search(body)
        title = TITLE_RE.search(body)
        pr = PR_RE.search(body)
        return cls(
            gid=gid,
            number=number,
            body=body,
            sha=sha.group(1) if sha else "",
            branch=html.unescape(branch.group(1).strip()) if branch else "",
            title=html.unescape(title.group(1).strip()) if title else "",
            pr_number=int(pr.group(1)) if pr else None,
        )

    @property
    def merged(self) -> bool:
        return STATUS_MERGED in self.body


def same_commit(left: str, right: str) -> bool:
    if not left or not right:
        return False
    size = min(len(left), len(right), 40)
    return left[:size] == right[:size]


def fetch_entries() -> tuple[list[Entry], int]:
    """Zwraca wszystkie dotychczasowe wpisy oraz numer, ktory dostanie nastepny."""
    entries: list[Entry] = []
    highest = 0
    query = urllib.parse.urlencode({"opt_fields": "gid,html_text", "limit": 100})
    url = f"{ASANA_API}/tasks/{require_env('ASANA_TASK_GID')}/stories?{query}"
    while url:
        payload = call_asana(url)
        for story in payload["data"]:
            html_text = story.get("html_text") or ""
            match = NUMBER_RE.search(html_text)
            if not match:
                continue
            number = int(match.group(1))
            highest = max(highest, number)
            entries.append(Entry.parse(story["gid"], number, html_text))
        url = (payload.get("next_page") or {}).get("uri")
    return entries, highest + 1


def post_comment(inner_html: str) -> None:
    call_asana(
        f"{ASANA_API}/tasks/{require_env('ASANA_TASK_GID')}/stories",
        {"data": {"html_text": f"<body>{inner_html}</body>"}},
    )


def update_comment(entry: Entry, inner_html: str) -> None:
    call_asana(
        f"{ASANA_API}/stories/{entry.gid}",
        {"data": {"html_text": f"<body>{inner_html}</body>"}},
        method="PUT",
    )
    entry.body = inner_html


def status_line(status: str, pr_number: int | None = None, pr_url: str = "") -> str:
    line = status
    if pr_number and pr_url:
        line += f" · <a href=\"{html.escape(pr_url)}\">PR #{pr_number}</a>"
    elif pr_number:
        line += f" · PR #{pr_number}"
    return line


def apply_status(body: str, line: str) -> str:
    """Podmienia linie statusu w istniejacym wpisie (lub ja dokłada)."""
    if STATUS_LINE_RE.search(body):
        return STATUS_LINE_RE.sub(lambda _: line, body, count=1)
    lines = body.split("\n")
    for index, current in enumerate(lines):
        if current.startswith(ICON_LINK):
            lines.insert(index, line)
            break
    else:
        lines.append(line)
    return "\n".join(lines)


def commit_entry(number: int, branch: str, commit: dict, status: str) -> str:
    author = commit.get("author") or {}
    name = html.escape(author.get("name") or author.get("username") or "?")
    email = author.get("email") or ""
    who = f"{name} ({html.escape(email)})" if email else name
    title = html.escape((commit.get("message") or "").splitlines()[0])
    url = html.escape(commit.get("url", ""))
    return (
        f"<strong>GitHub update #{number:04d}</strong>\n"
        f"{ICON_AUTHOR} {who}\n"
        f"{ICON_COMMIT} {title}\n"
        f"{ICON_BRANCH} {html.escape(branch)}\n"
        f"{status_line(status)}\n"
        f"{ICON_LINK} <a href=\"{url}\">Commit</a>"
    )


def pr_entry(number: int, pr: dict, status: str) -> str:
    """Wpis awaryjny - gdy do PR-a nie ma zadnego wpisu commitowego."""
    user = html.escape((pr.get("user") or {}).get("login") or "?")
    title = html.escape(pr.get("title") or "")
    branch = html.escape((pr.get("head") or {}).get("ref") or "")
    url = pr.get("html_url") or ""
    return (
        f"<strong>GitHub update #{number:04d}</strong>\n"
        f"{ICON_AUTHOR} {user}\n"
        f"{ICON_COMMIT} {title}\n"
        f"{ICON_BRANCH} {branch}\n"
        f"{status_line(status, pr.get('number'), url)}\n"
        f"{ICON_LINK} <a href=\"{html.escape(url)}\">Pull Request</a>"
    )


def ignored_branch(branch: str) -> bool:
    patterns = [
        pattern.strip()
        for pattern in re.split(r"[,\n]", os.environ.get("IGNORE_BRANCHES", ""))
        if pattern.strip()
    ]
    return any(fnmatch.fnmatch(branch, pattern) for pattern in patterns)


def is_merge_result(commit: dict, entries: list[Entry]) -> bool:
    """Czy commit na galezi domyslnej jest tylko efektem zmergowania PR-a."""
    title = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
    if title.startswith(MERGE_PREFIXES):
        return True
    squash = SQUASH_RE.search(title)
    if squash and any(entry.pr_number == int(squash.group(1)) for entry in entries):
        return True
    stripped = SQUASH_RE.sub("", title).strip()
    return any(entry.title and entry.title == stripped for entry in entries)


def handle_push(event: dict, entries: list[Entry], number: int) -> None:
    commits = event.get("commits") or []
    if not commits:
        print("Push bez commitow (np. usuniecie brancha) - pomijam.")
        return

    branch = event.get("ref", "").removeprefix("refs/heads/")
    if ignored_branch(branch):
        print(f"Branch {branch} jest na liscie IGNORE_BRANCHES - pomijam.")
        return

    default_branch = (event.get("repository") or {}).get("default_branch")
    for commit in commits:
        sha = commit.get("id", "")
        if any(same_commit(sha, entry.sha) for entry in entries):
            print(f"Commit {sha[:7]} ma juz wpis - pomijam.")
            continue
        if branch == default_branch and is_merge_result(commit, entries):
            print(f"Commit {sha[:7]} to efekt merge'a PR-a - pomijam.")
            continue
        # commit wypchniety wprost na galaz domyslna jest juz "w mainie"
        status = STATUS_MERGED if branch == default_branch else STATUS_OPEN
        post_comment(commit_entry(number, branch, commit, status))
        print(f"Dodano wpis #{number:04d} dla commita {sha[:7]}")
        number += 1


def handle_pull_request(event: dict, entries: list[Entry], number: int) -> None:
    pr = event["pull_request"]
    action = event.get("action")
    pr_number = pr["number"]
    pr_url = pr.get("html_url") or ""
    branch = (pr.get("head") or {}).get("ref") or ""

    if action == "closed" and pr.get("merged"):
        status = STATUS_MERGED
    elif action == "closed":
        status = STATUS_CLOSED
    elif action in {"opened", "reopened", "ready_for_review", "synchronize"}:
        status = STATUS_OPEN
    else:
        print(f"Nieobslugiwana akcja PR: {action}")
        return

    targets = [
        entry
        for entry in entries
        if entry.branch == branch and entry.pr_number in (None, pr_number)
    ]
    if not targets:
        if status is STATUS_OPEN:
            print(f"Brak wpisow dla brancha {branch} - czekam na push.")
            return
        post_comment(pr_entry(number, pr, status))
        print(f"Dodano wpis #{number:04d} dla PR #{pr_number} (brak wpisow commitowych)")
        return

    line = status_line(status, pr_number, pr_url)
    for entry in targets:
        if entry.merged and status is not STATUS_MERGED:
            continue
        body = apply_status(entry.body, line)
        if body == entry.body:
            print(f"Wpis #{entry.number:04d} juz aktualny - pomijam.")
            continue
        update_comment(entry, body)
        print(f"Zaktualizowano wpis #{entry.number:04d} -> {status} (PR #{pr_number})")


def main() -> None:
    event_path = require_env("GITHUB_EVENT_PATH")
    event_name = require_env("GITHUB_EVENT_NAME")
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)

    entries, number = fetch_entries()

    if event_name == "push":
        handle_push(event, entries, number)
    elif event_name == "pull_request":
        handle_pull_request(event, entries, number)
    else:
        print(f"Nieobslugiwane zdarzenie: {event_name}")
