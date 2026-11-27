"""NumTok: TikTok on the NumWorks calculator, the computer side.

The calculator shows one vertical video at a time, TikTok style: the video in
the middle of the screen (136x240), the author and caption on the left, the
likes, comments and shares on the right. Down goes to the next video, up to
the previous one, OK pauses. Videos loop until the user moves on.

Feeds come from yt-dlp:
  - "Pour toi": the latest videos of a mix of popular creators, shuffled
    (the list can be changed with the NUMTOK_CREATORS environment variable,
    comma separated);
  - a creator: the user types their name (with or without @; spaces are tried
    as ".", "_" or nothing, since the calculator has no easy "." or "_").

TikTok's video servers refuse direct streaming (they want the cookies of the
page), and TikTok videos are short: each one is downloaded whole (a few
seconds), then decoded locally. The next videos are downloaded in advance so
that swiping is instant.

Wire protocol (keep in sync with epsilon/apps/numtok/protocol.h):
  0xB0 FEED        calc -> PC  UTF-8 query, empty for "Pour toi"
  0xB1 FEED_READY  PC -> calc  u16 count (0: nothing found), UTF-8 feed title
  0xB2 PLAY        calc -> PC  u16 index in the feed
  0xB3 INFO        PC -> calc  u16 index, u16 count, u32 duration_ms, u32 likes,
                               u32 comments, u32 shares, u32 views, then three
                               u8-length strings: author, caption, sound
  0xB4 VIDEO       PC -> calc  u16 index, u16 frame, u16 start tile, u8 flags, tile ops
                               (320x240 picture, see TileEncoder; only the middle
                               136 px column carries video)
  0xB5 ACK         calc -> PC  u16 frame, once drawn
  0xB6 CONTROL     calc -> PC  u8: 0 stop, 1 pause, 2 resume
  0xB7 STATUS      PC -> calc  UTF-8 text for the video area, empty to clear
  0xB8 ERROR       PC -> calc  UTF-8: this video cannot be played
"""
from __future__ import annotations

import json
import os
import random
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

T_FEED = 0xB0
T_FEED_READY = 0xB1
T_PLAY = 0xB2
T_INFO = 0xB3
T_VIDEO = 0xB4
T_ACK = 0xB5
T_CONTROL = 0xB6
T_STATUS = 0xB7
T_ERROR = 0xB8
FIRST, LAST = 0xB0, 0xBF

CTRL_STOP, CTRL_PAUSE, CTRL_RESUME = 0, 1, 2
FLAG_END_OF_FRAME = 1

SCREEN_W, SCREEN_H = 320, 240
ROWS = SCREEN_H // 4
VIDEO_X, VIDEO_W = 92, 136  # tile aligned; keep in sync with protocol.h
FPS = 15

DEFAULT_CREATORS = [
    "khaby.lame", "zachking", "mrbeast", "bellapoarch", "charlidamelio",
    "willsmith", "squeezie", "mcflyetcarlito", "lenamahfouf", "inoxtag",
    "tiboinshape", "michou", "noholito", "scottsreality", "dylan.page",
]
FEED_PER_CREATOR = 6
CACHE_AHEAD = 2
CACHE_KEEP = 8


def creators() -> list[str]:
    env = os.environ.get("NUMTOK_CREATORS", "")
    names = [n.strip().lstrip("@") for n in env.split(",") if n.strip()]
    return names or list(DEFAULT_CREATORS)


def name_variants(query: str) -> list[str]:
    """What the user typed on the calculator, as possible TikTok handles."""
    words = query.strip().lstrip("@").lower().split()
    if not words:
        return []
    if len(words) == 1:
        return [words[0]]
    return [".".join(words), "".join(words), "_".join(words)]


