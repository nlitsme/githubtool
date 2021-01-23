from setuptools import setup
setup(
    name = "github_tool",
    version = "1.0.1",
    entry_points = {
        'console_scripts': ['github=github_tool:main'],
    },
    install_requires = ['aiohttp==3.6.2'],
    py_modules=['github_tool'],
    author = "Willem Hengeveld",
    author_email = "itsme@xs4all.nl",
    description = "Commandline search tool for github",
    long_description="""
Commandline tool which can query github, search for repositories
or code. Or get detailed info about a list of repositories, like
size, last update, etc.
""",

    license = "MIT",
    keywords = "github commandline",
    url = "https://github.com/nlitsme/githubtool/",
    classifiers = [
        'Environment :: Console',
        'Intended Audience :: End Users/Desktop',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3.7',
        'Topic :: Utilities',
        'Framework :: AsyncIO',
        'Topic :: Software Development :: Version Control :: Git',
    ],
    python_requires = '>=3.7',
)
