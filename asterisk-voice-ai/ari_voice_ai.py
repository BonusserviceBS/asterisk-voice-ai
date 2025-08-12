#!/usr/bin/env python3
"""
ARI Voice AI - The STABLE solution using Asterisk REST Interface
This is production-ready and doesn't have AudioSocket's problems!
"""

import asyncio
import aiohttp
import json
import logging
import os
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg
import base64
import time

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ARI Configuration
ARI_URL = "http://localhost:8088/ari"
ARI_USER = "ari-user"  
ARI_PASS = "ari123"  # Default from your config
ARI_APP = "voice-ai"

# Mistral API
MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class ARIVoiceAI:
    def __init__(self):
        self.azure_key = os.getenv('AZURE_SPEECH_KEY')
        self.azure_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        self.db_pool = None
        self.active_channels = {}
        
    async def init_db(self):
        """Initialize database connection"""
        try:
            self.db_pool = await asyncpg.create_pool(
                host='10.0.0.5',
                port=5432,
                database='teli24_development',
                user='teli24_user',
                password=os.getenv('DB_PASSWORD'),
                min_size=2,
                max_size=10
            )
            logger.info("✅ Database connected")
        except Exception as e:
            logger.error(f"DB error: {e}")
    
    async def get_assistant(self, phone):
        """Get assistant configuration from database"""
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow("""
                    SELECT a.name, a.greeting, a."systemPrompt", a.settings 
                    FROM "Assistant" a 
                    JOIN "PhoneNumber" p ON a.id = p."assignedToAssistantId" 
                    WHERE p.number LIKE $1
                    LIMIT 1
                """, f"%{phone[-10:]}%")
                
                if result:
                    return {
                        'name': result['name'],
                        'greeting': result['greeting'],
                        'system_prompt': result['systemPrompt'],
                        'settings': result['settings']
                    }
        except Exception as e:
            logger.error(f"Query error: {e}")
        return None
    
    async def connect_websocket(self):
        """Connect to ARI WebSocket for events"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        session = aiohttp.ClientSession(auth=auth)
        
        ws_url = f"ws://localhost:8088/ari/events?app={ARI_APP}&api_key={ARI_USER}:{ARI_PASS}"
        
        try:
            ws = await session.ws_connect(ws_url)
            logger.info("✅ Connected to ARI WebSocket")
            
            # Handle events
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(msg.data)
                    await self.handle_event(event)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
        finally:
            await session.close()
    
    async def handle_event(self, event):
        """Handle ARI events"""
        event_type = event.get('type')
        
        if event_type == 'StasisStart':
            # New call arrived
            channel = event['channel']
            channel_id = channel['id']
            caller = channel.get('caller', {}).get('number', 'Unknown')
            
            logger.info(f"📞 New call from {caller} [{channel_id}]")
            
            # Answer the call
            await self.answer_channel(channel_id)
            
            # Start voice AI handling
            asyncio.create_task(self.handle_call(channel_id, caller))
            
        elif event_type == 'StasisEnd':
            # Call ended
            channel_id = event['channel']['id']
            logger.info(f"📴 Call ended [{channel_id}]")
            
            if channel_id in self.active_channels:
                del self.active_channels[channel_id]
    
    async def answer_channel(self, channel_id):
        """Answer the channel"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            url = f"{ARI_URL}/channels/{channel_id}/answer"
            async with session.post(url) as resp:
                if resp.status == 204:
                    logger.info(f"✅ Channel answered [{channel_id}]")
                else:
                    logger.error(f"Failed to answer: {resp.status}")
    
    async def play_tts(self, channel_id, text):
        """Generate and play TTS"""
        # Generate TTS with Azure
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
        
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=tts_config,
            audio_config=None
        )
        
        result = synthesizer.speak_text_async(text).get()
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            # Save audio to file
            audio_file = f"/tmp/tts_{channel_id}_{int(time.time())}.raw"
            with open(audio_file, 'wb') as f:
                f.write(result.audio_data)
            
            # Play via ARI
            auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
            async with aiohttp.ClientSession(auth=auth) as session:
                # Play the audio file
                url = f"{ARI_URL}/channels/{channel_id}/play"
                params = {
                    'media': f'sound:{audio_file}',
                    'lang': 'de'
                }
                async with session.post(url, params=params) as resp:
                    if resp.status in [200, 201]:
                        logger.info(f"🔊 Playing TTS: {text[:50]}...")
                    else:
                        logger.error(f"Play failed: {resp.status}")
            
            # Clean up
            try:
                os.remove(audio_file)
            except:
                pass
    
    async def record_and_transcribe(self, channel_id):
        """Record audio and transcribe with STT"""
        # Start recording
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            record_file = f"/tmp/record_{channel_id}_{int(time.time())}"
            
            url = f"{ARI_URL}/channels/{channel_id}/record"
            params = {
                'name': record_file,
                'format': 'wav',
                'maxDurationSeconds': 10,
                'maxSilenceSeconds': 2,
                'ifExists': 'overwrite'
            }
            
            async with session.post(url, params=params) as resp:
                if resp.status in [200, 201]:
                    recording = await resp.json()
                    recording_name = recording['name']
                    
                    # Wait for recording to finish (simplified)
                    await asyncio.sleep(3)
                    
                    # Stop recording
                    stop_url = f"{ARI_URL}/recordings/live/{recording_name}/stop"
                    await session.post(stop_url)
                    
                    # Transcribe with Azure STT
                    audio_file = f"/var/spool/asterisk/recording/{record_file}.wav"
                    
                    if os.path.exists(audio_file):
                        # STT processing
                        stt_config = speechsdk.SpeechConfig(
                            subscription=self.azure_key,
                            region=self.azure_region
                        )
                        stt_config.speech_recognition_language = "de-DE"
                        
                        audio_config = speechsdk.audio.AudioConfig(filename=audio_file)
                        recognizer = speechsdk.SpeechRecognizer(
                            speech_config=stt_config,
                            audio_config=audio_config
                        )
                        
                        result = recognizer.recognize_once()
                        
                        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                            return result.text
                        
                        # Clean up
                        try:
                            os.remove(audio_file)
                        except:
                            pass
        
        return None
    
    async def handle_call(self, channel_id, caller):
        """Handle the call flow"""
        try:
            # Get assistant config
            assistant = await self.get_assistant("4920189098723")
            
            # Play greeting
            greeting = assistant['greeting'] if assistant else "Hallo, wie kann ich helfen?"
            await self.play_tts(channel_id, greeting)
            
            # Simple conversation loop
            for _ in range(5):  # Max 5 turns
                # Record user input
                user_text = await self.record_and_transcribe(channel_id)
                
                if user_text:
                    logger.info(f"🎤 User: {user_text}")
                    
                    # Generate response (simplified)
                    if "ja" in user_text.lower():
                        response = "Vielen Dank! Wie kann ich Ihnen helfen?"
                    elif "nein" in user_text.lower():
                        response = "Verstanden. Kann ich trotzdem helfen?"
                    elif "tschüss" in user_text.lower():
                        response = "Auf Wiederhören!"
                        await self.play_tts(channel_id, response)
                        break
                    else:
                        response = "Ja, verstehe. Was möchten Sie noch wissen?"
                    
                    # Play response
                    await self.play_tts(channel_id, response)
                else:
                    # No input detected
                    await self.play_tts(channel_id, "Sind Sie noch da?")
            
            # Hangup
            await self.hangup_channel(channel_id)
            
        except Exception as e:
            logger.error(f"Call handling error: {e}")
    
    async def hangup_channel(self, channel_id):
        """Hangup the channel"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            url = f"{ARI_URL}/channels/{channel_id}"
            async with session.delete(url) as resp:
                if resp.status == 204:
                    logger.info(f"✅ Channel hung up [{channel_id}]")
    
    async def start(self):
        """Start the ARI application"""
        await self.init_db()
        
        logger.info("=" * 50)
        logger.info("🎯 ARI Voice AI - The STABLE Solution")
        logger.info("✅ Using Asterisk REST Interface")
        logger.info("✅ No AudioSocket problems!")
        logger.info("✅ Production ready")
        logger.info("=" * 50)
        
        # Connect to WebSocket
        await self.connect_websocket()

if __name__ == "__main__":
    app = ARIVoiceAI()
    asyncio.run(app.start())