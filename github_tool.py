#!/usr/bin/python3
# updated by chatgpt GPT-6
"""
Author: Willem Hengeveld <itsme@xs4all.nl>

Commandline tool for searching github.

* find all very large repositories:
github -a -w repo -q "size:>8000000"


* find all very large files:
github -a -w code -q "in:path zip size:>500000000"

* list all issues, pull requests, and discussions (all pages/states):
github --issues --prs --discussions OWNER/REPO

* include bodies and all comments, reviews, and discussion replies:
github --issues --prs --discussions --comments OWNER/REPO

* list all issue uploads, including attachments in comments:
github --attachments OWNER/REPO
github --attachments --issue 12 --issue 34 OWNER/REPO
github --attachments --urls https://github.com/OWNER/REPO/issues/12

Thread modes support --state open/closed and --jsonl. --attachments outputs
only uploads; ordinary external links and repository files are excluded.
Discussions require a GitHub token. Existing search, repository, fork, and
rate-limit commands are retained. All HTTP requests share automatic rate
limit waits; diagnostics go to stderr. Requires Python 3.10+ and aiohttp.

"""
import asyncio
import argparse
import datetime
import html
import json
import os.path
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import AsyncIterator, Mapping, cast

import aiohttp


JsonObject = dict[str, object]


def json_object(value: object) -> JsonObject:
    """Validate an object at the JSON API boundary."""
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from GitHub")
    return cast(JsonObject, value)


