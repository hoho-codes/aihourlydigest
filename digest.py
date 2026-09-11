"""
digest.py

Standalone hourly "Top 5 Stories" digest video. Fetches news from
NewsData.io (falling back to Currents API), picks the 5 most newsworthy,
writes a ~60-second narration script for each, and assembles them into a
single listicle-style video:

    [1/5 image + ~60s narration, small title burned in near the top]
    [2/5 image + ~60s narration, small title burned in near the top]
    ... 3, 4, 5 ...

Each segment's duration is driven by its own narration audio length, then
all 5 segments are concatenated into one final video ready for upload.

Requires: pip install edge-tts requests
ffmpeg + ffprobe must be available on PATH.

Env vars required: NEWSDATA_API_KEY, CURRENTS_API_KEY (fallback), GROQ_API_KEY
(optional but recommended -- without it, scripts/ranking fall back to plain
readbacks and fetch order).

Run hourly via cron/GitHub Actions schedule.
"""

import asyncio
import os
import random
import subprocess
import textwrap
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEWSDATA_API_URL = "https://newsdata.io/api/1/latest"
CURRENTS_API_URL = "https://api.currentsapi.services/v1/latest-news"
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
NEWS_COUNTRY = "in"  # ISO 3166-1 alpha-2 -- restricts both APIs to India

NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
CURRENTS_API_KEY = os.environ.get("CURRENTS_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

YT_CLIENT_ID = os.environ["YT_CLIENT_ID"]
YT_CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
YT_REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]
YT_PRIVACY_STATUS = os.environ.get("YT_PRIVACY_STATUS", "unlisted")

WORKDIR = "assets/digest"
FINAL_VIDEO = "assets/hourly_digest.mp4"

NUM_STORIES = 5
MAX_VIDEO_SECONDS = 120  # hard ceiling on total video length
WORDS_PER_SECOND = 2.5   # rough natural speech rate, used to size per-story word targets
YT_TITLE_MAX_LEN = 100
SHORTS_TAG = " #Shorts"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920  # 9:16, standard Shorts frame

TITLE_FONT_SIZE = 54  # unused now -- font size is computed dynamically per facts.py's word-count rule
TITLE_TOP_MARGIN = 280  # matches facts.py's top-caption top_padding default
TITLE_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

CAPTION_COLOR_PALETTES = [
    {"fontcolor": "0xFFEE00", "bordercolor": "0x000000@0.9"},   # bright yellow / black
    {"fontcolor": "0x00F0FF", "bordercolor": "0x001A2E@0.9"},   # electric cyan / deep navy
    {"fontcolor": "0xFF2E63", "bordercolor": "0x1A0010@0.9"},   # hot pink / near-black
    {"fontcolor": "0x39FF14", "bordercolor": "0x0A1A00@0.9"},   # neon green / dark
    {"fontcolor": "0xFFFFFF", "bordercolor": "0xFF6B00@0.9"},   # white text / bold orange border
    {"fontcolor": "0xFF9F1C", "bordercolor": "0x1A0F00@0.9"},   # vivid orange / near-black
    {"fontcolor": "0xB026FF", "bordercolor": "0x0F001A@0.9"},   # electric purple / near-black
    {"fontcolor": "0x00FFC2", "bordercolor": "0x001A15@0.9"},   # aqua/teal / dark teal
    {"fontcolor": "0xFF3131", "bordercolor": "0x1A0000@0.9"},   # bold red / near-black
    {"fontcolor": "0xFFFFFF", "bordercolor": "0x2E1A47@0.9"},   # white text / deep violet border
    {"fontcolor": "0xFDF200", "bordercolor": "0xFF00A0@0.9"},   # lemon yellow / hot magenta border
    {"fontcolor": "0x7CFC00", "bordercolor": "0x0A1A00@0.9"},   # lawn green / dark
]

# Used only if both NewsData.io and Currents fail outright (network down, etc).
FALLBACK_ARTICLES = [
    {
        "title": "News feed unavailable",
        "description": "Our news sources could not be reached this run.",
        "image_url": None,
        "link": "",
        "source": "fallback",
    },
]

# ---------------------------------------------------------------------------
# 1. Fetch candidate news articles -- NewsData.io primary, Currents backup
# ---------------------------------------------------------------------------

