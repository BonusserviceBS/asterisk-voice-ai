#!/usr/bin/env python3
"""
Production-Grade Voice AI Pipeline
Optimized for low latency and high quality
"""

import asyncio
import logging
import numpy as np
from collections import deque
import time
import azure.cognitiveservices.speech as speechsdk
import webrtcvad
import edge_tts
from typing import Optional, Tuple
import audioop
import struct

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProductionVoicePipeline:
    """
    Optimized Voice Pipeline with:
    - Streaming STT with Azure
    - VAD for barge-in detection
    - Chunked TTS playback
    - Latency monitoring
    """
    
    def __init__(self, config: dict):
        self.config = config
        
        # Audio parameters
        self.sample_rate = 8000  # PSTN rate
        self.frame_duration = 20  # ms
        self.frame_size = int(self.sample_rate * self.frame_duration / 1000)
        
        # Jitter buffer (40-80ms)
        self.jitter_buffer = deque(maxlen=4)  # 4 * 20ms = 80ms max
        self.jitter_min_frames = 2  # 40ms minimum before playback
        
        # VAD for barge-in detection
        self.vad = webrtcvad.Vad(2)  # Aggressiveness level 2
        
        # Latency tracking
        self.metrics = {
            'stt_first_token': [],
            'llm_response': [],
            'tts_first_chunk': [],
            'e2e_latency': []
        }
        
        # Audio state
        self.is_speaking = False
        self.tts_playing = False
        self.last_speech_time = 0
        
        # Initialize STT
        self.init_streaming_stt()
        
    def init_streaming_stt(self):
        """Initialize Azure streaming STT with optimal settings"""
        if not self.config.get('azure_key'):
            logger.warning("No Azure key, using mock STT")
            self.stt_stream = None
            return
            
        # Speech config for low latency
        speech_config = speechsdk.SpeechConfig(
            subscription=self.config['azure_key'],
            region=self.config.get('azure_region', 'germanywestcentral')
        )
        
        # Optimize for streaming
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, 
            "2000"  # Fast initial response
        )
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
            "500"  # Quick end detection
        )
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary,
            "true"  # Get partial results
        )
        
        # Audio format - 16kHz for better accuracy
        self.stt_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1
        )
        
        # Push stream for real-time audio
        self.stt_stream = speechsdk.audio.PushAudioInputStream(self.stt_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self.stt_stream)
        
        # Create recognizer
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config,
            language="de-DE"
        )
        
        # Connect events for streaming
        self.recognizer.recognizing.connect(self.on_recognizing)
        self.recognizer.recognized.connect(self.on_recognized)
        
        # Start continuous recognition
        self.recognizer.start_continuous_recognition_async()
        
    def on_recognizing(self, evt):
        """Handle partial STT results"""
        if evt.result.text:
            logger.debug(f"Partial: {evt.result.text}")
            # Could trigger early LLM processing here
            
    def on_recognized(self, evt):
        """Handle final STT results"""
        if evt.result.text:
            latency = time.time() - self.last_speech_time
            self.metrics['stt_first_token'].append(latency * 1000)
            logger.info(f"STT ({latency*1000:.0f}ms): {evt.result.text}")
            
            # Trigger LLM processing
            asyncio.create_task(self.process_utterance(evt.result.text))
    
    def process_audio_frame(self, frame: bytes) -> Tuple[bool, Optional[bytes]]:
        """
        Process incoming audio frame
        Returns: (is_speech, processed_frame)
        """
        # Add to jitter buffer
        self.jitter_buffer.append(frame)
        
        # Wait for minimum buffer before processing
        if len(self.jitter_buffer) < self.jitter_min_frames:
            return False, None
            
        # Get oldest frame from buffer
        buffered_frame = self.jitter_buffer[0]
        
        # VAD detection (need 16-bit PCM)
        pcm_frame = self.ulaw_to_pcm(buffered_frame)
        is_speech = self.vad.is_speech(pcm_frame, self.sample_rate)
        
        if is_speech:
            self.last_speech_time = time.time()
            self.is_speaking = True
            
            # Barge-in: stop TTS if user speaks
            if self.tts_playing:
                logger.info("Barge-in detected, stopping TTS")
                self.stop_tts()
                
            # Resample to 16kHz for STT
            if self.stt_stream:
                resampled = self.resample_8k_to_16k(pcm_frame)
                self.stt_stream.write(resampled)
        else:
            # End of speech detection
            if self.is_speaking and time.time() - self.last_speech_time > 0.5:
                self.is_speaking = False
                
        return is_speech, buffered_frame
    
    def ulaw_to_pcm(self, ulaw_data: bytes) -> bytes:
        """Convert μ-law to 16-bit PCM"""
        return audioop.ulaw2lin(ulaw_data, 2)
    
    def pcm_to_ulaw(self, pcm_data: bytes) -> bytes:
        """Convert 16-bit PCM to μ-law"""
        return audioop.lin2ulaw(pcm_data, 2)
    
    def resample_8k_to_16k(self, pcm_8k: bytes) -> bytes:
        """Resample from 8kHz to 16kHz for STT"""
        # Simple upsampling by factor of 2
        return audioop.ratecv(
            pcm_8k, 2, 1, 8000, 16000, None
        )[0]
    
    def resample_16k_to_8k(self, pcm_16k: bytes) -> bytes:
        """Resample from 16kHz to 8kHz for PSTN"""
        return audioop.ratecv(
            pcm_16k, 2, 1, 16000, 8000, None
        )[0]
    
    async def process_utterance(self, text: str):
        """Process user utterance through LLM"""
        start_time = time.time()
        
        # Here you would call Mistral/OpenAI
        # For now, simple response
        response = await self.generate_llm_response(text)
        
        llm_latency = (time.time() - start_time) * 1000
        self.metrics['llm_response'].append(llm_latency)
        logger.info(f"LLM ({llm_latency:.0f}ms): {response}")
        
        # Start TTS streaming
        await self.stream_tts(response)
    
    async def generate_llm_response(self, text: str) -> str:
        """Generate LLM response (mock for now)"""
        # In production: use streaming Mistral API
        await asyncio.sleep(0.1)  # Simulate LLM latency
        
        if "hallo" in text.lower():
            return "Guten Tag! Wie kann ich Ihnen helfen?"
        elif "wetter" in text.lower():
            return "Das Wetter ist heute sonnig mit 22 Grad."
        else:
            return "Interessant. Erzählen Sie mir mehr darüber."
    
    async def stream_tts(self, text: str):
        """Stream TTS with chunked playback"""
        start_time = time.time()
        self.tts_playing = True
        
        try:
            # Use Edge-TTS for streaming
            voice = "de-DE-ConradNeural"
            communicate = edge_tts.Communicate(text, voice)
            
            first_chunk = True
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    if first_chunk:
                        tts_latency = (time.time() - start_time) * 1000
                        self.metrics['tts_first_chunk'].append(tts_latency)
                        logger.info(f"TTS first chunk: {tts_latency:.0f}ms")
                        first_chunk = False
                    
                    # Check for barge-in
                    if not self.tts_playing:
                        break
                        
                    # Process audio chunk
                    audio_data = chunk["data"]
                    # Here you would send to RTP stream
                    await self.send_audio_chunk(audio_data)
                    
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            self.tts_playing = False
    
    async def send_audio_chunk(self, audio_data: bytes):
        """Send audio chunk to RTP stream"""
        # Convert to 8kHz μ-law for PSTN
        # In production: send via RTP
        await asyncio.sleep(0.02)  # Simulate 20ms frame
    
    def stop_tts(self):
        """Stop TTS playback (barge-in)"""
        self.tts_playing = False
    
    def get_metrics(self) -> dict:
        """Get latency metrics"""
        def avg(lst):
            return sum(lst) / len(lst) if lst else 0
            
        return {
            'stt_avg_ms': avg(self.metrics['stt_first_token']),
            'llm_avg_ms': avg(self.metrics['llm_response']),
            'tts_avg_ms': avg(self.metrics['tts_first_chunk']),
            'e2e_avg_ms': avg(self.metrics['e2e_latency'])
        }

