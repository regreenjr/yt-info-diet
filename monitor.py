#!/usr/bin/env python3
"""
YouTube Channel Intelligence Monitor

Monitors YouTube channels for new videos, analyzes transcripts via Claude Code,
and sends structured intelligence reports to Slack.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yaml
from slack_sdk.webhook import WebhookClient
from youtube_transcript_api import YouTubeTranscriptApi

# Paths
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"
REPORTS_DIR = BASE_DIR / "reports"

# YouTube Shorts can be up to 180 seconds. Anything at or below this is
# either a Short or too thin to be worth analyzing.
SHORTS_MAX_DURATION_SECONDS = 180


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen_videos": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Channel polling via YouTube RSS
# ---------------------------------------------------------------------------

def fetch_new_videos(channels, seen_videos, since_days=None):
    """Check YouTube RSS feeds for new videos. Returns list of new video dicts.

    If since_days is set, ignores seen_videos and returns videos published within N days.
    """
    from datetime import timedelta
    new_videos = []
    cutoff = None
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    for ch in channels:
        channel_id = ch["channel_id"]
        channel_name = ch.get("name", channel_id)
        tags = ch.get("tags", [])
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

        print(f"  Checking {channel_name}...")
        feed = feedparser.parse(feed_url)

        if feed.bozo and not feed.entries:
            print(f"    Warning: Could not fetch feed for {channel_name}")
            continue

        seen_ids = set(seen_videos.get(channel_id, []))

        for entry in feed.entries:
            video_id = entry.get("yt_videoid", "")
            if not video_id:
                continue

            if cutoff is not None:
                published_str = entry.get("published", "")
                try:
                    pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                    if pub_dt < cutoff:
                        continue
                except (ValueError, AttributeError):
                    continue
            else:
                if video_id in seen_ids:
                    continue

            new_videos.append({
                "video_id": video_id,
                "title": entry.get("title", "Unknown"),
                "channel_name": channel_name,
                "channel_id": channel_id,
                "tags": tags,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published": entry.get("published", ""),
            })

        all_ids = [e.get("yt_videoid", "") for e in feed.entries if e.get("yt_videoid")]
        seen_videos[channel_id] = list(set(seen_videos.get(channel_id, []) + all_ids))[-50:]

    return new_videos


# ---------------------------------------------------------------------------
# Shorts detection
# ---------------------------------------------------------------------------

def is_short(video_id):
    """Return True if video is a YouTube Short (duration <= 180s or /shorts/ URL).

    Fail-open: if we can't determine duration, treat as long-form.
    """
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--no-warnings",
                "--print", "%(duration)s|%(webpage_url)s",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return False

        output = result.stdout.strip().split("|", 1)
        if len(output) != 2:
            return False

        duration_str, url = output
        if "/shorts/" in url:
            return True

        try:
            duration = int(float(duration_str))
            return duration <= SHORTS_MAX_DURATION_SECONDS
        except (ValueError, TypeError):
            return False

    except Exception:
        return False


# ---------------------------------------------------------------------------
# Transcript fetching
# ---------------------------------------------------------------------------

def fetch_transcript(video_id):
    """Fetch transcript. Tries youtube-transcript-api, yt-dlp, then Apify."""
    try:
        api = YouTubeTranscriptApi()
        result = api.fetch(video_id)
        text = " ".join(snippet.text for snippet in result.snippets)
        if text.strip():
            return text
    except Exception as e:
        print(f"    youtube-transcript-api failed ({str(e)[:60]}), trying yt-dlp")

    text = _fetch_transcript_ytdlp(video_id)
    if text:
        return text

    print(f"    Trying Apify fallback...")
    return _fetch_transcript_apify(video_id)


def _fetch_transcript_ytdlp(video_id):
    """Use yt-dlp to download and parse subtitles."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-auto-subs",
                    "--write-subs",
                    "--sub-langs", "en.*,en",
                    "--sub-format", "vtt",
                    "-o", f"{tmpdir}/%(id)s.%(ext)s",
                    url,
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )

            vtt_files = list(Path(tmpdir).glob("*.vtt"))
            if not vtt_files:
                print(f"    yt-dlp: no subtitles available")
                return None

            with open(vtt_files[0]) as f:
                return _parse_vtt(f.read())

        except subprocess.TimeoutExpired:
            print(f"    yt-dlp timeout")
            return None
        except Exception as e:
            print(f"    yt-dlp error: {e}")
            return None


