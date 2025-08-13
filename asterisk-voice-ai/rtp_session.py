#!/usr/bin/env python3
"""
Minimal but correct *bidirectional* RTP session for Asterisk External Media.

- Listens for inbound RTP from Asterisk (learns payload type, confirms path)
- Sends outbound RTP (e.g., Azure TTS PCM @ 16 kHz) with stable SSRC/Seq/Timestamp
- Paces packets at ~20 ms, optionally locking to inbound timing
- Optional RTCP keepalive (very small, minimalistic — can be disabled)

Use it for the **TTS ExternalMedia channel**: you must both RECEIVE (from
Asterisk) and SEND (to Asterisk). Even if you ignore the inbound audio, you
should read it so that Strict RTP and NAT keepalives behave.

Typical wiring for ARI ExternalMedia (TTS side)
----------------------------------------------
- Asterisk ExternalMedia created with external_host=AI_TTS_HOST:AI_TTS_PORT
  → Asterisk will SEND RTP to (AI_TTS_HOST, AI_TTS_PORT)
- You must SEND RTP back to Asterisk's local RTP port of that channel
  → Query via ARI: UNICASTRTP_LOCAL_ADDRESS / UNICASTRTP_LOCAL_PORT

This module assumes you already know:
- local_host/local_port (bind here to receive from Asterisk)
- remote_host/remote_port (send here to deliver audio into the bridge)

PCM format assumed: 16-bit little endian, mono, 16 kHz (slin16)
Timestamp step per 20 ms: 16000 * 0.02 = 320 samples

NOTE: This is intentionally compact and dependency-free. It is not a full RTP/RTCP stack,
but it covers what ExternalMedia needs for high stability.
"""

import asyncio
import os
import random
import socket
import struct
import time
from typing import Optional, Iterable

RTP_VERSION = 2
DEFAULT_PT = int(os.getenv("AI_PT", "118"))  # PT 118 is common for slin16 in Asterisk
SAMPLES_PER_PACKET_20MS = 320  # 16 kHz * 20 ms
BYTES_PER_PACKET_20MS = SAMPLES_PER_PACKET_20MS * 2  # 16-bit


