#!/usr/bin/env bash
# Regenerate the Postman collection from the live OpenAPI schema.
#
# The schema is produced by importing the application, so it always reflects the
# routes the running code actually mounts (including the org console and billing
# routes, which only register when user accounts are enabled).
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-venv/bin/python}
"$PY" - <<'PY'
import json, sys
sys.path.insert(0, "src")
from chatbot.api import app
spec = app.openapi()
with open("postman/openapi.json", "w") as fh:
    json.dump(spec, fh, indent=2)
print("wrote postman/openapi.json ({} paths)".format(len(spec["paths"])))
PY

npx -y openapi-to-postmanv2 -s postman/openapi.json -o postman/collection.json -p
echo "wrote postman/collection.json"