def json_objects(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array from GitHub")
    return [json_object(item) for item in value]


def text_value(value: object) -> str:
    return str(value) if value is not None else ""


def author_name(value: object) -> str:
    if not value:
        return "[deleted]"
    return text_value(json_object(value).get("login"))


@dataclass
class RateBudget:
    remaining: int
    reset: float

    def delay(self) -> float:
        if self.remaining == 0:
            return max(0.0, self.reset - time.time() + 1)
        return 0.0


class RateLimiter:
    """Keep separate primary budgets and honor secondary waits."""

    def __init__(self, pace: int | None) -> None:
        self.budgets: dict[str, RateBudget] = {}
        self.next_request = 0.0
        self.pace = (pace or 0) / 1000

    @staticmethod
    def resource(url: str) -> str:
        path = urllib.parse.urlsplit(url).path
        if path == "/graphql":
            return "graphql"
        if path == "/search/code":
            return "code_search"
        return "search" if path.startswith("/search/") else "core"

    def update(self, headers: Mapping[str, str], resource: str) -> None:
        if ("X-RateLimit-Remaining" in headers
                and "X-RateLimit-Reset" in headers):
            resource = headers.get("X-RateLimit-Resource", resource)
            self.budgets[resource] = RateBudget(
                int(headers["X-RateLimit-Remaining"]),
                float(headers["X-RateLimit-Reset"]),
            )

    async def before_request(self, url: str) -> None:
        budget = self.budgets.get(self.resource(url))
        if budget and budget.delay():
            await self.wait(budget.delay(), "primary API rate limit")
        delay = self.next_request - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.next_request = time.monotonic() + self.pace

    @staticmethod
    async def wait(delay: float, reason: str) -> None:
        delay = max(1.0, delay)
        until = datetime.datetime.fromtimestamp(
            time.time() + delay, datetime.timezone.utc
        ).isoformat(timespec="seconds")
        print(f"GitHub: {reason}; waiting {delay:.0f}s (until {until})",
              file=sys.stderr, flush=True)
        await asyncio.sleep(delay)

    @staticmethod
    def retry_delay(headers: Mapping[str, str], attempt: int) -> float:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                from email.utils import parsedate_to_datetime
                return max(1.0, parsedate_to_datetime(retry_after).timestamp()
                           - time.time() + 1)
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset")
            if reset:
                return max(1.0, float(reset) - time.time() + 1)
        return min(900.0, 60.0 * 2 ** min(attempt, 4))


def rate_limited(status: int, payload: object) -> bool:
    if status == 429:
        return True
    if not isinstance(payload, dict):
        return False
    message = text_value(payload.get("message")).lower()
    errors = payload.get("errors")
    if isinstance(errors, list):
        message += " ".join(
            text_value(error.get("message"))
            + " " + text_value(error.get("type"))
            for error in errors if isinstance(error, dict)
        ).lower()
    return (status in (200, 403) and any(word in message for word in (
        "rate limit", "rate_limit", "secondary", "abuse detection",
    )))


class HttpPrinter:

    def make(self):
        async def on_request_start(session, trace_config_ctx, params):
            print("Starting request", params.method, params.url,
                  file=sys.stderr)

        async def on_request_end(session, trace_config_ctx, params):
            print("Ending request", params.method, params.url, file=sys.stderr)
        async def on_response_chunk_received(
            session, trace_config_ctx, params,
        ):
            print("rx chunk", len(params.chunk), file=sys.stderr)
        async def on_request_chunk_sent(session, trace_config_ctx, params):
            print("tx chunk", len(params.chunk), file=sys.stderr)

        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_start.append(on_request_start)
        trace_config.on_request_end.append(on_request_end)
        trace_config.on_response_chunk_received.append(
            on_response_chunk_received
        )
        trace_config.on_request_chunk_sent.append(on_request_chunk_sent)

        return trace_config


class GithubApi:
    """
    Object for accessing github.
    Search, repositories, threads and rate-limit status.
    """
    DEFAULT_API_URLS: dict[str, str] = {
        "authorizations_url": "https://api.github.com/authorizations",
        "code_search_url": (
            "https://api.github.com/search/code?q={query}{&page,per_page,s"
            "ort,order}"
        ),
        "commit_search_url": (
            "https://api.github.com/search/commits?q={query}{&page,per_pag"
            "e,sort,order}"
        ),
        "current_user_authorizations_html_url": (
            "https://github.com/settings/connections/applications{/client_"
            "id}"
        ),
        "current_user_repositories_url": (
            "https://api.github.com/user/repos{?type,page,per_page,sort}"
        ),
        "current_user_url": "https://api.github.com/user",
        "emails_url": "https://api.github.com/user/emails",
        "emojis_url": "https://api.github.com/emojis",
        "events_url": "https://api.github.com/events",
        "feeds_url": "https://api.github.com/feeds",
        "followers_url": "https://api.github.com/user/followers",
        "following_url": "https://api.github.com/user/following{/target}",
        "gists_url": "https://api.github.com/gists{/gist_id}",
        "hub_url": "https://api.github.com/hub",
        "issue_search_url": (
            "https://api.github.com/search/issues?q={query}{&page,per_page"
            ",sort,order}"
        ),
        "issues_url": "https://api.github.com/issues",
        "keys_url": "https://api.github.com/user/keys",
        "label_search_url": (
            "https://api.github.com/search/labels?q={query}&repository_id="
            "{repository_id}{&page,per_page}"
        ),
        "notifications_url": "https://api.github.com/notifications",
        "organization_repositories_url": (
            "https://api.github.com/orgs/{org}/repos{?type,page,per_page,s"
            "ort}"
        ),
        "organization_teams_url": (
            "https://api.github.com/orgs/{org}/teams"
        ),
        "organization_url": "https://api.github.com/orgs/{org}",
        "public_gists_url": "https://api.github.com/gists/public",
        "rate_limit_url": "https://api.github.com/rate_limit",
        "repository_search_url": (
            "https://api.github.com/search/repositories?q={query}{&page,pe"
            "r_page,sort,order}"
        ),
        "repository_url": "https://api.github.com/repos/{owner}/{repo}",
        "starred_gists_url": "https://api.github.com/gists/starred",
        "starred_url": (
            "https://api.github.com/user/starred{/owner}{/repo}"
        ),
        # "team_url": "https://api.github.com/teams",
        "user_organizations_url": "https://api.github.com/user/orgs",
        "user_repositories_url": (
            "https://api.github.com/users/{user}/repos{?type,page,per_page"
            ",sort}"
        ),
        "user_search_url": (
            "https://api.github.com/search/users?q={query}{&page,per_page,"
            "sort,order}"
        ),
        "user_url": "https://api.github.com/users/{user}",
    }

    def __init__(self, args: argparse.Namespace) -> None:
        """
        The constructor takes a generic args object.
        Currently 'args.auth' is used from args.
        """
        self.baseurl = "https://api.github.com/"
        self.args = args

        self.rate_limiter = RateLimiter(args.pace)
        self.request_lock = asyncio.Lock()

        self.client: aiohttp.ClientSession | None = None

        # this list can also be obtained from 'baseurl'
        self.d = self.DEFAULT_API_URLS.copy()

    def getclient(self) -> aiohttp.ClientSession:
        if not self.client:
            hdrs: dict[str, str] = {
                "Accept": "application/vnd.github.v3+json",
            }

            if self.args.auth:
                # use the OAuth token
                hdrs['Authorization'] = 'Bearer %s' % self.args.auth

            self.client = aiohttp.ClientSession(
                headers=hdrs, timeout=aiohttp.ClientTimeout(total=120),
                trace_configs=(
                    [HttpPrinter().make()] if self.args.trace else []
                ),
            )
        return self.client

    async def close(self) -> None:
        """Close an opened HTTP session."""
        if self.client:
            await self.client.close()

    async def request(
        self, method: str, path: str, *, params: JsonObject | None = None,
        payload: JsonObject | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[object, Mapping[str, str]]:
        """Serialize requests so all commands share the same rate limits."""
        async with self.request_lock:
            return await self._request_with_retries(
                method, path, params, payload, headers
            )

    async def _request_with_retries(
        self, method: str, path: str, params: JsonObject | None,
        request_body: JsonObject | None,
        request_headers: Mapping[str, str] | None,
    ) -> tuple[object, Mapping[str, str]]:
        rate_attempt = 0
        secondary_attempt = 0
        transient_attempt = 0
        retryable = method == "GET" or path.endswith("/graphql")
        transient_limit = 5 if retryable else 0
        while True:
            await self.rate_limiter.before_request(path)
            try:
                payload, headers, status = await self._read_response(
                    method, path, params, request_body, request_headers
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if transient_attempt >= transient_limit:
                    raise RuntimeError(
                        f"GitHub request failed: {exc}"
                    ) from exc
                await self.rate_limiter.wait(
                    2 ** transient_attempt, "temporary connection failure"
                )
                transient_attempt += 1
                continue
            self.rate_limiter.update(headers, self.rate_limiter.resource(path))
            limited = rate_limited(status, payload) or (
                status == 403 and (headers.get("X-RateLimit-Remaining") == "0"
                                   or "Retry-After" in headers)
            )
            if limited:
                if headers.get("X-RateLimit-Remaining") != "0":
                    if secondary_attempt >= 5:
                        raise RuntimeError(
                            "GitHub secondary rate limit persisted "
                            "after five retries"
                        )
                    secondary_attempt += 1
                await self.rate_limiter.wait(
                    self.rate_limiter.retry_delay(headers, rate_attempt),
                    "API rate limit",
                )
                rate_attempt += 1
                continue
            if (status in (500, 502, 503, 504)
                    and transient_attempt < transient_limit):
                await self.rate_limiter.wait(
                    2 ** transient_attempt, f"temporary HTTP {status}"
                )
                transient_attempt += 1
                continue
            if not 200 <= status < 300:
                message = text_value(json_object(payload).get("message"))
                raise RuntimeError(f"GitHub HTTP {status}: {message}")
            return payload, headers

    async def _read_response(
        self, method: str, path: str, params: JsonObject | None,
        request_body: JsonObject | None,
        request_headers: Mapping[str, str] | None,
    ) -> tuple[object, Mapping[str, str], int]:
        async with self.getclient().request(
            method, path, params=params, json=request_body,
            headers=request_headers,
        ) as response:
            raw = await response.text()
            try:
                payload: object = json.loads(raw)
            except ValueError as exc:
                if 200 <= response.status < 300:
                    raise RuntimeError("GitHub returned invalid JSON") from exc
                payload = {"message": raw[:300]}
            return payload, response.headers.copy(), response.status

    async def get(
        self, path: str, params: JsonObject | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[object, Mapping[str, str]]:
        """Return JSON response and HTTP headers."""
        return await self.request("GET", path, params=params, headers=headers)

    async def post(
        self, path: str, payload: JsonObject,
    ) -> tuple[object, Mapping[str, str]]:
        return await self.request("POST", path, payload=payload)

    async def graphql(self, query: str, variables: JsonObject) -> JsonObject:
        if not self.args.auth:
            raise ValueError(
                "Discussions require --auth, GH_TOKEN, GITHUB_TOKEN, "
                "or an auth token in ~/.github_cmdline_rc"
            )
        payload, _ = await self.post(self.baseurl + "graphql", {
            "query": query, "variables": variables,
        })
        result = json_object(payload)
        if result.get("errors"):
            errors = json_objects(result["errors"])
            message = "; ".join(text_value(e.get("message")) for e in errors)
            raise RuntimeError(f"GitHub GraphQL: {message}")
        return json_object(result.get("data"))

    async def loadapi(self):
        """
        Load the api map from github.
        """
        self.d, _ = await self.get(self.baseurl)

    def getapi(self, apiname):
        """
        get the url for the specified API.
        """
        url = self.d.get(apiname)
        if not url:
            raise Exception("can't find '%s' api" % apiname)
        return url

    async def getlimits(self):
        """
        Request the current rate limit state.
        """
        url = self.getapi("rate_limit_url")
        result, hdrs = await self.get(url)
        return result

    async def addrepo(self, name, desc):
        """
        Add a new repository.
        """
        url = self.getapi("current_user_repositories_url")
        url = re.sub(r'\{.*?\}', '', url)

        req = {
          "name": name,
          "description": desc,
          "private": False,
          "has_issues": True,
          "has_projects": True,
          "has_wiki": True,
        }

        result, hdrs = await self.post(url, req)
        return result

    def list(self, username, pagenr=1):
        """
        Get one page of repository results for the specified user.
        """
        url = self.getapi("user_repositories_url")
        url = url.replace("{user}", username)
        url = url[:url.find('{')]

        return self.get(url, dict(per_page=100, page=pagenr))

    def query(self, where, query, pagenr=1):
        """
        Search query in domain 'where'

        TODO: add an Accept option for text-match search results.
        """
        url = self.getapi(where + "_search_url")
        url = url[:url.find('?')]

        return self.get(url, dict(per_page=100, q=query, page=pagenr))

    def info(self, fullname):
        owner, repo = fullname.split('/', 1)
        url = self.getapi('repository_url')
        url = url.replace('{owner}', owner)
        url = url.replace('{repo}', repo)

        return self.get(url)


@dataclass(frozen=True)
class RepositoryTarget:
    repository: str
    kind: str = ""
    number: int | None = None

    @classmethod
    def parse(cls, value: str) -> "RepositoryTarget":
        path = value.strip().rstrip("/")
        if "://" in path:
            url = urllib.parse.urlsplit(path)
            if url.hostname != "github.com":
                raise ValueError(f"Expected a github.com URL: {value}")
            path = url.path.strip("/")
        parts = path.split("/")
        if len(parts) < 2 or not all(parts[:2]):
            raise ValueError(f"Expected OWNER/REPO or a GitHub URL: {value}")
        owner, name = parts[:2]
        name = name.removesuffix(".git")
        if not all(re.fullmatch(r"[\w.-]+", part) for part in (owner, name)):
            raise ValueError(f"Invalid repository name: {value}")
        kinds = {"issues": "issue", "pull": "pr", "discussions": "discussion"}
        if len(parts) == 4 and parts[2] in kinds:
            return cls(
                f"{owner}/{name}", kinds[parts[2]], positive_number(parts[3])
            )
        if len(parts) != 2:
            raise ValueError(
                f"Expected a repository or issue/PR/discussion URL: {value}"
            )
        return cls(f"{owner}/{name}")

    def variables(self) -> JsonObject:
        owner, name = self.repository.split("/", 1)
        return {"owner": owner, "name": name, "number": self.number}


def positive_number(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("Thread numbers must be positive")
    return number


@dataclass(frozen=True)
class ThreadSelection:
    kind: str
    numbers: tuple[int, ...] = ()

    @classmethod
    def from_args(
        cls, args: argparse.Namespace,
    ) -> tuple["ThreadSelection", ...]:
        selections: list[ThreadSelection] = []
        for kind, enabled, numbers in (
            ("issue", args.issues, args.issue),
            ("pr", args.prs, args.pr),
            ("discussion", args.discussions, args.discussion),
        ):
            if enabled or numbers:
                unique_numbers = tuple(dict.fromkeys(numbers or ()))
                selections.append(cls(kind, unique_numbers))
        return tuple(selections) or (cls("issue"),)


@dataclass(frozen=True)
class Thread:
    repository: str
    kind: str
    number: int
    title: str
    state: str
    author: str
    created_at: str
    updated_at: str
    url: str
    body: str
    body_html: str
    node_id: str = ""

    @classmethod
    def from_rest(
        cls, repository: str, kind: str, data: JsonObject,
    ) -> "Thread":
        return cls(
            repository, kind, int(text_value(data["number"])),
            text_value(data.get("title")), text_value(data.get("state")),
            author_name(data.get("user")), text_value(data.get("created_at")),
            text_value(data.get("updated_at")),
            text_value(data.get("html_url")),
            text_value(data.get("body")), text_value(data.get("body_html")),
            text_value(data.get("node_id")),
        )

    @classmethod
    def from_graphql(cls, repository: str, data: JsonObject) -> "Thread":
        return cls(
            repository, "discussion", int(text_value(data["number"])),
            text_value(data.get("title")),
            "closed" if data.get("closed") else "open",
            author_name(data.get("author")), text_value(data.get("createdAt")),
            text_value(data.get("updatedAt")), text_value(data.get("url")),
            text_value(data.get("body")), text_value(data.get("bodyHTML")),
            text_value(data.get("id")),
        )


@dataclass(frozen=True)
class Comment:
    id: str
    kind: str
    author: str
    created_at: str
    updated_at: str
    url: str
    body: str
    body_html: str
    reply_to: str = ""
    path: str = ""
    line: int | None = None
    review_state: str = ""

    @classmethod
    def from_rest(cls, kind: str, data: JsonObject) -> "Comment":
        line = data.get("line") or data.get("original_line")
        return cls(
            text_value(data.get("id")), kind, author_name(data.get("user")),
            text_value(data.get("created_at") or data.get("submitted_at")),
            text_value(data.get("updated_at") or data.get("submitted_at")),
            text_value(data.get("html_url")), text_value(data.get("body")),
            text_value(data.get("body_html")),
            text_value(data.get("in_reply_to_id")),
            text_value(data.get("path")),
            int(text_value(line)) if line else None,
            text_value(data.get("state")) if kind == "review" else "",
        )

    @classmethod
    def from_graphql(cls, data: JsonObject) -> "Comment":
        parent = data.get("replyTo")
        return cls(
            text_value(data.get("id")), "discussion_comment",
            author_name(data.get("author")), text_value(data.get("createdAt")),
            text_value(data.get("updatedAt")), text_value(data.get("url")),
            text_value(data.get("body")), text_value(data.get("bodyHTML")),
            text_value(json_object(parent).get("id")) if parent else "",
        )


DISCUSSION_FIELDS = """
    id number title closed author { login }
    createdAt updatedAt url body bodyHTML
"""
COMMENT_FIELDS = """
    id author { login } createdAt updatedAt url body bodyHTML replyTo { id }
"""
PAGE_FIELDS = "pageInfo { hasNextPage endCursor }"


class ThreadApi:
    """Paginated repository threads, normalized into typed records."""

    def __init__(self, api: GithubApi) -> None:
        self.api = api

    async def rest_pages(
        self, path: str, params: JsonObject | None = None,
        review: bool = False,
    ) -> AsyncIterator[JsonObject]:
        url: str | None = self.api.baseurl + path
        query: JsonObject | None = {"per_page": 100, **(params or {})}
        media = "github-commitcomment" if review else "github"
        while url:
            data, headers = await self.api.get(
                url, query,
                headers={"Accept": f"application/vnd.{media}.full+json"},
            )
            for item in json_objects(data):
                yield item
            url = self.next_link(headers.get("Link", ""))
            if url and not url.startswith(self.api.baseurl):
                raise RuntimeError(
                    "GitHub returned a pagination URL outside its API"
                )
            query = None

    @staticmethod
    def next_link(link: str) -> str | None:
        for match in re.finditer(r'<([^>]+)>\s*;\s*rel="([^"]+)"', link):
            if "next" in match[2].split():
                return match[1]
        return None

    async def threads(
        self, target: RepositoryTarget, selection: ThreadSelection, state: str,
    ) -> AsyncIterator[Thread]:
        if selection.numbers:
            for number in selection.numbers:
                item = await self.single(
                    target.repository, selection.kind, number
                )
                if state == "all" or item.state == state:
                    yield item
            return
        if selection.kind == "discussion":
            async for item in self.discussions(target):
                if state == "all" or item.state == state:
                    yield item
            return
        endpoint = "pulls" if selection.kind == "pr" else "issues"
        path = f"repos/{target.repository}/{endpoint}"
        params: JsonObject = {
            "state": state, "sort": "created", "direction": "asc",
        }
        async for item in self.rest_pages(path, params):
            if selection.kind == "issue" and "pull_request" in item:
                continue
            yield Thread.from_rest(target.repository, selection.kind, item)

    async def single(self, repository: str, kind: str, number: int) -> Thread:
        target = RepositoryTarget(repository, kind, number)
        if kind == "discussion":
            query = """
                query($owner: String!, $name: String!, $number: Int!) {
                    repository(owner: $owner, name: $name) {
                        discussion(number: $number) { FIELDS }
                    }
                }
            """.replace("FIELDS", DISCUSSION_FIELDS)
            data = await self.api.graphql(query, target.variables())
            repo = self.require_node(data.get("repository"), repository)
            item = self.require_node(
                repo.get("discussion"), f"discussion #{number}"
            )
            return Thread.from_graphql(repository, item)
        endpoint = "pulls" if kind == "pr" else "issues"
        data, _ = await self.api.get(
            f"{self.api.baseurl}repos/{repository}/{endpoint}/{number}",
            headers={"Accept": "application/vnd.github.full+json"},
        )
        item = json_object(data)
        is_pr = "pull_request" in item or kind == "pr"
        actual_kind = "pr" if is_pr else "issue"
        return Thread.from_rest(repository, actual_kind, item)

    @staticmethod
    def require_node(value: object, description: str) -> JsonObject:
        if value is None:
            raise ValueError(f"Not found or inaccessible: {description}")
        return json_object(value)

    async def graphql_pages(
        self, query: str, variables: JsonObject, path: tuple[str, ...],
    ) -> AsyncIterator[JsonObject]:
        cursor: str | None = None
        while True:
            data = await self.api.graphql(
                query, {**variables, "after": cursor}
            )
            for key in path:
                data = self.require_node(data.get(key), key)
            for item in json_objects(data.get("nodes")):
                yield item
            page = json_object(data.get("pageInfo"))
            if not page.get("hasNextPage"):
                return
            following = text_value(page.get("endCursor"))
            if not following or following == cursor:
                raise RuntimeError(
                    "Missing or unchanged GitHub pagination cursor"
                )
            cursor = following

    async def discussions(
        self, target: RepositoryTarget,
    ) -> AsyncIterator[Thread]:
        query = """
            query($owner: String!, $name: String!, $after: String) {
                repository(owner: $owner, name: $name) {
                    discussions(first: 100, after: $after,
                        orderBy: {field: CREATED_AT, direction: ASC}) {
                        PAGE nodes { FIELDS }
                    }
                }
            }
        """.replace("FIELDS", DISCUSSION_FIELDS).replace("PAGE", PAGE_FIELDS)
        async for item in self.graphql_pages(
            query, target.variables(), ("repository", "discussions"),
        ):
            yield Thread.from_graphql(target.repository, item)

    async def comments(self, thread: Thread) -> AsyncIterator[Comment]:
        if thread.kind == "discussion":
            async for comment in self.discussion_comments(thread.node_id):
                yield comment
            return
        prefix = f"repos/{thread.repository}"
        path = f"{prefix}/issues/{thread.number}/comments"
        async for item in self.rest_pages(path):
            yield Comment.from_rest("comment", item)
        if thread.kind == "pr":
            review_types = (
                ("reviews", "review"), ("comments", "review_comment"),
            )
            for endpoint, kind in review_types:
                async for item in self.rest_pages(
                    f"{prefix}/pulls/{thread.number}/{endpoint}", review=True,
                ):
                    # Empty approvals/dismissals are not textual comments.
                    if kind != "review" or item.get("body"):
                        yield Comment.from_rest(kind, item)

    async def discussion_comments(
        self, node_id: str,
    ) -> AsyncIterator[Comment]:
        query = """
            query($id: ID!, $after: String) {
                node(id: $id) { ... on Discussion {
                    comments(first: 100, after: $after) {
                        PAGE nodes {
                            FIELDS
                            replies(first: 100) { PAGE nodes { FIELDS } }
                        }
                    }
                } }
            }
        """.replace("FIELDS", COMMENT_FIELDS).replace("PAGE", PAGE_FIELDS)
        async for item in self.graphql_pages(
            query, {"id": node_id}, ("node", "comments"),
        ):
            yield Comment.from_graphql(item)
            replies = json_object(item.get("replies"))
            for reply in json_objects(replies.get("nodes")):
                yield Comment.from_graphql(reply)
            page = json_object(replies.get("pageInfo"))
            if page.get("hasNextPage"):
                async for reply in self.discussion_replies(
                    text_value(item["id"]), text_value(page.get("endCursor")),
                ):
                    yield reply

    async def discussion_replies(
        self, node_id: str, initial_cursor: str,
    ) -> AsyncIterator[Comment]:
        if not initial_cursor:
            raise RuntimeError("Missing GitHub reply pagination cursor")
        query = """
            query($id: ID!, $after: String) {
                node(id: $id) { ... on DiscussionComment {
                    replies(first: 100, after: $after) {
                        PAGE nodes { FIELDS }
                    }
                } }
            }
        """.replace("FIELDS", COMMENT_FIELDS).replace("PAGE", PAGE_FIELDS)
        # The first reply page was bundled with the parent comment.
        cursor = initial_cursor
        while True:
            data = await self.api.graphql(
                query, {"id": node_id, "after": cursor}
            )
            node = self.require_node(data.get("node"), "discussion comment")
            replies = json_object(node.get("replies"))
            for item in json_objects(replies.get("nodes")):
                yield Comment.from_graphql(item)
            page = json_object(replies.get("pageInfo"))
            if not page.get("hasNextPage"):
                return
            following = text_value(page.get("endCursor"))
            if not following or following == cursor:
                raise RuntimeError(
                    "Missing or unchanged GitHub reply pagination cursor"
                )
            cursor = following


@dataclass(frozen=True)
class ContentLink:
    url: str
    label: str = ""


class HtmlLinks(HTMLParser):
    """Read upload links, image sources and embedded video sources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[ContentLink] = []
        self.anchor_url = ""
        self.anchor_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        values: dict[str, str] = {key: value for key, value in attrs if value}
        if tag == "a":
            self.anchor_url = values.get("href", "")
            self.anchor_text = []
        for name in ("src", "data-canonical-src", "poster"):
            if name in values:
                self.links.append(
                    ContentLink(values[name], values.get("alt", ""))
                )

    def handle_data(self, data: str) -> None:
        if self.anchor_url:
            self.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor_url:
            label = "".join(self.anchor_text).strip()
            self.links.append(ContentLink(self.anchor_url, label))
            self.anchor_url = ""
            self.anchor_text = []


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str
    source_url: str


class AttachmentFinder:
    """Find GitHub uploads linked in post bodies and comments."""

    @staticmethod
    def is_upload(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https"):
            return False
        if host == "github.com":
            return bool(re.match(
                r"/(?:user-attachments/(?:assets|files)/[^/]+|"
                r"[^/]+/[^/]+/files/\d+/[^/]+)", parsed.path,
            ))
        if host in ("user-images.githubusercontent.com",
                    "private-user-images.githubusercontent.com"):
            return bool(parsed.path.strip("/"))
        if host == "cloud.githubusercontent.com":
            return parsed.path.startswith("/assets/")
        return bool(re.fullmatch(
            r"github-production-(?:user-asset|repository-file)"
            r"(?:-[\w-]+)?\.s3(?:[.-][\w-]+)?\.amazonaws\.com", host,
        ))

    @staticmethod
    def raw_links(body: str) -> list[ContentLink]:
        # Rendered HTML handles Markdown titles, reference links, escaped or
        # balanced parentheses, and whitespace. Raw scanning also finds plain
        # URLs and uploads pasted inside code blocks, omitted by rendering.
        links: list[ContentLink] = []
        pattern = r'https?://[^\s<>"\x27`]+'
        for match in re.finditer(pattern, html.unescape(body)):
            url = match[0]
            url = re.sub(r"\\([()])", r"\1", url)
            url = url.rstrip(".,;!?")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            links.append(ContentLink(url))
        return links

    def find(
        self, body: str, body_html: str, source_url: str,
    ) -> tuple[Attachment, ...]:
        parser = HtmlLinks()
        parser.feed(body_html)
        candidates = parser.links + self.raw_links(body)
        found: dict[str, Attachment] = {}
        for link in candidates:
            url = html.unescape(link.url)
            if not self.is_upload(url) or url in found:
                continue
            parsed = urllib.parse.urlsplit(url)
            basename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            # UUID asset links do not expose the original filename. Use the
            # visible link label/alt text when available, otherwise its ID.
            opaque = bool(re.search(r"/user-attachments/assets/", parsed.path))
            name = link.label if opaque and link.label else basename
            found[url] = Attachment(name, url, source_url)
        return tuple(found.values())


class ThreadPrinter:

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.finder = AttachmentFinder()
        self.seen_urls: set[str] = set()

    def emit_json(self, record: JsonObject) -> None:
        print(json.dumps(record, ensure_ascii=False))

    def thread(self, item: Thread) -> None:
        if self.args.attachments:
            return
        if self.args.jsonl:
            self.emit_json({"type": item.kind, **asdict(item)})
        elif self.args.urls:
            print(item.url)
        else:
            print(
                f"{item.repository} {item.kind} #{item.number} "
                f"[{item.state}] {item.title}\n  {item.url}"
            )
            if self.args.verbose:
                print(
                    f"  @{item.author} created {item.created_at}; "
                    f"updated {item.updated_at}"
                )
            if self.args.comments and item.body:
                print(item.body)

    def comment(self, thread: Thread, item: Comment) -> None:
        if self.args.attachments or not self.args.comments:
            return
        if self.args.jsonl:
            self.emit_json({
                "type": item.kind, "repository": thread.repository,
                "thread_kind": thread.kind, "number": thread.number,
                **asdict(item),
            })
        elif self.args.urls:
            print(item.url)
        else:
            extra = f" reply to {item.reply_to}" if item.reply_to else ""
            if item.path:
                extra += f" {item.path}:{item.line or '?'}"
            if item.review_state:
                extra += f" [{item.review_state}]"
            print(
                f"\n  {item.kind} @{item.author} {item.created_at}{extra}"
                f"\n  {item.url}"
            )
            print(item.body)

    def attachments(self, thread: Thread, content: Thread | Comment) -> None:
        if not self.args.attachments:
            return
        uploads = self.finder.find(
            content.body, content.body_html, content.url
        )
        for item in uploads:
            # URL-only listings are globally unique; other formats retain each
            # body/comment occurrence so callers can trace the attachment.
            if self.args.urls:
                if item.url not in self.seen_urls:
                    print(item.url)
                    self.seen_urls.add(item.url)
            elif self.args.jsonl:
                self.emit_json({
                    "type": "attachment", "repository": thread.repository,
                    "thread_kind": thread.kind, "number": thread.number,
                    **asdict(item),
                })
            else:
                print(f"{thread.repository} {thread.kind} #{thread.number} "
                      f"{item.name}\n  {item.url}\n  from {item.source_url}")


async def list_threads(api: GithubApi, args: argparse.Namespace) -> None:
    client = ThreadApi(api)
    printer = ThreadPrinter(args)
    for name in args.REPOS:
        target = RepositoryTarget.parse(name)
        selections = ThreadSelection.from_args(args)
        if target.kind and target.number is not None:
            selections = (ThreadSelection(target.kind, (target.number,)),)
        for selection in selections:
            async for thread in client.threads(target, selection, args.state):
                printer.thread(thread)
                printer.attachments(thread, thread)
                if args.comments or args.attachments:
                    async for comment in client.comments(thread):
                        printer.comment(thread, comment)
                        printer.attachments(thread, comment)


def getjs(js, path, default=""):
    """
    path is a 'dotted path', with each DOT representing one level
    in the nested json dictionary.

    Returns the nested value pointed to by 'path'.
    """
    if not js:
        return default
    if path.find('.')>=0:
        this, rest = path.split('.', 1)
        return getjs(js[this], rest)
    return js[path]


async def getlimits(api):
    """
    Get the current rate limit status.
    """
    # guest  auth
    #    60  5000  (per hour) rate
    #    60  5000  (per hour) resources.core
    #     0  5000  (per hour) resources.graphql
    #    10    30  (per minute) resources.search
    js = await api.getlimits()
    tnow = time.time()
    for path in (
        'rate', 'resources.core', 'resources.search', 'resources.graphql',
    ):
        print("%5d of %5d  %5d(sec) %s" % (
            getjs(js, path + ".remaining"), getjs(js, path + ".limit"),
            getjs(js, path + ".reset") - tnow, path,
        ))


def findlast(link):
    """
    Extract the last pagenumber from the 'Link' headers.
    """
    if not link:
        return
    last = None
    for l in link.split(", "):
        url, rel = l.split("; ", 1)
        if rel == 'rel="last"':
            last = url
    if not last:
        return
    m = re.search(r'[?&]page=(\d+)', last)
    if not m:
        return
    return int(m.group(1))


def printresult(args, where, items):
    """
    Print one response batch of query results.
    """
    for item in items:
        if where == 'code':
            if args.urls:
                html_url = getjs(item, 'html_url') or ''
                match = re.compile('blob/([0-9a-f]+)/').search(html_url)
                if match:
                    blob = match.group(1)
                    print("https://raw.githubusercontent.com/%s/%s/%s" % (
                        getjs(item, "repository.full_name"), blob,
                        getjs(item, "path"),
                    ))
            else:
                print("%-20s %s" % (
                    getjs(item, "repository.full_name"), getjs(item, "path"),
                ))
        elif where == 'issue':
            print("%-20s %s" % (getjs(item, "html_url"), getjs(item, "body")))
        elif where == 'repo':
            if args.urls:
                print(getjs(item, "html_url"))
            else:
                print("%8d %-25s  %s" % (
                    getjs(item, "size"), getjs(item, "full_name"),
                    getjs(item, "description") or "",
                ))
        elif where == 'user':
            print("%s" % (getjs(item, "login")))
        else:
            print(item)
            # commit


async def querygithub(api, args):
    """
    Query a specific domain ( repo, user, issue, code ).
    """
    if args.where == 'repo':
        where = 'repository'
    else:
        where = args.where

    js, hdrs = await api.query(where, args.query)
    lastpage = findlast(hdrs.get('Link'))
    print("FOUND: %d items in %d pages" % (
        getjs(js, 'total_count'), lastpage or 1,
    ))
    printresult(args, args.where, js["items"])

    if not lastpage or not args.all:
        return
    for p in range(2, lastpage+1):
        js, _ = await api.query(where, args.query, p)
        printresult(args, args.where, js["items"])


async def createrepo(api, args, name, desc):
    result = await api.addrepo(name, desc)
    print("id = %s, nodeid = %s" % (result.get('id'), result.get('node_id')))


def printrepoinfo(repo, namefield, args):
    if args.urls:
        print(getjs(repo, "html_url"))
    elif args.verbose:
        print("%10d %5d %5d [%s ; %s] %-25s %s" % (
            getjs(repo, "size"), getjs(repo, "stargazers_count"),
            getjs(repo, "forks_count"), getjs(repo, "created_at"),
            getjs(repo, "updated_at"), getjs(repo, namefield),
            getjs(repo, "description"),
        ))
    else:
        print("%10d %-25s %s" % (
            getjs(repo, "size"), getjs(repo, namefield),
            getjs(repo, "description"),
        ))


async def printrepolist(api, jslist, args):
    """
    Print one response batch of user repositories
    """
    if args.verbose:
        print(
            f"{'size':>10} {'stars':>5} {'forks':>5} "
            f"[{'created':>20} ; {'updated':>20}] {'name':<25} {'desc'}"
        )
    for repo in jslist:
        if args.all or not repo["fork"]:
            printrepoinfo(repo, "name", args)
            if args.network:
                await recurse_network(
                    api, args, getjs(repo, "owner.login"),
                    repo.get("default_branch"), repo.get("forks_url"),
                    repo.get("forks"),
                )


async def listrepos(api, user, args):
    """
    List the repositories for the specified user
    """
    js, hdrs = await api.list(user)
    lastpage = findlast(hdrs.get('Link'))
    await printrepolist(api, js, args)
    if not lastpage or not args.all:
        return
    for p in range(2, lastpage+1):
        js, _ = await api.list(user, p)
        await printrepolist(api, js, args)


def sanitizereponame(name):
    name = re.sub(r'.*github.com/', '', name)
    m = re.match(r'^[^/]+/[^/]+', name)
    if not m:
        return
    return m.group(0)


async def inforepos(api, args):
    for name in args.REPOS:
        try:
            repo = sanitizereponame(name)
            if not repo:
                print("failed to parse repository name from '%s'" % name)
                continue
            repo, hdrs = await api.info(repo)
            printrepoinfo(repo, "full_name", args)
            if args.network and repo.get("forks"):
                await recurse_network(
                    api, args, getjs(repo, "owner.login"),
                    repo.get("default_branch"), repo.get("forks_url"),
                    repo.get("forks"),
                )
        except Exception as e:
            print("%s -- %s" % (name, e))
            if args.debug:
                raise


async def recurse_network(
    api, args, baseuser, default_branch, url, nrforks, level=0,
):
    per_page = 100

    for pagenr in range((nrforks-1)//per_page+1):
        forks, hdrs = await api.get(
            url, dict(per_page=per_page, page=pagenr + 1)
        )
        for fork in forks:
            default_branch_fork = fork.get("default_branch")

            try:
                if args.branch:
                    branch = args.branch.split(':')
                    compare_branch_base = branch[0]
                    compare_branch_head = (
                        branch[1] if len(branch) == 2 else branch[0]
                    )
                else:
                    compare_branch_base = default_branch
                    compare_branch_head = default_branch_fork

                compareurl = fork.get("compare_url")
                compareurl = compareurl.replace(
                    "{base}", "%s:%s" % (baseuser, compare_branch_base)
                )
                compareurl = compareurl.replace("{head}", compare_branch_head)

                compare, hdrs = await api.get(compareurl)
            except Exception as e:
                compare = None

            printforkinfo(fork, compare, level)
            if fork.get("forks"):
                await recurse_network(
                    api, args, getjs(fork, "owner.login"), default_branch_fork,
                    fork.get("forks_url"), fork.get("forks"), level + 1,
                )


def printforkinfo(fork, compare, indentlevel):
    print("%10d  %+3d %+3d  [%s] %s%s" % (
        getjs(fork, "size"), getjs(compare, "ahead_by", 0),
        -getjs(compare, "behind_by", 0),
        getjs(fork, "pushed_at"),
        "  " * indentlevel, getjs(fork, "full_name")))


def add_thread_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "repository threads (all pages and all states by default)"
    )
    group.add_argument(
        '--issues', action='store_true',
        help='list repository issues (excludes PRs)',
    )
    group.add_argument(
        '--prs', '--pulls', action='store_true', help='list pull requests',
    )
    group.add_argument(
        '--discussions', action='store_true',
        help='list discussions (requires a token)',
    )
    group.add_argument(
        '--issue', action='append', type=positive_number, metavar='NUMBER',
        help='select an issue number; repeat to select several',
    )
    group.add_argument(
        '--pr', '--pull', action='append', type=positive_number,
        metavar='NUMBER',
        help='select a PR number; repeat to select several',
    )
    group.add_argument(
        '--discussion', action='append', type=positive_number,
        metavar='NUMBER',
        help='select a discussion number; repeat to select several',
    )
    group.add_argument(
        '--comments', action='store_true',
        help='include bodies and all comments, including PR reviews '
             'and discussion replies',
    )
    group.add_argument(
        '--attachments', '--files', action='store_true',
        help='list only uploaded file/image/video links from bodies and all '
             'comments; defaults to issues',
    )
    group.add_argument(
        '--state', choices=('all', 'open', 'closed'), default='all',
        help='filter thread state (default: all)',
    )
    group.add_argument(
        '--jsonl', action='store_true',
        help='emit structured JSON records, one per line (thread modes only)',
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Tool for interrogating GitHub'
    )
    parser.add_argument(
        '--auth', type=str,
        help='GitHub token (also GH_TOKEN, GITHUB_TOKEN, '
             'or ~/.github_cmdline_rc)',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='print more info, such as times',
    )
    parser.add_argument(
        '--trace', action='store_true', help='print http requests',
    )
    parser.add_argument(
        '--debug', action='store_true', help='print full exception',
    )
    parser.add_argument(
        '--limits', action='store_true', help='print rate limit status',
    )
    parser.add_argument(
        '--list', '-l', type=str,
        help='List repositories for the specified user',
    )
    parser.add_argument(
        '--network', '-n', action='store_true',
        help='Show list of all forks and their state',
    )
    parser.add_argument(
        '--urls', '-u', action='store_true', help='output url listing',
    )
    add_search_arguments(parser)
    add_thread_arguments(parser)
    return parser


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--all', '-a', action='store_true',
        help='Request all search/repository pages (searches capped at 1000); '
             'thread modes always fetch all pages',
    )
    parser.add_argument(
        '--where', '-w', type=str, default='code',
        help='What type of object to search for: '
             'code, user, repo, commit, issue',
    )
    parser.add_argument(
        '--branch', '-b', type=str,
        help='Force branch to compare (can also be provided as <base>:<head>, '
             'e.g. "master:main")',
    )
    parser.add_argument(
        '--query', '-q', type=str,
        help='in:{path,file} language:{js,c,python,...} filename:substring '
             'extension:ext user: repo: size:',
    )
    parser.add_argument(
        '--create', '-c', type=str,
        help='Create a new repository, name:description',
    )
    parser.add_argument(
        '--slow', action='store_true',
        help='slow down requests to one per 5 seconds.',
    )
    parser.add_argument(
        '--pace', type=int,
        help='msec to wait between requests, to avoid running out of limits.',
    )
    parser.add_argument(
        'REPOS', nargs='*', type=str,
        help='OWNER/REPO, repository URLs, '
             'or individual issue/PR/discussion URLs',
    )


def load_config() -> JsonObject:
    path = os.path.expanduser('~/.github_cmdline_rc')
    try:
        with open(path, encoding='utf-8') as fh:
            return json_object(json.load(fh))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"GitHub config: {exc}", file=sys.stderr)
        return {}


def uses_thread_mode(args: argparse.Namespace) -> bool:
    flags = (args.issues, args.prs, args.discussions, args.issue, args.pr,
             args.discussion, args.comments, args.attachments)
    return any(flags) or any(
        re.search(
            r'github\.com/[^/]+/[^/]+/(?:issues|pull|discussions)/\d+', name
        )
        for name in args.REPOS
    )


def validate_thread_mode(
    args: argparse.Namespace, parser: argparse.ArgumentParser,
) -> None:
    if not args.REPOS:
        parser.error(
            'Thread modes require at least one OWNER/REPO or thread URL'
        )
    if args.list or args.limits or args.query or args.create or args.network:
        parser.error(
            'Thread modes cannot be combined with repository listing, '
            'search, creation, limits, or network'
        )
    if args.jsonl and args.urls:
        parser.error('Choose --jsonl or --urls')
    targets = tuple(RepositoryTarget.parse(name) for name in args.REPOS)
    has_numbers = any((args.issue, args.pr, args.discussion))
    if any(target.kind for target in targets) and has_numbers:
        parser.error(
            'Use individual thread URLs or numeric selectors, '
            'rather than mixing both'
        )
    if not args.auth and (args.discussions or args.discussion or any(
        target.kind == 'discussion' for target in targets
    )):
        parser.error(
            'Discussions require a GitHub token (--auth, GH_TOKEN, '
            'GITHUB_TOKEN, or ~/.github_cmdline_rc)'
        )


async def run_command(api: GithubApi, args: argparse.Namespace) -> None:
    if uses_thread_mode(args):
        await list_threads(api, args)
    elif args.list:
        await listrepos(api, args.list, args)
    elif args.limits:
        await getlimits(api)
    elif args.query:
        await querygithub(api, args)
    elif args.create:
        name, desc = args.create.split(':', 1)
        await createrepo(api, args, name, desc)
    else:
        await inforepos(api, args)


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.slow:
        args.pace = 5000
    if args.pace is not None and args.pace < 0:
        parser.error('--pace must be nonnegative')
    cfg = load_config()
    if not args.auth:
        args.auth = (
            os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN')
            or cfg.get('auth')
        )
    if uses_thread_mode(args):
        try:
            validate_thread_mode(args, parser)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.jsonl:
        parser.error('--jsonl requires a thread mode')
    api = GithubApi(args)
    try:
        await run_command(api, args)
    except (ValueError, RuntimeError, aiohttp.ClientError) as exc:
        if args.debug:
            raise
        print(f'GitHub: {exc}', file=sys.stderr)
        return 1
    finally:
        await api.close()
    return 0

if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('GitHub: interrupted', file=sys.stderr)
        sys.exit(130)

