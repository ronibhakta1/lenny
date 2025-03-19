#!/usr/bin/env python
from fastapi import APIRouter

router = APIRouter(prefix="/v1")

@router.get("/api")
async def home():
    return {"message": "Hello from Lenny API!", "status": "online"}