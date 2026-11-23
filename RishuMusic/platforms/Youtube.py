import asyncio
import os
import re
import logging
from typing import Union
from urllib.parse import parse_qs, quote, urlparse
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from py_yt import VideosSearch, Playlist
import aiohttp


# Separate API responsibilities:
# ADam Music API = catalogue search and metadata (X-API-Key protected).
# OWN_API_URL = the existing audio/video stream source used by download_song/download_video.
API_URL = os.environ.get("OWN_API_URL", "http://vapters.site").rstrip("/")
ADAM_API_URL = os.environ.get(
    "ADAM_MUSIC_API_URL", "https://adam-music-api.onrender.com"
).rstrip("/")
ADAM_API_KEY = os.environ.get("ADAM_MUSIC_API_KEY", "").strip()

DOWNLOAD_DIR = "downloads"
logger = logging.getLogger(__name__)


def _duration_label(seconds):
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _normalise_track(item):
    """Convert ADam and legacy search payloads into the bot's old result shape."""
    if not isinstance(item, dict):
        return None

    video_id = item.get("video_id") or item.get("videoId") or item.get("id")
    if not video_id:
        return None

    duration = item.get("duration") or item.get("duration_min")
    duration_seconds = (
        item.get("duration_seconds")
        or item.get("length_seconds")
        or item.get("duration_sec")
    )
    if duration_seconds is None and duration:
        try:
            duration_seconds = time_to_seconds(duration)
        except (TypeError, ValueError):
            duration_seconds = None
    if not duration and duration_seconds is not None:
        duration = _duration_label(duration_seconds)

    thumbnails = item.get("thumbnails") or []
    if isinstance(thumbnails, dict):
        thumbnails = thumbnails.get("thumbnails") or []
    thumbnail = item.get("thumbnail") or item.get("thumb")
    if not thumbnail and isinstance(thumbnails, list):
        for image in thumbnails:
            if isinstance(image, dict) and image.get("url"):
                thumbnail = image["url"]
                break

    video_url = (
        item.get("youtube_music_url")
        or item.get("link")
        or f"https://www.youtube.com/watch?v={video_id}"
    )
    return {
        "title": item.get("title") or item.get("name") or "Unknown title",
        "duration": duration,
        "duration_min": duration,
        "duration_sec": int(duration_seconds or 0),
        "id": str(video_id),
        "vidid": str(video_id),
        "link": video_url,
        "thumbnail": thumbnail,
        "thumb": thumbnail,
        "thumbnails": [{"url": thumbnail}] if thumbnail else [],
    }


async def _adam_get_json(endpoint, params=None):
    """Call ADam Music API without ever exposing its key in URLs or logs."""
    if not ADAM_API_URL or not ADAM_API_KEY:
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"X-API-Key": ADAM_API_KEY}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{ADAM_API_URL}/{endpoint.lstrip('/')}",
                params=params or {},
                headers=headers,
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "ADam Music API request failed at %s (HTTP %s)",
                        endpoint,
                        response.status,
                    )
                    return None
                return await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            "ADam Music API request failed at %s: %s",
            endpoint,
            type(exc).__name__,
        )
        return None


async def _adam_search(query, limit=1):
    payload = await _adam_get_json(
        "/v1/platforms/youtube/search",
        {"q": query, "limit": max(1, min(int(limit), 25))},
    )
    if not isinstance(payload, dict):
        return []
    results = payload.get("results") or []
    return [
        track
        for track in (_normalise_track(item) for item in results)
        if track
    ]


async def _adam_resolve(url):
    payload = await _adam_get_json(
        "/v1/platforms/youtube/resolve",
        {"url": url},
    )
    if not isinstance(payload, dict):
        return None
    return _normalise_track(payload.get("result"))


async def _legacy_search(query, limit=1):
    """Fallback for deployments where ADam API credentials are not configured."""
    try:
        payload = await VideosSearch(query, limit=limit).next()
        results = (payload or {}).get("result") or []
        return [
            track
            for track in (_normalise_track(item) for item in results)
            if track
        ]
    except Exception as exc:
        logger.warning("Fallback YouTube search failed: %s", type(exc).__name__)
        return []


async def _search_videos(query, limit=1, videoid=None, base=None):
    search_query = f"{base}{query}" if videoid else str(query).strip()

    if ADAM_API_KEY:
        # Resolve a supplied YouTube URL directly before trying catalogue search.
        parsed = urlparse(search_query)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            resolved = await _adam_resolve(search_query)
            if resolved:
                return [resolved]

        results = await _adam_search(search_query, limit)
        if results:
            return results

    return await _legacy_search(search_query, limit)


def time_to_seconds(time):
    stringt = str(time)
    return sum(int(x) * 60 ** i for i, x in enumerate(reversed(stringt.split(":"))))


