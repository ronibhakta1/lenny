#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from fastapi import APIRouter, status, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi import HTTPException
from pathlib import Path
from lenny.core.itemsUpload import upload_items
from lenny.models import db
from lenny.models.items import Item
router = APIRouter()

MAX_FILE_SIZE = 50 * 1024 * 1024

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

@router.post('/upload', status_code=status.HTTP_200_OK)
async def create_items(
    openlibrary_edition: int = Form(..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(..., description="The PDF or EPUB file to upload (max 50MB)")
    ):
    allowed_extensions = {".pdf", ".epub"}
    allowed_mime_types = {"application/pdf", "application/epub+zip"}

    if file.size is not None and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File '{file.filename}' is too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB."
        )

    existing_item = db.query(Item).filter(Item.openlibrary_edition == openlibrary_edition).first()
    if existing_item:
        db.close()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An item with OpenLibrary Edition ID '{openlibrary_edition}' already exists."
        )

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A file with no name was uploaded.")

    file_extension = Path(file.filename).suffix.lower()
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File '{file.filename}' has an invalid extension. Only PDF and EPUB files are allowed."
        )

    if file.content_type not in allowed_mime_types:
        if not (file_extension == ".epub" and file.content_type == "application/octet-stream"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file.filename}' has an invalid MIME type. Only PDF (application/pdf) and EPUB (application/epub+zip) files are allowed."
            )

    try:
        upload_items(
            openlibrary_edition=openlibrary_edition,
            encrypted=encrypted,
            files=[file]
        )
        return HTMLResponse(status_code=status.HTTP_200_OK, content="File uploaded successfully.")
    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred during upload: {str(e)}")
    finally:
        db.close()
