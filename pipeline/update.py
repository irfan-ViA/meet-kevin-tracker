"""Update the Meet Kevin tracker data.

Runs in GitHub Actions. Steps:
1. Read Meet Kevin's YouTube RSS feed and find videos not yet tracked.
2. Pull each new video's transcript (YouTube captions).
3. Ask Claude which stocks he gives a view on, and his stance.
4. Refresh daily closes from Yahoo Finance for every tracked ticker.
5. Write docs/data/*.json and a run summary (status.json) the app reads.

Info only, not financial advice.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent.parent / "docs" / "data"
CHANNEL_HANDLE = "meetkevin"
CHANNEL_ID_FALLBACK = "UCUvvj5lwue7PspotMDjk5UA"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_NEW_VIDEOS = int(os.environ.get("MAX_NEW_VIDEOS", "12"))
MAX_VIDEO_SECONDS = 60 * 60  # skip long livestreams
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")
STANCES = {"Bullish", "Bearish", "Bought", "Neutral"}

log_lines = []


def log(msg):
    print(msg, flush=True)
    log_lines.append(msg)


def load(name, default):
    p = DATA / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    (DATA / f"{name}.json").write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def mmss(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------- 1. new videos ----------
def channel_id():
    try:
        html = requests.get(f"https://www.youtube.com/@{CHANNEL_HANDLE}", headers=UA, timeout=30).text
        m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', html)
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        log(f"channel page failed: {e}")
    return CHANNEL_ID_FALLBACK


def page_videos():
    """Backup: read the channel's Videos tab when the RSS feed is unavailable."""
    html = requests.get(f"https://www.youtube.com/@{CHANNEL_HANDLE}/videos", headers=UA, timeout=30)
    log(f"videos page: HTTP {html.status_code}, {len(html.text)} chars")
    html.raise_for_status()
    ids = list(dict.fromkeys(re.findall(r'"videoId":"([\w-]{11})"', html.text)))[:15]
    out = []
    for vid in ids:
        w = requests.get(f"https://www.youtube.com/watch?v={vid}", headers=UA, timeout=30).text
        pub = re.search(r'"(?:publishDate|uploadDate)":"([^"]+)"', w)
        title = re.search(r'<meta name="title" content="([^"]*)"', w) or re.search(r'"title":"((?:[^"\\]|\\.)*)"', w)
        if not pub:
            continue
        p = pub.group(1)
        if len(p) == 10:
            p += "T12:00:00-07:00"
        import html as _h
        out.append({"video_id": vid, "title": _h.unescape(title.group(1)) if title else vid,
                    "published_at": p, "short": False})
        time.sleep(0.5)
    return out


def feed_videos(cid):
    r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", headers=UA, timeout=30)
    log(f"feed: HTTP {r.status_code} for channel {cid}")
    if r.status_code != 200:
        return page_videos()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        vid = e.find("yt:videoId", ns).text
        link = e.find("a:link", ns).attrib.get("href", "")
        out.append({"video_id": vid, "title": e.find("a:title", ns).text,
                    "published_at": e.find("a:published", ns).text,
                    "short": "/shorts/" in link})
    return out


def pacific_date(iso):
    """YouTube shows publish dates in US Pacific time; match that."""
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # noqa: BLE001
        return (t - dt.timedelta(hours=7)).date().isoformat()


# ---------- 2. transcripts ----------
def transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    proxies = None
    if os.environ.get("TRANSCRIPT_PROXY_URL"):
        from youtube_transcript_api.proxies import GenericProxyConfig
        proxies = GenericProxyConfig(http_url=os.environ["TRANSCRIPT_PROXY_URL"], https_url=os.environ["TRANSCRIPT_PROXY_URL"])
    api = YouTubeTranscriptApi(proxy_config=proxies) if proxies else YouTubeTranscriptApi()
    snippets = api.fetch(video_id, languages=["en", "en-US"]).snippets
    if not snippets:
        raise RuntimeError("empty transcript")
    duration = snippets[-1].start + snippets[-1].duration
    parts, last = [], -99
    for s in snippets:
        if s.start - last >= 20:
            parts.append(f"[{mmss(s.start)}]")
            last = s.start
        parts.append(s.text.replace("\n", " "))
    return " ".join(parts), duration