def _fetch_transcript_apify(video_id):
    """Use Apify's YouTube transcript scraper."""
    import urllib.request
    import urllib.error

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print(f"    Apify: no APIFY_TOKEN set")
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    actor_url = f"https://api.apify.com/v2/acts/pintostudio~youtube-transcript-scraper/run-sync-get-dataset-items?token={token}"
    payload = json.dumps({"videoUrl": url}).encode()

    try:
        req = urllib.request.Request(
            actor_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())

        if not data:
            return None

        if isinstance(data, list) and data:
            item = data[0]
            if isinstance(item, dict):
                if "data" in item and isinstance(item["data"], list):
                    return " ".join(seg.get("text", "") for seg in item["data"])
                if "transcript" in item:
                    t = item["transcript"]
                    if isinstance(t, str):
                        return t
                    if isinstance(t, list):
                        return " ".join(seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in t)
                if "text" in item:
                    return item["text"]
        return None
    except urllib.error.HTTPError as e:
        print(f"    Apify HTTP error: {e.code}")
        return None
    except Exception as e:
        print(f"    Apify error: {e}")
        return None


def _parse_vtt(vtt_content):
    """Strip VTT formatting, return plain text."""
    lines = []
    seen = set()
    for line in vtt_content.split("\n"):
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.startswith("NOTE"):
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """You are a strategic content analyst. Analyze this YouTube video transcript and produce a structured intelligence report.

**Channel:** {channel_name}
**Video Title:** {title}
**Tags:** {tags}

**Transcript:**
{transcript}

---

Produce your analysis in EXACTLY this format:

## SUMMARY
2-3 paragraphs capturing the core message, key arguments, and unique perspectives. Focus on what's NEW or DIFFERENT, not obvious surface-level stuff.

## KEY INSIGHTS
- Bullet each distinct, actionable insight (aim for 5-8)
- Each should be specific enough to act on, not generic platitudes
- Flag anything contrarian or surprising

## PLAYBOOK
Step-by-step actions someone could take based on this video's content:
1. [Action] - [Why it matters]
2. [Action] - [Why it matters]
(aim for 3-6 concrete steps)

## WATCH RECOMMENDATION
**Verdict:** [WATCH / SKIM / SKIP]
**Time investment:** [estimated minutes if watching key parts]
**Reasoning:** 1-2 sentences on why this verdict. Be honest. If it's recycled content or thin on substance, say so.

## QUOTABLE
Pull 1-2 standout quotes or statements worth saving (with approximate context).
"""


def analyze_video(video, transcript, config):
    """Send transcript to Claude Code (headless) for analysis. Returns analysis text."""
    max_chars = config.get("max_transcript_chars", 100000)
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n\n[Transcript truncated]"

    prompt = ANALYSIS_PROMPT.format(
        channel_name=video["channel_name"],
        title=video["title"],
        tags=", ".join(video["tags"]) if video["tags"] else "none",
        transcript=transcript,
    )

    result = subprocess.run(
        ["claude", "-p", "--disallowedTools", "*"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr.strip()}")

    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def send_to_slack(video, analysis, webhook_url):
    """Send formatted analysis to Slack via webhook."""
    webhook = WebhookClient(webhook_url)

    verdict = "UNKNOWN"
    for line in analysis.split("\n"):
        if "**Verdict:**" in line:
            verdict = line.split("**Verdict:**")[-1].strip()
            break

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{verdict} | {video['title'][:120]}",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{video['channel_name']}* | <{video['url']}|Watch Video> | {video.get('published', 'Unknown date')}",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _convert_to_slack_markdown(analysis)[:3000],
            },
        },
    ]

    response = webhook.send(blocks=blocks)
    if response.status_code != 200:
        print(f"    Slack error: {response.status_code} - {response.body}")
    return response.status_code == 200


