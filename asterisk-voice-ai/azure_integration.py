#!/usr/bin/env python3
"""
Azure Speech Services Integration
STT and TTS for Voice AI
"""
import os
import asyncio
import logging
import azure.cognitiveservices.speech as speechsdk
from collections import deque
import threading
import time

logger = logging.getLogger(__name__)

class AzureSTT:
    def __init__(self, key=None, region="germanywestcentral"):
        self.key = key or os.getenv("AZURE_SPEECH_KEY")
        self.region = region
        self.recognizer = None
        self.stream = None
        self.recognition_done = False
        self.results = deque(maxlen=100)
        
        if not self.key:
            logger.warning("No Azure Speech key provided, using Edge-TTS fallback")
            return
            
        self.setup_recognizer()
    
    def setup_recognizer(self):
        """Setup Azure Speech recognizer with push stream"""
        # Create push stream for 8kHz audio
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=8000,
            bits_per_sample=16,
            channels=1
        )
        
        self.stream = speechsdk.audio.PushAudioInputStream(stream_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self.stream)
        
        # Create speech config
        speech_config = speechsdk.SpeechConfig(
            subscription=self.key,
            region=self.region
        )
        speech_config.speech_recognition_language = "de-DE"
        
        # Enable partial results for low latency
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_EnableAudioLogging,
            "false"
        )
        
        # Create recognizer
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config
        )
        
        # Connect callbacks
        self.recognizer.recognizing.connect(self.on_recognizing)
        self.recognizer.recognized.connect(self.on_recognized)
        self.recognizer.session_started.connect(self.on_session_started)
        
    def on_session_started(self, evt):
        logger.info("STT session started")
    
    def on_recognizing(self, evt):
        """Handle partial recognition results"""
        if evt.result.text:
            logger.debug(f"STT partial: {evt.result.text}")
            self.results.append(("partial", evt.result.text))
    
    def on_recognized(self, evt):
        """Handle final recognition results"""
        if evt.result.text:
            logger.info(f"STT final: {evt.result.text}")
            self.results.append(("final", evt.result.text))
    
    def start(self):
        """Start continuous recognition"""
        if self.recognizer:
            self.recognizer.start_continuous_recognition()
            logger.info("STT started")
    
    def push_audio(self, pcm_data):
        """Push audio data to recognizer"""
        if self.stream:
            self.stream.write(pcm_data)
    
    def stop(self):
        """Stop recognition"""
        if self.recognizer:
            self.recognizer.stop_continuous_recognition()
            if self.stream:
                self.stream.close()
            logger.info("STT stopped")


class AzureTTS:
    def __init__(self, key=None, region="germanywestcentral"):
        self.key = key or os.getenv("AZURE_SPEECH_KEY")
        self.region = region
        self.synthesizer = None
        self.voice = "de-DE-KatjaNeural"
        
        if not self.key:
            logger.warning("No Azure Speech key provided, using Edge-TTS fallback")
            self.use_edge = True
        else:
            self.use_edge = False
            self.setup_synthesizer()
    
    def setup_synthesizer(self):
        """Setup Azure Speech synthesizer"""
        # Speech config
        speech_config = speechsdk.SpeechConfig(
            subscription=self.key,
            region=self.region
        )
        
        # Set voice
        speech_config.speech_synthesis_voice_name = self.voice
        
        # Audio config for 8kHz output
        audio_format = speechsdk.SpeechSynthesisOutputFormat.Riff8Khz16BitMonoPcm
        speech_config.set_speech_synthesis_output_format(audio_format)
        
        # Create synthesizer without audio output (we'll get the stream)
        self.synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
            audio_config=None
        )
    
    async def synthesize(self, text):
        """Synthesize text to 8kHz PCM"""
        if self.use_edge:
            return await self.synthesize_edge(text)
        
        # Azure TTS
        result = self.synthesizer.speak_text_async(text).get()
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            # Get audio data (includes WAV header)
            audio_data = result.audio_data
            # Skip WAV header (44 bytes for standard WAV)
            pcm_data = audio_data[44:]
            logger.info(f"TTS synthesized: {len(pcm_data)} bytes")
            return pcm_data
        else:
            logger.error(f"TTS failed: {result.reason}")
            return None
    
    async def synthesize_edge(self, text):
        """Use Edge-TTS as fallback"""
        try:
            import edge_tts
            
            # Create communication object
            tts = edge_tts.Communicate(text, "de-DE-KatjaNeural")
            
            # Generate audio
            audio_data = b""
            async for chunk in tts.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            
            # Edge-TTS returns MP3, we need PCM
            # For now, return empty (would need ffmpeg conversion)
            logger.warning("Edge-TTS needs audio conversion, returning silence")
            return b'\x00' * 320  # 20ms of silence
            
        except Exception as e:
            logger.error(f"Edge-TTS error: {e}")
            return None


class VoiceAI:
    """Simple Voice AI with Azure integration"""
    
    def __init__(self):
        self.stt = AzureSTT()
        self.tts = AzureTTS()
        self.conversation = []
        self.is_speaking = False
        
    def start(self):
        """Start Voice AI"""
        self.stt.start()
        logger.info("Voice AI started")
    
    def process_audio(self, pcm_data):
        """Process incoming audio"""
        # Push to STT
        self.stt.push_audio(pcm_data)
        
        # Check for results
        while self.stt.results:
            result_type, text = self.stt.results.popleft()
            
            if result_type == "final":
                # Process the final text
                asyncio.create_task(self.respond(text))
    
    async def respond(self, user_text):
        """Generate and speak response"""
        logger.info(f"User said: {user_text}")
        
        # Simple echo response for testing
        response = f"Sie sagten: {user_text}"
        
        # Synthesize response
        audio = await self.tts.synthesize(response)
        
        if audio:
            # Return audio for sending
            return audio
        
        return None
    
    def stop(self):
        """Stop Voice AI"""
        self.stt.stop()
        logger.info("Voice AI stopped")