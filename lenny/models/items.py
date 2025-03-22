#!/usr/bin/env python 
from pydantic import BaseModel

class ItemSettings(BaseModel):
    is_lendable: bool
    num_lendable_total: int