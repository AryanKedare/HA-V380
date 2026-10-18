"""Minimal Annex-B H.264/H.265 to MPEG-TS packetizer (video only).

No transcoding or decoder dependency. Each push is one camera access unit,
with presentation time in seconds. Browser codec support is handled by HA.
"""
from __future__ import annotations

VIDEO_PID = 0x100
PMT_PID = 0x1000


def _crc32_mpeg(data: bytes) -> bytes:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc.to_bytes(4, "big")


def _pts(value: int) -> bytes:
    value &= (1 << 33) - 1
    return bytes((
        0x21 | (((value >> 30) & 7) << 1),
        (value >> 22) & 255,
        (((value >> 15) & 127) << 1) | 1,
        (value >> 7) & 255,
        ((value & 127) << 1) | 1,
    ))


class MpegTsMuxer:
    """Create PAT/PMT and PES packets with independent continuity counters."""

    def __init__(self, codec: str) -> None:
        if codec not in ("h264", "h265", "hevc"):
            raise ValueError("Unsupported compressed video codec")
        self.codec = codec
        self._counters: dict[int, int] = {}
        self._frames = 0
        self._last_pts = -1
        self._origin: float | None = None

    def _packets(self, pid: int, payload: bytes, *, pcr: int | None = None) -> bytes:
        output = bytearray()
        position = 0
        first = True
        while position < len(payload):
            cc = self._counters.get(pid, 0)
            self._counters[pid] = (cc + 1) % 16
            header = bytes((0x47, (0x40 if first else 0) | (pid >> 8), pid & 255))
            # An adaptation field carrying PCR needs 8 bytes, leaving176.
            max_payload = 176 if first and pcr is not None else 184
            part = payload[position:position + max_payload]
            spare = 184 - len(part)
            if spare:
                adaptation = bytearray((spare - 1,))
                if spare > 1:
                    has_pcr = first and pcr is not None
                    adaptation.append(0x10 if has_pcr else 0)
                    if has_pcr:
                        base = pcr & ((1 << 33) - 1)
                        adaptation.extend(bytes((
                            (base >> 25) & 255, (base >> 17) & 255,
                            (base >> 9) & 255, (base >> 1) & 255,
                            ((base & 1) << 7) | 0x7E, 0,
                        )))
                    adaptation.extend(b"\xff" * (spare - len(adaptation)))
                output.extend(header + bytes((0x30 | cc,)) + adaptation + part)
            else:
                output.extend(header + bytes((0x10 | cc,)) + part)
            first = False
            position += len(part)
        return bytes(output)

    def tables(self) -> bytes:
        pat = bytes.fromhex("00b00d0001c100000001f000")
        pat += _crc32_mpeg(pat)
        stream_type = 0x1B if self.codec == "h264" else 0x24
        pmt = bytes.fromhex("02b0120001c10000e100f000") + bytes((stream_type, 0xE1, 0, 0xF0, 0))
        pmt += _crc32_mpeg(pmt)
        return self._packets(0, b"\x00" + pat) + self._packets(PMT_PID, b"\x00" + pmt)

    def push(self, data: bytes, timestamp: float | None = None, *, keyframe: bool = False) -> bytes:
        if not data:
            return b""
        if not data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            raise ValueError("Video payload is not an Annex-B access unit")
        if timestamp is None:
            pts = max(0, self._last_pts + 3600)  # fallback25fps until clock known
        else:
            if self._origin is None:
                self._origin = timestamp
            pts = max(self._last_pts + 1, int((timestamp - self._origin) * 90000))
        self._last_pts = pts
        tables = self.tables() if self._frames % 50 == 0 or keyframe else b""
        self._frames += 1
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts(pts) + data
        return tables + self._packets(VIDEO_PID, pes, pcr=pts)
