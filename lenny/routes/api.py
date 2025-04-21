from fastapi import APIRouter, Depends, HTTPException, status, Form, UploadFile, Request, Body, Header
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from lenny.models import get_db
from lenny.models.items import Item
from lenny.core.admin import verify_librarian
from lenny.core.items import upload_item, delete_item, update_item_access
from lenny.schemas.item import Item as ItemSchema, ItemDeleteResponse, ItemUpdate as ItemUpdateSchema
import os
import shutil

router = APIRouter()

def get_session():
    return next(get_db())

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

@router.post("/items", status_code=status.HTTP_201_CREATED, response_model=ItemSchema)
async def create_item(
    identifier: str = Form(...),
    title: str = Form(...),
    item_status: str = Form(...),  
    language: str = Form(...),
    is_readable: str = Form(False),
    is_lendable: str = Form(True),
    is_waitlistable: str = Form(True),
    is_printdisabled: str = Form(False),
    is_login_required: str = Form(False),
    num_lendable_total: str = Form(0),
    current_num_lendable: str = Form(0),
    current_waitlist_size: str = Form(0),
    file: UploadFile = Form(...),
    s3_access_key: str = Form(...),
    s3_secret_key: str = Form(...),
    db: Session = Depends(get_session),
    request: Request = None,  # For potential future use
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    # Validates file format (only PDF or EPUB)
    filename = file.filename.lower()
    if not (filename.endswith('.pdf') or filename.endswith('.epub')):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Only PDF and EPUB file formats are supported"
        )

    is_readable = is_readable.lower() == "true"
    is_lendable = is_lendable.lower() == "true"
    is_waitlistable = is_waitlistable.lower() == "true"
    is_printdisabled = is_printdisabled.lower() == "true"
    is_login_required = is_login_required.lower() == "true"
    num_lendable_total = int(num_lendable_total)
    current_num_lendable = int(current_num_lendable)
    current_waitlist_size = int(current_waitlist_size)

    temp_path = f"/tmp/{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        uploaded_item = upload_item(
            db,
            identifier,
            title,
            item_status, 
            language,
            temp_path,
            is_readable,
            is_lendable,
            is_waitlistable,
            is_printdisabled,
            is_login_required,
            num_lendable_total,
            current_num_lendable,
            current_waitlist_size,
        )
        return uploaded_item
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@router.get("/items/{identifier}", response_model=ItemSchema)
def get_item_endpoint(identifier: str, db: Session = Depends(get_session)):
    item = db.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item

@router.patch("/items/{identifier}", response_model=ItemSchema)
def update_item_endpoint(
    identifier: str,
    item_update_data: ItemUpdateSchema = Body(...),
    s3_access_key: str = Header(...),
    s3_secret_key: str = Header(...),
    db: Session = Depends(get_session),
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    item = db.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    # Get update data from the Pydantic model, excluding unset fields
    update_data = item_update_data.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided")

    item_status_changed = 'item_status' in update_data and update_data['item_status'] != item.item_status

    updated = False
    for key, value in update_data.items():
        if hasattr(item, key):
            setattr(item, key, value)
            updated = True

    if updated:
        try:
            db.commit()
            db.refresh(item)
            # If item_status was part of the update, synchronize S3 access
            if item_status_changed:
                try:
                    update_item_access(session=db, identifier=identifier)
                    # Note: update_item_access now commits its own changes if successful
                except Exception as s3_error:
                    # Log the S3 error, but the primary item update is already committed.
                    # Decide if this should be a critical failure or just a warning.
                    print(f"Warning: Failed to update S3 access for item {identifier} after status change: {s3_error}")
                    # Optionally raise an HTTPException here if S3 sync failure is critical
                    # raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Item updated but failed to sync S3 access: {s3_error}")
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {str(e)}")

    return item

@router.delete("/items/{identifier}", response_model=ItemDeleteResponse)
def delete_item_endpoint(
    identifier: str,
    s3_access_key: str = Form(...),
    s3_secret_key: str = Form(...),
    db: Session = Depends(get_session),
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    try:
        # Getting item info before deletion for the response
        item = db.query(Item).filter(Item.identifier == identifier).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        
        item_info = {
            "identifier": item.identifier,
            "title": item.title,
            "status": "deleted"
        }
        
        if not delete_item(db, identifier):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        
        # Return item info instead of No Content
        return JSONResponse(content=item_info)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))