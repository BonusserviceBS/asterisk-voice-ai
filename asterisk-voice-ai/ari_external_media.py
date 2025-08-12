#!/usr/bin/env python3
"""
ARI External Media Voice AI - The correct implementation for Asterisk 22
Uses ARI to create ExternalMedia channels with RTP streaming
"""

import asyncio
import aiohttp
import json
import logging
import socket
import struct
import time
import os
import random
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg
import numpy as np

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ARI Configuration
ARI_URL = "http://localhost:8088/ari"
ARI_USER = "ari-user"
ARI_PASS = "ari123"
ARI_APP = "voice-ai"

# RTP Configuration
RTP_PORT_START = 10000
RTP_PORT_END = 20000

MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class ARIExternalMedia:
    def __init__(self):
        self.azure_key = os.getenv('AZURE_SPEECH_KEY')
        self.azure_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        self.db_pool = None
        self.sessions = {}
        self.rtp_sockets = {}
        
    async def init_db(self):
        """Initialize database"""
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
        """Get assistant from database"""
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
            logger.info(f"Connecting to ARI WebSocket: {ws_url}")
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
            
            # Start handling the call
            asyncio.create_task(self.handle_call(channel_id, caller))
            
        elif event_type == 'StasisEnd':
            # Call ended
            channel_id = event['channel']['id']
            logger.info(f"📴 Call ended [{channel_id}]")
            
            # Clean up session
            if channel_id in self.sessions:
                # Close RTP socket if exists
                if channel_id in self.rtp_sockets:
                    self.rtp_sockets[channel_id].close()
                    del self.rtp_sockets[channel_id]
                del self.sessions[channel_id]
    
    async def handle_call(self, channel_id, caller):
        """Handle call with External Media"""
        try:
            # Answer the channel first
            await self.answer_channel(channel_id)
            
            # Find available RTP port
            rtp_port = self.find_available_port()
            
            # Create RTP socket
            rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtp_socket.bind(('0.0.0.0', rtp_port))
            rtp_socket.setblocking(False)
            self.rtp_sockets[channel_id] = rtp_socket
            
            logger.info(f"📡 RTP socket on port {rtp_port} for channel {channel_id}")
            
            # Create External Media channel via ARI
            external_media_channel = await self.create_external_media(channel_id, rtp_port)
            
            if external_media_channel:
                # Initialize session
                self.sessions[channel_id] = {
                    'channel_id': channel_id,
                    'external_channel_id': external_media_channel,
                    'rtp_port': rtp_port,
                    'rtp_socket': rtp_socket,
                    'audio_buffer': bytearray(),
                    'silence_count': 0,
                    'processing': False,
                    'conversation': []
                }
                
                # Get assistant
                assistant = await self.get_assistant("4920189098723")
                self.sessions[channel_id]['assistant'] = assistant
                
                # Setup TTS/STT configs
                await self.setup_speech_configs(channel_id)
                
                # Start RTP handler
                asyncio.create_task(self.handle_rtp(channel_id))
                
                # Send greeting
                greeting = assistant['greeting'] if assistant else "Hallo, wie kann ich helfen?"
                await self.send_tts(channel_id, greeting)
                
        except Exception as e:
            logger.error(f"Call handling error: {e}")
    
    async def answer_channel(self, channel_id):
        """Answer the channel via ARI"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            url = f"{ARI_URL}/channels/{channel_id}/answer"
            async with session.post(url) as resp:
                if resp.status == 204:
                    logger.info(f"✅ Channel answered [{channel_id}]")
                else:
                    logger.error(f"Failed to answer: {resp.status}")
    
    async def create_external_media(self, channel_id, rtp_port):
        """Create External Media channel via ARI"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            # Get local IP
            local_ip = self.get_local_ip()
            
            # Create external media channel
            url = f"{ARI_URL}/channels/externalMedia"
            params = {
                'app': ARI_APP,
                'external_host': f'{local_ip}:{rtp_port}',
                'format': 'ulaw',  # G.711 μ-law
                'encapsulation': 'rtp',
                'transport': 'udp'
            }
            
            async with session.post(url, params=params) as resp:
                if resp.status in [200, 201]:
                    data = await resp.json()
                    external_channel_id = data['id']
                    logger.info(f"✅ External Media channel created: {external_channel_id}")
                    
                    # Bridge with original channel
                    await self.bridge_channels(channel_id, external_channel_id)
                    
                    return external_channel_id
                else:
                    error = await resp.text()
                    logger.error(f"Failed to create External Media: {resp.status} - {error}")
                    return None
    
    async def bridge_channels(self, channel1_id, channel2_id):
        """Bridge two channels together"""
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        async with aiohttp.ClientSession(auth=auth) as session:
            # Create bridge
            bridge_url = f"{ARI_URL}/bridges"
            bridge_params = {'type': 'mixing'}
            
            async with session.post(bridge_url, params=bridge_params) as resp:
                if resp.status in [200, 201]:
                    bridge_data = await resp.json()
                    bridge_id = bridge_data['id']
                    
                    # Add both channels to bridge
                    for chan_id in [channel1_id, channel2_id]:
                        add_url = f"{ARI_URL}/bridges/{bridge_id}/addChannel"
                        add_params = {'channel': chan_id}
                        await session.post(add_url, params=add_params)
                    
                    logger.info(f"✅ Channels bridged: {channel1_id} <-> {channel2_id}")
    
    def get_local_ip(self):
        """Get local IP address"""
        # For local testing
        return "127.0.0.1"
    
    def find_available_port(self):
        """Find available RTP port"""
        return random.randint(RTP_PORT_START, RTP_PORT_END)
    
    async def setup_speech_configs(self, channel_id):
        """Setup TTS and STT configs"""
        session = self.sessions[channel_id]
        
        # TTS config
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # Get voice from assistant settings
        voice = "de-DE-SeraphinaMultilingualNeural"
        assistant = session.get('assistant')
        if assistant and isinstance(assistant.get('settings'), dict):
            v = assistant['settings'].get('providers', {}).get('tts', {}).get('voice')
            if v and isinstance(v, str):
                voice = v.strip('"')
        
        tts_config.speech_synthesis_voice_name = voice
        session['tts_config'] = tts_config
        
        # STT config
        stt_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        stt_config.speech_recognition_language = "de-DE"
        session['stt_config'] = stt_config
    
    async def handle_rtp(self, channel_id):
        """Handle RTP packets for a channel"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        
        rtp_socket = session['rtp_socket']
        loop = asyncio.get_event_loop()
        
        while channel_id in self.sessions:
            try:
                # Receive RTP packet
                data, addr = await loop.sock_recvfrom(rtp_socket, 2048)
                
                if len(data) > 12:  # RTP header is 12 bytes
                    # Parse RTP header
                    payload = data[12:]  # Skip RTP header
                    
                    # Add to audio buffer
                    session['audio_buffer'].extend(payload)
                    
                    # Simple VAD
                    if len(payload) >= 160:
                        # G.711 to PCM for energy calculation
                        pcm = self.g711_to_pcm(payload)
                        energy = np.sqrt(np.mean(np.array(pcm) ** 2))
                        
                        if energy > 200:
                            session['silence_count'] = 0
                        else:
                            session['silence_count'] += 1
                    
                    # Process after silence
                    if (len(session['audio_buffer']) > 4800 and 
                        session['silence_count'] > 30 and 
                        not session['processing']):
                        
                        session['processing'] = True
                        audio = bytes(session['audio_buffer'])
                        session['audio_buffer'] = bytearray()
                        
                        # Process speech
                        asyncio.create_task(self.process_speech(channel_id, audio))
                    
                    # Prevent overflow
                    if len(session['audio_buffer']) > 80000:
                        session['audio_buffer'] = session['audio_buffer'][-40000:]
                        
            except BlockingIOError:
                await asyncio.sleep(0.001)
            except Exception as e:
                if channel_id in self.sessions:
                    logger.error(f"RTP error: {e}")
                break
    
    def g711_to_pcm(self, g711_data):
        """Convert G.711 μ-law to PCM"""
        pcm = []
        for byte in g711_data:
            # μ-law to PCM (simplified)
            byte = ~byte
            sign = (byte & 0x80)
            exponent = (byte >> 4) & 0x07
            mantissa = byte & 0x0F
            sample = mantissa << (exponent + 3)
            if sign == 0:
                sample = -sample
            pcm.append(sample)
        return pcm
    
    def pcm_to_g711(self, pcm_data):
        """Convert PCM to G.711 μ-law"""
        g711 = bytearray()
        
        # Process 16-bit PCM samples
        for i in range(0, len(pcm_data), 2):
            if i + 1 < len(pcm_data):
                sample = struct.unpack('<h', pcm_data[i:i+2])[0]
                
                # PCM to μ-law (simplified)
                if sample < 0:
                    sample = -sample
                    sign = 0x80
                else:
                    sign = 0
                
                # Simple compression
                if sample > 32767:
                    sample = 32767
                compressed = (sample >> 8) & 0x7F
                g711.append(sign | compressed)
        
        return bytes(g711)
    
    async def process_speech(self, channel_id, audio):
        """Process speech with STT and generate response"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        
        try:
            # Convert G.711 to PCM for STT
            pcm_audio = bytearray()
            for byte in audio:
                # μ-law to 16-bit PCM (simplified)
                pcm_audio.extend(struct.pack('<h', (byte - 128) * 256))
            
            # STT
            stream = speechsdk.audio.PushAudioInputStream(
                stream_format=speechsdk.audio.AudioStreamFormat(
                    samples_per_second=8000,
                    bits_per_sample=16,
                    channels=1,
                    wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.PCM
                )
            )
            stream.write(bytes(pcm_audio))
            stream.close()
            
            audio_config = speechsdk.audio.AudioConfig(stream=stream)
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=session['stt_config'],
                audio_config=audio_config
            )
            
            result = recognizer.recognize_once()
            
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = result.text
                logger.info(f"🎤 User: {text}")
                
                # Generate response
                if "ja" in text.lower():
                    response = "Vielen Dank! Wie kann ich Ihnen helfen?"
                elif "nein" in text.lower():
                    response = "Verstanden. Kann ich trotzdem helfen?"
                else:
                    response = "Ja, verstehe. Was möchten Sie noch wissen?"
                
                # Send response
                await self.send_tts(channel_id, response)
                
        except Exception as e:
            logger.error(f"Process error: {e}")
        finally:
            session['processing'] = False
    
    async def send_tts(self, channel_id, text):
        """Generate and send TTS via RTP"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        
        try:
            # Generate TTS
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=session['tts_config'],
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Convert PCM to G.711
                g711_audio = self.pcm_to_g711(result.audio_data)
                
                # Send via RTP
                rtp_socket = session['rtp_socket']
                packet_size = 160  # 20ms at 8kHz
                ssrc = random.randint(1000000, 9999999)
                seq = 0
                timestamp = 0
                
                # Get destination from first received packet (simplified)
                # In production, track the source address properly
                dest_addr = ('127.0.0.1', session['rtp_port'] + 1)  # Simplified
                
                for i in range(0, len(g711_audio), packet_size):
                    chunk = g711_audio[i:i+packet_size]
                    
                    # Create RTP header
                    header = struct.pack('!BBHII',
                        0x80,  # V=2, P=0, X=0, CC=0
                        0x00,  # M=0, PT=0 (PCMU)
                        seq,
                        timestamp,
                        ssrc
                    )
                    
                    # Send RTP packet
                    rtp_socket.sendto(header + chunk, dest_addr)
                    
                    seq = (seq + 1) & 0xFFFF
                    timestamp += packet_size
                    
                    await asyncio.sleep(0.02)  # 20ms pacing
                
                logger.info(f"🔊 Sent TTS: {text[:50]}...")
                
        except Exception as e:
            logger.error(f"TTS error: {e}")
    
    async def start(self):
        """Start the ARI External Media server"""
        await self.init_db()
        
        logger.info("=" * 50)
        logger.info("🚀 ARI External Media Voice AI")
        logger.info("✅ Using Asterisk 22 LTS")
        logger.info("✅ ExternalMedia via ARI REST API")
        logger.info("✅ RTP streaming support")
        logger.info("=" * 50)
        
        # Connect to ARI WebSocket
        await self.connect_websocket()

if __name__ == "__main__":
    app = ARIExternalMedia()
    asyncio.run(app.start())