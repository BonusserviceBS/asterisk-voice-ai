#!/usr/bin/env python3
"""
RTP Media Agent with Azure STT/TTS Integration
Full-featured voice assistant with low latency
"""
import socket
import struct
import time
import logging
import threading
from queue import Queue
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Use newer pyaudio methods instead of deprecated audioop
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RTP_PT = 0  # PCMU
SR = 8000
FRAME = 0.02  # 20 ms
SMP = int(SR * FRAME)  # 160 samples/20ms

def ulaw2linear(ulaw_bytes):
    """Convert µ-law to 16-bit linear PCM"""
    ULAW_TABLE = [
        -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
        -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
        -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
        -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
        -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
        -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
        -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
        -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
        -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
        -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
        -876, -844, -812, -780, -748, -716, -684, -652,
        -620, -588, -556, -524, -492, -460, -428, -396,
        -372, -356, -340, -324, -308, -292, -276, -260,
        -244, -228, -212, -196, -180, -164, -148, -132,
        -120, -112, -104, -96, -88, -80, -72, -64,
        -56, -48, -40, -32, -24, -16, -8, 0,
        32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
        23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
        15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
        11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
        7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
        5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
        3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
        2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
        1884, 1820, 1756, 1692, 1628, 1564, 1500, 1436,
        1372, 1308, 1244, 1180, 1116, 1052, 988, 924,
        876, 844, 812, 780, 748, 716, 684, 652,
        620, 588, 556, 524, 492, 460, 428, 396,
        372, 356, 340, 324, 308, 292, 276, 260,
        244, 228, 212, 196, 180, 164, 148, 132,
        120, 112, 104, 96, 88, 80, 72, 64,
        56, 48, 40, 32, 24, 16, 8, 0
    ]
    
    samples = []
    for byte in ulaw_bytes:
        samples.append(ULAW_TABLE[byte])
    
    # Convert to bytes
    return struct.pack(f'{len(samples)}h', *samples)

def linear2ulaw(pcm_bytes):
    """Convert 16-bit linear PCM to µ-law"""
    # Unpack PCM samples
    samples = struct.unpack(f'{len(pcm_bytes)//2}h', pcm_bytes)
    
    ulaw_bytes = bytearray()
    for sample in samples:
        # Simplified µ-law encoding
        if sample < 0:
            sign = 0x80
            sample = -sample
        else:
            sign = 0
        
        if sample > 32635:
            sample = 32635
        
        sample = sample + 0x84
        exp = 7
        
        for exp in range(7, -1, -1):
            if sample & (0x4000 >> exp):
                break
        
        mantissa = (sample >> (exp + 3)) & 0x0F
        ulaw = ~(sign | (exp << 4) | mantissa) & 0xFF
        ulaw_bytes.append(ulaw)
    
    return bytes(ulaw_bytes)


