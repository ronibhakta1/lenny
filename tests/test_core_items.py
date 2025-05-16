#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    tests.test_packaging
    ~~~~~~~~~~~~~~~~~~~~

    This module tests the core items of the Lenny package.

    :copyright: (c) 2015 by Authors.
    :license: see LICENSE for more details.
"""

import pytest 
from lenny.models import get_db,Base

@pytest.fixture
def db_session():
    with next(get_db()) as session:
        Base.metadata.creal_all(bind=session.bind)