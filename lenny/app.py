#!/usr/bin/env python
import requests
from flask import Flask, jsonify, abort, request, render_template, redirect, make_response
from flask_cors import CORS
from configs import (
    approot,
    cors,
    media_root,
    options,
    version,
)

app = Flask(__name__)
cors = CORS(app) if cors else None

@app.route('/')
def home():
    return render_template('home.html')

if __name__ == '__main__':
    app.run(**options)
