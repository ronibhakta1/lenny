#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    setup.py
    ~~~~~~~~
    Lenny, the open source Library Lending System web application

    :copyright: (c) 2025 by mek.
    :license: see LICENSE for more details.
"""

import os
import re
import codecs
from setuptools import setup

here = os.path.abspath(os.path.dirname(__file__))


def read(*parts):
    """Taken from pypa pip setup.py:
    intentionally *not* adding an encoding option to open, See:
       https://github.com/pypa/virtualenv/issues/201#issuecomment-3145690
    """
    return codecs.open(os.path.join(here, *parts), 'r').read()


def find_version(*file_paths):
    version_file = read(*file_paths)
    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]",
                              version_file, re.M)
    if version_match:
        return version_match.group(1)
    raise RuntimeError("Unable to find version string.")

setup(
    name='lenny',
    version=find_version("lenny", "__init__.py"),
    description='Lenny, the open source Library Lending System',
    long_description=read('README.md'),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.12",
        "Topic :: Internet :: WWW/HTTP",
    ],
    author='mek',
    author_email='michael.karpeles@gmail.com',
    packages=[
        'lenny'
        ],
    platforms='any',
    license='LICENSE',
    install_requires=[
        'flask',
        'flask-cors'
        ],
    include_package_data=True
    )