def format_count(value: int) -> str:
    for limit, suffix in ((1_000_000_000, "Md"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            number = value / limit
            text = f"{number:.1f}" if number < 100 else f"{number:.0f}"
            return text.rstrip("0").rstrip(".").replace(".", ",") + suffix
    return str(value)


class Video:
    """One entry of a feed. Filled more completely once downloaded."""

    def __init__(self, entry: dict) -> None:
        self.id = str(entry.get("id") or "")
        self.url = entry.get("url") or entry.get("webpage_url") or ""
        if not self.url.startswith("http"):
            uploader = entry.get("uploader") or "_"
            self.url = f"https://www.tiktok.com/@{uploader}/video/{self.id}"
        self.update(entry)
        self.path: str | None = None
        self.error: str | None = None
        self.ready = threading.Event()
        self.started = False

    def update(self, info: dict) -> None:
        def number(key: str) -> int:
            try:
                return max(0, min(int(info.get(key) or 0), 0xFFFFFFFF))
            except (TypeError, ValueError):
                return 0

        self.author = str(info.get("uploader") or info.get("channel") or "")
        self.caption = str(info.get("description") or info.get("title") or "")
        track, artist = info.get("track") or "", info.get("artist") or ""
        self.sound = f"{track} - {artist}" if track and artist else str(track or artist)
        self.duration_ms = int(float(info.get("duration") or 0) * 1000)
        self.likes = number("like_count")
        self.comments = number("comment_count")
        self.shares = number("repost_count")
        self.views = number("view_count")


class TokService:
    """One per connected calculator."""

    def __init__(self, gw, log) -> None:
        # gw: the numtube_gateway module (yt-dlp/ffmpeg paths, TileEncoder,
        # text helpers), passed in to avoid an import cycle.
        self.gw = gw
        self.log = log
        self.link = None
        self.feed: list[Video] = []
        self.feed_lock = threading.Lock()
        self.feed_generation = 0
        self.player: TokPlayer | None = None
        self.workdir = tempfile.mkdtemp(prefix="numtok-")
        self.downloads = ThreadPoolExecutor(max_workers=2)

    # -- frames from the calculator
    def handle(self, link, frame_type: int, payload: bytes) -> None:
        self.link = link
        if frame_type == T_FEED:
            query = payload.decode("utf-8", "ignore").strip()
            self.stop()
            self.feed_generation += 1
            generation = self.feed_generation
            threading.Thread(target=self._safe_feed, args=(query, generation), daemon=True).start()
        elif frame_type == T_PLAY and len(payload) >= 2:
            self.play(struct.unpack("<H", payload[:2])[0])
        elif frame_type == T_ACK and len(payload) >= 2 and self.player:
            self.player.on_ack(struct.unpack("<H", payload[:2])[0])
        elif frame_type == T_CONTROL and payload:
            if payload[0] == CTRL_STOP:
                self.stop()
            elif self.player:
                self.player.set_paused(payload[0] == CTRL_PAUSE)

    def stop(self) -> None:
        if self.player:
            self.player.stop()
            self.player = None

    def detach(self, link=None) -> None:
        self.stop()
        self.feed_generation += 1
        self.downloads.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        if self.link is not None:
            self.link.send(frame_type, payload)

    def text(self, value: str, max_bytes: int) -> bytes:
        return self.gw.truncate_utf8(self.gw.clean_text(value), max_bytes)

    # -- feeds
    def _list(self, url: str, count: int) -> list[dict]:
        cmd = [*self.gw.YTDLP, "--no-warnings", "--flat-playlist", "-J", "--playlist-end", str(count), url]
        result = subprocess.run(cmd, capture_output=True, timeout=60, **self.gw.POPEN_FLAGS)
        if result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "TikTok ne repond pas")
        entries = json.loads(result.stdout).get("entries") or []
        return [e for e in entries if e and e.get("id")]

    def _safe_feed(self, query: str, generation: int) -> None:
        try:
            self._build_feed(query, generation)
        except OSError:
            pass
        except Exception as error:  # never leave the calculator waiting
            self.log(f"numtok: {error}")
            try:
                self.send(T_FEED_READY, struct.pack("<H", 0) + self.text(str(error), 100))
            except OSError:
                pass

    def _publish(self, videos: list[Video], title: str, generation: int) -> None:
        if generation != self.feed_generation:
            return
        with self.feed_lock:
            self.feed = videos
        self.log(f"numtok: {title}, {len(videos)} videos")
        self.send(T_FEED_READY, struct.pack("<H", len(videos)) + self.text(title, 40))
        for video in videos[:CACHE_AHEAD]:
            self._prefetch(video)

    def _build_feed(self, query: str, generation: int) -> None:
        if query.startswith("#"):
            tag = query[1:].strip().replace(" ", "")
            self.send(T_STATUS, self.text(f"Recherche de #{tag}...", 60))
            try:
                entries = self._list(f"https://www.tiktok.com/tag/{tag}", 20)
            except Exception:
                entries = []
            if not entries:
                raise RuntimeError("TikTok bloque les hashtags. Tape un pseudo.")
            self._publish([Video(e) for e in entries], f"#{tag}", generation)
            return
        if query:
            for name in name_variants(query):
                self.send(T_STATUS, self.text(f"Recherche de @{name}...", 60))
                self.log(f"numtok: recherche de @{name}")
                try:
                    entries = self._list(f"https://www.tiktok.com/@{name}", 24)
                except Exception as error:
                    self.log(f"  @{name}: {error}")
                    continue
                if entries:
                    self._publish([Video(e) for e in entries], f"@{name}", generation)
                    return
            raise RuntimeError(f"Personne ne s'appelle @{name_variants(query)[0]}")
        self._for_you(generation)

    def _for_you(self, generation: int) -> None:
        """A mix of popular creators. The feed is published as soon as a few
        creators answered; the others are added to the end as they come."""
        names = creators()
        random.shuffle(names)
        names = names[:8]
        self.send(T_STATUS, b"Pour toi : chargement...")
        self.log(f"numtok: pour toi ({', '.join(names)})")
        pool = ThreadPoolExecutor(max_workers=4)
        futures = [pool.submit(self._list, f"https://www.tiktok.com/@{n}", FEED_PER_CREATOR) for n in names]
        pool.shutdown(wait=False)
        lists: list[list[dict]] = []
        published = False
        deadline = time.monotonic() + 25
        pending = list(futures)
        while pending and generation == self.feed_generation:
            done = [f for f in pending if f.done()]
            for future in done:
                pending.remove(future)
                try:
                    entries = future.result()
                except Exception as error:
                    self.log(f"  createur ignore: {error}")
                    continue
                random.shuffle(entries)
                if not published:
                    lists.append(entries)
                else:
                    with self.feed_lock:
                        self.feed.extend(Video(e) for e in entries)
            enough = len(lists) >= 3 or (lists and time.monotonic() > deadline)
            if not published and (enough or not pending) and lists:
                mixed: list[Video] = []
                for row in range(max(len(entries) for entries in lists)):
                    for entries in lists:
                        if row < len(entries):
                            mixed.append(Video(entries[row]))
                self._publish(mixed, "Pour toi", generation)
                published = True
            if not done:
                time.sleep(0.1)
        if not published and generation == self.feed_generation:
            raise RuntimeError("TikTok ne repond pas. Reessaie.")

    # -- downloads
    def _prefetch(self, video: Video) -> None:
        if not video.started:
            video.started = True
            self.downloads.submit(self._download, video)

    def _download(self, video: Video) -> None:
        path = os.path.join(self.workdir, f"{video.id}.mp4")
        cmd = [*self.gw.YTDLP, "--no-warnings", "--no-playlist", "-q", "-j", "--no-simulate",
               "-f", "b[vcodec^=h264]/b[vcodec!*=av01]/b", "-S", "res:540,+size",
               "--force-overwrites", "-o", path, video.url]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120, **self.gw.POPEN_FLAGS)
            if result.returncode != 0 or not os.path.exists(path):
                lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
                raise RuntimeError(lines[-1] if lines else "telechargement impossible")
            try:
                video.update(json.loads(result.stdout.splitlines()[-1]))
            except (ValueError, IndexError):
                pass
            video.path = path
        except Exception as error:
            video.error = str(error)
            self.log(f"numtok: video {video.id} illisible: {error}")
        finally:
            video.ready.set()

    def _trim_cache(self, index: int) -> None:
        with self.feed_lock:
            feed = list(self.feed)
        for i, video in enumerate(feed):
            if video.path and not (index - 2 <= i <= index + CACHE_KEEP - 2):
                try:
                    os.remove(video.path)
                except OSError:
                    pass
                video.path = None
                video.started = False
                video.ready.clear()

    # -- playback
    def play(self, index: int) -> None:
        self.stop()
        with self.feed_lock:
            if not 0 <= index < len(self.feed):
                return
            video = self.feed[index]
            ahead = self.feed[index + 1:index + 1 + CACHE_AHEAD]
        self._trim_cache(index)
        self._prefetch(video)
        for following in ahead:
            self._prefetch(following)
        self.player = TokPlayer(self, index, video)
        self.player.start()

    def info_payload(self, index: int, video: Video) -> bytes:
        with self.feed_lock:
            count = len(self.feed)
        payload = struct.pack("<HHIIIII", index, min(count, 0xFFFF), video.duration_ms, video.likes,
                              video.comments, video.shares, video.views)
        for value, limit in ((video.author, 40), (video.caption, 180), (video.sound, 60)):
            data = self.text(value, limit)
            payload += bytes([len(data)]) + data
        return payload