def _normalize_newsdata_article(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    image_url = item.get("image_url")
    if not title or not image_url:
        return None
    return {
        "title": title,
        "description": (item.get("description") or "").strip(),
        "image_url": image_url,
        "link": item.get("link", ""),
        "source": item.get("source_id", "newsdata.io"),
    }


def _normalize_currents_article(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    image_url = item.get("image")
    # Currents uses the literal string "None" when there's no image, not null.
    if not title or not image_url or image_url == "None":
        return None
    return {
        "title": title,
        "description": (item.get("description") or "").strip(),
        "image_url": image_url,
        "link": item.get("url", ""),
        "source": (item.get("author") or "currentsapi"),
    }


def fetch_from_newsdata(n: int, category: str = "top", language: str = "en", country: str = NEWS_COUNTRY) -> list[dict]:
    if not NEWSDATA_API_KEY:
        print("No NEWSDATA_API_KEY set; skipping NewsData.io.")
        return []

    articles = []
    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(
                NEWSDATA_API_URL,
                params={
                    "apikey": NEWSDATA_API_KEY,
                    "language": language,
                    "category": category,
                    "country": country,
                },
                timeout=15,
            )
            res.raise_for_status()
            data = res.json()
            if data.get("status") != "success":
                raise ValueError(f"NewsData.io returned non-success status: {data.get('status')}")

            seen = set()
            for item in data.get("results", []):
                normalized = _normalize_newsdata_article(item)
                if normalized and normalized["title"] not in seen:
                    seen.add(normalized["title"])
                    articles.append(normalized)
                if len(articles) >= n:
                    break
            print(f"NewsData.io returned {len(articles)} usable candidate(s).")
            return articles
        except Exception as e:
            last_err = e
            print(f"fetch_from_newsdata attempt {attempt + 1} failed ({e}); retrying...")

    print(f"NewsData.io failed after retries ({last_err}).")
    return articles


def fetch_from_currents(n: int, category: str = "world", language: str = "en", country: str = NEWS_COUNTRY) -> list[dict]:
    if not CURRENTS_API_KEY:
        print("No CURRENTS_API_KEY set; skipping Currents API.")
        return []

    articles = []
    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(
                CURRENTS_API_URL,
                params={
                    "apiKey": CURRENTS_API_KEY,
                    "language": language,
                    "category": category,
                    "country": country,
                },
                timeout=15,
            )
            res.raise_for_status()
            data = res.json()
            if data.get("status") != "ok":
                raise ValueError(f"Currents API returned non-ok status: {data.get('status')}")

            seen = set()
            for item in data.get("news", []):
                normalized = _normalize_currents_article(item)
                if normalized and normalized["title"] not in seen:
                    seen.add(normalized["title"])
                    articles.append(normalized)
                if len(articles) >= n:
                    break
            print(f"Currents API returned {len(articles)} usable candidate(s).")
            return articles
        except Exception as e:
            last_err = e
            print(f"fetch_from_currents attempt {attempt + 1} failed ({e}); retrying...")

    print(f"Currents API failed after retries ({last_err}).")
    return articles


def fetch_five_stories() -> list[dict]:
    """
    Pulls candidates from NewsData.io, tops up with Currents API if short,
    then de-dupes by title and returns a buffer of articles (up to
    2x NUM_STORIES when available), each guaranteed to have a usable
    image_url, for the ranking step to narrow down.
    """
    articles = fetch_from_newsdata(NUM_STORIES * 2)  # over-fetch a bit as a buffer
    seen = {a["title"] for a in articles}

    if len(articles) < NUM_STORIES:
        print(f"Only {len(articles)} from NewsData.io; topping up with Currents API.")
        backup = fetch_from_currents(NUM_STORIES * 2)
        for a in backup:
            if a["title"] not in seen:
                articles.append(a)
                seen.add(a["title"])

    if not articles:
        print("Both NewsData.io and Currents API failed; using fallback article.")
        articles = list(FALLBACK_ARTICLES)
    elif len(articles) < NUM_STORIES:
        print(f"Warning: only found {len(articles)} usable stories this hour (wanted {NUM_STORIES}).")

    return articles


def rank_stories_with_groq(articles: list[dict]) -> list[dict]:
    """
    Ranks ALL fetched candidates by newsworthiness, most to least
    important, and returns them in that order -- not just the top
    NUM_STORIES -- so the caller can fall through to the next-best
    story if an earlier pick has to be skipped (e.g. no usable image).
    Falls back to the original fetch order if Groq is unavailable or
    its reply can't be parsed.
    """
    if not GROQ_API_KEY or len(articles) <= 1:
        return articles

    numbered = "\n".join(
        f"{i + 1}. {a['title']} -- {a['description']}" for i, a in enumerate(articles)
    )
    system_instruction = (
        "You are ranking news stories for an hourly news digest video aimed "
        "at a general audience. You will be given a numbered list of "
        "candidate stories (title -- description). Rank ALL of them from "
        "most to least newsworthy, timely, and broadly relevant. "
        "Return ONLY the numbers of every story, most important first, "
        "separated by commas, e.g. '3,1,7,2,5,4,6', nothing else."
    )

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-20b",
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": numbered},
                ],
                "max_tokens": 300,
                "temperature": 0.5,
                "reasoning_effort": "low",
            },
            timeout=30,
        )
        res.raise_for_status()
        raw = res.json()["choices"][0]["message"]["content"].strip()
        indices = [int(tok) - 1 for tok in raw.replace(" ", "").split(",") if tok.strip().isdigit()]
        ranked = [articles[i] for i in indices if 0 <= i < len(articles)]

        deduped, seen_idx = [], set()
        for a in ranked:
            if a["title"] not in seen_idx:
                deduped.append(a)
                seen_idx.add(a["title"])
        for a in articles:  # safety net for anything Groq's reply missed
            if a["title"] not in seen_idx:
                deduped.append(a)
                seen_idx.add(a["title"])

        print(f"Groq ranked all {len(deduped)} candidate stories.")
        return deduped
    except Exception as e:
        print(f"rank_stories_with_groq failed ({e}); using original fetch order.")
        return articles

