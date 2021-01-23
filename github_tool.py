"""
Author: Willem Hengeveld <itsme@xs4all.nl>

Commandline tool for searching github.

* find all very large repositories:
github -a -w repo -q "size:>8000000"


* find all very large files:
github -a -w code -q "in:path zip size:>500000000"

TODO:  make multiple queries from a single commandline possible.

TODO: add option to list all forks, and their current branches ( and forks of forks )

"""
import asyncio
import aiohttp.connector
import aiohttp
import os.path
import html.parser
import urllib.parse
import time
import re
import json
from collections import defaultdict


class GithubApi:
    """
    Object for accessing github.
    Currently several search functions, user repository list and ratelimit status are implemented.
    """
    def __init__(self, loop, args):
        """
        The constructor takes a runloop, and a generic args object.
        Currently 'args.auth' is used from args.
        """
        self.baseurl = "https://api.github.com/"
        self.loop = loop
        self.args = args

        self.client = None

        # this list can also be obtained from 'baseurl'
        self.d = {
            "authorizations_url": "https://api.github.com/authorizations",
            "code_search_url": "https://api.github.com/search/code?q={query}{&page,per_page,sort,order}",
            "commit_search_url": "https://api.github.com/search/commits?q={query}{&page,per_page,sort,order}",
            "current_user_authorizations_html_url": "https://github.com/settings/connections/applications{/client_id}",
            "current_user_repositories_url": "https://api.github.com/user/repos{?type,page,per_page,sort}",
            "current_user_url": "https://api.github.com/user",
            "emails_url": "https://api.github.com/user/emails",
            "emojis_url": "https://api.github.com/emojis",
            "events_url": "https://api.github.com/events",
            "feeds_url": "https://api.github.com/feeds",
            "followers_url": "https://api.github.com/user/followers",
            "following_url": "https://api.github.com/user/following{/target}",
            "gists_url": "https://api.github.com/gists{/gist_id}",
            "hub_url": "https://api.github.com/hub",
            "issue_search_url": "https://api.github.com/search/issues?q={query}{&page,per_page,sort,order}",
            "issues_url": "https://api.github.com/issues",
            "keys_url": "https://api.github.com/user/keys",
            "label_search_url": "https://api.github.com/search/labels?q={query}&repository_id={repository_id}{&page,per_page}",
            "notifications_url": "https://api.github.com/notifications",
            "organization_repositories_url": "https://api.github.com/orgs/{org}/repos{?type,page,per_page,sort}",
            "organization_teams_url": "https://api.github.com/orgs/{org}/teams",
            "organization_url": "https://api.github.com/orgs/{org}",
            "public_gists_url": "https://api.github.com/gists/public",
            "rate_limit_url": "https://api.github.com/rate_limit",
            "repository_search_url": "https://api.github.com/search/repositories?q={query}{&page,per_page,sort,order}",
            "repository_url": "https://api.github.com/repos/{owner}/{repo}",
            "starred_gists_url": "https://api.github.com/gists/starred",
            "starred_url": "https://api.github.com/user/starred{/owner}{/repo}",
            #"team_url": "https://api.github.com/teams",
            "user_organizations_url": "https://api.github.com/user/orgs",
            "user_repositories_url": "https://api.github.com/users/{user}/repos{?type,page,per_page,sort}",
            "user_search_url": "https://api.github.com/search/users?q={query}{&page,per_page,sort,order}",
            "user_url": "https://api.github.com/users/{user}",
        }
    def getclient(self):
        if not self.client:
            hdrs = dict(Accept='application/vnd.github.v3+json')

            moreargs = dict()
            if self.args.auth:
                if self.args.auth.find(':')>0:
                    user, pw = self.args.auth.split(':', 1)
                    # use basic authentication - using a plaintext password.
                    moreargs['auth'] = aiohttp.BasicAuth(user, pw)
                else:
                    # use the OAuth token
                    hdrs['Authorization'] = 'token %s' % self.args.auth

            # note: 2 more authentication methods exist:
            #  * with the OAuth token passed with the 'access_token' url query parameter
            #  * with the client_id + client_secret query parameters


            self.client = aiohttp.ClientSession(loop=self.loop, headers=hdrs, **moreargs)
        return self.client

    def close(self):
        """
        Make sure we close the client when this object is destroyed.
        """
        return self.getclient().close()

    async def get(self, path, params=dict()):
        """
        Return json response + http header list.
        """
        r = await self.getclient().get(path, params=params)
        try:
            js = await r.json()
        except Exception as e:
            # NOTE: this is a workaround for a bug in aiohttp v2 handling of large http headers
            #       using aiohttp v3 solves this. But since v2 is supplied with python, i
            #       include this workaround here.
            if r._content.startswith(b'\x1f\x8b'):
                import zlib
                data = zlib.decompress(r._content, wbits=31)
                js = json.loads(data)

        r.close()

        if r.status!=200:
            print("HTTP status", r.status, js)
            print(r)
            raise Exception(js.get('message'))

        return js, r.headers

    async def loadapi(self):
        """
        Load the api map from github.
        """
        self.d = await self.get(self.baseurl)

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
          "private": false,
          "has_issues": true,
          "has_projects": true,
          "has_wiki": true
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

        todo: add option for: 'Accept: application/vnd.github.v3.text-match+json' \
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
    for path in ('rate', 'resources.core','resources.search', 'resources.graphql'):
        print("%5d of %5d  %5d(sec) %s" % (getjs(js, path+".remaining"), getjs(js, path+".limit"), getjs(js, path+".reset") - tnow, path))


