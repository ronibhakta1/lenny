#!/usr/bin/env python
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from lenny.configs import db_config

# Build connection string from the centralized db_config
DB_URL = f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['dbname']}"

# SQLAlchemy setup
engine = create_engine(DB_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

def init_db():
    Base.metadata.create_all(engine)