async def download_song(link: str) -> str:
    video_id = link.split("v=")[-1].split("&")[0] if "v=" in link else link
    if not video_id or len(video_id) < 3:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if os.path.exists(file_path) and os.path.getsize(file_path) > 10000:
        return file_path

    try:
        async with aiohttp.ClientSession() as session:
            stream_url = f"{API_URL}/stream/{video_id}?vid=false&quality=128"
            async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return None
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(131072):
                        f.write(chunk)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 10000:
            return file_path
        return None
    except Exception:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        return None


async def download_video(link: str) -> str:
    video_id = link.split("v=")[-1].split("&")[0] if "v=" in link else link
    if not video_id or len(video_id) < 3:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")

    if os.path.exists(file_path) and os.path.getsize(file_path) > 10000:
        return file_path

    try:
        async with aiohttp.ClientSession() as session:
            stream_url = f"{API_URL}/stream/{video_id}?vid=true&quality=480"
            async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    return None
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(131072):
                        f.write(chunk)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 10000:
            return file_path
        return None
    except Exception:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        return None


class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
            elif message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return None

    async def details(self, link: str, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=1, videoid=videoid, base=self.base)
        if not results:
            raise ValueError("No matching YouTube Music track was found.")
        item = results[0]
        return (
            item["title"],
            item.get("duration_min"),
            int(item.get("duration_sec") or 0),
            item.get("thumbnail"),
            item["id"],
        )

    async def title(self, link: str, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=1, videoid=videoid, base=self.base)
        return results[0]["title"] if results else None

    async def duration(self, link: str, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=1, videoid=videoid, base=self.base)
        return results[0].get("duration_min") if results else None

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=1, videoid=videoid, base=self.base)
        return results[0].get("thumbnail") if results else None

    async def video(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        try:
            downloaded_file = await download_video(link)
            if downloaded_file:
                return 1, downloaded_file
            return 0, "Video download failed"
        except Exception as e:
            return 0, f"Video download error: {e}"

    async def playlist(self, link, limit, user_id, videoid: Union[bool, str] = None):
        playlist_id = str(link) if videoid else (
            parse_qs(urlparse(str(link)).query).get("list") or [None]
        )[0]

        if playlist_id and ADAM_API_KEY:
            payload = await _adam_get_json(
                f"/v1/playlists/{quote(playlist_id, safe='')}",
                {"limit": max(1, min(int(limit), 100))},
            )
            playlist_data = (payload or {}).get("result") or {}
            tracks = playlist_data.get("tracks") or []
            ids = [
                str(item.get("video_id") or item.get("videoId") or item.get("id"))
                for item in tracks
                if isinstance(item, dict)
                and (item.get("video_id") or item.get("videoId") or item.get("id"))
            ]
            if ids:
                return ids[:limit]

        if videoid:
            link = self.listbase + str(link)
        if "&" in str(link):
            link = str(link).split("&")[0]
        try:
            plist = await Playlist.get(link)
        except Exception:
            return []
        videos = plist.get("videos") or []
        ids = []
        for data in videos[:limit]:
            if not data:
                continue
            vid = data.get("id")
            if not vid:
                continue
            ids.append(vid)
        return ids

    async def track(self, link: str, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=1, videoid=videoid, base=self.base)
        if not results:
            raise ValueError("No matching YouTube Music track was found.")
        item = results[0]
        track_details = {
            "title": item["title"],
            "link": item["link"],
            "vidid": item["id"],
            "duration_min": item.get("duration_min"),
            "thumb": item.get("thumbnail"),
        }
        return track_details, item["id"]

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        ytdl_opts = {"quiet": True}
        ydl = yt_dlp.YoutubeDL(ytdl_opts)
        with ydl:
            formats_available = []
            r = ydl.extract_info(link, download=False)
            for format in r["formats"]:
                try:
                    if "dash" not in str(format["format"]).lower():
                        formats_available.append(
                            {
                                "format": format["format"],
                                "filesize": format.get("filesize"),
                                "format_id": format["format_id"],
                                "ext": format["ext"],
                                "format_note": format["format_note"],
                                "yturl": link,
                            }
                        )
                except Exception:
                    continue
        return formats_available, link

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str] = None):
        results = await _search_videos(link, limit=10, videoid=videoid, base=self.base)
        if not results:
            raise ValueError("No YouTube Music search results were found.")
        index = max(0, min(int(query_type), len(results) - 1))
        item = results[index]
        return (
            item["title"],
            item.get("duration_min"),
            item.get("thumbnail"),
            item["id"],
        )

    async def download(
        self,
        link: str,
        mystic,
        video: Union[bool, str] = None,
        videoid: Union[bool, str] = None,
        songaudio: Union[bool, str] = None,
        songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None,
        title: Union[bool, str] = None,
    ) -> str:
        if videoid:
            link = self.base + link
        try:
            if video:
                downloaded_file = await download_video(link)
            else:
                downloaded_file = await download_song(link)
            if downloaded_file:
                return downloaded_file, True
            return None, False
        except Exception:
            return None, False


YouTube = YouTubeAPI()