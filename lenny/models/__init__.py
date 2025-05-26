#!/usr/bin/env python
"""
    Models Configurations for Lenny,
    including handling database connections and ORM setup.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from lenny.configs import DB_URI, DEBUG

Base = declarative_base()

# Configure Database Connection
engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
db = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))

# Ensure all SQLAlchemy tables are created at startup
Base.metadata.create_all(bind=engine)

__all__ = ["Base", "db", "engine"]
