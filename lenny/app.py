#!/usr/bin/env python3

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from lenny.routes import api, oauth
from lenny.configs import OPTIONS
from lenny import __version__ as VERSION
from lenny.core.limiter import limiter

app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version=VERSION,
)

# Rate Limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# App-level CORS should be a setting, as to not
# conflict with CORS handled by nginx
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.templates = Jinja2Templates(directory="lenny/templates")

app.include_router(api.router, prefix="/v1/api")
app.include_router(oauth.router, prefix="/v1/oauth")

app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
