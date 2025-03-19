#!/usr/bin/env python3
from lenny.app import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7002)