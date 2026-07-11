#!/bin/bash
# run.sh - QA Agent Orchestrator

# Exit on critical command errors
set -e

# Initialize empty variables for URL and flow selector
BASE_URL=""
FLOW_SELECT=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --url) BASE_URL="$2"; shift ;;
    --flow) FLOW_SELECT="$2"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# Validate URL
if [ -z "$BASE_URL" ]; then
  echo "Error: The --url parameter is mandatory."
  echo "Usage: ./run.sh --url <project_URL> [--flow <flow_name_or_path>]"
  echo "Example: ./run.sh --url http://localhost:3000 --flow example_flow.md"
  exit 1
fi

# Load variables from .env file if it exists
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # Skip comments and empty lines
    [[ "$line" =~ ^#.*$ ]] && continue
    [[ -z "$line" ]] && continue
    key=$(echo "$line" | cut -d '=' -f 1 | xargs)
    val=$(echo "$line" | cut -d '=' -f 2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
    export "$key=$val"
  done < .env
fi

# Validate API key
if [ -z "$GEMINI_API_KEY" ]; then
  echo "Error: The GEMINI_API_KEY environment variable is not configured."
  echo "Please set it in your .env file or export it in the terminal."
  exit 1
fi

# Find available flows (.md)
flows=()
for f in ./flows/*.md; do
  [ -f "$f" ] && flows+=("$f")
done

if [ ${#flows[@]} -eq 0 ]; then
  echo "Error: No flow files (.md) found in the ./flows folder"
  exit 1
fi

# Determine which flows to run
flows_to_run=()
if [ -n "$FLOW_SELECT" ]; then
  if [ -f "$FLOW_SELECT" ]; then
    flows_to_run+=("$FLOW_SELECT")
  elif [ -f "./flows/$FLOW_SELECT" ]; then
    flows_to_run+=("./flows/$FLOW_SELECT")
  else
    echo "Error: The specified flow '$FLOW_SELECT' does not exist."
    exit 1
  fi
else
  echo "=================================================="
  echo "Select the QA flow you want to run:"
  echo "0) [Run all flows]"
  for i in "${!flows[@]}"; do
    echo "$((i+1))) $(basename "${flows[$i]}")"
  done
  echo "=================================================="
  read -p "Choose an option (0-${#flows[@]}) [0]: " choice
  choice=${choice:-0}
  
  if [ "$choice" -eq 0 ] 2>/dev/null; then
    flows_to_run=("${flows[@]}")
  else
    idx=$((choice-1))
    if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#flows[@]}" ]; then
      flows_to_run+=("${flows[$idx]}")
    else
      echo "Invalid option."
      exit 1
    fi
  fi
fi

echo "=================================================="
echo "Starting Autonomous QA Test Suite"
echo "Target URL: $BASE_URL"
echo "=================================================="

# Create necessary directories
mkdir -p ./reports
mkdir -p ./flows

# Flag to track failures (disable set -e for iteration to allow continuing)
set +e
FAILED=0

# Iterate over selected flows
for flow in "${flows_to_run[@]}"; do
  echo ""
  echo "--------------------------------------------------"
  echo "Running flow: $(basename "$flow")"
  echo "--------------------------------------------------"
  
  # Run the Python runner
  python3 ./antigravity_agent.py --run --spec "$flow" --base-url "$BASE_URL"
  RESULT=$?
  
  # Define colors for output
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  NC='\033[0m' # No Color

  if [ $RESULT -eq 0 ]; then
    echo -e ">> RESULT: ${GREEN}[SUCCESS] $(basename "$flow")${NC}"
  else
    echo -e ">> RESULT: ${RED}[FAILURE] $(basename "$flow")${NC} (Exit code: $RESULT)"
    FAILED=1
  fi
done

echo ""
echo "=================================================="
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}PROCESS COMPLETED: All selected flows were executed successfully.${NC}"
  exit 0
else
  echo -e "${RED}PROCESS COMPLETED with ERRORS. Some flows failed.${NC}"
  echo "Please check the ./reports folder for diagnostic details and error logs."
  exit 1
fi