def _convert_to_slack_markdown(text):
    """Light conversion from GitHub markdown to Slack mrkdwn."""
    lines = []
    for line in text.split("\n"):
        if line.startswith("## "):
            line = f"\n*{line[3:].strip()}*"
        line = line.replace("**", "*")
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report saving
# ---------------------------------------------------------------------------

def save_report(video, analysis):
    """Save analysis as local markdown file."""
    REPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in video["title"])[:60]
    filename = f"{date_str}_{safe_title.strip().replace(' ', '-')}.md"

    report = f"""# {video['title']}

- **Channel:** {video['channel_name']}
- **URL:** {video['url']}
- **Published:** {video.get('published', 'Unknown')}
- **Analyzed:** {datetime.now().isoformat()}
- **Tags:** {', '.join(video['tags']) if video['tags'] else 'none'}

---

{analysis}
"""

    report_path = REPORTS_DIR / filename
    with open(report_path, "w") as f:
        f.write(report)
    print(f"    Saved report: {report_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run=False, force_video_id=None, since_days=None, only_channels=None):
    """Main monitoring loop."""
    config = load_config()
    state = load_state()

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL") or config.get("slack_webhook_url")
    if not webhook_url and not dry_run:
        print("Error: No SLACK_WEBHOOK_URL set. Use env var or config.yaml.")
        sys.exit(1)

    channels = config.get("channels") or []
    if not channels and not force_video_id:
        print("No channels configured. Add channels to config.yaml.")
        sys.exit(1)

    if only_channels:
        filter_set = {c.lower() for c in only_channels}
        channels = [
            c for c in channels
            if c["channel_id"].lower() in filter_set
            or c.get("name", "").lower() in filter_set
        ]
        print(f"Filtered to {len(channels)} channel(s): {[c['name'] for c in channels]}")

    if force_video_id:
        videos = [{
            "video_id": force_video_id,
            "title": "Manual test",
            "channel_name": "Manual",
            "channel_id": "manual",
            "tags": [],
            "url": f"https://www.youtube.com/watch?v={force_video_id}",
            "published": "",
        }]
    else:
        if since_days:
            print(f"Checking {len(channels)} channels for videos in past {since_days} days...")
        else:
            print(f"Checking {len(channels)} channels for new videos...")
        videos = fetch_new_videos(channels, state.get("seen_videos", {}), since_days=since_days)

    if not videos:
        print("No new videos found.")
        save_state(state)
        return

    print(f"Found {len(videos)} new video(s). Processing...\n")

    shorts_skipped = 0
    for video in videos:
        print(f"  [{video['channel_name']}] {video['title']}")

        if is_short(video["video_id"]):
            print(f"    Skipping: YouTube Short (duration <= {SHORTS_MAX_DURATION_SECONDS}s)")
            shorts_skipped += 1
            continue

        print(f"    Fetching transcript...")
        transcript = fetch_transcript(video["video_id"])
        if not transcript:
            print(f"    No transcript available, skipping.")
            continue

        print(f"    Transcript: {len(transcript):,} chars")

        if dry_run:
            print(f"    [DRY RUN] Would analyze and send to Slack.")
            continue

        print(f"    Analyzing with Claude...")
        try:
            analysis = analyze_video(video, transcript, config)
        except Exception as e:
            print(f"    Analysis error: {e}")
            continue

        save_report(video, analysis)

        if webhook_url:
            print(f"    Sending to Slack...")
            send_to_slack(video, analysis, webhook_url)

        time.sleep(2)

    save_state(state)
    if shorts_skipped:
        print(f"\nDone. Skipped {shorts_skipped} Short(s).")
    else:
        print("\nDone.")


def add_channel(name, channel_id, tags=None):
    """Add a channel to config.yaml."""
    config = load_config()
    channels = config.get("channels") or []

    for ch in channels:
        if ch["channel_id"] == channel_id:
            print(f"Channel {name} ({channel_id}) already exists.")
            return

    entry = {"name": name, "channel_id": channel_id}
    if tags:
        entry["tags"] = tags
    channels.append(entry)
    config["channels"] = channels

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"Added channel: {name} ({channel_id})")


