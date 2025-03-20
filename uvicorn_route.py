#!/usr/bin/env python3
import uvicorn
from lenny.app import app

if __name__ == "__main__":
    print("Starting uvicorn server on port 7002...")
    uvicorn.run(app, host="0.0.0.0", port=7002, log_level="info")