# No changes needed if this is already correct
from fastapi import FastAPI
from lenny.routes import api

app = FastAPI()

# Include API routes
app.include_router(api.router, prefix="/v1/api")