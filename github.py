"""
Author: Willem Hengeveld <itsme@xs4all.nl>

idea is to have this tool automatically follow the '*_url' links, and enumerate lists received.

So have an option to list further options.

"""
import asyncio
import aiohttp.connector
import aiohttp
import os.path
import html.parser
import urllib.parse
import time
import re
from collections import defaultdict


class GithubApi:
    """

    /users/<username>
        *_url
    /orgs/<orgname>
        *_url

    -- need auth
    /user/repos       -- current user repos  


    pager:  page=<n>&per_page=100

    /search/code?q=

    """
    def __init__(self, loop, args):
        self.baseurl = "https://api.github.com/"

        hdrs = dict(Accept='application/vnd.github.v3+json')


        #  application/vnd.github+json
        #  application/vnd.github-commitcomment.full+json
        #  application/vnd.github-commitcomment.html+json
        #  application/vnd.github-commitcomment.raw+json
        #  application/vnd.github-commitcomment.text+json
        #  application/vnd.github.VERSION+json
        #  application/vnd.github.VERSION.base64
        #  application/vnd.github.VERSION.diff
        #  application/vnd.github.VERSION.full+json
        #  application/vnd.github.VERSION.html
        #  application/vnd.github.VERSION.html+json
        #  application/vnd.github.VERSION.object
        #  application/vnd.github.VERSION.patch
        #  application/vnd.github.VERSION.raw
        #  application/vnd.github.VERSION.raw+json
        #  application/vnd.github.VERSION.sha
        #  application/vnd.github.VERSION.text+json

        #  application/vnd.github.v3+json
        #  application/vnd.github.v3.diff
        #  application/vnd.github.v3.full+json
        #  application/vnd.github.v3.raw+json
        #  application/vnd.github.v3.repository+json
        #  application/vnd.github.v3.star+json
        #  application/vnd.github.v3.text-match+json
        #  application/vnd.github.v3.text-match+json,
        #  application/vnd.github[.version].param[+json]

        #  application/vnd.github.full+json

        #  application/vnd.github.ant-man-preview+json
        #  application/vnd.github.barred-rock-preview
        #  application/vnd.github.black-panther-preview+json
        #  application/vnd.github.cloak-preview
        #  application/vnd.github.cloud-9-preview+json+scim
        #  application/vnd.github.dazzler-preview+json
        #  application/vnd.github.echo-preview+json
        #  application/vnd.github.giant-sentry-fist-preview+json
        #  application/vnd.github.hagar-preview+json
        #  application/vnd.github.hellcat-preview+json
        #  application/vnd.github.inertia-preview+json
        #  application/vnd.github.jean-grey-preview+json
        #  application/vnd.github.luke-cage-preview+json
        #  application/vnd.github.machine-man-preview
        #  application/vnd.github.machine-man-preview+json
        #  application/vnd.github.mercy-preview
        #  application/vnd.github.mercy-preview+json
        #  application/vnd.github.mister-fantastic-preview+json
        #  application/vnd.github.mockingbird-preview
        #  application/vnd.github.nightshade-preview+json
        #  application/vnd.github.sailor-v-preview+json
        #  application/vnd.github.scarlet-witch-preview+json
        #  application/vnd.github.squirrel-girl-preview
        #  application/vnd.github.squirrel-girl-preview+json
        #  application/vnd.github.symmetra-preview+json
        #  application/vnd.github.symmetra-preview+json,
        #  application/vnd.github.valkyrie-preview+json
        #  application/vnd.github.wyandotte-preview+json
        #  application/vnd.github.zzzax-preview+json

        moreargs = dict()
        if args.auth:
            user, pw = args.auth.split(':', 1)
            moreargs['auth'] = aiohttp.BasicAuth(user, pw)

        self.client = aiohttp.ClientSession(loop=loop, headers=hdrs, **moreargs)

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
            "notifications_url": "https://api.github.com/notifications",
            "organization_repositories_url": "https://api.github.com/orgs/{org}/repos{?type,page,per_page,sort}",
            "organization_url": "https://api.github.com/orgs/{org}",
            "public_gists_url": "https://api.github.com/gists/public",
            "rate_limit_url": "https://api.github.com/rate_limit",
            "repository_search_url": "https://api.github.com/search/repositories?q={query}{&page,per_page,sort,order}",
            "repository_url": "https://api.github.com/repos/{owner}/{repo}",
            "starred_gists_url": "https://api.github.com/gists/starred",
            "starred_url": "https://api.github.com/user/starred{/owner}{/repo}",
            "team_url": "https://api.github.com/teams",
            "user_organizations_url": "https://api.github.com/user/orgs",
            "user_repositories_url": "https://api.github.com/users/{user}/repos{?type,page,per_page,sort}",
            "user_search_url": "https://api.github.com/search/users?q={query}{&page,per_page,sort,order}",
            "user_url": "https://api.github.com/users/{user}"
        }

    def __del__(self):
        self.client.close()

    async def get(self, path, params=dict()):
        r = await self.client.get(path, params=params)
        json = await r.json()

        r.close()
        return json

    async def loadapi(self):
        self.d = await self.get(url)

    def getapi(self, name):
        url = self.d.get(name)
        if not url:
            raise Exception("can't find '%s' api" % name)
        return url

    def getlimits(self):
        url = self.getapi("rate_limit_url")
        return self.get(url)

    def listrepos(self, username):
        url = self.getapi("user_repositories_url")
        url = url.replace("{user}", username)
        url = url[:url.find('{')]

        return self.get(url, dict(per_page=100))

    async def startquery(self, where, query):
        url = self.getapi(where + "_search_url")
        url = url[:url.find('?')]

        r = await self.client.get(url, params=dict(per_page=100, q=query))
        json = await r.json()
        link = r.headers['Link']

        r.close()

        return json, link


    def query(self, where, pagenr, query):
        url = self.getapi(where + "_search_url")
        url = url[:url.find('?')]

        return self.get(url, dict(per_page=100, page=pagenr, q=query))

