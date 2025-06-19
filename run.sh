#!/usr/bin/env bash

./docker/configure.sh

# Function to wait for a Docker container to be running
# Arguments:
#   $1: The name of the Docker container to wait for (e.g., "lenny_api")
#   $2: (Optional) Maximum number of attempts (default: 15)
#   $3: (Optional) Sleep duration between attempts in seconds (default: 2)
function wait_for_docker_container() {
    local container_name="${1}"
    local max_attempts="${2:-15}" # Default to 15 attempts if not provided
    local wait_seconds="${3:-2}" # Default to 2 seconds if not provided

    if [[ -z "$container_name" ]]; then
        echo "[!] Error: No container name provided to wait_for_docker_container function."
        return 1 # Indicate failure
    fi

    echo "[+] Waiting up to $((max_attempts * wait_seconds)) seconds for '$container_name' to start and pass health checks..."

    local container_ready=false
    for ((i=1; i<=max_attempts; i++)); do
        if docker ps -f "name=$container_name" -f status=running -q &>/dev/null; then
            echo "[+] '$container_name' service is running."
            container_ready=true
            break # Exit the loop immediately
        fi

        echo "    Attempt $i/$max_attempts: Still waiting for '$container_name'..."
        sleep "$wait_seconds"
    done

    if ! "$container_ready"; then
        echo "[!] Error: '$container_name' did not launch after $((max_attempts * wait_seconds)) seconds."
        return 1 # Indicate failure
    fi

    return 0 # Indicate success
}

MODE=
LOG=
REBUILD=false
PRELOAD=""
PUBLIC=false
OS=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="mac"
elif [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$(uname -s)" == "Linux" ]]; then
    OS="linux"
fi

# Parse cli args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)
      REBUILD=true
      shift
      ;;
    --preload)
      # If next arg exists and is a value (not another flag)
      if [[ -n "$2" && "$2" != --* ]]; then
        PRELOAD="$2"
        shift 2
      else
        PRELOAD=true
        shift
      fi
      ;;
    --dev)
      MODE="dev"
      shift
      ;;
    --log)
      LOG=true
      shift
      ;;
    --public)
      PUBLIC=true
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      shift
      ;;
  esac
done


if [[ "$MODE" == "dev" ]]; then
    echo "Running in development mode..."
    if [ ! -f ./env/bin/activate ]; then
        virtualenv env
    fi
    source ./env/bin/activate
    pip install --index-url --index-url "${PIP_INDEX_URL:-https://pypi.org/simple}" --no-cache-dir -r requirements.txt
    source ./env/bin/activate
    uvicorn lenny.app:app --reload
    # Expose public URL if --public is set
    if [[ "$PUBLIC" == "true" ]]; then
        if ! command -v ngrok &> /dev/null; then
            echo "[+] Installing ngrok..."
            if [[ "$OS" == "mac" ]]; then
                brew install --cask ngrok
            elif [[ "$OS" == "linux" ]]; then
                NGROK_ZIP="ngrok-stable-linux-amd64.zip"
                curl -L -o "$NGROK_ZIP" https://bin.equinox.io/c/4VmDzA7iaHb/ngrok-stable-linux-amd64.zip
                unzip -o "$NGROK_ZIP"
                sudo mv ngrok /usr/local/bin/
                rm "$NGROK_ZIP"
            else
                echo "[!] Please install ngrok manually from https://ngrok.com/download"
                exit 1
            fi
        fi
        PORT="${LENNY_PORT:-8080}"
        echo "[+] Exposing local service on port $PORT via ngrok..."
        ngrok http "$PORT" --log=stdout > ngrok.log 2>&1 &
        NGROK_PID=$!
        # Wait for ngrok to start and fetch the public URL
        for i in {1..10}; do
            sleep 1
            URL=$(curl -s http://localhost:4040/api/tunnels | grep -Eo '"public_url":"https:[^"]+' | head -n1 | cut -d '"' -f4)
            if [[ -n "$URL" ]]; then
                echo "[+] Your public URL is: $URL"
                break
            fi
        done
        if [[ -z "$URL" ]]; then
            echo "[!] Failed to get ngrok public URL. Check ngrok.log for details."
        fi
        wait $NGROK_PID
    fi
else
    echo "Running in production mode..."
    export $(grep -v '^#' .env | xargs)

    if [[ "$REBUILD" == "true" ]]; then
        echo "Performing full rebuild..."
        docker compose down --volumes --remove-orphans
        docker compose build --no-cache
        docker compose up -d
    else
        docker-compose -p lenny up -d
    fi

    if [[ -n "$PRELOAD" ]]; then
        if wait_for_docker_container "lenny_api" 15 2; then
            if [[ "$PRELOAD" =~ ^[0-9]+$ ]]; then
                EST_MIN=$(echo "scale=2; (800 / $PRELOAD) / 60" | bc)
                LIMIT="-n $PRELOAD"
            else
                EST_MIN=$(echo "scale=2; 800 / 60" | bc)
                LIMIT=""
            fi

            echo "[+] Preloading ${PRELOAD:-ALL}/~800 book(s) from StandardEbooks (~$EST_MIN minutes)..."
            docker exec -it lenny_api python scripts/preload.py $LIMIT
        fi
    fi

    if [[ "$PUBLIC" == "true" ]]; then
        if ! command -v cloudflared &> /dev/null; then
            echo "[+] Installing cloudflared..."
            if [[ "$OS" == "mac" ]]; then
                brew install cloudflared
            elif [[ "$OS" == "linux" ]]; then
                curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
                chmod +x cloudflared
                sudo mv cloudflared /usr/local/bin/
            else
                echo "[!] Please install cloudflared manually from https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
                exit 1
            fi
        fi
        PORT="${LENNY_PORT:-8080}"
        echo "[+] Exposing local service on port $PORT via cloudflared..."
        cloudflared tunnel --url http://localhost:"$PORT" --no-autoupdate > cloudflared.log 2>&1 &
        CF_PID=$!
        for i in {1..30}; do
            sleep 1
            # Try to extract any https://*.trycloudflare.com or https://*.cfargotunnel.com URL
            URL=$(grep -Eo 'https://[a-zA-Z0-9.-]+\.(trycloudflare|cfargotunnel)\.com' cloudflared.log | head -n1)
            if [[ -n "$URL" ]]; then
                echo "[+] Your public URL is: $URL/v1/api/"
                break
            fi
        done
        if [[ -z "$URL" ]]; then
            echo "[!] Failed to get cloudflared public URL. Full cloudflared.log follows:"
            cat cloudflared.log
        fi
        if [[ "$LOG" == "true" ]]; then
            docker compose logs -f &
            LOG_PID=$!
            # Wait for either process to exit (cloudflared or logs)
            wait $CF_PID $LOG_PID
        else
            wait $CF_PID
        fi
        exit 0
    fi

    if [[ "$LOG" == "true" ]]; then
        docker compose logs -f
    fi
fi