# ---------- 3. Claude extraction ----------
TOOL = {
    "name": "record_calls",
    "description": "Record every individual stock or ETF Meet Kevin gives a view on in this video.",
    "input_schema": {
        "type": "object",
        "properties": {"calls": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "US ticker in Yahoo Finance form, e.g. NVDA, BRK-B, QQQ"},
                "stance": {"type": "string", "enum": sorted(STANCES)},
                "owns": {"type": "string", "enum": ["Yes", "No", "Not stated"]},
                "at": {"type": "string", "description": "Timestamp m:ss (or h:mm:ss) where he says it, from the transcript markers"},
                "what_he_said": {"type": "string", "description": "One faithful sentence paraphrasing his view; any quoted words under 12 words"},
            },
            "required": ["ticker", "stance", "owns", "at", "what_he_said"]}}},
        "required": ["calls"],
    },
}

PROMPT = """You are reading the auto-generated transcript of a Meet Kevin (Kevin Paffrath) YouTube video titled "{title}".
List every individual stock or ETF he gives a view on. Be conservative and never invent a stance:
- Bullish: he expresses a positive view or says it's cheap / a buy.
- Bought: he says he, or his membership, bought it, added to it, or sent a buy alert.
- Bearish: he expresses a negative view, avoids it, or says it's expensive / a sell.
- Neutral: only mentioned in passing, as news, or as an upcoming catalyst.
"owns" is Yes/No only if he states it on camera, otherwise "Not stated".
One entry per stock per distinct view (usually one per stock). Skip sponsors, indexes he only references (S&P 500), private companies (OpenAI, Anthropic, SpaceX) and crypto.
If he names a company, convert it to its US ticker (Marvell -> MRVL, Google -> GOOGL, Berkshire -> BRK-B).
Auto-captions misspell names (e.g. "Marll" is Marvell, "Coreweave"/"Cororeweave" is CoreWeave).
If the video has no stock views (pure macro), return an empty list.

Transcript:
{text}"""


def extract(title, text):
    import anthropic
    client = anthropic.Anthropic()
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=4000, tools=[TOOL],
                tool_choice={"type": "tool", "name": "record_calls"},
                messages=[{"role": "user", "content": PROMPT.format(title=title, text=text[:120000])}],
            )
            for block in msg.content:
                if block.type == "tool_use":
                    return block.input.get("calls", [])
            return []
        except Exception as e:  # noqa: BLE001
            log(f"Claude call failed ({attempt + 1}/3): {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Claude extraction failed")


def to_seconds(at):
    try:
        parts = [int(p) for p in str(at).split(":")]
    except ValueError:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


