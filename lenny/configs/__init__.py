#!/usr/bin/env python

"""
    Configurations for Lenny

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import os
import sys
import types
import configparser
import json

# Get directory paths
path = os.path.dirname(os.path.realpath(__file__))
approot = os.path.abspath(os.path.join(path, os.pardir))
sys.path.append(approot)

# Helper method for getting config values with defaults
def getdef(self, section, option, default_value):
    try:
        return self.get(section, option)
    except:
        return default_value

# Load configuration once at module import time
config = configparser.ConfigParser()
config.read(os.path.join(path, 'settings.cfg'))
config.getdef = types.MethodType(getdef, config)

# Helper for environment variable override
def get_env_or_config(section, key, default=None):
    env_var = f"{section.upper()}_{key.upper()}"
    return os.environ.get(env_var, config.getdef(section, key, default))

# Server configuration
HOST = get_env_or_config('server', 'host', '0.0.0.0')
PORT = int(get_env_or_config('server', 'port', 8080))
DEBUG = bool(int(get_env_or_config('server', 'debug', 1)))
CRT = get_env_or_config('ssl', 'crt', '')
KEY = get_env_or_config('ssl', 'key', '')
options = {'debug': DEBUG, 'host': HOST, 'port': PORT}
if CRT and KEY:
    options['ssl_context'] = (CRT, KEY)

# CORS settings
cors = bool(int(get_env_or_config('server', 'cors', 1)))
app_domain = get_env_or_config('server', 'domain', '127.0.0.1')

# Media configuration
media_root = get_env_or_config('media', 'root', 'media')
if not os.path.isabs(media_root):
    media_root = os.path.join(approot, media_root)
if not os.path.exists(media_root):
    os.makedirs(media_root)

# API configuration
API_PORT = int(os.environ.get('API_PORT', get_env_or_config('api', 'port', 7000)))
version = int(get_env_or_config('api', 'version', 1))

# Database configuration - prioritize environment variables 
db_config = {
    'user': os.environ.get('POSTGRES_USER', config.getdef('database', 'user', 'postgres')),
    'password': os.environ.get('POSTGRES_PASSWORD', config.getdef('database', 'password', '')),
    'host': os.environ.get('POSTGRES_HOST', config.getdef('database', 'host', 'localhost')),
    'port': int(os.environ.get('POSTGRES_PORT', config.getdef('database', 'port', '5432'))),
    'dbname': os.environ.get('POSTGRES_DB', config.getdef('database', 'dbname', 'lenny'))
}

# MinIO configuration - prioritize environment variables
minio_config = {
    'root_user': os.environ.get('MINIO_ROOT_USER', config.getdef('minio', 'root_user', 'minioadmin')),
    'root_password': os.environ.get('MINIO_ROOT_PASSWORD', config.getdef('minio', 'root_password', '')),
    'host': os.environ.get('MINIO_HOST', config.getdef('minio', 'host', 'localhost')),
    'port': int(os.environ.get('MINIO_PORT', config.getdef('minio', 'port', '9000')))
}

# Export all configuration variables
__all__ = ['config', 'HOST', 'PORT', 'DEBUG', 'options', 
           'cors', 'app_domain', 'media_root', 'version', 
           'db_config', 'minio_config', 'API_PORT']
