
githubtool
==========

A tool for searching things on github from the commandline.
This tool needs python3.

You can search code, repositories, users. Or list repositories owned by a specific user.
The `--network` option will show info for all forks, like how many commits a fork is behind or ahead
of the main repo.

You can specify your `username:password`, or an authentiction token with the `--auth` commandline
argument. Or you can store the `auth` parameter in a json file named `~/.github_cmdline_rc`.

Install
=======

You can insall this tool from [pypi](https://pypi.org/project/github-tool/) using pip:

    pip install github-tool

And then execute it like this:

    github -l nlitsme


Or run it directly from the source directory using:

    python3 github_tool.py -l nlitsme

Examples
========

List repositories owned by `nlitsme`:

    github -l nlitsme


list all files containing a specific string:

    github -a -q specific_string


Find all very large repositories:

    github -a -w repo -q "size:>8000000"
 

Find all very large files:

    github -a -w code -q "in:path zip size:>500000000"

or

    github -a -w code -q "in:path zip size:400000000..500000000"


Print size and description for a list of repositories:

    github nlitsme/ubidump https://github.com/nlitsme/github

List your current ratelimit status:

    github --limits

List information for all forks of a specific repository:

    github --network https://github.com/nlitsme/extfstools

Or list information for all forks from a user:

    github -n -l nlitsme


TODO
====

 * slow down after receiving a '403' - ratelimit triggered.

AUTHOR
======

Willem Hengeveld <itsme@xs4all.nl>

