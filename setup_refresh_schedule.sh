#!/usr/bin/env bash
# Sets up a weekly background refresh of prices.db on macOS (launchd).
# Run once:  bash setup_refresh_schedule.sh
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then echo "uv not found on PATH. Install uv first."; exit 1; fi

LABEL="com.aws-cost-agent.refresh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string>
    <string>run</string>
    <string>--with</string>
    <string>ijson</string>
    <string>python</string>
    <string>$DIR/refresh_prices.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DIR/refresh.log</string>
  <key>StandardErrorPath</key><string>$DIR/refresh.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Scheduled weekly refresh (Sundays 3am) + immediate first run."
echo "It refreshes on load, so prices.db is being built now. Watch: tail -f $DIR/refresh.log"
