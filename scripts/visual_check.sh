#!/bin/bash
# visual_check.sh — Take desktop + mobile screenshots of all key pages
# Usage: ./scripts/visual_check.sh [output_dir]

OUTPUT_DIR="${1:-/tmp/visual-check-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUTPUT_DIR"
BASE_URL="http://localhost:8000"

echo "📸 Visual check — saving to $OUTPUT_DIR"

pages=("dashboard" "accounts" "ledgers")

for page in "${pages[@]}"; do
  URL="$BASE_URL/$page"
  echo "  → $page (desktop)"
  peeky screenshot --url "$URL" --width 1440 --height 900 --output "$OUTPUT_DIR/${page}-desktop.png" 2>/dev/null || \
    peeky screenshot "$OUTPUT_DIR/${page}-desktop.png" 2>/dev/null || \
    echo "    [peeky failed for $page desktop]"
  
  echo "  → $page (mobile)"
  peeky screenshot --url "$URL" --width 390 --height 844 --output "$OUTPUT_DIR/${page}-mobile.png" 2>/dev/null || \
    echo "    [peeky failed for $page mobile]"
done

echo "✅ Done — screenshots in $OUTPUT_DIR"
ls "$OUTPUT_DIR"
