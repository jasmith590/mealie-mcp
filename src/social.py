"""Read the caption of a social media post (TikTok, Instagram, Facebook, YouTube)
without downloading the video.

yt-dlp reads the post's metadata (caption, creator, thumbnail). For TikTok, the
public oEmbed endpoint is a fallback when yt-dlp is broken by a TikTok change,
which happens every few weeks until yt-dlp catches up.
"""

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("mealie-mcp")

SOCIAL_HOSTS = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}

TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
TIMEOUT = 20


def platform_of(url: str) -> Optional[str]:
    """The platform a link belongs to, or None for an ordinary website."""
    host = (urlparse(url.strip()).hostname or "").lower()
    for domain, name in SOCIAL_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def clean_caption(text: str) -> str:
    """Drop a trailing run of hashtags (kept if they're mid-sentence) and tidy spacing."""
    text = (text or "").replace("\r\n", "\n").strip()
    text = re.sub(r"(\s*#[\wÀ-￿]+)+\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _from_ytdlp(url: str) -> Dict[str, Any]:
    import yt_dlp  # imported here so the rest of the server works without it

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": TIMEOUT,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return {
        "caption": info.get("description") or info.get("title") or "",
        "title": info.get("title"),
        "creator": info.get("uploader") or info.get("channel") or info.get("creator"),
        "creatorHandle": info.get("uploader_id"),
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url") or url,
    }


def _from_tiktok_oembed(url: str) -> Dict[str, Any]:
    resp = httpx.get(
        TIKTOK_OEMBED, params={"url": url}, timeout=TIMEOUT, follow_redirects=True
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "caption": data.get("title") or "",
        "title": None,
        "creator": data.get("author_name"),
        "creatorHandle": data.get("author_unique_id"),
        "thumbnail": data.get("thumbnail_url"),
        "url": url,
    }


def fetch_caption(url: str) -> Dict[str, Any]:
    """Caption and creator of a social post. Never raises: ok=false carries a reason."""
    platform = platform_of(url)
    if not platform:
        return {
            "ok": False,
            "url": url,
            "reason": "Not a social media link. Use import_recipe_from_url for websites.",
        }

    attempts = [("yt-dlp", _from_ytdlp)]
    if platform == "tiktok":
        attempts.append(("oembed", _from_tiktok_oembed))

    errors = []
    for source, fn in attempts:
        try:
            got = fn(url)
        except Exception as e:
            logger.warning({"message": "Caption fetch failed", "source": source, "url": url, "error": str(e)})
            errors.append(f"{source}: {str(e).splitlines()[0][:200]}")
            continue
        caption = clean_caption(got.get("caption", ""))
        if not caption:
            errors.append(f"{source}: the post has no caption")
            continue
        out = {"ok": True, "platform": platform, "source": source, **got, "caption": caption}
        return {k: v for k, v in out.items() if v not in (None, "")}

    hint = (
        " Instagram and Facebook often hide posts from logged-out requests."
        if platform in ("instagram", "facebook")
        else ""
    )
    return {
        "ok": False,
        "platform": platform,
        "url": url,
        "reason": "Could not read the caption (" + "; ".join(errors) + ")." + hint
        + " Ask the user to paste the caption or attach a screenshot.",
    }