def _pause_overlay(picture: np.ndarray) -> np.ndarray:
    """Darkens the video and draws the two bars of a pause sign on it."""
    out = picture.copy()
    region = out[:, VIDEO_X:VIDEO_X + VIDEO_W]
    region[:] = (region.astype(np.uint16) * 2 // 5).astype(np.uint8)
    cx, cy = VIDEO_X + VIDEO_W // 2, SCREEN_H // 2
    for x in (cx - 12, cx + 4):
        out[cy - 16:cy + 16, x:x + 8] = 255
    return out


class TokPlayer(threading.Thread):
    """Plays one video of the feed in a loop, until stopped."""

    def __init__(self, service: TokService, index: int, video: Video) -> None:
        super().__init__(daemon=True)
        self.service = service
        self.gw = service.gw
        self.index = index
        self.video = video
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.paused = False
        self.encoder = self.gw.TileEncoder(ROWS)
        self.frame_counter = 0
        self.pending: dict[int, tuple[int, float]] = {}
        self.rate = 300_000.0
        self.process: subprocess.Popen | None = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.kill()

    def set_paused(self, paused: bool) -> None:
        with self.lock:
            self.paused = paused

    def on_ack(self, frame_id: int) -> None:
        with self.lock:
            if frame_id not in self.pending:
                return
            size, started = self.pending.pop(frame_id)
            for stale in [f for f in self.pending if ((frame_id - f) & 0xFFFF) < 0x8000]:
                self.pending.pop(stale)
            elapsed = time.monotonic() - started
            if size >= 6_000 and elapsed > 0:
                measured = min(max(size / elapsed, 30_000), 1_500_000)
                self.rate = 0.7 * self.rate + 0.3 * measured

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        if not self.stop_event.is_set():
            self.service.send(frame_type, payload)

    def _send_picture(self, picture: np.ndarray, budget: int | None = None) -> None:
        if budget is None:
            budget = max(1_500, min(int(self.rate / FPS), 40_000))
        chunks = self.encoder.encode(picture, budget) or [(0, b"")]
        self.frame_counter = (self.frame_counter + 1) & 0xFFFF
        frame_id = self.frame_counter
        payloads = [
            struct.pack("<HHHB", self.index, frame_id, start,
                        FLAG_END_OF_FRAME if i == len(chunks) - 1 else 0) + ops
            for i, (start, ops) in enumerate(chunks)
        ]
        with self.lock:
            self.pending[frame_id] = (sum(len(p) + 10 for p in payloads), time.monotonic())
        for payload in payloads:
            self.send(T_VIDEO, payload)

    def _in_flight(self) -> int:
        with self.lock:
            return len(self.pending)

    def run(self) -> None:
        try:
            self._run()
        except OSError:
            pass  # the calculator went away; the session handles it
        except Exception as error:
            self.service.log(f"numtok: erreur de lecture: {error}")
            try:
                self.send(T_ERROR, self.service.text(str(error), 100))
            except OSError:
                pass

    def _run(self) -> None:
        video = self.video
        if not video.ready.is_set():
            self.send(T_STATUS, b"Chargement...")
            waited = 0.0
            while not video.ready.wait(0.25):
                if self.stop_event.is_set():
                    return
                waited += 0.25
                if waited >= 6 and waited % 3 == 0:
                    self.send(T_STATUS, f"Chargement... {int(waited)} s".encode())
        if self.stop_event.is_set():
            return
        self.send(T_INFO, self.service.info_payload(self.index, video))
        if video.error or not video.path:
            self.send(T_ERROR, b"Video illisible\nbas : la suivante")
            return
        self.service.log(f"numtok: @{video.author} ({video.duration_ms // 1000} s)")

        canvas = np.zeros((SCREEN_H, SCREEN_W, 3), dtype=np.uint8)
        frame_bytes = VIDEO_W * SCREEN_H * 3
        period = 1.0 / FPS
        first = True
        while not self.stop_event.is_set():  # TikTok videos loop
            vf = (f"scale={VIDEO_W}:{SCREEN_H}:force_original_aspect_ratio=decrease:flags=area,"
                  f"pad={VIDEO_W}:{SCREEN_H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=rgb24")
            self.process = subprocess.Popen(
                [self.gw.FFMPEG, "-nostdin", "-loglevel", "error", "-i", video.path, "-an", "-sn",
                 "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=frame_bytes * 4, **self.gw.POPEN_FLAGS)
            assert self.process.stdout is not None
            origin: float | None = None
            frame_index = 0
            was_paused = False
            while not self.stop_event.is_set():
                with self.lock:
                    paused = self.paused
                if paused:
                    if not was_paused:
                        self._send_picture(_pause_overlay(canvas), 40_000)
                        was_paused = True
                    time.sleep(0.03)
                    if origin is not None:
                        origin += 0.03
                    continue
                if was_paused:
                    was_paused = False
                    with self.lock:
                        self.pending.clear()  # whatever was not acked before the pause is gone
                    self.encoder.invalidate()  # redraw the whole picture without the overlay
                raw = self.process.stdout.read(frame_bytes)
                if not raw or len(raw) < frame_bytes:
                    break
                now = time.monotonic()
                if origin is None:
                    origin = now
                    if first:
                        self.send(T_STATUS, b"")
                        first = False
                due = origin + frame_index * period
                pts_ms = int(frame_index * 1000 / FPS)
                frame_index += 1
                if now - due > 1.5:
                    origin = now - (frame_index - 1) * period
                elif due > now:
                    self.stop_event.wait(due - now)
                if self._in_flight() >= 2:
                    continue
                canvas[:, VIDEO_X:VIDEO_X + VIDEO_W] = np.frombuffer(raw, dtype=np.uint8).reshape(
                    SCREEN_H, VIDEO_W, 3)
                # TikTok's thin progress line at the bottom of the video.
                if video.duration_ms > 0:
                    filled = min(VIDEO_W, VIDEO_W * pts_ms // video.duration_ms)
                    canvas[SCREEN_H - 2:, VIDEO_X:VIDEO_X + VIDEO_W] = 70
                    canvas[SCREEN_H - 2:, VIDEO_X:VIDEO_X + filled] = 255
                self._send_picture(canvas)
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            if frame_index == 0 and not self.stop_event.is_set():
                self.send(T_ERROR, b"Video illisible\nbas : la suivante")
                return
