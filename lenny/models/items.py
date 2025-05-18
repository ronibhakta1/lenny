#!/usr/bin/env python 
from sqlalchemy  import Column, String, Boolean, Integer, DateTime
from sqlalchemy.sql import func
from . import Base

class Item(Base):
    __tablename__ = 'items'
    
    lenny_edition = Column(String(100),primary_key=True)
    title = Column(String(255), nullable=False)
    item_status = Column(Boolean, default=True)
    language = Column(String(30), nullable=False)
    is_readable = Column(Boolean, default=True, nullable=False)
    is_lendable = Column(Boolean, default= True, nallable= False)
    is_wishlistable = Column(Boolean, default= True, nullable=False)
    is_Printdisabled = Column(Boolean, default=False, nullable= False)
    num_lendable_total = Column(Integer, nullable=False) 
    current_num_lendable = Column(Integer, nullable=False)
    current_waitlist_size =Column(Integer, default= 0, nullable=False)
    encrypted = Column(Boolean, default= False, nullable=False)
    s3_filepath = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    