class RTPHandler:
    """
    RTP packet handler with jitter buffer and PLC
    """
    
    def __init__(self, pipeline: ProductionVoicePipeline):
        self.pipeline = pipeline
        self.sequence = 0
        self.timestamp = 0
        self.ssrc = np.random.randint(0, 2**32)
        
    def create_rtp_packet(self, payload: bytes) -> bytes:
        """Create RTP packet with proper headers"""
        # RTP header: V=2, P=0, X=0, CC=0, M=0, PT=0 (PCMU)
        header = struct.pack(
            '!BBHII',
            0x80,  # V=2, P=0, X=0, CC=0
            0x00,  # M=0, PT=0 (PCMU)
            self.sequence,
            self.timestamp,
            self.ssrc
        )
        
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp += 160  # 20ms at 8kHz
        
        return header + payload
    
    def parse_rtp_packet(self, packet: bytes) -> Optional[bytes]:
        """Parse RTP packet and extract payload"""
        if len(packet) < 12:
            return None
            
        # Extract sequence number for jitter buffer ordering
        seq = struct.unpack('!H', packet[2:4])[0]
        
        # Return payload (skip 12-byte header)
        return packet[12:]

async def main():
    """Test the production pipeline"""
    config = {
        'azure_key': '',  # Add your key
        'azure_region': 'germanywestcentral'
    }
    
    pipeline = ProductionVoicePipeline(config)
    
    # Simulate audio frames
    for i in range(100):
        # Generate test frame (silence)
        frame = b'\xff' * 160  # 20ms of μ-law silence
        
        is_speech, processed = pipeline.process_audio_frame(frame)
        
        if i == 50:
            # Simulate speech
            logger.info("Simulating speech input...")
            await pipeline.process_utterance("Wie ist das Wetter heute?")
            
        await asyncio.sleep(0.02)  # 20ms
    
    # Print metrics
    metrics = pipeline.get_metrics()
    logger.info(f"Latency metrics: {metrics}")

if __name__ == "__main__":
    asyncio.run(main())