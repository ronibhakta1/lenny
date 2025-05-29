#!/usr/bin/env python
"""
    Item Schema for Lenny,
    including the definition of the Item model and its attributes.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from pydantic import BaseModel
from typing import Optional

class Item(BaseModel):
    
    openlibrary_edition: int
    encrypted: Optional[bool] = False
    
    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": 1,
                "openlibrary_edition": 1234567890,
                "encrypted": False,
                "s3_filepath": "s3://bookshelf-public/path/to/file",
                "created_at": "2023-10-01T12:00:00Z",
                "updated_at": "2023-10-01T12:00:00Z"
            }
        }
        
