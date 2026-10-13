#!/usr/bin/env bash
#
# Removes the throwaway test deployment created by scripts/azure-test-up.sh.
# Deletes the entire resource group, so everything (plan, app, storage) goes
# with it and billing stops.
#
# Override the resource group name with RG=... if you used a custom one.
#
set -euo pipefail

RG="${RG:-esg-chatbot-test-rg}"

command -v az >/dev/null 2>&1 || {
  echo "Azure CLI (az) not found." >&2
  exit 1
}

read -r -p "Delete resource group '$RG' and everything in it? [y/N] " answer
case "$answer" in
  [yY]*) ;;
  *) echo "Aborted."; exit 1 ;;
esac

az group delete -n "$RG" --yes --no-wait
echo "Deletion of '$RG' started in the background. Check with: az group list -o table"
