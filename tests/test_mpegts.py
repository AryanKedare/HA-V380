"""Independent FFmpeg/PyAV decode of the live video MPEG-TS packetizer."""
from __future__ import annotations
from fractions import Fraction
import importlib.util
import io
from pathlib import Path
import unittest

import av

p=Path(__file__).resolve().parents[1]/"custom_components"/"v380_cloud"/"mpegts.py"
spec=importlib.util.spec_from_file_location("v380_mpegts_test",p)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class VideoPlaybackTests(unittest.TestCase):
    def test_h264_stream_decodes_and_has_monotonic_clock(self):
        encoder=av.CodecContext.create("libx264","w")
        encoder.width=64;encoder.height=64;encoder.pix_fmt="yuv420p"
        encoder.time_base=Fraction(1,25);encoder.framerate=Fraction(25,1)
        encoder.options={"preset":"ultrafast","tune":"zerolatency"};encoder.open()
        mux=module.MpegTsMuxer("h264");output=bytearray()
        for index in range(15):
            frame=av.VideoFrame(64,64,"yuv420p");frame.pts=index;frame.time_base=Fraction(1,25)
            for plane in frame.planes:plane.update(bytes([index*10])*plane.buffer_size)
            for packet in encoder.encode(frame):
                output.extend(mux.push(bytes(packet),index/25,keyframe=packet.is_keyframe))
        for packet in encoder.encode(None):output.extend(mux.push(bytes(packet)))
        self.assertEqual(len(output)%188,0)
        self.assertTrue(all(output[pos]==0x47 for pos in range(0,len(output),188)))
        with av.open(io.BytesIO(output)) as video:
            self.assertEqual(video.streams.video[0].codec_context.name,"h264")
            decoded=list(video.decode(video=0))
        self.assertEqual(len(decoded),15)
        self.assertTrue(all(a.pts<b.pts for a,b in zip(decoded,decoded[1:])))
        self.assertEqual(decoded[-1].pts-decoded[0].pts,50400)

    def test_invalid_framing_rejected(self):
        with self.assertRaises(ValueError):module.MpegTsMuxer("mjpeg")
        with self.assertRaises(ValueError):module.MpegTsMuxer("h264").push(b"arbitrary data")
        self.assertEqual(module._crc32_mpeg(b"123456789"),bytes.fromhex("0376e6e7"))
