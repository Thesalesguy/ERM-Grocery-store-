#!/usr/bin/env bash
# M13 Phase 3: generate a throwaway self-signed TLS certificate.
#
# This is NOT what a real deployment uses — a real deployment runs
# `certbot --nginx` against a real public domain (docs/M13_DESIGN.md
# Section 4.1). This script exists because this sandbox has no public
# domain to complete a real ACME challenge against, so the TLS
# termination/redirect/header MECHANICS (everything nginx itself does)
# are tested against a self-signed pair instead — swapping in a real
# Let's-Encrypt-issued cert later changes zero nginx configuration,
# only the two file paths this script also writes to.
#
# Usage: generate_self_signed_cert.sh <output_dir>
# Writes <output_dir>/fullchain.pem and <output_dir>/privkey.pem.
# Never commits anything — the caller is responsible for using a
# throwaway/gitignored directory (deploy/tests uses a tempdir).
set -euo pipefail

OUT_DIR="${1:?Usage: generate_self_signed_cert.sh <output_dir>}"
mkdir -p "$OUT_DIR"

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$OUT_DIR/privkey.pem" \
    -out "$OUT_DIR/fullchain.pem" \
    -days 365 \
    -subj "/CN=erp.local.test" \
    -addext "subjectAltName=DNS:erp.local.test,DNS:localhost,IP:127.0.0.1" \
    2>/dev/null

chmod 600 "$OUT_DIR/privkey.pem"
echo "Self-signed certificate written to $OUT_DIR (valid 365 days, CN=erp.local.test)"
