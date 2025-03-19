#!/usr/bin/env python
from fastapi import FastAPI
# from lenny.configs.db import init_db
from lenny.routes.api import router


app = FastAPI(title="Lenny API")
# init_db()
app.include_router(router)



@app.get("/")
async def root():
    return {"message": "Welcome to Lenny API Root"}

@app.get("/v1/api")
async def api():
    return {"message": "Hello from Lenny API!", "status": "online"}