githubtool
==========

A commandline tool for searching GitHub and inspecting repositories, forks,
issues, pull requests, and discussions.

Search code, repositories, users, commits, or issues; list repositories owned by
a user; compare forks with their upstream repository; and list thread contents
or uploaded attachments.

Requires Python 3.10 or newer and `aiohttp`.

Install
=======

Install from [PyPI](https://pypi.org/project/github-tool/) using pip:

    pip install github-tool

Then execute it like this:

    github -l nlitsme

Or install the dependency and run directly from the source directory:

    python3 -m pip install aiohttp
    python3 github_tool.py -l nlitsme

Use `github --help` to list all commandline options.

Authentication
==============

Supply a GitHub token through one of these sources, in order of precedence:

1. The `--auth TOKEN` commandline argument.
2. The `GH_TOKEN` environment variable.
3. The `GITHUB_TOKEN` environment variable.
4. The `auth` value in the JSON file `~/.github_cmdline_rc`:

   ```json
   {"auth": "YOUR_GITHUB_TOKEN"}
   ```

For example, after setting `GH_TOKEN` in your environment:

    github --discussions OWNER/REPO

Discussions require a token because they use the GraphQL API. Issue and pull
request commands can access public repositories without a token; private
repositories require a token with access to that repository. Tokens are sent as
Bearer credentials; the old `username:password` form is not supported.

Repository and search examples
==============================

List repositories owned by `nlitsme`:

    github -l nlitsme

Fetch all repository pages, including repositories that are forks:

    github -a -l nlitsme

Without `-a`, user listings show the first page and omit forks.

List files containing a specific string:

    github -a -q specific_string

Find very large repositories:

    github -a -w repo -q "size:>8000000"

Find very large files:

    github -a -w code -q "in:path zip size:>500000000"
    github -a -w code -q "in:path zip size:400000000..500000000"

Select the search category with `--where` / `-w`: `code` (the default), `repo`,
`user`, `commit`, or `issue`. Pass GitHub search qualifiers in `--query` / `-q`.
`--all` / `-a` fetches all available search pages, subject to the API's
1,000-result search cap.

Print size and description for a list of repositories:

    github nlitsme/ubidump https://github.com/nlitsme/github

List information for all forks of a repository, including how many commits each
fork is ahead of or behind the upstream branch:

    github --network https://github.com/nlitsme/extfstools

Or inspect forks for a user's repositories:

    github -n -l nlitsme

Use `--branch` / `-b` to select a comparison branch, or specify base and head
branches separately:

    github --network --branch master:main OWNER/REPO

Create a public repository in the authenticated user's account:

    github --create "new-repository:Repository description"

Issues, pull requests, and discussions
====================================

List one or more kinds of repository threads:

    github --issues OWNER/REPO
    github --prs OWNER/REPO
    github --discussions OWNER/REPO
    github --issues --prs --discussions OWNER/REPO

`--issues` excludes pull requests. `--pulls` is an alias for `--prs`.
Thread listings fetch every page and include open and closed threads by default;
`-a` is unnecessary. Filter with `--state open` or `--state closed`:

    github --issues --prs --state open OWNER/REPO

Select individual threads by number. Repeat selectors to select several:

    github --issue 12 --issue 34 OWNER/REPO
    github --pr 56 OWNER/REPO
    github --discussion 78 OWNER/REPO

`--pull` is an alias for `--pr`. Numeric selectors restrict their corresponding
thread kind and can be combined across kinds:

    github --issue 12 --pr 56 OWNER/REPO

Repository names and repository URLs are accepted. Multiple targets can be
provided in one command:

    github --issues OWNER/REPO https://github.com/OTHER/REPO

Individual issue, pull request, and discussion URLs select their thread directly:

    github https://github.com/OWNER/REPO/issues/12
    github --comments https://github.com/OWNER/REPO/pull/56
    github https://github.com/OWNER/REPO/discussions/78

Do not mix individual thread URLs with numeric selectors. State filtering also
applies to individually selected threads. Thread commands cannot be combined
with repository listing, search, creation, rate-limit status, or fork inspection.

Bodies and comments
-------------------

Include thread bodies and all pages of comments with `--comments`:

    github --issues --prs --discussions --comments OWNER/REPO

For pull requests, this includes conversation comments, review bodies, and
inline review comments. Reviews without body text are omitted. For discussions,
it includes comments and their replies, including additional reply pages.

When used without a thread selector, `--comments` defaults to issues:

    github --comments OWNER/REPO

Uploaded attachments
--------------------

List uploaded files, images, and videos linked from thread bodies and all
comments:

    github --attachments OWNER/REPO
    github --attachments --issue 12 --issue 34 OWNER/REPO
    github --attachments --prs --discussions OWNER/REPO

`--files` is an alias for `--attachments`. Attachment mode defaults to issues
and always scans comments, even without `--comments`. For pull requests and
discussions, it also scans the reviews and replies described above.

Only recognized GitHub upload URLs are included: current user attachments,
repository upload links, legacy user images, and supported GitHub upload storage
URLs. Ordinary external links and repository files are excluded. Links are
extracted from rendered HTML and raw body text, including plain URLs and URLs in
code blocks. This command lists links; it does not download the files.

Default output includes the attachment name, URL, and source post URL. Where an
upload URL contains only an opaque asset ID, its visible label or image alt text
is used when available; otherwise the ID is shown.

Print only attachment URLs, deduplicated across the entire command:

    github --attachments --urls https://github.com/OWNER/REPO/issues/12

Text and JSONL output retain separate occurrences in different bodies or
comments so each attachment can be traced to its source.

Output formats
--------------

Thread commands normally print a summary and URL. `--verbose` / `-v` adds author
and timestamp information. `--urls` / `-u` prints URLs only; with `--comments`,
this also includes comment URLs.

Use `--jsonl` for one JSON object per line:

    github --issues --prs --comments --jsonl OWNER/REPO > threads.jsonl
    github --attachments --jsonl OWNER/REPO > attachments.jsonl

Thread records have a `type` of `issue`, `pr`, or `discussion`, plus repository,
number, title, state, author, timestamps, URL, body, rendered body HTML, and node
ID fields. Bodies are included in JSONL thread records even without `--comments`;
that option is still needed to fetch comment records.

Comment types are `comment`, `review`, `review_comment`, and
`discussion_comment`. Records identify their repository, thread kind, and thread
number, with comment content, author, timestamps, reply relationship, and review
location or state where available. Attachment records have `type: attachment`,
`repository`, `thread_kind`, `number`, `name`, `url`, and `source_url` fields.

`--jsonl` is available only for thread commands and cannot be combined with
`--urls`. Diagnostics go to stderr, keeping thread JSONL and URL output usable
in pipelines.

Rate limits and diagnostics
===========================

Show the current API rate-limit status:

    github --limits

All commands share automatic rate-limit handling. Requests wait for exhausted
primary limits to reset and honor `Retry-After` or back off on secondary limits.
Wait messages include the delay and UTC resume time and are written to stderr.
Persistent secondary limits fail after five retries. Reads also retry temporary
connection failures and HTTP 500, 502, 503, and 504 responses up to five times.

Add a minimum interval between requests with `--pace`, in milliseconds:

    github --pace 1000 --issues --comments OWNER/REPO

`--slow` sets the interval to five seconds and overrides `--pace`.
`--trace` prints HTTP request and transfer diagnostics to stderr; `--debug`
enables full exception tracebacks. Reported command failures exit with status 1;
interruption with Ctrl-C exits with status 130.

TODO
====

 * show network of a list of repos by querying github.com/<user>/<repo>/network/meta
 * make multiple queries from a single commandline possible.
 * add option to list all forks, and their current branches ( and forks of forks )
 * '-l' should take its arg from the REPOS list, and also understand githuburls

AUTHOR
======

Willem Hengeveld <itsme@xs4all.nl>
