#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " Starting Telegram-Stremio Backend Server on port 7860..."
echo "=========================================================="
python3 -m uvicorn addon:app --host 127.0.0.1 --port 7860 &
UVICORN_PID=$!

for i in {1..30}; do
  if curl -s http://127.0.0.1:7860 > /dev/null 2>&1; then
    echo "✔ FastAPI server is responsive!"
    break
  fi
  sleep 1
done

if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
  echo "🌐 Connecting to Cloudflare Named Tunnel..."
  cloudflared tunnel run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
  if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    echo "### 🚀 Stremio Addon Server Active" >> "$GITHUB_STEP_SUMMARY"
    echo "Running with custom Cloudflare Named Tunnel." >> "$GITHUB_STEP_SUMMARY"
  fi
else
  echo "🌐 Starting Quick TryCloudflare Tunnel..."
  cloudflared tunnel --url http://127.0.0.1:7860 > /tmp/tunnel.log 2>&1 &

  TUNNEL_URL=""
  for i in {1..30}; do
    TUNNEL_URL=$(grep -o 'https://[-a-zA-Z0-9@:%._\+~#=]\+\.trycloudflare\.com' /tmp/tunnel.log | head -n 1 || true)
    if [ -n "$TUNNEL_URL" ]; then
      break
    fi
    sleep 1
  done

  echo "=========================================================="
  echo " 🎉 YOUR STREMIO ADDON URL IS LIVE: "
  echo " 🔗 $TUNNEL_URL/manifest.json"
  echo "=========================================================="

  if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    echo "# 🎉 Telegram-Stremio Addon is LIVE!" >> "$GITHUB_STEP_SUMMARY"
    echo "" >> "$GITHUB_STEP_SUMMARY"
    echo "### 🔗 Manifest URL:" >> "$GITHUB_STEP_SUMMARY"
    echo "\`\`\`text" >> "$GITHUB_STEP_SUMMARY"
    echo "$TUNNEL_URL/manifest.json" >> "$GITHUB_STEP_SUMMARY"
    echo "\`\`\`" >> "$GITHUB_STEP_SUMMARY"
    echo "" >> "$GITHUB_STEP_SUMMARY"
    echo "### 📱 How to Install in Stremio:" >> "$GITHUB_STEP_SUMMARY"
    echo "1. Copy the Manifest URL: \`$TUNNEL_URL/manifest.json\`" >> "$GITHUB_STEP_SUMMARY"
    echo "2. Open Stremio (Mobile / Desktop / Web / TV)" >> "$GITHUB_STEP_SUMMARY"
    echo "3. Go to Add-ons -> paste URL -> Install" >> "$GITHUB_STEP_SUMMARY"
  fi
fi

wait $UVICORN_PID
