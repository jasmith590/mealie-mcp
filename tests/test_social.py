"""
Social caption unit tests (this fork). No live infrastructure: yt-dlp and the
TikTok oEmbed call are stubbed.

- platform detection for social links vs. ordinary websites
- trailing hashtags are stripped, mid-caption ones are kept
- yt-dlp result is returned; TikTok falls back to oEmbed when yt-dlp fails
- an empty caption or a total failure comes back ok=false with a reason
- get_social_caption is registered as a tool

Usage:
    python tests/test_social.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp.server.fastmcp import FastMCP

import social
from tools.recipe_tools import register_recipe_tools

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, bool(condition), detail))


def section(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def boom(url):
    raise RuntimeError("ERROR: [TikTok] Unable to extract webpage video data\nmore")


YTDLP_OK = lambda url: {
    "caption": "Crispy chili chicken\n\n1 lb chicken thighs\n2 tbsp chili crunch\n\n#food #easyrecipe #fyp",
    "title": "Crispy chili chicken",
    "creator": "Chef Sam",
    "creatorHandle": "chefsam",
    "thumbnail": "https://p16.example/thumb.jpg",
    "url": url,
}

OEMBED_OK = lambda url: {
    "caption": "Garlic noodles: 8 oz noodles, 4 cloves garlic #dinner",
    "title": None, "creator": "noodlequeen", "creatorHandle": "noodlequeen",
    "thumbnail": "https://p16.example/oe.jpg", "url": url,
}

section("Platform detection")
check("tiktok", social.platform_of("https://www.tiktok.com/@a/video/1") == "tiktok")
check("vm.tiktok short link", social.platform_of("https://vm.tiktok.com/ZMabc/") == "tiktok")
check("instagram", social.platform_of("https://www.instagram.com/reel/abc/") == "instagram")
check("fb.watch", social.platform_of("https://fb.watch/xyz/") == "facebook")
check("youtu.be", social.platform_of("https://youtu.be/abc") == "youtube")
check("website is not social", social.platform_of("https://www.seriouseats.com/x") is None)
check("lookalike host is not social", social.platform_of("https://nottiktok.com/x") is None)

section("Caption cleanup")
check("trailing hashtags dropped",
      social.clean_caption("Mix it well #food #fyp") == "Mix it well")
check("mid-caption hashtag kept",
      social.clean_caption("Use #1 rated sauce\nthen bake #yum") == "Use #1 rated sauce\nthen bake")

section("fetch_caption")
social._from_ytdlp, social._from_tiktok_oembed = YTDLP_OK, OEMBED_OK
r = social.fetch_caption("https://www.tiktok.com/@chefsam/video/1")
check("yt-dlp result used", r["ok"] and r["source"] == "yt-dlp", str(r.get("source")))
check("hashtags stripped", r["caption"].endswith("2 tbsp chili crunch"), repr(r["caption"][-30:]))
check("creator and thumbnail returned", r["creatorHandle"] == "chefsam" and r["thumbnail"])

social._from_ytdlp = boom
r = social.fetch_caption("https://www.tiktok.com/@n/video/2")
check("tiktok falls back to oEmbed", r["ok"] and r["source"] == "oembed", str(r))
check("oEmbed caption cleaned", r["caption"] == "Garlic noodles: 8 oz noodles, 4 cloves garlic")

r = social.fetch_caption("https://www.instagram.com/reel/abc/")
check("instagram has no oEmbed fallback", not r["ok"])
check("reason mentions login and asks for caption",
      "logged-out" in r["reason"] and "paste the caption" in r["reason"], r["reason"])
check("only first line of the error kept", "more" not in r["reason"])

social._from_ytdlp = lambda url: {"caption": "#fyp #food", "url": url}
social._from_tiktok_oembed = lambda url: {"caption": "", "url": url}
r = social.fetch_caption("https://www.tiktok.com/@n/video/3")
check("hashtag-only caption is ok=false", not r["ok"] and "no caption" in r["reason"], r.get("reason", ""))

r = social.fetch_caption("https://www.seriouseats.com/x")
check("website refused with a pointer", not r["ok"] and "import_recipe_from_url" in r["reason"])

section("Tool registration")
social._from_ytdlp = YTDLP_OK
srv = FastMCP("t")
register_recipe_tools(srv, object())
names = [t.name for t in asyncio.run(srv.list_tools())]
check("get_social_caption registered", "get_social_caption" in names)
res = asyncio.run(srv.call_tool("get_social_caption", {"url": "https://www.tiktok.com/@a/video/9"}))
content = res[0] if isinstance(res, tuple) else res
check("tool returns the caption", json.loads(content[0].text)["ok"] is True)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
