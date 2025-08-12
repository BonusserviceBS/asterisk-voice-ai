#!/usr/bin/env python3
"""
SIMPLE STABLE AI - Minimal complexity for maximum stability
Uses continuous audio stream to prevent disconnections
"""

import asyncio
import struct
import logging
import time
import os
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg
import numpy as np
import threading
import queue

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SimpleStableAI:
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.db_pool = None
        
        self.azure_key = os.getenv('AZURE_SPEECH_KEY')
        self.azure_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        
        # Pre-generated responses for testing
        self.quick_responses = {
            "ja": "Vielen Dank! Wie kann ich Ihnen helfen?",
            "nein": "Verstanden. Kann ich trotzdem helfen?",
            "hilfe": "Gerne helfe ich Ihnen. Was möchten Sie wissen?",
            "danke": "Sehr gerne! Gibt es noch etwas?",
            "tschüss": "Auf Wiederhören und einen schönen Tag!",
            "default": "Ja, verstehe. Können Sie das genauer erklären?"
        }
        
    async def init_db(self):
        try:
            self.db_pool = await asyncpg.create_pool(
                host='10.0.0.5',
                port=5432,
                database='teli24_development',
                user='teli24_user',
                password=os.getenv('DB_PASSWORD'),
                min_size=1,
                max_size=3
            )
            logger.info("✅ DB connected")
        except Exception as e:
            logger.error(f"DB error: {e}")
    
    async def continuous_silence_sender(self, writer):
        """Send continuous silence to keep connection alive"""
        try:
            while True:
                # Send 20ms of silence every 20ms
                silence = b'\x00\x00' * 160  # 20ms at 8kHz
                msg = struct.pack('>BH', 0x10, len(silence)) + silence
                writer.write(msg)
                await writer.drain()
                await asyncio.sleep(0.02)  # 20ms
        except:
            pass
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time()))[-6:]
        logger.info(f"📞 Call [{client_id}]")
        
        # Audio queue for outgoing audio
        audio_queue = queue.Queue()
        stop_silence = threading.Event()
        
        # Start continuous audio sender in background
        async def audio_sender():
            """Continuously send audio or silence"""
            try:
                while True:
                    try:
                        # Check for audio to send
                        audio_chunk = audio_queue.get_nowait()
                        
                        # Send actual audio
                        for i in range(0, len(audio_chunk), 320):
                            chunk = audio_chunk[i:i+320]
                            if len(chunk) < 320:
                                chunk += b'\x00' * (320 - len(chunk))
                            
                            msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                            writer.write(msg)
                            await writer.drain()
                            await asyncio.sleep(0.02)
                            
                    except queue.Empty:
                        # Send silence when no audio
                        if not stop_silence.is_set():
                            silence = b'\x00\x00' * 160
                            msg = struct.pack('>BH', 0x10, len(silence)) + silence
                            writer.write(msg)
                            await writer.drain()
                            await asyncio.sleep(0.02)
                        else:
                            await asyncio.sleep(0.01)
            except:
                pass
        
        # Start audio sender
        sender_task = asyncio.create_task(audio_sender())
        
        # Audio buffer for incoming
        audio_buffer = bytearray()
        silence_count = 0
        
        # TTS config
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
        
        # STT config
        stt_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        stt_config.speech_recognition_language = "de-DE"
        
        greeting_sent = False
        
        try:
            while True:
                # Read header
                header = await asyncio.wait_for(reader.readexactly(3), timeout=30)
                if not header:
                    break
                    
                msg_type = header[0]
                length = struct.unpack('>H', header[1:3])[0]
                
                payload = b''
                if length > 0:
                    payload = await reader.readexactly(length)
                
                if msg_type == 0x01:  # UUID
                    logger.info("UUID received - sending greeting")
                    
                    # Simple greeting
                    greeting = "Hallo, ich bin Sabine. Sind Sie mit der Aufnahme einverstanden?"
                    
                    # Generate TTS
                    synthesizer = speechsdk.SpeechSynthesizer(
                        speech_config=tts_config,
                        audio_config=None
                    )
                    result = synthesizer.speak_text_async(greeting).get()
                    
                    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                        # Queue audio for sending
                        audio_queue.put(result.audio_data)
                        logger.info("Greeting queued")
                    
                    greeting_sent = True
                    
                elif msg_type == 0x10:  # Audio
                    # Collect audio
                    audio_buffer.extend(payload)
                    
                    # Simple VAD
                    if len(payload) >= 320:
                        audio_array = np.frombuffer(payload, dtype=np.int16)
                        energy = np.sqrt(np.mean(audio_array.astype(float) ** 2))
                        
                        if energy > 200:
                            silence_count = 0
                        else:
                            silence_count += 1
                    
                    # Process after silence
                    if len(audio_buffer) > 4800 and silence_count > 25:
                        # Process speech
                        audio_data = bytes(audio_buffer)
                        audio_buffer = bytearray()
                        silence_count = 0
                        
                        # STT
                        stream = speechsdk.audio.PushAudioInputStream(
                            stream_format=speechsdk.audio.AudioStreamFormat(
                                samples_per_second=8000,
                                bits_per_sample=16,
                                channels=1,
                                wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.PCM
                            )
                        )
                        stream.write(audio_data)
                        stream.close()
                        
                        audio_config = speechsdk.audio.AudioConfig(stream=stream)
                        recognizer = speechsdk.SpeechRecognizer(
                            speech_config=stt_config,
                            audio_config=audio_config
                        )
                        
                        result = recognizer.recognize_once()
                        
                        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                            text = result.text.lower().strip()
                            logger.info(f"User: {text}")
                            
                            # Quick response lookup
                            response = None
                            for key in self.quick_responses:
                                if key in text:
                                    response = self.quick_responses[key]
                                    break
                            
                            if not response:
                                response = self.quick_responses["default"]
                            
                            logger.info(f"Response: {response}")
                            
                            # Generate TTS
                            synthesizer = speechsdk.SpeechSynthesizer(
                                speech_config=tts_config,
                                audio_config=None
                            )
                            result = synthesizer.speak_text_async(response).get()
                            
                            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                                # Queue audio
                                audio_queue.put(result.audio_data)
                    
                    # Prevent overflow
                    if len(audio_buffer) > 80000:
                        audio_buffer = audio_buffer[-40000:]
                        
                elif msg_type == 0x00:  # Hangup
                    logger.info("Hangup")
                    break
                    
        except asyncio.TimeoutError:
            logger.info("Timeout")
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            stop_silence.set()
            sender_task.cancel()
            
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
                
            logger.info(f"Call ended [{client_id}]")
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info("=" * 50)
        logger.info("🎯 SIMPLE STABLE Voice AI")
        logger.info(f"📍 Port: {self.port}")
        logger.info("✅ Strategy:")
        logger.info("  • Continuous audio stream")
        logger.info("  • Pre-defined responses")
        logger.info("  • No AI delays")
        logger.info("  • Always sending (audio or silence)")
        logger.info("=" * 50)
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = SimpleStableAI()
    asyncio.run(server.start())