#!/usr/bin/env python3
"""
Script to upload a book to Lenny with an OpenLibrary Edition ID.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lenny.core.client import LennyClient


def main():
    parser = argparse.ArgumentParser(
        description="Upload a book to Lenny with an OpenLibrary Edition ID"
    )
    parser.add_argument(
        "--olid",
        type=str,
        required=True,
        help="OpenLibrary Edition ID (e.g., OL123456M or just 123456)"
    )
    parser.add_argument(
        "--filepath",
        type=str,
        required=True,
        help="Path to the EPUB file to upload"
    )
    parser.add_argument(
        "--encrypted",
        action="store_true",
        default=False,
        help="Flag to indicate if the book is encrypted"
    )
    
    args = parser.parse_args()
    
    filepath = Path(args.filepath)
    if not filepath.exists():
        print(f"Error: File not found: {args.filepath}")
        sys.exit(1)
    
    if not filepath.suffix.lower() == '.epub':
        print(f"Warning: File does not have .epub extension: {args.filepath}")
    
    olid = args.olid
    if olid.startswith('OL') and olid.endswith('M'):
        olid = olid[2:-1]  
    
    try:
        olid_int = int(olid)
    except ValueError:
        print(f"Error: Invalid OLID format: {args.olid}")
        print("Expected format: OL123456M or 123456")
        sys.exit(1)
    
    print(f"Uploading book with OLID: {olid_int}")
    print(f"File: {filepath}")
    print(f"Encrypted: {args.encrypted}")
    
    try:
        with open(filepath, 'rb') as epub:
            success = LennyClient.upload(
                olid=olid_int,
                file_content=epub,
                encrypted=args.encrypted
            )
        
        if success:
            print("✓ Book uploaded successfully!")
            sys.exit(0)
        else:
            print("✗ Book upload failed. Check logs for details.")
            sys.exit(1)
    except Exception as e:
        error_msg = str(e)
        if "409" in error_msg or "Conflict" in error_msg:
            print(f"✗ Book with OLID {olid_int} already exists in the database.")
            print("  If you want to replace it, you'll need to delete it first.")
        else:
            print(f"✗ Upload failed: {error_msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
