#!/usr/bin/env python
from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def home():
    return {"message": "Hello from Lenny API!", "status": "online"}