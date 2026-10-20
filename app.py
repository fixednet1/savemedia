"""SaveMedia: downloads real QuickTime-compatible MP4 (H.264 + AAC)."""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool


# ffmpeg must be available: merely renaming or remuxing VP9/AV1/Opus to
# .mp4 does NOT make these codecs playable by QuickTime.
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg

        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        FFMPEG_PATH = None

app = FastAPI(title="SaveMedia Downloader API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class VideoRequest(BaseModel):
    url: str


BASE_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "no_color": True,
    "noplaylist": True,
}
if FFMPEG_PATH:
    BASE_YDL_OPTS["ffmpeg_location"] = FFMPEG_PATH


def cleanup_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def validate_quality(quality: str) -> None:
    if quality != "best" and not (quality.isdigit() and 144 <= int(quality) <= 4320):
        raise HTTPException(status_code=400, detail="Недопустимое разрешение видео")


def find_downloaded_file(temp_dir: str, media_format: str) -> str:
    """Locate the final media file (not .part, metadata, or partial streams)."""
    extensions = {".mp3"} if media_format == "mp3" else {
        ".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts", ".m4v"
    }
    found = [
        p for p in Path(temp_dir).iterdir()
        if p.is_file() and p.suffix.lower() in extensions and p.stat().st_size > 0
    ]
    if not found:
        raise RuntimeError("Не удалось найти готовый медиафайл после скачивания")
    # There should be just one file because noplaylist=True, and yt-dlp
    # removes the separate temporary audio/video tracks after merging.
    return str(max(found, key=lambda p: p.stat().st_mtime))


def convert_for_quicktime(source: str, destination: str) -> None:
    """Re-encode codecs, not just the container: H.264/yuv420p + AAC/MP4.

    Always encode: incoming .mp4 may still have AV1, VP9 or Opus tracks.
    The final result also has even dimensions and faststart metadata.
    """
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg не установлен. Конвертация в MP4 невозможна")

    command = [
        FFMPEG_PATH,
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", source,
        "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-tag:v", "avc1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-max_muxing_queue_size", "4096",
        "-f", "mp4", destination,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise RuntimeError(f"Не удалось запустить FFmpeg: {exc}") from exc

    if result.returncode != 0 or not os.path.isfile(destination) or os.path.getsize(destination) == 0:
        detail = result.stderr.strip()[-1200:] or "FFmpeg не смог обработать этот видеофайл"
        raise RuntimeError(f"Ошибка конвертации видео: {detail}")


def prepare_media(url: str, media_format: str, quality: str, temp_dir: str) -> str:
    """Run yt-dlp and (for video) FFmpeg outside the async event loop."""
    file_id = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(temp_dir, f"original_{file_id}.%(ext)s")

    if media_format == "mp3":
        ydl_opts = {
            **BASE_YDL_OPTS,
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
        }
    else:
        if quality == "best":
            stream_format = "bestvideo+bestaudio/best"
        else:
            # Syntax is height<=N. The original project had height<<=N.
            stream_format = (
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/best"
            )
        ydl_opts = {
            **BASE_YDL_OPTS,
            "format": stream_format,
            "outtmpl": outtmpl,
            # MKV accepts the codecs yt-dlp receives; we then *encode* MP4.
            "merge_output_format": "mkv",
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    downloaded = find_downloaded_file(temp_dir, media_format)
    if media_format == "mp3":
        return downloaded

    converted = os.path.join(temp_dir, f"SaveMedia_{file_id}_QuickTime.mp4")
    convert_for_quicktime(downloaded, converted)
    return converted


@app.get("/", response_class=HTMLResponse)
async def serve_landing():
    path = Path(__file__).with_name("index.html")
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "<h1>Файл index.html не найден</h1>"


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "ffmpeg_installed": FFMPEG_PATH is not None,
        "quicktime_compatible": FFMPEG_PATH is not None,
    }


@app.post("/api/info")
async def get_video_info(req: VideoRequest):
    options = {**BASE_YDL_OPTS, "skip_download": True}
    try:
        def fetch_info():
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(req.url, download=False)

        info = await run_in_threadpool(fetch_info)
        heights = {
            int(f["height"])
            for f in info.get("formats", [])
            if f.get("vcodec") not in (None, "none")
            and isinstance(f.get("height"), (int, float))
            and f["height"] >= 144
        }
        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "webpage_url": info.get("webpage_url"),
            "qualities": sorted(heights, reverse=True),
            "ffmpeg_ready": FFMPEG_PATH is not None,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось обработать ссылку: {exc}") from exc


@app.get("/api/download")
async def download_media(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="Ссылка на видео"),
    format: str = Query("mp4", description="mp4 или mp3"),
    quality: str = Query("best", description="Высота видео или best"),
):
    if format not in {"mp4", "mp3"}:
        raise HTTPException(status_code=400, detail="Формат должен быть mp4 или mp3")
    if format == "mp4":
        validate_quality(quality)
    if not FFMPEG_PATH:
        raise HTTPException(
            status_code=503,
            detail="Для правильного MP4 (H.264/AAC) и MP3 нужен FFmpeg. "
                   "Установите FFmpeg или imageio-ffmpeg и перезапустите сервер.",
        )

    temp_dir = tempfile.mkdtemp(prefix="savemedia_")
    try:
        filepath = await run_in_threadpool(prepare_media, url, format, quality, temp_dir)
        file_id = uuid.uuid4().hex[:8]
        q_label = f"{quality}p" if quality.isdigit() else "MAX"
        filename = (
            f"SaveMedia_{file_id}.mp3" if format == "mp3"
            else f"SaveMedia_{q_label}_{file_id}_Mac.mp4"
        )
        background_tasks.add_task(cleanup_temp_dir, temp_dir)
        return FileResponse(
            path=filepath,
            filename=filename,
            media_type="audio/mpeg" if format == "mp3" else "video/mp4",
            background=background_tasks,
        )
    except Exception as exc:
        cleanup_temp_dir(temp_dir)
        raise HTTPException(status_code=400, detail=f"Ошибка скачивания: {exc}") from exc


if __name__ == "__main__":
    import uvicorn

    print("SaveMedia запущен: http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