class RtpSession:
    def __init__(
        self,
        *,
        local_host: str,
        local_port: int,
        remote_host: str,
        remote_port: int,
        clock_rate: int = 16000,
        payload_type: Optional[int] = None,
        pace_by_inbound: bool = True,
        send_rtcp: bool = False,
    ):
        self.local_host = local_host
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.clock_rate = clock_rate
        self.pt_out = payload_type if payload_type is not None else DEFAULT_PT
        self.pt_in_learned: Optional[int] = None

        self.ssrc = random.randint(1, 0xFFFFFFFF)
        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)

        self.transport = None
        self.sock: Optional[socket.socket] = None
        self.loop = asyncio.get_event_loop()

        self.pace_by_inbound = pace_by_inbound
        self.last_inbound_at: Optional[float] = None
        self.last_out_at: Optional[float] = None
        self.send_rtcp = send_rtcp
        self._rtcp_task: Optional[asyncio.Task] = None

    async def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # Increase buffer sizes for stability
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1<<20)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1<<20)
        # FIX: Add socket reuse option to prevent "Address already in use" errors
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.local_host, self.local_port))
        # DON'T use connect() - we need to use sendto() with explicit address
        # self.sock.connect((self.remote_host, self.remote_port))
        self.loop.create_task(self._rx_loop())
        if self.send_rtcp:
            self._rtcp_task = self.loop.create_task(self._rtcp_loop())
        # Wait for PT learning with fallback
        await self.wait_pt_or_fallback(timeout=0.5, fallback_pt=97)

    async def wait_pt_or_fallback(self, timeout=0.5, fallback_pt=97):
        """Wait for PT learning with fallback to slin16 default"""
        import logging
        log = logging.getLogger("rtp-session")
        t0 = time.monotonic()
        while self.pt_in_learned is None and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.01)
        if self.pt_in_learned is None:
            # no inbound seen -> force slin16 payload type commonly used by Asterisk
            self.pt_out = fallback_pt
            log.warning(f"⚠️ No inbound RTP after {timeout}s, using fallback PT={fallback_pt}")
        else:
            log.info(f"✅ PT learned from Asterisk: {self.pt_in_learned}")
    
    @property
    def is_open(self) -> bool:
        return getattr(self, "sock", None) is not None
    
    async def close(self):
        if self._rtcp_task:
            self._rtcp_task.cancel()
        if self.sock:
            self.sock.close()
            self.sock = None

    async def _rx_loop(self):
        # Receive inbound RTP to: (a) learn PT, (b) keep Strict RTP/NAT happy, (c) optionally pace
        assert self.sock is not None
        while True:
            try:
                data, addr = await self.loop.sock_recvfrom(self.sock, 2048)
            except (asyncio.CancelledError, OSError):
                break
            now = time.monotonic()
            self.last_inbound_at = now
            if len(data) < 12:
                continue
            v_p_x_cc, m_pt, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
            version = v_p_x_cc >> 6
            if version != RTP_VERSION:
                continue
            pt = m_pt & 0x7F
            if self.pt_in_learned is None:
                self.pt_in_learned = pt
                import logging
                log = logging.getLogger("rtp-session")
                log.info(f"👂 First inbound RTP from {addr[0]}:{addr[1]} PT={pt}")
                # Mirror PT for outbound unless user forced it
                if self.pt_out == DEFAULT_PT:
                    self.pt_out = pt

    async def _rtcp_loop(self):
        # Extremely small RTCP RR every ~5s. This is optional; Asterisk does not require it,
        # but some middleboxes like seeing RTCP.
        # We send a minimal Receiver Report (no report blocks) to remote_port+1.
        assert self.sock is not None
        rtcp_addr = (self.remote_host, self.remote_port + 1)
        while True:
            try:
                await asyncio.sleep(5.0)
                # RTCP Header: V=2,P=0,PT=RR(201), length=2 (3 words - 1), SSRC
                pkt = struct.pack("!BBH I", (RTP_VERSION << 6), 201, 2, self.ssrc)
                try:
                    self.sock.sendto(pkt, rtcp_addr)
                except OSError:
                    pass
            except asyncio.CancelledError:
                break

    def _build_rtp_header(self, marker: int = 0) -> bytes:
        v_p_x_cc = (RTP_VERSION << 6) | 0
        m_pt = ((1 if marker else 0) << 7) | (self.pt_out & 0x7F)
        hdr = struct.pack("!BBHII", v_p_x_cc, m_pt, self.seq, self.ts, self.ssrc)
        return hdr

    async def send_audio(self, audio_iter: Iterable[bytes], frame_bytes: int = 640):
        """Safe sender: breaks out gracefully if socket closes mid-stream.
        - frame_bytes: chunk size (640 for PCM16 @16kHz)
        """
        if self.sock is None:
            # socket not started or already closed -> just return
            import logging
            logging.getLogger("rtp-session").warning("⚠️ send_audio called but socket not open")
            return
        
        # Debug log for troubleshooting
        import logging
        log = logging.getLogger("rtp-session")
        log.info(f"📡 RTP send starting → {self.remote_host}:{self.remote_port} PT={self.pt_out} open={self.sock is not None}")
        
        # CRITICAL FIX: Force immediate sending without complex iteration
        packet_count = 0
        
        marker_next = 1  # set marker for start of talkspurt
        next_due = time.monotonic()
        for chunk in audio_iter:
            # bail out if closed during a long TTS
            if self.sock is None:
                break
            if len(chunk) != frame_bytes:
                # pad or trim to exact 20 ms
                if len(chunk) < frame_bytes:
                    chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
                else:
                    chunk = chunk[:frame_bytes]
            # pacing
            if self.pace_by_inbound and self.last_inbound_at is not None:
                # try to keep ~20ms cadence from inbound
                target = self.last_out_at + 0.02 if self.last_out_at else self.last_inbound_at + 0.02
                now = time.monotonic()
                sleep = target - now
                if sleep > 0:
                    await asyncio.sleep(sleep)
                self.last_out_at = time.monotonic()
            else:
                now = time.monotonic()
                if now < next_due:
                    await asyncio.sleep(next_due - now)
                next_due = time.monotonic() + 0.02

            # send
            hdr = self._build_rtp_header(marker=marker_next)
            try:
                # Use sendto() with explicit address since we don't connect()
                bytes_sent = self.sock.sendto(hdr + chunk, (self.remote_host, self.remote_port))
                packet_count += 1
                if packet_count <= 3:  # Log first 3 packets for debugging
                    log.info(f"📤 RTP packet {packet_count} sent: {bytes_sent} bytes to {self.remote_host}:{self.remote_port}")
            except Exception as e:
                # Socket closed or error - bail out gracefully
                log.error(f"❌ RTP send failed: {e}")
                break
            # advance RTP (samples per 20ms depends on clock_rate)
            self.seq = (self.seq + 1) & 0xFFFF
            samples_per_chunk = int(self.clock_rate * 0.02)  # 20ms worth of samples
            self.ts = (self.ts + samples_per_chunk) & 0xFFFFFFFF
            marker_next = 0

    async def learn_inbound_pt(self, timeout: float = 1.0) -> Optional[int]:
        """Learn the payload type from incoming RTP packets"""
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if self.pt_in_learned is not None:
                return self.pt_in_learned
            await asyncio.sleep(0.01)
        return self.pt_in_learned
    
    def set_outgoing_pt(self, pt: int):
        """Set the outgoing payload type"""
        self.pt_out = pt
    
    @staticmethod
    def chunker(audio_bytes: bytes, frame_bytes: int = BYTES_PER_PACKET_20MS):
        for i in range(0, len(audio_bytes), frame_bytes):
            yield audio_bytes[i : i + frame_bytes]


