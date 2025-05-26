#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from fastapi import APIRouter, status, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi import HTTPException
from lenny.schemas.item import ItemCreate
from lenny.core.itemsUpload import schedule_file_for_upload # Added import

router = APIRouter()

@router.get('/', status_code=status.HTTP_200_OK)
async def root():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lenny API</title>
    </head>
    <body>
        <h1 style="text-align: center;">Lenny: A Free, Open Source Lending System for Libraries</h1>
        <img src="/static/lenny.png" alt="Lenny Logo" style="display: block; margin: 0 auto;">
        <p style="text-align: center;">You can download & deploy it from <a href="https://github.com/ArchiveLabs/lenny">Github</a> </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, media_type="text/html")

@router.post('/upload', status_code=status.HTTP_200_OK )
async def create_items(items: ItemCreate ,files: list[UploadFile] = File(...)):
    allowed_extensions = {".pdf", ".epub"}
    allowed_mime_types = {"application/pdf", "application/epub+zip"}
    
    processed_files_count = 0
    for file_to_upload in files:
        # Validate file extension
        file_extension = ""
        if file_to_upload.filename:
            parts = file_to_upload.filename.split(".")
            if len(parts) > 1:
                file_extension = "." + parts[-1].lower()

        if file_extension not in allowed_extensions:
            # Optionally, collect errors and report them all at once, 
            # or raise immediately / skip this file
            print(f"Skipping file {file_to_upload.filename}: Invalid extension {file_extension}")
            continue # Skip this file

        # Validate MIME type
        if file_to_upload.content_type not in allowed_mime_types:
            print(f"Skipping file {file_to_upload.filename}: Invalid MIME type {file_to_upload.content_type}")
            continue # Skip this file
        
        # If validations pass, schedule the file for upload
        # Assuming schedule_file_for_upload will handle the 'items' metadata (title, openlibrary_edition)
        # and the file itself.
        try:
            await schedule_file_for_upload(
                item_data=items, 
                file=file_to_upload,
                filename=file_to_upload.filename, 
                content_type=file_to_upload.content_type
            )
            processed_files_count += 1
        except Exception as e:
            # Log the error and continue with other files, or raise HTTPException
            print(f"Failed to schedule file {file_to_upload.filename} for upload: {e}")
            # Depending on desired behavior, you might want to raise an HTTPException here
            # or collect errors to return to the client.
            # For now, just printing and continuing.

    if processed_files_count == 0 and len(files) > 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid files were processed. Only PDF and EPUB files are allowed.")
    
    if processed_files_count < len(files):
        return HTMLResponse(status_code=status.HTTP_207_MULTI_STATUS, content=f"{processed_files_count} of {len(files)} files accepted for upload. Some files were skipped due to validation errors.")

    return HTMLResponse(status_code=status.HTTP_200_OK, content=f"All {processed_files_count} files accepted and scheduled for upload successfully.")

