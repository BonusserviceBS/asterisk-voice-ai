#!/usr/bin/env python3
"""
Real-time Streaming Voice Agent
True real-time conversation with <500ms latency
Uses WebSocket for continuous audio streaming instead of file-based processing
"""

import asyncio
from aiohttp import web
import aiohttp
import numpy as np
import azure.cognitiveservices.speech as speechsdk
from azure.cognitiveservices.speech.audio import AudioOutputStream, AudioOutputConfig
import edge_tts
import logging
import json
import base64
import io
import wave
import audioop
import struct
import time
from collections import deque
from typing import Optional
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Azure Configuration
AZURE_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_REGION = os.getenv("AZURE_SPEECH_REGION", "germanywestcentral")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

class StreamingVoiceAgent:
    """
    Real-time voice agent with:
    - Continuous audio streaming (no waiting for silence!)
    - Parallel STT/LLM/TTS processing
    - Voice Activity Detection for interruption handling
    - Sub-500ms response time
    """
    
    def __init__(self):
        self.websocket = None
        self.call_id = None
        
        # Audio buffers
        self.audio_buffer = deque(maxlen=100)  # 2 seconds at 20ms chunks
        self.stt_buffer = io.BytesIO()
        
        # State management
        self.is_listening = False
        self.is_speaking = False
        self.conversation_active = False
        
        # Timing metrics
        self.last_speech_time = 0
        self.response_start_time = 0
        
        # Initialize components
        self.init_streaming_stt()
        self.init_openai()
        
        # Conversation history
        self.messages = [
            {"role": "system", "content": "Du bist ein freundlicher Assistent für Telefongespräche. Antworte kurz und präzise auf Deutsch."}
        ]
        
    def init_streaming_stt(self):
        """Initialize Azure STT for continuous streaming"""
        if not AZURE_KEY:
            logger.warning("No Azure key configured")
            self.recognizer = None
            return
            
        # Configure for German with low latency
        speech_config = speechsdk.SpeechConfig(
            subscription=AZURE_KEY,
            region=AZURE_REGION
        )
        speech_config.speech_recognition_language = "de-DE"
        
        # Optimize for real-time
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs,
            "1000"  # 1 second initial silence
        )
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
            "300"  # 300ms end silence - FAST!
        )
        
        # Enable partial results for faster response
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary,
            "true"
        )
        
        # Create push stream for continuous audio
        self.push_stream = speechsdk.audio.PushAudioInputStream()
        audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)
        
        # Create recognizer
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config
        )
        
        # Connect event handlers
        self.recognizer.recognizing.connect(self.on_recognizing)
        self.recognizer.recognized.connect(self.on_recognized)
        
        # Start continuous recognition
        self.recognizer.start_continuous_recognition_async()
        logger.info("Streaming STT initialized")
        
    def init_openai(self):
        """Initialize OpenAI client"""
        if AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT:
            from openai import AzureOpenAI
            self.llm_client = AzureOpenAI(
                api_key=AZURE_OPENAI_KEY,
                api_version="2024-02-15-preview",
                azure_endpoint=AZURE_OPENAI_ENDPOINT
            )
            logger.info("Azure OpenAI initialized")
        else:
            self.llm_client = None
            logger.warning("No OpenAI configuration")
            
    def on_recognizing(self, evt):
        """Handle partial recognition results"""
        if evt.result.text:
            logger.debug(f"Partial: {evt.result.text}")
            # Could trigger "typing" indicator here
            
    def on_recognized(self, evt):
        """Handle final recognition results"""
        if evt.result.text:
            logger.info(f"Recognized: {evt.result.text}")
            
            # Stop any ongoing TTS playback (barge-in)
            if self.is_speaking:
                asyncio.create_task(self.stop_speaking())
            
            # Process immediately - no waiting!
            asyncio.create_task(self.process_utterance(evt.result.text))
            
    async def process_utterance(self, text: str):
        """Process user input and generate response"""
        start_time = time.time()
        
        # Add to conversation
        self.messages.append({"role": "user", "content": text})
        
        # Generate LLM response
        if self.llm_client:
            try:
                # Stream the response for faster first token
                stream = self.llm_client.chat.completions.create(
                    model=AZURE_OPENAI_DEPLOYMENT,
                    messages=self.messages,
                    max_tokens=60,  # Short responses for speed
                    temperature=0.7,
                    stream=True  # Streaming!
                )
                
                response_text = ""
                first_chunk_time = None
                
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        if first_chunk_time is None:
                            first_chunk_time = time.time() - start_time
                            logger.info(f"LLM first token: {first_chunk_time*1000:.0f}ms")
                        
                        response_text += chunk.choices[0].delta.content
                        
                        # Start TTS as soon as we have a sentence
                        if any(p in response_text for p in ['.', '!', '?', ',']):
                            await self.speak_streaming(response_text)
                            response_text = ""
                
                # Speak any remaining text
                if response_text:
                    await self.speak_streaming(response_text)
                    
                total_time = time.time() - start_time
                logger.info(f"Total response time: {total_time*1000:.0f}ms")
                
            except Exception as e:
                logger.error(f"LLM error: {e}")
                await self.speak_streaming("Entschuldigung, ich habe das nicht verstanden.")
        else:
            # Fallback response
            await self.speak_streaming("Ich bin bereit zu helfen.")
            
    async def speak_streaming(self, text: str):
        """Stream TTS output with chunking for low latency"""
        self.is_speaking = True
        start_time = time.time()
        
        try:
            # Use Edge-TTS for fast German synthesis
            communicate = edge_tts.Communicate(
                text,
                "de-DE-ConradNeural",  # Fast male voice
                rate="+10%"  # Slightly faster speech
            )
            
            first_chunk = True
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    if first_chunk:
                        latency = (time.time() - start_time) * 1000
                        logger.info(f"TTS first chunk: {latency:.0f}ms")
                        first_chunk = False
                    
                    # Send audio immediately to WebSocket
                    if self.websocket:
                        await self.send_audio_chunk(chunk["data"])
                        
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            self.is_speaking = False
            
    async def stop_speaking(self):
        """Stop current TTS playback (for barge-in)"""
        logger.info("Stopping TTS (barge-in detected)")
        self.is_speaking = False
        # Send silence or stop command to clear audio buffer
        if self.websocket:
            await self.websocket.send_json({
                "type": "clear_audio"
            })
            
    async def handle_audio_frame(self, audio_data: bytes):
        """Handle incoming audio frame from WebSocket"""
        # Push to STT stream immediately
        if self.recognizer and self.push_stream:
            # Convert from 8kHz to 16kHz for better STT accuracy
            audio_16k = audioop.ratecv(
                audio_data,
                2,  # 16-bit
                1,  # mono
                8000,  # from 8kHz
                16000,  # to 16kHz
                None
            )[0]
            
            # Push to Azure STT
            self.push_stream.write(audio_16k)
            
    async def send_audio_chunk(self, audio_data: bytes):
        """Send audio chunk to WebSocket"""
        if self.websocket:
            # Convert from 24kHz TTS to 8kHz for telephony
            audio_8k = audioop.ratecv(
                audio_data,
                2,  # 16-bit
                1,  # mono
                24000,  # from 24kHz
                8000,  # to 8kHz
                None
            )[0]
            
            # Send as base64
            await self.websocket.send_json({
                "type": "audio",
                "data": base64.b64encode(audio_8k).decode()
            })
            
    async def handle_websocket(self, ws):
        """Main WebSocket handler"""
        self.websocket = ws
        logger.info("WebSocket connected - starting real-time conversation")
        
        # Send greeting immediately
        await self.speak_streaming("Hallo, wie kann ich Ihnen helfen?")
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data["type"] == "audio":
                        # Decode and process audio frame
                        audio_bytes = base64.b64decode(data["data"])
                        await self.handle_audio_frame(audio_bytes)
                        
                    elif data["type"] == "call_start":
                        self.call_id = data.get("call_id")
                        logger.info(f"Call started: {self.call_id}")
                        
                    elif data["type"] == "call_end":
                        logger.info(f"Call ended: {self.call_id}")
                        break
                        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            if self.recognizer:
                self.recognizer.stop_continuous_recognition_async()
            self.websocket = None


async def main():
    """WebSocket server for real-time voice streaming"""
    app = web.Application()
    
    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        agent = StreamingVoiceAgent()
        await agent.handle_websocket(ws)
        
        return ws
    
    app.router.add_get('/ws', websocket_handler)
    
    # Health check endpoint
    async def health(request):
        return web.json_response({
            "status": "healthy",
            "type": "streaming_voice_agent",
            "latency_target": "500ms"
        })
    
    app.router.add_get('/health', health)
    
    logger.info("Starting Real-time Voice Agent on port 8090")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8090)
    await site.start()
    
    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())