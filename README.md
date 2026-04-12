# YT Info Diet

**Stop scrolling YouTube. Let Claude tell you what's worth watching.**

Automated YouTube channel intelligence monitor. Polls your favorite creators every 12 hours, runs new long-form videos through Claude Code for structured analysis, and delivers WATCH/SKIM/SKIP verdicts to Slack.

## What You Get

Every 12 hours, for each new long-form video on your watchlist:

- **Verdict** — WATCH, SKIM, or SKIP (in the Slack notification header)
- **Summary** — 2-3 paragraphs of the core argument
- **Key Insights** — 5-8 actionable takeaways
- **Playbook** — Step-by-step actions you could take from the content
- **Quotable** — Standout lines worth saving

Delivered to Slack. Archived as markdown locally. YouTube Shorts auto-filtered.

## Why

If you follow 10+ creators, your subscription feed becomes a firehose. Most videos aren't worth your time, but you don't know that until you've already invested 10 minutes. YT Info Diet inverts the loop: Claude reads everything first, you only watch what passes the filter.

## Cost

- **Claude analysis**: $0 — uses your existing Claude Code subscription via headless mode (`claude -p`). No API key needed.
- **Apify transcripts**: ~$0 — free tier handles thousands of videos/month.
- **Slack**: $0 — free workspace tier works fine.
- **YouTube polling**: $0 — uses public RSS feeds, no API key.

## Requirements

- macOS (Linux works too with cron instead of launchd)
- Python 3.9+
- [Claude Code](https://claude.com/claude-code) installed and logged in
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — `pip install yt-dlp`
- An [Apify](https://apify.com) account (free tier)
- A Slack workspace where you can create incoming webhooks

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/regreenjr/yt-info-diet.git
cd yt-info-diet
pip3 install -r requirements.txt
cp config.example.yaml config.yaml
```

### 2. Get your Apify token

1. Sign up at [apify.com](https://apify.com)
2. **Settings → Integrations → API tokens** → copy token

### 3. Create a Slack webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it (e.g. "YT Info Diet"), pick your workspace
3. Sidebar: **Incoming Webhooks** → toggle ON
4. **Add New Webhook to Workspace** → pick the channel for reports
5. Copy the webhook URL

### 4. Add channels

Find a channel ID:

```bash
yt-dlp --skip-download --playlist-items 1 --print "%(channel_id)s" "https://www.youtube.com/@peterdiamandis"
```

Add it:

```bash
python3 monitor.py add "Peter Diamandis" "UCvxm0qTrGN_1LMYgUaftWyQ" --tags ai futurism
```

Repeat for each channel.

### 5. Seed the state

This marks all current videos as "already seen" so your first run doesn't dump 60 backfilled videos into Slack:

```bash
python3 monitor.py seed
```

### 6. Test it

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
export APIFY_TOKEN="apify_api_YOUR_TOKEN"

# Process anything from the past 1 day as a test
python3 monitor.py run --since-days 1
```

You should see Slack notifications arrive within a few minutes.

### 7. Schedule every 12 hours (macOS)

```bash
cp com.example.yt-monitor.plist ~/Library/LaunchAgents/com.yourname.yt-monitor.plist
```

Edit the plist — replace all `YOUR_USERNAME`, `YOUR_SLACK_WEBHOOK_URL`, and `YOUR_APIFY_TOKEN` placeholders. Then load:

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.yt-monitor.plist
launchctl list | grep yt-monitor   # confirm loaded
```

Done. Reports land in Slack at 6 AM and 6 PM daily.

## CLI Reference

```bash
# Add a channel
python3 monitor.py add "Channel Name" "UC..." --tags ai marketing

# List monitored channels
python3 monitor.py list

# Remove a channel
python3 monitor.py remove "UC..."

# Seed current state (mark all current videos as seen)
python3 monitor.py seed

# Run normally — only process truly new videos
python3 monitor.py run

# Process all videos from past N days (catch-up mode)
python3 monitor.py run --since-days 3

# Limit a run to specific channels
python3 monitor.py run --since-days 7 --only "UC123" "UC456"

# Dry run — fetch transcripts but skip Claude analysis and Slack
python3 monitor.py run --dry-run

# Force analyze a specific video by ID
python3 monitor.py run --video "dQw4w9WgXcQ"
```

## Operations

```bash
# Run manually right now (via launchd)
launchctl start com.yourname.yt-monitor

# Watch the logs
tail -f monitor.log
tail -f monitor.error.log

# Stop the schedule
launchctl unload ~/Library/LaunchAgents/com.yourname.yt-monitor.plist

# Reload after editing the plist
launchctl unload ~/Library/LaunchAgents/com.yourname.yt-monitor.plist
launchctl load ~/Library/LaunchAgents/com.yourname.yt-monitor.plist
```

## How It Works

1. **YouTube RSS** — Each channel exposes a free RSS feed at `youtube.com/feeds/videos.xml?channel_id=UC...`. No YouTube API key needed.
2. **Shorts filter** — `yt-dlp` checks each new video's duration. Anything ≤ 180s or with `/shorts/` in the URL is skipped.
3. **Three-tier transcript fetching** — Tries `youtube-transcript-api` first (fast but often IP-blocked), falls back to `yt-dlp` subtitle download, then to Apify (rotating proxies, most reliable).
4. **Claude Code headless** — `claude -p --disallowedTools "*"` runs Claude as a pure text generator using your subscription. No Anthropic API key.
5. **Slack delivery** — Block Kit message with verdict in the header for instant scanning.
6. **Local archive** — Each analysis saved as `reports/YYYY-MM-DD_video-title.md` for grep/search later.
7. **State tracking** — `state.json` remembers which videos have been processed so each is only handled once.

## Tips

- **Start small** — 3-5 channels first. Easier to tune than 20.
- **Tag aggressively** — tags appear in the Slack feed and help when scanning.
- **Heavy Shorts posters are mostly invisible to you** — the filter strips them, but their occasional long-form gems still come through.
- **Run `--since-days 3` once** after first setup to get a sample analysis from each channel. Then decide which to keep.
- **Tune the verdict prompt** to match your taste — edit `ANALYSIS_PROMPT` in `monitor.py`.

## Customization Ideas

- **Different schedules**: Edit the `StartCalendarInterval` array in your plist to add more times.
- **Channel-specific prompts**: Pass `tags` into the analysis prompt to give Claude domain context.
- **Email digest instead of Slack**: Replace `send_to_slack` with an SMTP function.
- **Database storage**: Swap `save_report` to write to Supabase/Postgres for a searchable archive.
- **Different LLM**: Swap `claude -p` for any CLI-accessible model.

## Troubleshooting

**No Slack notifications?**
```bash
# Check the error log
tail -50 monitor.error.log

# Test the webhook directly
curl -X POST -H 'Content-type: application/json' --data '{"text":"test"}' "$SLACK_WEBHOOK_URL"
```

**Transcript fetch failing?**
YouTube IP-blocks the `youtube-transcript-api` library aggressively. The script falls back to `yt-dlp`, then to Apify. If all three fail, your IP is heavily rate-limited — wait an hour or use a VPN.

**Claude command not found in launchd?**
Make sure the `PATH` in your plist includes the directory where `claude` is installed. Run `which claude` and add that directory.

**Too many videos getting analyzed?**
The `seed` command prevents this. Run it after adding new channels.

## License

MIT