async def demo():
    """Example usage.
    Suppose ARI told you:
      - Asterisk will send to us at (local_host=0.0.0.0, local_port=7000)
      - We must send back to (remote_host=10.0.0.10, remote_port=16350) — UNICASTRTP_LOCAL_* of EM_TTS
    """
    local_host, local_port = "0.0.0.0", int(os.getenv("LOCAL_PORT", "7000"))
    remote_host = os.getenv("REMOTE_HOST", "10.0.0.10")
    remote_port = int(os.getenv("REMOTE_PORT", "16350"))

    sess = RtpSession(
        local_host=local_host,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
        payload_type=None,         # will mirror inbound PT
        pace_by_inbound=True,
        send_rtcp=False,
    )
    await sess.start()

    # Wait until we learned inbound PT or timeout (so Strict RTP has seen traffic)
    t0 = time.time()
    while sess.pt_in_learned is None and time.time() - t0 < 1.0:
        await asyncio.sleep(0.01)
    print(f"Inbound PT learned: {sess.pt_in_learned}, outbound PT: {sess.pt_out}")

    # Load a WAV/PCM sample and send it (for real TTS, stream PCM as you generate it)
    pcm_path = os.getenv("PCM_PATH", "sample_hello_16k_slin16.pcm")
    if os.path.exists(pcm_path):
        with open(pcm_path, "rb") as f:
            pcm = f.read()
        await sess.send_audio(RtpSession.chunker(pcm))
    else:
        print("No PCM file found; sending 1s of silence as a smoke test…")
        silence = b"\x00" * (BYTES_PER_PACKET_20MS * 50)
        await sess.send_audio(RtpSession.chunker(silence))

    await asyncio.sleep(0.2)
    await sess.close()


if __name__ == "__main__":
    try:
        asyncio.run(demo())
    except KeyboardInterrupt:
        pass