# ---------- 4. prices ----------
def yahoo(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1mo&interval=1d"
    for attempt in range(3):
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        name = res["meta"].get("shortName") or res["meta"].get("longName") or ticker
        closes = res["indicators"]["quote"][0]["close"]
        rows = [{"ticker": ticker, "name": name.strip(),
                 "date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(),
                 "close": round(c, 4), "fetched_at": NOW_ISO}
                for ts, c in zip(res["timestamp"], closes) if c is not None]
        return rows[-11:]
    raise RuntimeError("rate limited")


# ---------- 5. alerts ----------
def tracker_rows(calls, prices, watchlist, new_ids):
    tickers = list(dict.fromkeys([c["ticker"] for c in calls] + [w["ticker"] for w in watchlist]))
    rows = []
    for t in tickers:
        cs = [c for c in calls if c["ticker"] == t]
        ps = sorted([p for p in prices if p["ticker"] == t], key=lambda p: p["date"])
        if not ps:
            continue
        last = ps[-1]["close"]
        prev = ps[-2]["close"] if len(ps) > 1 else None
        day = last / prev - 1 if prev else None
        first = next((p["close"] for p in ps if cs and p["date"] == cs[0]["video_date"]), None)
        since = last / first - 1 if first else None
        stance = next((c["stance"] for c in reversed(cs) if c["stance"] != "Neutral"),
                      cs[-1]["stance"] if cs else "Not mentioned")
        new = [c for c in cs if c["id"] in new_ids]
        big = (day is not None and abs(day) >= 0.05) or (since is not None and abs(since) >= 0.10)
        rows.append({"ticker": t, "name": ps[-1]["name"], "stance": stance, "price": last,
                     "day_pct": day, "since_pct": since, "new_call": bool(new),
                     "new_stance": new[-1]["stance"] if new else None, "big_move": big,
                     "on_watchlist": any(w["ticker"] == t for w in watchlist)})
    return rows


def main():
    calls = load("calls", [])
    videos = load("videos", [])
    prices = load("prices", [])
    watchlist = load("watchlist", [])
    status = load("status", {})
    seen = {v["video_id"] for v in videos}
    skipped = set(status.get("skipped_ids", []))
    errors, new_videos, new_call_ids = [], [], set()

    # 1-3. videos
    try:
        cid = channel_id()
        feed = feed_videos(cid)
        log(f"feed: {len(feed)} videos (channel {cid})")
        # Only look at videos from the day before the newest tracked one onward,
        # so the first run doesn't backfill older uploads as "new".
        newest = max((v["published"] for v in videos), default="2000-01-01")
        cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=1)).isoformat()
        todo = [v for v in feed if v["video_id"] not in seen and v["video_id"] not in skipped and not v["short"]
                and pacific_date(v["published_at"]) >= cutoff]
        todo.sort(key=lambda v: v["published_at"])
        if todo and not os.environ.get("ANTHROPIC_API_KEY"):
            # No key yet: just prove the transcript fetch works, record nothing.
            v = todo[-1]
            try:
                text, dur = transcript(v["video_id"])
                log(f"TEST OK: transcript for {v['video_id']} has {len(text)} chars")
            except Exception as e:  # noqa: BLE001
                log(f"TEST FAILED: transcript for {v['video_id']}: {type(e).__name__}: {str(e)[:300]}")
            errors.append("ANTHROPIC_API_KEY not set yet, so new videos weren't read")
            todo = []
        for v in todo[:MAX_NEW_VIDEOS]:
            try:
                text, dur = transcript(v["video_id"])
            except Exception as e:  # noqa: BLE001
                age_h = (NOW - dt.datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))).total_seconds() / 3600
                log(f"transcript not ready for {v['video_id']} ({type(e).__name__}); will retry")
                errors.append(f"No transcript yet: {v['title']}")
                if age_h > 48:
                    skipped.add(v["video_id"])
                continue
            if dur > MAX_VIDEO_SECONDS:
                log(f"skip long stream {v['video_id']}")
                skipped.add(v["video_id"])
                continue
            found = extract(v["title"], text)
            date = pacific_date(v["published_at"])
            n = max([int(c["id"][1:]) for c in calls if c["id"][1:].isdigit()], default=0)
            for c in found:
                t = c["ticker"].strip().upper().replace(".", "-")
                if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}", t) or c["stance"] not in STANCES:
                    continue
                n += 1
                sec = to_seconds(c["at"])
                row = {"id": f"c{n:02d}", "ticker": t, "stance": c["stance"], "owns": c["owns"],
                       "video_date": date, "video_title": v["title"], "video_id": v["video_id"],
                       "at": mmss(sec), "at_seconds": sec, "what_he_said": c["what_he_said"].strip(),
                       "link": f"https://www.youtube.com/watch?v={v['video_id']}&t={sec}s"}
                calls.append(row)
                new_call_ids.add(row["id"])
            videos.append({"video_id": v["video_id"], "title": v["title"], "published": date,
                           "published_at": v["published_at"],
                           "url": f"https://www.youtube.com/watch?v={v['video_id']}"})
            new_videos.append({"title": v["title"], "video_id": v["video_id"], "calls": len(found)})
            log(f"read {v['video_id']}: {len(found)} calls")
            save("calls", calls)
            save("videos", videos)
    except Exception as e:  # noqa: BLE001
        log(f"video step failed: {e}")
        errors.append(f"Couldn't check YouTube: {type(e).__name__}: {str(e)[:120]}")

    # 4. prices
    tickers = list(dict.fromkeys([c["ticker"] for c in calls] + [w["ticker"] for w in watchlist]))
    fresh, failed = [], []
    for t in tickers:
        try:
            fresh += yahoo(t)
        except Exception as e:  # noqa: BLE001
            failed.append(t)
            log(f"price {t} failed: {e}")
        time.sleep(0.4)
    if fresh:
        got = {r["ticker"] for r in fresh}
        prices = fresh + [p for p in prices if p["ticker"] not in got]  # keep old rows for failures
        save("prices", prices)
    if failed:
        errors.append("No price for: " + ", ".join(failed))

    # 5. summary for the app
    rows = tracker_rows(calls, prices, watchlist, new_call_ids)
    save("status", {
        "run_at": NOW_ISO,
        "price_date": max((p["date"] for p in prices), default=None),
        "latest_video": max((v["published"] for v in videos), default=None),
        "new_videos": new_videos,
        "new_calls": [{"ticker": r["ticker"], "stance": r["new_stance"]} for r in rows if r["new_call"]],
        "big_moves": [{"ticker": r["ticker"], "day_pct": r["day_pct"], "since_pct": r["since_pct"]} for r in rows if r["big_move"]],
        "watchlist": [{"ticker": r["ticker"], "price": r["price"], "day_pct": r["day_pct"], "stance": r["stance"]} for r in rows if r["on_watchlist"]],
        "errors": errors,
        "skipped_ids": sorted(skipped),
        "log": log_lines[-40:],
    })
    log(f"done: {len(new_videos)} new videos, {len(new_call_ids)} new calls, {len(errors)} issues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
