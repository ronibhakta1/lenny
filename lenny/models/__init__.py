#!/usr/bin/env python
"""
    Models Configurations for Lenny,
    including handling database connections and ORM setup.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from lenny.configs import DB_URI, TESTING

Base = declarative_base()

if TESTING:
    engine = create_engine("sqlite:///:memory:", connect_args={'check_same_thread': False})
    Base.metadata.creat_all(bind=engine)
else:
    engine = create_engine(DB_URI)
    
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        if TESTING:
            engine.dispose()

__all__ = ["Base", "SessionLocal", "get_db", "engine"]