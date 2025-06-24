#!/bin/bash

set -o allexport
source .env
set +o allexport

mkdir -p ./configs/lcp/processed
mkdir -p ./configs/lcp/cert

envsubst < lcpserver.yaml.template > ./configs/lcp/processed/lcpserver.yaml
