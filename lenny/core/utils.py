import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

def encode_book_path(book_id: str, format=".epub") -> str:
    """This should be moved to a general utils.py within core"""
    if not "." in book_id:
        book_id += format
    path = f"s3://bookshelf/{book_id}"
    logger.info(f"path: {path}")
    encoded = base64.b64encode(path.encode()).decode()
    return encoded.replace('/', '_').replace('+', '-').replace('=', '')

def hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
