#!/bin/bash

./docker/configure.sh

if [[ "$1" == "--dev" ]]; then
    echo "Running in development mode..."
    if [ ! -f ./env/bin/activate ]; then
        virtualenv env
    fi
    source ./env/bin/activate
    pip install --index-url --index-url "${PIP_INDEX_URL:-https://pypi.org/simple}" --no-cache-dir -r requirements.txt
    source ./env/bin/activate
    uvicorn lenny.app:app --reload
else
    echo "Running in production mode..."
    export $(grep -v '^#' .env | xargs)

    docker-compose -p lenny up -d
fi