class RTPAgent:
    def __init__(self, bind_host="0.0.0.0", bind_port=4000):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.sock = None
        self.peer = None
        self.seq = 1
        self.ts = 0
        self.ssrc = 0x12345678
        self.running = False
        
        # Audio queues
        self.incoming_audio = Queue()
        self.outgoing_audio = Queue()
        
        # Voice AI
        self.voice_ai = None
        self.init_voice_ai()
        
        # Barge-in control
        self.is_bot_speaking = False
        self.user_speaking = False
        
    def init_voice_ai(self):
        """Initialize Voice AI if Azure key is available"""
        azure_key = os.getenv("AZURE_SPEECH_KEY")
        if azure_key:
            try:
                from azure_integration import VoiceAI
                self.voice_ai = VoiceAI()
                self.voice_ai.start()
                logger.info("Azure Voice AI initialized")
            except Exception as e:
                logger.error(f"Failed to init Voice AI: {e}")
                self.voice_ai = None
        else:
            logger.warning("No Azure key, using echo mode")
    
    def rtp_header(self, seq, ts, pt=RTP_PT, marker=0):
        b0 = 0x80
        b1 = (marker << 7) | (pt & 0x7F)
        return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, self.ssrc)
    
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.bind_host, self.bind_port))
        self.sock.settimeout(0.1)
        logger.info(f"RTP listening on {self.bind_host}:{self.bind_port} (PCMU)")
        
        self.running = True
        
        # Start threads
        recv_thread = threading.Thread(target=self.receive_loop)
        recv_thread.start()
        
        send_thread = threading.Thread(target=self.send_loop)
        send_thread.start()
        
        # Start audio processing
        process_thread = threading.Thread(target=self.process_audio)
        process_thread.start()
        
        # Keep main thread alive
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
    
    def receive_loop(self):
        """Receive RTP packets"""
        silence_count = 0
        
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
                
                # Remember peer
                if self.peer is None:
                    self.peer = addr
                    logger.info(f"RTP peer: {addr}")
                
                # Strip RTP header
                if len(data) >= 12:
                    payload = data[12:]
                    
                    # µ-law to PCM
                    pcm = ulaw2linear(payload)
                    
                    # Voice activity detection
                    samples = np.frombuffer(pcm, dtype=np.int16)
                    rms = np.sqrt(np.mean(samples**2))
                    
                    if rms > 500:
                        if not self.user_speaking:
                            logger.info("User started speaking")
                            self.user_speaking = True
                            # Implement barge-in: stop TTS
                            if self.is_bot_speaking:
                                logger.info("Barge-in: stopping TTS")
                                self.is_bot_speaking = False
                                # Clear outgoing queue
                                while not self.outgoing_audio.empty():
                                    self.outgoing_audio.get()
                        silence_count = 0
                    else:
                        silence_count += 1
                        if silence_count > 50 and self.user_speaking:  # 1 second silence
                            logger.info("User stopped speaking")
                            self.user_speaking = False
                    
                    self.incoming_audio.put(pcm)
                    
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"RTP receive error: {e}")
    
    def send_loop(self):
        """Send RTP packets"""
        while self.running:
            try:
                if not self.outgoing_audio.empty() and self.peer:
                    pcm = self.outgoing_audio.get(timeout=0.02)
                    
                    # PCM to µ-law
                    ulaw = linear2ulaw(pcm)
                    
                    # Send RTP
                    pkt = self.rtp_header(self.seq, self.ts) + ulaw
                    self.sock.sendto(pkt, self.peer)
                    
                    self.seq += 1
                    self.ts += SMP
                else:
                    time.sleep(0.01)
                    
            except Exception as e:
                if self.running:
                    logger.debug(f"Send loop: {e}")
    
    def process_audio(self):
        """Process audio with Voice AI or echo"""
        logger.info("Audio processing started")
        
        # Accumulate audio for processing
        audio_buffer = b""
        
        while self.running:
            try:
                if not self.incoming_audio.empty():
                    pcm = self.incoming_audio.get()
                    audio_buffer += pcm
                    
                    # Process in chunks (160ms = 8 frames)
                    if len(audio_buffer) >= 2560:  # 160ms of audio
                        
                        if self.voice_ai and not self.is_bot_speaking:
                            # Send to Voice AI
                            self.voice_ai.process_audio(audio_buffer)
                            
                            # Check for response
                            if hasattr(self.voice_ai, 'response_ready'):
                                response_audio = self.voice_ai.get_response()
                                if response_audio:
                                    self.send_tts(response_audio)
                        else:
                            # Echo mode with reduced volume
                            samples = np.frombuffer(audio_buffer, dtype=np.int16)
                            echo = (samples * 0.3).astype(np.int16)
                            echo_bytes = echo.tobytes()
                            
                            # Send in 20ms chunks
                            for i in range(0, len(echo_bytes), 320):
                                chunk = echo_bytes[i:i+320]
                                if len(chunk) == 320:
                                    self.outgoing_audio.put(chunk)
                        
                        audio_buffer = b""
                
                time.sleep(0.001)
                
            except Exception as e:
                logger.error(f"Process audio error: {e}")
    
    def send_tts(self, audio_data):
        """Send TTS audio in chunks"""
        self.is_bot_speaking = True
        
        # Send in 20ms chunks
        for i in range(0, len(audio_data), 320):
            if not self.user_speaking:  # Check for barge-in
                chunk = audio_data[i:i+320]
                if len(chunk) == 320:
                    self.outgoing_audio.put(chunk)
            else:
                break
        
        self.is_bot_speaking = False
    
    def stop(self):
        logger.info("Stopping RTP agent")
        self.running = False
        if self.voice_ai:
            self.voice_ai.stop()
        if self.sock:
            self.sock.close()


def main():
    agent = RTPAgent()
    agent.start()


if __name__ == "__main__":
    main()