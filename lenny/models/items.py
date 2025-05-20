#!/usr/bin/env python 
from sqlalchemy  import Column, String, Boolean, Integer, BigInteger, DateTime
from sqlalchemy.sql import func
from . import Base

class Item(Base):
    __tablename__ = 'items'
    
    id = Column(Integer, primary_key=True)
    openlibrary_edition = Column(BigInteger, nullable=False)
    encrypted = Column(Boolean, default= False, nullable=False)
    s3_filepath = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    