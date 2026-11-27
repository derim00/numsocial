"""End-to-end test of NumTok: a fake calculator talks to the real gateway over
a pty, the way the firmware does, and watches real TikTok videos.

Checks: a creator feed and the "Pour toi" feed arrive, a video plays (info,
pictures paced by the acks, only the middle column is drawn), swiping to the
next video is fast (it was downloaded in advance), pause sends a single
picture, and Stop ends the stream.

Usage: ../.venv/bin/python3 fake_numtok.py [pseudo] [--save picture.png]
Needs Internet (TikTok).
"""
import os
import struct
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
import numtok_service as tok  # noqa: E402
from fake_numchat import FakeCalculator  # noqa: E402
import numscreen_service as ns  # noqa: E402
from fake_numscreen import decode  # noqa: E402  (same decoder, 80 tiles wide)

ns.ROWS = tok.ROWS  # that decoder reads its row count from there: 60 rows of tiles here


def parse_info(p: bytes):
    index, count, duration, likes, comments, shares, views = struct.unpack_from("<HHIIIII", p)
    cursor = 24
    strings = []
    for _ in range(3):
        size = p[cursor]
        strings.append(p[cursor + 1:cursor + 1 + size].decode())
        cursor += 1 + size
    return dict(index=index, count=count, duration=duration, likes=likes, comments=comments,
                shares=shares, views=views, author=strings[0], caption=strings[1], sound=strings[2])


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None
    if save in args:
        args.remove(save)
    query = args[0] if args else "khaby lame"
    calc = FakeCalculator()
    gateway = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"), "--port", calc.slave_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    picture = np.zeros((tok.SCREEN_H, tok.SCREEN_W), dtype=np.uint16)
    picture[:] = 0xFFFF  # white: whatever the decoder never touches stays white
    try:
        calc.send(gw.T_HELLO)
        calc.wait_for(gw.T_HELLO_ACK, 10)

        def open_feed(text: str):
            calc.send(tok.T_FEED, text.encode())
            started = time.monotonic()
            for frame_type, payload in calc.frames(90):
                if frame_type == tok.T_STATUS:
                    print("  statut:", payload.decode())
                elif frame_type == tok.T_FEED_READY:
                    count = struct.unpack_from("<H", payload)[0]
                    print(f"fil {payload[2:].decode()!r}: {count} videos en {time.monotonic() - started:.1f} s")
                    return count
            raise AssertionError("pas de fil")

        def watch(index: int, seconds: float, ack: bool = True):
            calc.send(tok.T_PLAY, struct.pack("<H", index))
            asked = time.monotonic()
            info, first, frames, errors = None, None, 0, []
            for frame_type, payload in calc.frames(seconds):
                if frame_type == tok.T_INFO:
                    info = parse_info(payload)
                elif frame_type == tok.T_VIDEO:
                    video_index, frame_id, start, flags = struct.unpack_from("<HHHB", payload)
                    if video_index != index:
                        continue  # still on the wire from the previous video: the firmware skips these too
                    decode(picture, start, payload[7:])
                    if flags & tok.FLAG_END_OF_FRAME:
                        frames += 1
                        first = first or time.monotonic() - asked
                        if ack:
                            calc.send(tok.T_ACK, payload[2:4])
                elif frame_type == tok.T_ERROR:
                    errors.append(payload.decode())
            return info, first, frames, errors

        count = open_feed(query)
        assert count > 2, "fil trop court"
        info, first, frames, errors = watch(0, 6)
        assert not errors, errors
        assert info and info["author"], info
        print(f"video 0: @{info['author']} {info['duration'] / 1000:.0f} s, {tok.format_count(info['likes'])} j'aime, "
              f"{tok.format_count(info['comments'])} commentaires, {tok.format_count(info['shares'])} partages")
        print(f"  legende: {info['caption'][:70]!r}  son: {info['sound']!r}")
        print(f"  premiere image apres {first:.1f} s, {frames} images en 6 s")
        assert frames >= 30, "flux trop lent"
        sides = np.concatenate([picture[:, :tok.VIDEO_X].ravel(), picture[:, tok.VIDEO_X + tok.VIDEO_W:].ravel()])
        middle = picture[:, tok.VIDEO_X:tok.VIDEO_X + tok.VIDEO_W]
        assert (sides == 0).all(), "les cotes doivent rester noirs (le firmware n'y dessine pas la video)"
        assert middle.std() > 0, "video vide"
        if save:
            from PIL import Image
            rgb = np.stack([(picture >> 11) << 3, ((picture >> 5) & 63) << 2, (picture & 31) << 3], axis=-1)
            Image.fromarray(rgb.astype(np.uint8)).save(save)
            print("image enregistree :", save)

        # Swipe: the next video was downloaded in advance.
        info, first, frames, errors = watch(1, 4)
        assert info and info["index"] == 1 and not errors, (info, errors)
        print(f"video 1 (@{info['author']}): premiere image apres {first:.1f} s")
        assert first < 2.5, "la video suivante aurait du etre prete"

        # Pause: one darkened picture, then nothing.
        calc.send(tok.T_CONTROL, bytes([tok.CTRL_PAUSE]))
        paused = 0
        for frame_type, payload in calc.frames(1.5):
            if frame_type == tok.T_VIDEO and payload[6] & tok.FLAG_END_OF_FRAME:
                calc.send(tok.T_ACK, payload[2:4])
                paused += 1
        print(f"images pendant la pause: {paused}")
        assert paused <= 3
        calc.send(tok.T_CONTROL, bytes([tok.CTRL_RESUME]))
        resumed = 0
        for frame_type, payload in calc.frames(1.5):
            if frame_type == tok.T_VIDEO and payload[6] & tok.FLAG_END_OF_FRAME:
                calc.send(tok.T_ACK, payload[2:4])
                resumed += 1
        print(f"images apres la reprise: {resumed}")
        assert resumed >= 8

        calc.send(tok.T_CONTROL, bytes([tok.CTRL_STOP]))
        time.sleep(0.3)
        leftovers = [f for f in calc.frames(1.0) if f[0] == tok.T_VIDEO]
        print(f"apres STOP: {len(leftovers)} image(s)")
        assert len(leftovers) <= 2

        count = open_feed("")
        assert count >= 5, "Pour toi trop court"
        info, first, frames, errors = watch(0, 8)
        assert info and frames > 10 and not errors, (info, frames, errors)
        print(f"Pour toi, video 0: @{info['author']}, premiere image apres {first:.1f} s")
        calc.send(tok.T_CONTROL, bytes([tok.CTRL_STOP]))
    finally:
        gateway.terminate()
        out, _ = gateway.communicate(timeout=5)
        print("--- passerelle ---")
        print("\n".join(line for line in out.splitlines() if "numtok" in line or "erreur" in line.lower()))
    print("OK : NumTok fonctionne avec la fausse calculatrice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
