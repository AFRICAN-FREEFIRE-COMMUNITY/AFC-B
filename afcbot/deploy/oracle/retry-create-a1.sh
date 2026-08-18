#!/usr/bin/env bash
# Retry-create an Ampere A1 instance until Oracle has capacity.
# Run this from anywhere with the OCI CLI installed and a configured profile
# (https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm).
#
# Fill the variables, then: bash retry-create-a1.sh
#
# It calls `oci compute instance launch` in a loop, sleeping 60s between
# attempts, until the API stops returning "Out of host capacity".

set -uo pipefail

# ── Required: paste these from Oracle Console ─────────────────────────────
COMPARTMENT_OCID="ocid1.compartment.oc1..xxxxx"
SUBNET_OCID="ocid1.subnet.oc1.af-johannesburg-1.xxxxx"
IMAGE_OCID="ocid1.image.oc1.af-johannesburg-1.xxxxx"   # Ubuntu 22.04 Aarch64
AVAILABILITY_DOMAIN="xxxx:AF-JOHANNESBURG-1-AD-1"
SSH_PUBKEY_FILE="$HOME/.ssh/afc_oracle.pub"
DISPLAY_NAME="afc-bot"
# Read cloud-init from the repo (relative path from this script)
CLOUD_INIT_FILE="$(dirname "$0")/cloud-init.yaml"

# ── A1 shape sizing ───────────────────────────────────────────────────────
SHAPE="VM.Standard.A1.Flex"
OCPUS=1
MEMORY_GB=6

# ── Loop ──────────────────────────────────────────────────────────────────
attempt=0
while true; do
    attempt=$((attempt + 1))
    echo "[$(date -Iseconds)] Attempt #${attempt}…"

    if oci compute instance launch \
        --compartment-id "${COMPARTMENT_OCID}" \
        --availability-domain "${AVAILABILITY_DOMAIN}" \
        --subnet-id "${SUBNET_OCID}" \
        --image-id "${IMAGE_OCID}" \
        --shape "${SHAPE}" \
        --shape-config "{\"ocpus\": ${OCPUS}, \"memoryInGBs\": ${MEMORY_GB}}" \
        --display-name "${DISPLAY_NAME}" \
        --assign-public-ip true \
        --ssh-authorized-keys-file "${SSH_PUBKEY_FILE}" \
        --user-data-file "${CLOUD_INIT_FILE}" \
        --wait-for-state RUNNING; then
        echo "Got an instance. Done."
        exit 0
    fi

    echo "Capacity miss. Sleeping 60s…"
    sleep 60
done
