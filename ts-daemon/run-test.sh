#!/usr/bin/env bash
# Run the SDK proof-of-concept
set -euo pipefail
cd "$(dirname "$0")"
npx tsx test-sdk.ts