def getjs(js, path):
    if path.find('.')>=0:
        this, rest = path.split('.', 1)
        return getjs(js[this], rest)
    return js[path]

async def getlimits(api):
    # guest  auth
    #    60  5000  (per hour) rate
    #    60  5000  (per hour) resources.core
    #     0  5000  (per hour) resources.graphql
    #    10    30  (per minute) resources.search
    js = await api.getlimits()
    tnow = time.time()
    for path in ('rate', 'resources.core','resources.search', 'resources.graphql'):
        print("%5d of %5d  %5d(sec) %s" % (getjs(js, path+".remaining"), getjs(js, path+".limit"), getjs(js, path+".reset") - tnow, path))

def printresult(where, items):
    for item in items:
        if where == 'code':
            print("%-20s %s" % (getjs(item, "repository.full_name"), getjs(item, "path")))
        elif where == 'issue':
            print("%-20s %s" % (getjs(item, "html_url"), getjs(item, "body")))
        elif where == 'repo':
            print("%-20s %s" % (getjs(item, "full_name"), getjs(item, "description")))
        elif where == 'user':
            print("%s" % (getjs(item, "login")))
        else:
            print(item)
            # commit
 
def findlast(link):
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

async def querygithub(api, args):
    if args.where == 'repo':
        where = 'repository'
    else:
        where = args.where
    js, link = await api.startquery(where, args.query)

    lastpage = findlast(link)

    printresult(args.where, js["items"])

    if not args.all:
        return
    for p in range(2, lastpage):
        js = await api.query(where, p, args.query)
        printresult(args.where, js["items"])

async def listrepos(api, user):
    js = await api.listrepos(user)
    for repo in js:
        if not repo["fork"]:
            print("%-30s %s" % (repo["name"], repo["description"]))

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Tool for interogating github')
    parser.add_argument('--auth', type=str)
    parser.add_argument('--limits', action='store_true')
    parser.add_argument('--list', '-l', type=str)
    parser.add_argument('--all', '-a', action='store_true')
    parser.add_argument('--where', '-w', type=str, default='code', help='What type of object to search for: code, user, repo, commit, issue')
    parser.add_argument('--query', '-q', type=str)
    args = parser.parse_args()

    loop = asyncio.get_event_loop()

    api = GithubApi(loop, args)

    tasks = [ ]
    if args.list:
        tasks.append(listrepos(api, args.list))
    if args.limits:
        tasks.append(getlimits(api))
    if args.query:
        tasks.append(querygithub(api, args))

    loop.run_until_complete(asyncio.gather(*tasks))

if __name__ == '__main__':
    main()

