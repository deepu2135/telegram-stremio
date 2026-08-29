#!/usr/bin/env bash
set -e

if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
  echo "🌐 Connecting to Cloudflare Named Tunnel..."
  cloudflared tunnel run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
  TUNNEL_PID=$!
  if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    echo "### 🚀 Stremio Addon Server Active" >> "$GITHUB_STEP_SUMMARY"
    echo "Running with custom Cloudflare Named Tunnel." >> "$GITHUB_STEP_SUMMARY"
  fi
else
  echo "🌐 Starting Quick TryCloudflare Tunnel on port 7860..."
  cloudflared tunnel --url http://127.0.0.1:7860 > /tmp/tunnel.log 2>&1 &
  TUNNEL_PID=$!

  TUNNEL_URL=""
  for i in {1..30}; do
    TUNNEL_URL=$(grep -o 'https://[-a-zA-Z0-9@:%._\+~#=]\+\.trycloudflare\.com' /tmp/tunnel.log | head -n 1 || true)
    if [ -n "$TUNNEL_URL" ]; then
      break
    fi
    sleep 1
  done

  export ADDON_URL="$TUNNEL_URL"

  if [ -n "$CF_WORKER_URL" ]; then
    echo "📡 Syncing live tunnel with Cloudflare Worker: $CF_WORKER_URL"
    SECRET_KEY="${CF_WORKER_SECRET:-$API_KEY}"
    curl -s -X POST "${CF_WORKER_URL%/}/__update_backend" \
      -H "Authorization: Bearer $SECRET_KEY" \
      -H "Content-Type: application/json" \
      -d "{\"backend_url\": \"$TUNNEL_URL\"}" || true
    echo "✔ Cloudflare Worker Gateway synced!"
    
    DISPLAY_URL="${CF_WORKER_URL%/}"
  else
    DISPLAY_URL="$TUNNEL_URL"
  fi

  echo "=========================================================="
  echo " 🎉 YOUR STREMIO ADDON URL IS LIVE: "
  echo " 🔗 $DISPLAY_URL/manifest.json"
  echo "=========================================================="

  if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    echo "# 🎉 Telegram-Stremio Addon is LIVE!" >> "$GITHUB_STEP_SUMMARY"
    echo "" >> "$GITHUB_STEP_SUMMARY"
    echo "### 🔗 Manifest URL:" >> "$GITHUB_STEP_SUMMARY"
    echo "\`\`\`text" >> "$GITHUB_STEP_SUMMARY"
    echo "$DISPLAY_URL/manifest.json" >> "$GITHUB_STEP_SUMMARY"
    echo "\`\`\`" >> "$GITHUB_STEP_SUMMARY"
    echo "" >> "$GITHUB_STEP_SUMMARY"
    echo "### 📱 How to Install in Stremio:" >> "$GITHUB_STEP_SUMMARY"
    echo "1. Copy the Manifest URL: \`$DISPLAY_URL/manifest.json\`" >> "$GITHUB_STEP_SUMMARY"
    echo "2. Open Stremio (Mobile / Desktop / Web / TV)" >> "$GITHUB_STEP_SUMMARY"
    echo "3. Go to Add-ons -> paste URL -> Install" >> "$GITHUB_STEP_SUMMARY"
  fi
fi

echo "=========================================================="
echo " Starting Telegram-Stremio Backend Server on port 7860..."
echo "=========================================================="
python3 -m uvicorn addon:app --host 127.0.0.1 --port 7860 &
UVICORN_PID=$!

wait $UVICORN_PID