# ---------------------------------------------------------------------------
# 2. Write a ~60-second script for each story
# ---------------------------------------------------------------------------

def write_segment_script(article: dict, position: int, total: int) -> str:
    """
    Writes a short narration script for one story in the countdown/list.
    Word target is sized so that all `total` segments together keep the
    whole video under MAX_VIDEO_SECONDS (2 minutes): ~2.5 words/sec of
    natural speech, split evenly across stories, minus a small buffer.
    Falls back to a plain readback of title + description if Groq is
    unavailable or fails after retries.
    """
    raw_text = f"Title: {article['title']}\nDescription: {article['description']}"
    fallback = f"Story {position} of {total}. {article['title']}."

    if not GROQ_API_KEY:
        print("No GROQ_API_KEY set; using title unmodified.")
        return fallback

    seconds_per_story = (MAX_VIDEO_SECONDS * 0.9) / total  # 10% buffer for pacing/pauses
    words_per_story = int(seconds_per_story * WORDS_PER_SECOND)
    low, high = max(words_per_story - 10, 15), words_per_story + 5

    system_instruction = (
        f"You write narration for story #{position} of {total} in a news "
        "countdown video. The ENTIRE video across all stories must stay "
        f"under {MAX_VIDEO_SECONDS} seconds total, so THIS story's script "
        f"must be short: roughly {low}-{high} words, no more. Given a news "
        "title and description, write a tight, spoken-language script that "
        "states what happened and the single most important detail -- do "
        "not try to cover everything, pick the one thing that matters most. "
        "Stay strictly neutral and factual -- do not add claims, "
        "speculation, or detail beyond the source text; if the description "
        "is thin, stay general rather than inventing specifics. No "
        "hashtags, no emojis, no quotation marks, no headers, no "
        "'story 1 of 5' framing -- that's added separately. Return ONLY the "
        "narration script, nothing else."
    )

    last_err = None
    for attempt in range(3):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openai/gpt-oss-20b",
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": raw_text},
                    ],
                    "max_tokens": 150,
                    "temperature": 0.7,
                    "reasoning_effort": "low",
                },
                timeout=30,
            )
            res.raise_for_status()
            script = res.json()["choices"][0]["message"]["content"].strip().strip('"')
            if not script:
                raise ValueError("Groq returned an empty script")
            word_count = len(script.split())
            if word_count > high + 15:  # generous slack before we bother truncating
                print(f"Warning: script for story {position} ran long ({word_count} words, target {low}-{high}); trimming.")
                script = " ".join(script.split()[:high])
            print(f"Script for story {position} ({word_count} words): {script[:80]}...")
            return script
        except Exception as e:
            last_err = e
            print(f"write_segment_script attempt {attempt + 1} failed ({e}); retrying...")

    print(f"Groq scripting failed after retries ({last_err}); using fallback readback.")
    return fallback


