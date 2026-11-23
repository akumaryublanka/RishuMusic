import asyncio
import os
import re
import logging
import time
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
    "ADAM_MUSIC_API_URL", "https://adam-music-api-a20u.onrender.com"
).rstrip("/")
ADAM_API_KEY = os.environ.get("ADAM_MUSIC_API_KEY", "").strip()

DOWNLOAD_DIR = "downloads"
logger = logging.getLogger(__name__)


def _timeout_setting(name, default, maximum):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)
    if value <= 0 or value > maximum:
        return float(default)
    return value


# The default read timeout is above the old 20-second limit, which was
# too aggressive for slower upstream media APIs. Values can be tuned in Render.
OWN_API_CONNECT_TIMEOUT = _timeout_setting("OWN_API_CONNECT_TIMEOUT", 10, 120)
OWN_API_READ_TIMEOUT = _timeout_setting("OWN_API_READ_TIMEOUT", 60, 300)
OWN_API_TOTAL_TIMEOUT = _timeout_setting("OWN_API_TOTAL_TIMEOUT", 180, 1800)


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


LAST_DL = {}


def _extract_video_id(link):
    raw = str(link or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.hostname:
        query_id = (parse_qs(parsed.query).get("v") or [None])[0]
        if query_id:
            return query_id.strip()

        host = parsed.hostname.lower()
        parts = [part for part in parsed.path.split("/") if part]
        if "youtu.be" in host and parts:
            return parts[0].split("?")[0].strip()
        for marker in ("shorts", "embed", "live", "v"):
            if marker in parts:
                index = parts.index(marker)
                if index + 1 < len(parts):
                    return parts[index + 1].split("?")[0].strip()

    return raw.split("&", 1)[0].strip()


def _valid_download(path):
    try:
        return bool(path and os.path.isfile(path) and os.path.getsize(path) > 10000)
    except (OSError, TypeError, ValueError):
        return False


def _record_download(**values):
    LAST_DL.clear()
    LAST_DL.update(values)


def _yt_dlp_download_sync(video_id, video=False):
    """Local fallback when OWN_API_URL is unavailable or returns an empty file."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

    options = {
        "format": (
            "best[height<=480][ext=mp4]/best[height<=480]/best"
            if video
            else "bestaudio/best"
        ),
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "overwrites": True,
    }
    if video:
        options["merge_output_format"] = "mp4"
    else:
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=True)
        prepared = ydl.prepare_filename(info)

    stem = os.path.splitext(prepared)[0]
    candidates = (
        [f"{stem}.mp4", os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4"),
         f"{stem}.mkv", f"{stem}.webm"]
        if video
        else [f"{stem}.mp3", os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3"),
              prepared, f"{stem}.m4a", f"{stem}.webm"]
    )
    for candidate in candidates:
        if _valid_download(candidate):
            return candidate

    raise RuntimeError("yt-dlp finished without producing a usable media file")


async def _download_media(link, video=False):
    video_id = _extract_video_id(link)
    if not video_id or len(video_id) < 3:
        _record_download(ok=False, why="Invalid YouTube video ID")
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    extension = ".mp4" if video else ".mp3"
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{extension}")

    if _valid_download(file_path):
        size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)
        _record_download(
            ok=True, source="cache", wait=0, transfer=0, size_mb=size_mb
        )
        return file_path

    api_started = time.monotonic()
    api_wait = 0.0
    api_transfer = 0.0
    api_error = "OWN_API_URL did not return a usable file"

    try:
        timeout = aiohttp.ClientTimeout(
            total=min(1800, OWN_API_TOTAL_TIMEOUT * (3 if video else 1)),
            connect=OWN_API_CONNECT_TIMEOUT,
            sock_read=min(300, OWN_API_READ_TIMEOUT * (2 if video else 1)),
        )
        stream_url = (
            f"{API_URL}/stream/{video_id}"
            f"?vid={'true' if video else 'false'}"
            f"&quality={480 if video else 128}"
        )
        partial_path = file_path + ".part"
        try:
            if os.path.exists(partial_path):
                os.remove(partial_path)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(stream_url) as response:
                    api_wait = round(time.monotonic() - api_started, 1)
                    if response.status != 200:
                        api_error = f"OWN_API_URL HTTP {response.status}"
                    else:
                        content_type = (response.headers.get("Content-Type") or "").lower()
                        if "text/html" in content_type or "application/json" in content_type:
                            api_error = "OWN_API_URL returned an error page instead of media"
                        else:
                            transfer_started = time.monotonic()
                            with open(partial_path, "wb") as output:
                                async for chunk in response.content.iter_chunked(131072):
                                    if chunk:
                                        output.write(chunk)
                            api_transfer = round(time.monotonic() - transfer_started, 1)

                            if _valid_download(partial_path):
                                os.replace(partial_path, file_path)
                                size_mb = round(
                                    os.path.getsize(file_path) / (1024 * 1024), 2
                                )
                                _record_download(
                                    ok=True,
                                    source="OWN_API_URL",
                                    wait=api_wait,
                                    transfer=api_transfer,
                                    size_mb=size_mb,
                                )
                                return file_path
                            api_error = "OWN_API_URL returned an empty or incomplete file"
        finally:
            # Never leave a partial response that could be mistaken for a track.
            if os.path.exists(partial_path):
                try:
                    os.remove(partial_path)
                except OSError:
                    pass
    except asyncio.TimeoutError:
        api_wait = round(time.monotonic() - api_started, 1)
        api_error = "OWN_API_URL timed out"
    except Exception as exc:
        api_wait = round(time.monotonic() - api_started, 1)
        api_error = f"OWN_API_URL {type(exc).__name__}: {str(exc)[:100]}"

    if os.path.exists(file_path) and not _valid_download(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass

    fallback_started = time.monotonic()
    try:
        fallback_path = await asyncio.to_thread(
            _yt_dlp_download_sync, video_id, video
        )
        fallback_elapsed = round(time.monotonic() - fallback_started, 1)
        if _valid_download(fallback_path):
            size_mb = round(
                os.path.getsize(fallback_path) / (1024 * 1024), 2
            )
            _record_download(
                ok=True,
                source="yt-dlp fallback",
                wait=api_wait,
                transfer=fallback_elapsed,
                size_mb=size_mb,
            )
            logger.warning(
                "OWN_API_URL download unavailable for %s (%s); yt-dlp fallback succeeded",
                video_id,
                api_error,
            )
            return fallback_path
        raise RuntimeError("fallback output file is missing or too small")
    except Exception as exc:
        fallback_elapsed = round(time.monotonic() - fallback_started, 1)
        reason = f"{api_error}; yt-dlp {type(exc).__name__}: {str(exc)[:140]}"
        _record_download(
            ok=False,
            why=reason,
            wait=api_wait,
            transfer=fallback_elapsed,
        )
        logger.warning("YouTube download failed for %s: %s", video_id, reason)
        return None


async def download_song(link: str) -> str:
    return await _download_media(link, video=False)


async def download_video(link: str) -> str:
    return await _download_media(link, video=True)


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
    ) -> tuple:
        if videoid:
            link = self.base + str(link)
        try:
            if video:
                downloaded_file = await download_video(link)
            else:
                downloaded_file = await download_song(link)
            if downloaded_file and _valid_download(downloaded_file):
                return downloaded_file, True
            return None, False
        except Exception as exc:
            _record_download(ok=False, why=f"{type(exc).__name__}: {str(exc)[:140]}")
            logger.exception("Unexpected YouTube download wrapper error")
            return None, False


YouTube = YouTubeAPI()