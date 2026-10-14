#!/bin/bash
# Ad-hoc re-sign a macOS app bundle while PRESERVING each component's own
# entitlements (critical for sandboxed apps like WeChat: without
# com.apple.security.app-sandbox it loses access to its own container).
#
# Also drops the Hardened Runtime flag so a debugger can attach.
#
# Usage: sign_app.sh <app-bundle>
set -euo pipefail

APP="${1:?usage: sign_app.sh <app-bundle>}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sign_one() {
  local target="$1"
  local ent="$TMP/ent.plist"
  rm -f "$ent"
  if codesign -d --entitlements :- "$target" >"$ent" 2>/dev/null && grep -q "<dict" "$ent" 2>/dev/null; then
    :
  else
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
      '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
      '<plist version="1.0"><dict/></plist>' >"$ent"
  fi
  codesign --force --sign - --timestamp=none --entitlements "$ent" "$target" >/dev/null 2>&1 \
    || echo "  WARN: sign failed for $target" >&2
}

echo "== collecting code objects under $APP/Contents"
LIST="$TMP/list.txt"
: >"$LIST"

# nested bundles / frameworks
find "$APP/Contents" -depth \
  \( -name "*.app" -o -name "*.appex" -o -name "*.framework" -o -name "*.xpc" -o -name "*.bundle" \) \
  -type d -print >>"$LIST" 2>/dev/null || true

# Mach-O files (executables + dylibs)
find "$APP/Contents" -type f -print 2>/dev/null | while IFS= read -r f; do
  if file -b "$f" 2>/dev/null | grep -q "Mach-O"; then
    echo "$f" >>"$LIST"
  fi
done

# deepest paths first: inner code must be sealed before the bundle containing it
sort -u "$LIST" | awk '{ print length($0)"\t"$0 }' | sort -rn | cut -f2- >"$TMP/ordered.txt"
echo "== signing $(wc -l <"$TMP/ordered.txt" | tr -d ' ') objects (inside-out)"

while IFS= read -r obj; do
  [ -e "$obj" ] || continue
  sign_one "$obj"
done <"$TMP/ordered.txt"

echo "== signing outer bundle"
sign_one "$APP"

echo "== result"
codesign -dvvv "$APP" 2>&1 | grep -E "flags=|Signature=" | head -3
echo "-- entitlements kept:"
codesign -d --entitlements :- "$APP" 2>/dev/null | grep -oE "app-sandbox|downloads\.read-write|application-identifier" | sort -u
echo "-- strict verify:"
if codesign --verify --deep --strict "$APP" 2>&1 | head -5; then :; fi
codesign --verify --deep --strict "$APP" >/dev/null 2>&1 && echo "VERIFY OK" || echo "VERIFY FAILED"