def make_short_title(article: dict, max_words: int = 8) -> str:
    """Short on-screen title for the burned-in caption near the top."""
    words = article["title"].split()
    if len(words) <= max_words:
        return article["title"]
    return " ".join(words[:max_words]) + "..."


def generate_youtube_title(run_time: datetime | None = None) -> str:
    """
    Builds the video title as "Hourly Brief - <date> <time>" rather than
    from any single story's headline, since this video covers 5 stories,
    not one. Uses the run's own timestamp (UTC) so it reflects when the
    digest was actually generated, not when it happens to get uploaded.
    """
    run_time = run_time or datetime.now(timezone.utc)
    stamp = run_time.strftime("%b %d, %Y %H:%M UTC")
    title = f"Hourly Brief - {stamp}"
    max_len = YT_TITLE_MAX_LEN - len(SHORTS_TAG)
    if len(title) > max_len:
        title = textwrap.shorten(title, width=max_len, placeholder="...")
    return f"{title}{SHORTS_TAG}"


def generate_youtube_description(stories: list[dict], run_time: datetime | None = None) -> str:
    """
    Builds the video description: a short header, then each story used in
    the video listed in the same order they appear, with its source and
    original link so it's clear where each summary came from.
    """
    run_time = run_time or datetime.now(timezone.utc)
    stamp = run_time.strftime("%b %d, %Y %H:%M UTC")

    lines = [
        f"Hourly Brief - {stamp}",
        "Top 5 stories this hour, summarized in order:",
        "",
    ]
    for i, article in enumerate(stories):
        source = article.get("source") or "Unknown source"
        link = article.get("link") or ""
        entry = f"{i + 1}. {article['title']} - {source}"
        if link:
            entry += f" ({link})"
        lines.append(entry)

    lines += [
        "",
        "Summaries are auto-generated from the linked reporting and are "
        "intended as a quick overview, not a substitute for the full "
        "articles above.",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 3. Download the article's own image (no image generation)
# ---------------------------------------------------------------------------

def download_article_image(article: dict, out_path: str) -> str | None:
    """
    Downloads the article's own image_url. Returns out_path on success, or
    None if there's no usable image (caller should skip this story).
    """
    image_url = article.get("image_url")
    if not image_url:
        print("Article has no image_url; cannot produce a segment for this story.")
        return None

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(image_url, timeout=20)
            res.raise_for_status()
            content_type = res.headers.get("Content-Type", "")
            if "image" not in content_type:
                raise ValueError(f"URL did not return image content (Content-Type: {content_type!r})")
            with open(out_path, "wb") as f:
                f.write(res.content)
            print(f"Downloaded article image to {out_path}")
            return out_path
        except Exception as e:
            last_err = e
            print(f"download_article_image attempt {attempt + 1} failed ({e}); retrying...")

    print(f"Failed to download article image after retries ({last_err}).")
    return None

def build_image_search_query_with_groq(article: dict) -> str:
    """
    Turns a news article into a short, generic stock-photo search query --
    the TOPIC or SETTING of the story, not the specific event, since a
    stock library won't have a picture of that exact incident.
    """
    raw_text = f"Title: {article['title']}\nDescription: {article['description']}"
    fallback = " ".join(article["title"].split()[:4])

    if not GROQ_API_KEY:
        return fallback

    system_instruction = (
        "You turn news headlines into short stock-photo search queries. "
        "Given a news title and description, return a 2-5 word generic "
        "search query describing the topic or setting of the story (e.g. "
        "'parliament building', 'cricket stadium crowd', 'stock market "
        "screen', 'monsoon city street') -- not the specific event, named "
        "people, or incident. Return ONLY the search query, nothing else."
    )

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-20b",
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": raw_text},
                ],
                "max_tokens": 100,
                "temperature": 0.7,
                "reasoning_effort": "low",
            },
            timeout=30,
        )
        res.raise_for_status()
        query = res.json()["choices"][0]["message"]["content"].strip().strip('"')
        return query or fallback
    except Exception as e:
        print(f"build_image_search_query_with_groq failed ({e}); using fallback query.")
        return fallback