def findlast(link):
    """
    Extract the last pagenumber from the 'Link' headers.
    """
    if not link:
        return
    for l in link.split(", "):
        url, rel = l.split("; ", 1)
        if rel == 'rel="last"':
            last = url
    if not last:
        return
    m = re.search(r'&page=(\d+)', last)
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
                print("https://raw.githubusercontent.com/%s/master/%s" % (getjs(item, "repository.full_name"), getjs(item, "path")))
            else:
                print("%-20s %s" % (getjs(item, "repository.full_name"), getjs(item, "path")))
        elif where == 'issue':
            print("%-20s %s" % (getjs(item, "html_url"), getjs(item, "body")))
        elif where == 'repo':
            if args.urls:
                print(getjs(item, "html_url"))
            else:
                print("%8d %-25s  %s" % (getjs(item, "size"), getjs(item, "full_name"), getjs(item, "description") or ""))
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
    print("FOUND: %d items in %d pages" % (getjs(js, 'total_count'), lastpage or 1))
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
        print("%10d [%s ; %s] %-25s %s" % (getjs(repo, "size"), getjs(repo, "created_at"), getjs(repo, "updated_at"), getjs(repo, namefield), getjs(repo, "description")))
    else:
        print("%10d %-25s %s" % (getjs(repo, "size"), getjs(repo, namefield), getjs(repo, "description")))


async def printrepolist(api, jslist, args):
    """
    Print one response batch of user repositories
    """
    for repo in jslist:
        if args.all or not repo["fork"]:
            printrepoinfo(repo, "name", args)
            if args.network:
                await recurse_network(api, getjs(repo, "owner.login"), repo.get("forks_url"), repo.get("forks"))


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
                await recurse_network(api, getjs(repo, "owner.login"), repo.get("forks_url"), repo.get("forks"))
        except Exception as e:
            print("%s -- %s" % (name, e))
            if args.debug:
                raise

async def recurse_network(api, baseuser, url, nrforks, level=0):
    for pagenr in range((nrforks-1)//30+1):
        forks, hdrs = await api.get(url, dict(per_page=30, page=pagenr+1))
        for fork in forks:
            try:
                compareurl = fork.get("compare_url")
                compareurl = compareurl.replace("{base}", baseuser+":master")
                compareurl = compareurl.replace("{head}", "master")
                compare, hdrs = await api.get(compareurl)
            except Exception as e:
                compare = None

            printforkinfo(fork, compare, level)
            if fork.get("forks"):
                await recurse_network(api, baseuser, fork.get("forks_url"), fork.get("forks"), level+1)


def printforkinfo(fork, compare, indentlevel):
    print("%10d  %+3d %+3d  [%s] %s%s" % (
        getjs(fork, "size"), getjs(compare, "ahead_by", 0), -getjs(compare, "behind_by", 0),
        getjs(fork, "pushed_at"),
        "  " * indentlevel, getjs(fork, "full_name")))

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Tool for interogating github')
    parser.add_argument('--auth', type=str, help='OAuth token, or "username:password"')
    parser.add_argument('--verbose', '-v', action='store_true', help='print more info, such as times')
    parser.add_argument('--debug', action='store_true', help='print full exception')
    parser.add_argument('--limits', action='store_true', help='print rate limit status')
    parser.add_argument('--list', '-l', type=str, help='List repositories for the specified user')
    parser.add_argument('--network', '-n', action='store_true', help='Show list of all forks and their state')
    parser.add_argument('--urls', '-u', action='store_true', help='output url listing')
    parser.add_argument('--all', '-a', action='store_true', help='Request all pages, up to 1000 items')
    parser.add_argument('--where', '-w', type=str, default='code', help='What type of object to search for: code, user, repo, commit, issue')
    parser.add_argument('--query', '-q', type=str, help='in:{path,file} language:{js,c,python,...} filename:substring extension:ext user: repo: size:')
    parser.add_argument('--create', '-c', type=str, help='Create a new repository, name:description')
    parser.add_argument('REPOS', nargs='*', type=str, help='repository list to summarize')
    args = parser.parse_args()

    try:
        with open(os.getenv("HOME")+"/.github_cmdline_rc") as fh:
            cfg = json.load(fh)
    except Exception as e:
        print("ERROR", e)
        cfg = dict()

    if not args.auth:
        args.auth = cfg.get('auth')

    loop = asyncio.get_event_loop()

    api = GithubApi(loop, args)

    tasks = [ ]
    if args.list:
        tasks.append(listrepos(api, args.list, args))
    elif args.limits:
        tasks.append(getlimits(api))
    elif args.query:
        tasks.append(querygithub(api, args))
    elif args.create:
        name, desc = args.create.split(':', 1)
        tasks.append(createrepo(api, args, name, desc))
    else:
        tasks.append(inforepos(api, args))

    loop.run_until_complete(asyncio.gather(*tasks))

    loop.run_until_complete(api.close())

if __name__ == '__main__':
    main()