def list_channels():
    """List configured channels."""
    config = load_config()
    channels = config.get("channels", [])
    if not channels:
        print("No channels configured.")
        return
    print(f"\n{'Name':<30} {'Channel ID':<26} {'Tags'}")
    print("-" * 80)
    for ch in channels:
        tags = ", ".join(ch.get("tags", []))
        print(f"{ch.get('name', 'Unknown'):<30} {ch['channel_id']:<26} {tags}")
    print()


def resolve_channel_id(url_or_handle):
    """Try to resolve a channel URL or handle to a channel ID."""
    if url_or_handle.startswith("UC") and len(url_or_handle) == 24:
        return url_or_handle

    match = re.search(r"/channel/(UC[a-zA-Z0-9_-]{22})", url_or_handle)
    if match:
        return match.group(1)

    print(f"Could not resolve channel ID from: {url_or_handle}")
    print("Please provide the channel ID directly (starts with 'UC', 24 chars).")
    print("Tip: Use yt-dlp --print '%(channel_id)s' --playlist-items 1 <channel-url>")
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube Channel Intelligence Monitor")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Check channels and process new videos")
    run_parser.add_argument("--dry-run", action="store_true", help="Check for videos without analyzing")
    run_parser.add_argument("--video", type=str, help="Force analyze a specific video ID")
    run_parser.add_argument("--since-days", type=int, help="Process all videos from past N days (ignores seen state)")
    run_parser.add_argument("--only", nargs="+", help="Only process specific channels (by channel_id or name)")

    add_parser = sub.add_parser("add", help="Add a channel to monitor")
    add_parser.add_argument("name", help="Display name for the channel")
    add_parser.add_argument("channel_id", help="YouTube channel ID (UC...) or channel URL")
    add_parser.add_argument("--tags", nargs="+", help="Tags for categorization")

    sub.add_parser("list", help="List monitored channels")
    sub.add_parser("seed", help="Mark all current videos as seen (initial setup)")

    rm_parser = sub.add_parser("remove", help="Remove a channel")
    rm_parser.add_argument("channel_id", help="Channel ID to remove")

    args = parser.parse_args()

    if args.command == "run":
        run(dry_run=args.dry_run, force_video_id=args.video, since_days=args.since_days, only_channels=args.only)
    elif args.command == "add":
        cid = resolve_channel_id(args.channel_id)
        if cid:
            add_channel(args.name, cid, args.tags)
    elif args.command == "list":
        list_channels()
    elif args.command == "seed":
        config = load_config()
        state = load_state()
        channels = config.get("channels") or []
        print(f"Seeding state with current videos from {len(channels)} channel(s)...")
        fetch_new_videos(channels, state.setdefault("seen_videos", {}))
        save_state(state)
        total = sum(len(v) for v in state["seen_videos"].values())
        print(f"Done. {total} videos marked as seen. Future runs will only process new uploads.")
    elif args.command == "remove":
        config = load_config()
        config["channels"] = [c for c in config.get("channels", []) if c["channel_id"] != args.channel_id]
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f"Removed channel: {args.channel_id}")
    else:
        parser.print_help()
