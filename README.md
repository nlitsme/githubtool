
githubtool
==========

A tool for searching things on github from the commandline.
This tool needs python3.

You can search code, repositories, users. Or list repositories owned by a specific user.

You can specify your `username:password`, or an authentiction token with the `--auth` commandline
argument. Or you can store the `auth` parameter in a json file named `~/.github_cmdline_rc`.


Examples
========

List repositories owned by `nlitsme`:

    python3 github.py -l nlitsme


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


TODO
====

 * slow down after receiving a '403' - ratelimit triggered.

AUTHOR
======

Willem Hengeveld <itsme@xs4all.nl>

