#!/usr/bin/env python3
"""
RTP Media Agent with Azure STT/TTS
Receives/sends RTP PCMU (G.711 µ-law) at 8kHz
"""
import socket
import struct
import time
import audioop
import logging
import asyncio
import threading
from queue import Queue
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RTP_PT = 0  # PCMU
SR = 8000
FRAME = 0.02  # 20 ms
SMP = int(SR * FRAME)  # 160 samples/20ms

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
        
        # Start RTP receive thread
        recv_thread = threading.Thread(target=self.receive_loop)
        recv_thread.start()
        
        # Start RTP send thread
        send_thread = threading.Thread(target=self.send_loop)
        send_thread.start()
        
        # Start audio processing
        self.process_audio()
        
    def receive_loop(self):
        """Receive RTP packets"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
                
                # Remember peer address from first packet
                if self.peer is None:
                    self.peer = addr
                    logger.info(f"RTP peer: {addr}")
                
                # Strip RTP header (12 bytes minimum)
                if len(data) >= 12:
                    payload = data[12:]
                    # µ-law to 16-bit PCM
                    pcm = audioop.ulaw2lin(payload, 2)
                    self.incoming_audio.put(pcm)
                    
            except socket.timeout:
                continue
            except Exception as e:
                logger.error(f"RTP receive error: {e}")
    
    def send_loop(self):
        """Send RTP packets"""
        while self.running:
            try:
                if not self.outgoing_audio.empty() and self.peer:
                    # Get PCM audio from queue
                    pcm = self.outgoing_audio.get(timeout=0.02)
                    
                    # PCM to µ-law
                    ulaw = audioop.lin2ulaw(pcm, 2)
                    
                    # Send RTP packet
                    pkt = self.rtp_header(self.seq, self.ts) + ulaw
                    self.sock.sendto(pkt, self.peer)
                    
                    self.seq += 1
                    self.ts += SMP
                else:
                    time.sleep(0.01)
                    
            except Exception as e:
                if self.running:
                    logger.error(f"RTP send error: {e}")
    
    def process_audio(self):
        """Main audio processing loop"""
        logger.info("Audio processing started")
        
        while self.running:
            try:
                # Get incoming audio
                if not self.incoming_audio.empty():
                    pcm = self.incoming_audio.get()
                    
                    # TODO: Send to Azure STT
                    # For now: simple echo with reduced volume
                    echo = audioop.mul(pcm, 2, 0.3)
                    
                    # Add silence detection here
                    rms = audioop.rms(pcm, 2)
                    if rms > 500:  # Basic voice activity
                        logger.debug(f"Voice activity: RMS={rms}")
                    
                    # Send echo back
                    self.outgoing_audio.put(echo)
                    
                time.sleep(0.001)
                
            except Exception as e:
                logger.error(f"Audio processing error: {e}")
    
    def stop(self):
        logger.info("Stopping RTP agent")
        self.running = False
        if self.sock:
            self.sock.close()

def main():
    agent = RTPAgent()
    
    try:
        agent.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        agent.stop()

if __name__ == "__main__":
    main()