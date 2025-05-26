#!/usr/bin/env python

"""
    Core module for Lenny,
    including the main application setup and configuration.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from lenny.configs import S3_CONFIG


s3 = boto3.client(
    's3',
    endpoint_url=S3_CONFIG['endpoint'],
    access_key_id=S3_CONFIG['access_key'],
    secret_access_key=S3_CONFIG['secret_key'],
    use_ssl=S3_CONFIG['secure']
)
__all__ = ['s3']