def fetch_stock_image(article: dict, out_path: str) -> str | None:
    """
    Replaces download_article_image(): searches Pexels for a generic,
    properly-licensed image matching the story's topic instead of
    hotlinking the article's own publisher-owned photo. Returns out_path
    on success, or None if there's no usable result.
    """
    if not PEXELS_API_KEY:
        print("No PEXELS_API_KEY set; skipping stock photo search.")
        return None

    query = build_image_search_query_with_groq(article)
    print(f"Stock photo search query: {query!r}")

    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(
                PEXELS_SEARCH_URL,
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "per_page": 1, "orientation": "portrait"},
                timeout=15,
            )
            res.raise_for_status()
            photos = res.json().get("photos", [])
            if not photos:
                print(f"No Pexels results for query {query!r}.")
                return None

            image_url = photos[0]["src"]["large2x"]
            img_res = requests.get(image_url, timeout=20)
            img_res.raise_for_status()
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(img_res.content)
            print(f"Downloaded stock photo to {out_path}")
            return out_path
        except Exception as e:
            last_err = e
            print(f"fetch_stock_image attempt {attempt + 1} failed ({e}); retrying...")

    print(f"Stock photo fetch failed after retries ({last_err}).")
    return None

# ---------------------------------------------------------------------------
# 4. Narration audio
# ---------------------------------------------------------------------------

NARRATION_VOICE = "en-IN-NeerjaNeural"  # or "en-IN-PrabhatNeural" for a male voice

async def _synthesize_speech(text: str, out_path: str, voice: str = NARRATION_VOICE) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)

def generate_narration(script: str, out_path: str) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    asyncio.run(_synthesize_speech(script, out_path))
    print(f"Generated narration at {out_path}")
    return out_path


def get_audio_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    return float(result.stdout.strip())

# ---------------------------------------------------------------------------
# 5. Build one segment: image + narration + burned-in title near the top
# ---------------------------------------------------------------------------

