#!/usr/bin/env python

"""
    Core module for Lenny,
    including the main application setup and configuration.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from lenny.configs import S3_CONFIG


# Initialize S3 client for MinIO
session = boto3.session.Session()

s3 = session.client(
    service_name='s3',
    aws_access_key_id=S3_CONFIG['access_key'],
    aws_secret_access_key=S3_CONFIG['secret_key'],
    endpoint_url=f"http://{S3_CONFIG['endpoint']}",
    use_ssl=S3_CONFIG['secure']
)
__all__ = ['s3']

