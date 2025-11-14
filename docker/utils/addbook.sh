#!/usr/bin/env bash
# Helper script to add a book to Lenny via Docker

set -e

source "$(dirname "$0")/docker_helpers.sh"

OLID=""
FILEPATH=""
ENCRYPTED="false"

while [[ $# -gt 0 ]]; do
    case $1 in
        --olid)
            OLID="$2"
            shift 2
            ;;
        --filepath)
            FILEPATH="$2"
            shift 2
            ;;
        --encrypted)
            ENCRYPTED="true"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [ -z "$OLID" ] || [ -z "$FILEPATH" ]; then
    echo "Error: Missing required arguments."
    echo "Usage: $0 --olid <OLID> --filepath <PATH> [--encrypted]"
    exit 1
fi

FILEPATH=$(eval echo "$FILEPATH")
FILEPATH=$(cd "$(dirname "$FILEPATH")" 2>/dev/null && pwd)/$(basename "$FILEPATH") || FILEPATH="$FILEPATH"

if [ ! -f "$FILEPATH" ]; then
    echo "Error: File not found: $FILEPATH"
    exit 1
fi

if [ ! -r "$FILEPATH" ]; then
    echo "Error: Cannot read file: $FILEPATH"
    echo ""
    echo "If the file is in ~/Downloads, macOS may be blocking access."
    echo "Please either:"
    echo "  1. Move/copy the file to the lenny project directory, or"
    echo "  2. Grant Terminal 'Full Disk Access' in System Settings > Privacy & Security"
    exit 1
fi

# Ensure container is running
if ! wait_for_docker_container "lenny_api" 15 2; then
    echo "Error: lenny_api container is not running"
    exit 1
fi

FILENAME=$(basename "$FILEPATH")
CONTAINER_PATH="/tmp/${FILENAME}"

echo "[+] Copying file to container..."
if ! docker cp "$FILEPATH" "lenny_api:${CONTAINER_PATH}" 2>/dev/null; then
    echo "[+] Direct copy failed, trying alternative method..."
    cat "$FILEPATH" | docker exec -i lenny_api sh -c "cat > ${CONTAINER_PATH}"
fi

echo "[+] Uploading book with OLID: $OLID..."

CMD="python scripts/addbook.py --olid $OLID --filepath $CONTAINER_PATH"
if [ "$ENCRYPTED" = "true" ]; then
    CMD="$CMD --encrypted"
fi

if docker exec -i lenny_api $CMD; then
    echo "[✓] Book uploaded successfully!"
    EXIT_CODE=0
else
    echo "[✗] Upload failed"
    EXIT_CODE=1
fi

echo "[+] Cleaning up temporary file..."
docker exec -i lenny_api rm -f "$CONTAINER_PATH"

exit $EXIT_CODE
