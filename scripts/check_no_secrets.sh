#!/bin/bash
# Fails if tracked files contain obvious secrets. Cheap guard; CI runs it on every PR.
cd "$(dirname "$0")/.."
pat='-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|eyJhbGciOi[A-Za-z0-9._-]{40,}|AKIA[0-9A-Z]{16}'
hits=$(git ls-files -z | xargs -0 grep -InE "$pat" -- 2>/dev/null | grep -v "scripts/check_no_secrets.sh" || true)
if [ -n "$hits" ]; then echo "Possible secrets found:"; echo "$hits" | cut -c1-160; exit 1; fi
bad=$(git ls-files | grep -E '\.(pem|key)$|(^|/)\.env$|profile\.json$' || true)
if [ -n "$bad" ]; then echo "Files that must never be committed:"; echo "$bad"; exit 1; fi
echo "no secrets found"