def escape_drawtext(text: str) -> str:
    """
    Escapes characters that break ffmpeg's drawtext filter syntax.
    Same escaping as facts.py's _escape_drawtext.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\u2019")
    text = text.replace(",", "\\,")
    text = text.replace("%", "\\%")
    return text


def _build_single_title_caption(
    text: str,
    caption_file_path: str,
    font_path: str = TITLE_FONT_PATH,
    out_w: int = VIDEO_WIDTH,
    position: str = "top",
    top_padding: int = TITLE_TOP_MARGIN,
    bottom_padding: int = 220,
    palette: dict = None,
) -> str:
    """
    Builds one drawtext filter -- same sizing/wrap/shadow/outline rules as
    before, but now usable at either the top or bottom of the frame so
    "Story N" and the headline can be placed separately.
    """
    escaped = escape_drawtext(text)

    word_count = len(text.split())
    if word_count <= 5:
        font_size = 100
    elif word_count <= 10:
        font_size = 80
    else:
        font_size = 64

    avg_char_width_px = font_size * 0.58
    usable_width_px = out_w - 80
    wrap_width_chars = max(int(usable_width_px / avg_char_width_px), 8)

    wrapped = textwrap.fill(escaped, width=wrap_width_chars)
    with open(caption_file_path, "w", encoding="utf-8") as f:
        f.write(wrapped)

    palette = palette or random.choice(CAPTION_COLOR_PALETTES)
    y_expr = f"{top_padding}" if position == "top" else f"h-text_h-{bottom_padding}"

    return (
        f"drawtext=fontfile={font_path}:textfile={caption_file_path}:"
        f"fontsize={font_size}:fontcolor={palette['fontcolor']}:"
        f"borderw=3:bordercolor={palette['bordercolor']}:"
        f"shadowcolor=black@0.9:shadowx=3:shadowy=3:"
        f"text_align=C:"
        f"x=(w-text_w)/2:y={y_expr}:line_spacing=16"
    )


def build_title_caption_filter(
    story_number: int,
    title: str,
    story_caption_path: str,
    title_caption_path: str,
    font_path: str = TITLE_FONT_PATH,
    out_w: int = VIDEO_WIDTH,
    top_padding: int = TITLE_TOP_MARGIN,
    bottom_padding: int = 220,
    palette: dict = None,
) -> str:
    """
    "Story N" stays fixed near the top (same spot as before); the
    headline itself moves to the bottom of the frame -- same top/bottom
    split as facts.py's two-part caption (hook top, answer bottom). Both
    pieces share one palette so they read as a matched pair.
    """
    palette = palette or random.choice(CAPTION_COLOR_PALETTES)

    story_filter = _build_single_title_caption(
        f"Story {story_number}", story_caption_path,
        font_path=font_path, out_w=out_w,
        position="top", top_padding=top_padding,
        palette=palette,
    )
    title_filter = _build_single_title_caption(
        title, title_caption_path,
        font_path=font_path, out_w=out_w,
        position="bottom", bottom_padding=bottom_padding,
        palette=palette,
    )
    return f"{story_filter},{title_filter}"

def build_segment(article: dict, image_path: str, audio_path: str, position: int, out_path: str) -> str:
    duration = get_audio_duration(audio_path)
    palette = CAPTION_COLOR_PALETTES[position % len(CAPTION_COLOR_PALETTES)]

    story_caption_path = f"{WORKDIR}/caption_story_{position}.txt"
    title_caption_path = f"{WORKDIR}/caption_headline_{position}.txt"
    drawtext = build_title_caption_filter(
        story_number=position + 1,
        title=make_short_title(article),
        story_caption_path=story_caption_path,
        title_caption_path=title_caption_path,
        palette=palette,
    )

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"{drawtext}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-i", audio_path,
        "-vf", vf,
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    print(f"Built segment {position + 1}/{NUM_STORIES} -> {out_path} ({duration:.1f}s)")
    return out_path

# ---------------------------------------------------------------------------
# 6. Concatenate all segments into the final digest video
# ---------------------------------------------------------------------------

def concatenate_segments(segment_paths: list[str], out_path: str = FINAL_VIDEO) -> str:
    list_file = os.path.join(WORKDIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    total_duration = get_audio_duration(out_path)  # works for video files too, ffprobe reads any media
    print(f"Final digest video assembled -> {out_path} ({total_duration:.1f}s total)")
    if total_duration > MAX_VIDEO_SECONDS:
        print(
            f"Warning: final video is {total_duration:.1f}s, over the "
            f"{MAX_VIDEO_SECONDS}s target -- Groq's per-story word targets "
            "may need tightening, or fewer stories per digest."
        )
    return out_path

# ---------------------------------------------------------------------------
# 7. YouTube upload
# ---------------------------------------------------------------------------

def yt_refresh_access_token() -> str:
    res = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "refresh_token": YT_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not res.ok:
        print(f"YouTube token refresh error body: {res.text}")
    res.raise_for_status()
    return res.json()["access_token"]


def publish_to_youtube(video_path: str, title: str, description: str, tags=None):
    """
    Same OAuth-refresh + resumable-upload flow as facts.py's
    publish_to_youtube(), with categoryId 25 (News & Politics) instead of
    27 (Education) to match this content.
    """
    try:
        access_token = yt_refresh_access_token()

        metadata = {
            "snippet": {
                "title": title[:YT_TITLE_MAX_LEN],
                "description": description,
                "tags": tags or ["news", "shorts", "dailybrief", "top5"],
                "categoryId": "25",  # News & Politics
            },
            "status": {
                "privacyStatus": YT_PRIVACY_STATUS,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": true,
            },
        }

        init_res = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
            },
            json=metadata,
            timeout=30,
        )
        if not init_res.ok:
            print(f"YouTube init error body: {init_res.text}")
        init_res.raise_for_status()
        upload_url = init_res.headers["Location"]

        with open(video_path, "rb") as f:
            video_bytes = f.read()

        upload_res = requests.put(
            upload_url,
            headers={"Content-Type": "video/mp4"},
            data=video_bytes,
            timeout=180,
        )
        if not upload_res.ok:
            print(f"YouTube upload error body: {upload_res.text}")
        upload_res.raise_for_status()
        return upload_res
    except Exception as e:
        print(f"YouTube error: {e}")
        return None


def commit_video(video_path: str = FINAL_VIDEO):
    """
    Optional: commits and pushes the final digest video to the repo, same
    pattern as facts.py's commit_video(). Note this isn't required for the
    YouTube upload itself -- publish_to_youtube() reads the local file's
    bytes directly, it doesn't need a hosted URL the way the coffee
    pipeline's Pinterest/Tumblr/Bluesky posting does. Since this pipeline
    runs hourly rather than daily, committing every run's video will add
    up fast (~24 video commits/day vs. facts.py's 1/day) and bloat repo
    history quickly. Call this only if you actually want a persisted
    archive of past digests; otherwise skip it and let each run's video
    disappear with the ephemeral runner after upload.
    """
    print("Committing video to repo...")
    subprocess.run(["git", "config", "user.name", "hourly-digest-bot"])
    subprocess.run(["git", "config", "user.email", "hourly-digest-bot@users.noreply.github.com"])
    subprocess.run(["git", "add", video_path])
    commit_result = subprocess.run(["git", "commit", "-m", "Hourly news digest"], capture_output=True, text=True)
    if commit_result.returncode != 0:
        print(f"Nothing to commit or commit failed:\n{commit_result.stderr}")
        return
    push_result = subprocess.run(["git", "push"], capture_output=True, text=True)
    if push_result.returncode != 0:
        print(f"Video push failed:\n{push_result.stderr}")
    else:
        print("Video committed and pushed successfully.")

# ---------------------------------------------------------------------------
# 8. Orchestration
# ---------------------------------------------------------------------------

def build_hourly_digest() -> tuple[str, str, str]:
    os.makedirs(WORKDIR, exist_ok=True)
    run_time = datetime.now(timezone.utc)

    candidates = fetch_five_stories()
    if not candidates:
        raise SystemExit("No usable stories fetched this hour; aborting.")

    ranked = rank_stories_with_groq(candidates)
    print(f"Have {len(ranked)} ranked candidates; assembling top {NUM_STORIES}.")

    segment_paths = []
    used_stories = []
    for article in ranked:
        if len(used_stories) >= NUM_STORIES:
            break

        position = len(used_stories)
        image_path = fetch_stock_image(article, out_path=f"{WORKDIR}/image_{position}.jpg")
        if image_path is None:
            print(f"Skipping story (no stock photo match): {article['title']}")
            continue

        script = write_segment_script(article, position=position + 1, total=NUM_STORIES)
        audio_path = generate_narration(script, out_path=f"{WORKDIR}/audio_{position}.mp3")
        segment_path = build_segment(
            article, image_path, audio_path, position=position,
            out_path=f"{WORKDIR}/segment_{position}.mp4",
        )
        segment_paths.append(segment_path)
        used_stories.append(article)

    if not segment_paths:
        raise SystemExit("Every candidate story lacked a usable image; aborting.")
    if len(segment_paths) < NUM_STORIES:
        print(f"Warning: only assembled {len(segment_paths)}/{NUM_STORIES} stories "
              f"this run (ran out of candidates with usable images).")

    video_path = concatenate_segments(segment_paths)
    title = generate_youtube_title(run_time)
    description = generate_youtube_description(used_stories, run_time)
    return video_path, title, description


def main():
    video_path, title, description = build_hourly_digest()
    print(f"\nDone. Final video: {video_path}")
    print(f"\nTitle: {title}")
    print(f"\nDescription:\n{description}")

    # Uncomment if you want an archived copy of each hourly digest in-repo
    # (see the size/history caveat in commit_video's docstring):
    # commit_video(video_path)

    # res = publish_to_youtube(video_path, title, description)

    #if res is not None and res.ok:
    #    print(f"Uploaded Short: {res.json().get('id')}")
    #else:
    #    print("YouTube upload failed; see error above.")
    #    raise SystemExit(1)


if __name__ == "__main__":
    main()
