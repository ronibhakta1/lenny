#!/usr/bin/env python
import os
import configparser
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "settings.cfg")
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

# Dynamic DB host
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_URL = f"postgresql://{config['database']['user']}:{config['database']['password']}@{DB_HOST}:{config['database']['port']}/{config['database']['dbname']}"

# SQLAlchemy setup
engine = create_engine(DB_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

def init_db():
    Base.metadata.create_all(engine)