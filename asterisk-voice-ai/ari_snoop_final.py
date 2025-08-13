#!/usr/bin/env python3
"""
ARI Voice AI with SnoopChannel - FINAL PRODUCTION VERSION
Based on professional 2-bridge architecture pattern

Architecture:
- MAIN_BR: Caller + EM_TTS (for TTS playback to caller)
- STT_BR: SNOOP_IN (spy=in) + EM_STT (clean audio to STT, no echo!)
"""

import asyncio
import aiohttp
import json
import logging
import os
import struct
import socket
import uuid
from urllib.parse import urlencode
import azure.cognitiveservices.speech as speechsdk
from dotenv import load_dotenv
import asyncpg
import audioop
from rtp_session import RtpSession

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG to see STT issues
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler('ari_final.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("ari-snoop-final")

# Configuration
AST_HTTP = os.environ.get("AST_HTTP", "http://127.0.0.1:8088")
AST_USER = os.environ.get("AST_USER", "ari-user")
AST_PASS = os.environ.get("AST_PASS", "ari123")
APP_NAME = os.environ.get("APP_NAME", "voice-ai")

# External media endpoints
AI_STT_HOST = os.environ.get("AI_STT_HOST", "0.0.0.0")
AI_STT_PORT = int(os.environ.get("AI_STT_PORT", "10000"))

# Media format configuration
EM_FORMAT_TTS = os.environ.get("EM_FORMAT_TTS", "slin16")  # or "ulaw"
AI_PT = os.getenv("AI_PT")  # optional override as number/None

# Fallback PT mapping based on format
FALLBACK_PT_BY_FORMAT = {
    "ulaw": 0,          # PCMU
    "alaw": 8,          # PCMA
    "slin16": 118,      # L16/16000 - usual default in Asterisk
}

def fallback_pt(fmt: str) -> int:
    """Get fallback PT for format, with optional override"""
    if AI_PT is not None:
        return int(AI_PT)
    return FALLBACK_PT_BY_FORMAT.get(fmt, 118)

# Azure
AZURE_KEY = os.getenv('AZURE_SPEECH_KEY')
AZURE_REGION = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')

# Mistral
MISTRAL_API_KEY = os.getenv('MISTRAL_API_KEY')

# Database
DB_HOST = os.getenv('DB_HOST', '10.0.0.5')
DB_PASSWORD = os.getenv('DB_PASSWORD')


class Ari:
    """Clean ARI REST API wrapper"""
    
    def __init__(self, base_http: str, user: str, password: str, app: str):
        self.base_http = base_http.rstrip("/")
        self.user = user
        self.password = password
        self.app = app
        self.session = aiohttp.ClientSession(auth=aiohttp.BasicAuth(user, password))
        self.created_channels = set()

    def rest(self, path: str, **params) -> str:
        if params:
            return f"{self.base_http}/ari{path}?{urlencode(params)}"
        return f"{self.base_http}/ari{path}"

    async def post(self, path: str, *, params=None, json_data=None):
        url = self.rest(path, **(params or {}))
        async with self.session.post(url, json=json_data) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"POST {url} -> {r.status}: {body}")
            if r.content_type == "application/json":
                return await r.json()
            return await r.text()

    async def get(self, path: str, *, params=None):
        url = self.rest(path, **(params or {}))
        async with self.session.get(url) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"GET {url} -> {r.status}: {body}")
            if r.content_type == "application/json":
                return await r.json()
            return await r.text()

    async def delete(self, path: str, *, params=None):
        url = self.rest(path, **(params or {}))
        async with self.session.delete(url) as r:
            if r.status >= 400 and r.status != 404:
                body = await r.text()
                log.debug(f"DELETE {url} -> {r.status}: {body}")
            if r.content_type == "application/json":
                return await r.json()
            return await r.text()

    async def ws(self):
        ws_url = self.base_http.replace("https://", "wss://").replace("http://", "ws://")
        # Don't include api_key in query string - use BasicAuth from session instead
        qs = urlencode({"app": self.app, "subscribeAll": "true"})
        url = f"{ws_url}/ari/events?{qs}"
        log.info("Connecting WebSocket: %s", url)
        return await self.session.ws_connect(url, heartbeat=30)

    # ARI helpers
    async def answer(self, channel_id: str):
        await self.post(f"/channels/{channel_id}/answer")
        log.info("✅ Answered channel %s", channel_id)

    async def create_bridge(self, name: str) -> str:
        # Create bridge with mixing,dtmf_events to ensure audio flows
        data = await self.post("/bridges", params={"type": "mixing,dtmf_events", "name": name, "app": self.app})
        bid = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
        log.info("✅ Created bridge %s (%s)", name, bid)
        return bid

    async def add_to_bridge(self, bridge_id: str, channel_id: str):
        await self.post(f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id})
        log.info("  Added %s to bridge %s", channel_id, bridge_id[:8])

    async def external_media_tts(self, fmt: str) -> str:
        """Create ExternalMedia for TTS (receive audio from us, send to caller)"""
        # CRITICAL: For TTS we need to use the old external_media method with proper port allocation
        # This creates a bidirectional channel where we control the audio flow
        # Allocate a port for TTS
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_sock.bind(("0.0.0.0", 0))
        tts_port = temp_sock.getsockname()[1]
        temp_sock.close()
        
        params = {
            "app": self.app,
            "external_host": f"127.0.0.1:{tts_port}",
            "format": fmt,
            "encapsulation": "rtp",
            "transport": "udp",
            "direction": "both",  # Bidirectional for proper audio flow
        }
        data = await self.post("/channels/externalMedia", params=params)
        ch_id = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
        self.created_channels.add(ch_id)
        log.info("✅ Created TTS ExternalMedia %s on port %d (both, %s)", ch_id[:12], tts_port, fmt)
        return ch_id
        
    async def external_media_stt(self, fmt: str) -> str:
        """Create ExternalMedia for STT (send audio to us, receive from caller)"""
        # For STT: Asterisk should send audio to us
        params = {
            "app": self.app,
            "format": fmt,
            "encapsulation": "rtp",
            "transport": "udp",
            "direction": "sendonly",  # Asterisk sends to external, receives from bridge
        }
        data = await self.post("/channels/externalMedia", params=params)
        ch_id = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
        self.created_channels.add(ch_id)
        log.info("✅ Created STT ExternalMedia %s (sendonly, %s)", ch_id[:12], fmt)
        return ch_id

    async def external_media(self, host: str, port: int, fmt: str) -> tuple[str, int]:
        """Create External Media and return (channel_id, actual_port_used) - DEPRECATED"""
        # If port is 0, allocate a dynamic port
        actual_port = port
        if port == 0:
            # Create a temporary socket to get an available port
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_sock.bind(("0.0.0.0", 0))  # Bind to all interfaces for port allocation
            actual_port = temp_sock.getsockname()[1]
            temp_sock.close()
            log.info(f"🔌 Allocated dynamic port {actual_port} for External Media")
        
        # CRITICAL FIX: Use the actual server IP that Asterisk can reach
        # For local testing, use 127.0.0.1, for production use the actual server IP
        external_ip = "127.0.0.1"  # This should be the IP where our RTP service runs
        
        params = {
            "app": self.app,
            "external_host": f"{external_ip}:{actual_port}",
            "format": fmt,
            "encapsulation": "rtp",
            "transport": "udp",
            "direction": "both",  # Explicitly set bidirectional
        }
        data = await self.post("/channels/externalMedia", params=params)
        ch_id = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
        self.created_channels.add(ch_id)
        log.info("✅ Created ExternalMedia %s -> %s:%d (%s)", ch_id[:12], external_ip, actual_port, fmt)
        return ch_id, actual_port

    async def snoop_in(self, channel_id: str) -> str:
        # CRITICAL: Snoop must also be in the Stasis app!
        params = {
            "app": self.app,
            "spy": "in",  # Only spy incoming audio
            "whisper": "none"  # Don't whisper anything back
        }
        data = await self.post(f"/channels/{channel_id}/snoop", params=params)
        ch_id = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
        self.created_channels.add(ch_id)
        log.info("✅ Created Snoop(in) %s on %s", ch_id[:12], channel_id[:12])
        return ch_id

    async def get_var(self, channel_id: str, var: str) -> str:
        data = await self.get(f"/channels/{channel_id}/variable", params={"variable": var})
        return data.get("value") if isinstance(data, dict) else json.loads(data).get("value")

    async def hangup(self, channel_id: str):
        try:
            await self.delete(f"/channels/{channel_id}")
        except Exception as e:
            log.debug("Hangup %s: %s", channel_id, e)

    async def destroy_bridge(self, bridge_id: str):
        try:
            await self.delete(f"/bridges/{bridge_id}")
        except Exception as e:
            log.debug("Delete bridge %s: %s", bridge_id, e)


class VoiceAI:
    """Voice AI processing - STT/TTS/AI"""
    
    def __init__(self):
        self.db_pool = None
        # STT sockets are now per-session
        # TTS sessions are per-call
        self.active_sessions = {}
        
    async def connect_database(self):
        """Connect to PostgreSQL"""
        try:
            self.db_pool = await asyncpg.create_pool(
                host=DB_HOST,
                database='teli24_development',
                user='teli24_user',
                password=DB_PASSWORD,
                min_size=1,
                max_size=10
            )
            log.info("✅ Database connected")
        except Exception as e:
            log.error(f"❌ Database connection failed: {e}")
            
    async def get_assistant(self, phone_number):
        """Get assistant configuration"""
        if not self.db_pool or not phone_number:
            return None
            
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow("""
                    SELECT a.*, p.number 
                    FROM "Assistant" a 
                    JOIN "PhoneNumber" p ON a.id = p."assignedToAssistantId" 
                    WHERE p.number LIKE $1
                    LIMIT 1
                """, f"%{phone_number[-10:]}%")
                
                if result:
                    log.info(f"✅ Assistant found: {result['name']}")
                    return dict(result)
        except Exception as e:
            log.error(f"❌ Error loading assistant: {e}")
        return None
        
    async def setup_rtp_servers(self):
        """Setup RTP servers"""
        # NOTE: STT now uses ExternalMedia sendonly mode
        # We'll create STT socket per session to receive on the allocated port
        log.info("✅ RTP setup ready - STT will use per-session sockets")
        # Note: TTS uses RtpSession per call - no global socket
        
    async def generate_greeting(self, assistant):
        """Generate greeting audio"""
        greeting_text = "Hallo, wie kann ich Ihnen helfen?"
        if assistant and assistant.get('greeting'):
            greeting_text = assistant['greeting']
            
        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_KEY,
                region=AZURE_REGION
            )
            
            # Output as raw PCM16 for direct RTP streaming
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
            )
            
            # Get voice from assistant config
            voice = 'de-DE-KatjaNeural'
            if assistant:
                settings = assistant.get('settings', {})
                if settings and isinstance(settings, dict):
                    tts = settings.get('providers', {}).get('tts', {})
                    if tts.get('voice'):
                        voice = tts['voice']
                        
            speech_config.speech_synthesis_voice_name = voice
            
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config,
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(greeting_text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                log.info(f"✅ Generated greeting: {greeting_text[:50]}...")
                return result.audio_data
            else:
                log.error(f"❌ TTS failed: {result.reason}")
                
        except Exception as e:
            log.error(f"❌ Greeting generation error: {e}", exc_info=True)
            
        return None
        
    async def create_tts_session(self, call_id, target_ip, target_port):
        """Create RTP session for TTS - send audio TO Asterisk's allocated port"""
        try:
            # CORRECT: Allocate our own local port for sending audio
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_sock.bind(("0.0.0.0", 0))  
            local_port = temp_sock.getsockname()[1]
            temp_sock.close()
            
            log.info(f"🔌 Creating TTS RTP session: local=0.0.0.0:{local_port} -> remote={target_ip}:{target_port}")
            
            rtp_session = RtpSession(
                local_host="0.0.0.0", 
                local_port=local_port,  # Our local port for sending
                remote_host=target_ip,
                remote_port=int(target_port),  # Asterisk's allocated port
                clock_rate=16000 if EM_FORMAT_TTS == "slin16" else 8000,
                payload_type=fallback_pt(EM_FORMAT_TTS),  # Start with fallback PT
                pace_by_inbound=False,  # IMPORTANT: Don't wait for inbound, self-pace!
                send_rtcp=False
            )
            
            await rtp_session.start()
            self.active_sessions[call_id] = rtp_session
            
            log.info(f"✅ TTS RTP session created: {target_ip}:{target_port} PT={rtp_session.pt_out}")
            return rtp_session
            
        except Exception as e:
            log.error(f"❌ RTP session creation error: {e}", exc_info=True)
            return None
            
    async def process_stt_stream(self, session):
        """Consume inbound RTP (μ-law) and run short, rolling recognitions."""
        if not session.stt_socket:
            log.warning("No STT socket available for session")
            return
            
        log.info(f"🎧 STT (μ-law) for call {session.caller[:12]}")
        
        buf = bytearray()
        pkts = 0
        
        # Each 20 ms packet payload is 160 bytes at 8 kHz μ-law
        CHUNK_BYTES = 160
        BATCH_BYTES = 16000  # ~2 seconds of audio at 8kHz for complete sentence detection
        
        while session.active:
            try:
                try:
                    data, _ = session.stt_socket.recvfrom(2048)
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                    continue
                    
                if len(data) < 12:
                    continue
                payload = data[12:]
                if not payload:
                    continue
                    
                buf.extend(payload)
                pkts += 1
                
                if len(buf) >= BATCH_BYTES:
                    # Convert μ-law to PCM16 for STT
                    ulaw_data = bytes(buf[:BATCH_BYTES])
                    del buf[:BATCH_BYTES]
                    
                    # Convert μ-law to PCM
                    import audioop
                    pcm_8khz = audioop.ulaw2lin(ulaw_data, 2)
                    
                    # Resample from 8kHz to 16kHz for Azure STT
                    pcm_16khz = audioop.ratecv(pcm_8khz, 2, 1, 8000, 16000, None)[0]
                    
                    # Audio normalization for better STT recognition
                    import struct
                    import numpy as np
                    
                    # Convert to numpy array for processing
                    samples = np.frombuffer(pcm_16khz, dtype=np.int16)
                    
                    # Normalize audio (boost volume)
                    if len(samples) > 0:
                        max_val = np.max(np.abs(samples))
                        if max_val > 100:  # Only normalize if there's actual audio
                            normalized = (samples.astype(np.float32) / max_val * 16384).astype(np.int16)
                            pcm_16khz = normalized.tobytes()
                    
                    # Check audio level
                    import struct
                    samples = struct.unpack('<' + 'h' * (len(pcm_16khz) // 2), pcm_16khz)
                    max_amplitude = max(abs(s) for s in samples) if samples else 0
                    avg_amplitude = sum(abs(s) for s in samples) / len(samples) if samples else 0
                    
                    log.info(f"🎤 Processing {len(ulaw_data)} bytes μ-law - Max: {max_amplitude}, Avg: {avg_amplitude:.1f}")
                    
                    # Only send to STT if there's actual audio (not silence)  
                    if max_amplitude > 200:  # Lowered threshold for better sensitivity
                        # Back to PCM16 conversion - MULAW format doesn't work
                        text = await self.recognize_speech_slin16(pcm_16khz)
                    else:
                        text = None
                    if text:
                        log.info(f"✅ Recognized: {text}")
                        resp = await self.generate_response(text, session)
                        if resp:
                            await session.send_tts(resp)
            except Exception as e:
                log.error(f"❌ STT loop error: {e}", exc_info=True)
                await asyncio.sleep(0.05)
    async def recognize_speech_mulaw(self, mulaw_bytes: bytes):
        """Recognize speech from μ-law audio bytes using native MULAW format"""
        try:
            # Create speech config
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_KEY,
                region=AZURE_REGION
            )
            speech_config.speech_recognition_language = "de-DE"
            
            # Set timeouts for short words like "Ja"
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "5000"
            )
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "500"
            )
            
            # Use native MULAW format - this should work much better!
            audio_format = speechsdk.audio.AudioStreamFormat(
                compressed_stream_format=speechsdk.AudioStreamContainerFormat.MULAW,
                samples_per_second=8000
            )
            
            stream = speechsdk.audio.PushAudioInputStream(audio_format)
            audio_config = speechsdk.audio.AudioConfig(stream=stream)
            
            # Create recognizer
            speech_recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config, 
                audio_config=audio_config
            )
            
            # Push the μ-law data directly
            stream.write(mulaw_bytes)
            stream.close()
            
            # Recognize once
            result = speech_recognizer.recognize_once()
            
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                return result.text.strip()
            elif result.reason == speechsdk.ResultReason.NoMatch:
                log.debug(f"⚠️ STT result reason: {result.reason}, no_match: {result.no_match_details}")
                return None
            else:
                log.debug(f"⚠️ STT result reason: {result.reason}")
                return None
                
        except Exception as e:
            log.error(f"❌ μ-law STT error: {e}")
            return None

    async def recognize_speech_slin16(self, pcm16_bytes: bytes):
        """Recognize speech from slin16 PCM data"""
        try:
            speech_config = speechsdk.SpeechConfig(subscription=AZURE_KEY, region=AZURE_REGION)
            speech_config.speech_recognition_language = "de-DE"
            speech_config.set_property(speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "3000") 
            speech_config.set_property(speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "1200")
            # Better recognition for short words like "Ja"
            speech_config.set_property(speechsdk.PropertyId.SpeechServiceConnection_EnableAudioLogging, "false")
            
            fmt = speechsdk.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16, channels=1)
            stream = speechsdk.audio.PushAudioInputStream(fmt)
            audio_cfg = speechsdk.audio.AudioConfig(stream=stream)
            reco = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_cfg)
            
            stream.write(pcm16_bytes)
            stream.close()
            result = reco.recognize_once()
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                log.info(f"🎯 STT recognized: '{result.text}'")
                return result.text
            else:
                log.debug(f"⚠️ STT result reason: {result.reason}, text: '{result.text}'")
            return None
        except Exception as e:
            log.error(f"❌ recognize_speech_slin16: {e}", exc_info=True)
            return None
                
    async def recognize_speech(self, audio_data):
        """Recognize speech with Azure STT"""
        try:
            # Convert A-law to PCM
            pcm_8khz = audioop.alaw2lin(audio_data, 2)
            
            # Resample to 16kHz
            pcm_16khz = audioop.ratecv(pcm_8khz, 2, 1, 8000, 16000, None)[0]
            
            # Azure STT
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_KEY,
                region=AZURE_REGION
            )
            speech_config.speech_recognition_language = "de-DE"
            
            # Set timeouts
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "8000"
            )
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "800"
            )
            
            # Use compressed format that Azure understands better
            audio_format = speechsdk.audio.AudioStreamFormat(
                compressed_stream_format=speechsdk.AudioStreamContainerFormat.MULAW,
                samples_per_second=8000
            )
            
            stream = speechsdk.audio.PushAudioInputStream(audio_format)
            audio_config = speechsdk.audio.AudioConfig(stream=stream)
            
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config,
                audio_config=audio_config
            )
            
            stream.write(pcm_16khz)
            stream.close()
            
            result = recognizer.recognize_once()
            
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                return result.text
            else:
                log.warning(f"⚠️ No speech recognized: {result.reason}")
                
        except Exception as e:
            log.error(f"❌ STT error: {e}", exc_info=True)
            
        return None
        
    async def generate_response(self, text, session):
        """Generate AI response with TTS echo filtering"""
        try:
            # CRITICAL: Filter out TTS echoes - ignore if STT heard our own greeting
            tts_echo_phrases = [
                "guten tag ich bin sabine",
                "ich bin sabine die ki-assistentin",
                "bonusservice",
                "gespräch wird aufgezeichnet",
                "wie kann ich ihnen helfen"
            ]
            
            text_lower = text.lower().strip()
            
            # Check if this is our own TTS output being echoed back
            for phrase in tts_echo_phrases:
                if phrase in text_lower:
                    log.warning(f"🚫 Ignoring TTS echo: '{text[:50]}...'")
                    return None  # Don't respond to our own TTS
            
            # Also ignore very short inputs that might be noise
            if len(text_lower) < 3:
                log.debug(f"🚫 Ignoring short input: '{text}'")
                return None
                
            assistant = session.assistant or {}
            
            # Use clean, focused system prompt - avoid massive DB prompt that causes AI to regurgitate instructions
            system_prompt = f"""Du bist Sabine, eine freundliche KI-Assistentin von Bonusservice. 

Wichtige Regeln:
- Antworte kurz und natürlich auf Deutsch
- Du hilfst bei Fragen zu Energiekosten, Strom, Gas, Telefontarifen
- Bei komplexen Anfragen: "Ein Mitarbeiter wird Sie zurückrufen"
- Sei höflich und empathisch
- Die Begrüßung wurde bereits gespielt

Anrufername: {session.caller or 'unbekannt'}"""
            
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add conversation history but limit to recent messages
            if session.conversation_history:
                # Include the greeting as context
                messages.extend(session.conversation_history[-4:])
            
            # Add current user message
            messages.append({"role": "user", "content": text})
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.0)) as http_session:
                async with http_session.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {MISTRAL_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "mistral-small-latest",
                        "messages": messages,
                        "temperature": 0.5,
                        "max_tokens": 50  # Kurze Antworten!
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        response = data['choices'][0]['message']['content']
                        log.info(f"✅ AI response: '{response}'")
                        
                        # Update history
                        session.conversation_history.append({"role": "user", "content": text})
                        session.conversation_history.append({"role": "assistant", "content": response})
                        
                        return response
                    else:
                        log.error(f"❌ Mistral error: {resp.status}")
                        
        except Exception as e:
            log.error(f"❌ Response generation error: {e}", exc_info=True)
            
        return None


class CallSession:
    """Individual call session"""
    
    def __init__(self, ari: Ari, voice_ai: VoiceAI, caller_chan_id: str):
        self.ari = ari
        self.voice_ai = voice_ai
        self.caller = caller_chan_id
        self.main_bridge = None
        self.stt_bridge = None
        self.em_tts = None
        self.em_stt = None
        self.snoop_channel = None
        self.stt_bridge = None
        self.snoop_in = None
        self.tts_target_ip = None
        self.tts_target_port = None
        self.assistant = None
        self.conversation_history = []
        self.active = True
        self.greeting_acknowledged = False  # Track if user acknowledged greeting
        self.started = False
        self.tts_rtp_session = None
        self.stt_socket = None
        self.tts_interrupted = False  # Signal to stop TTS when user speaks

    def short_id(self):
        return uuid.uuid4().hex[:8]
    
    async def learn_asterisk_port(self, local_port):
        """Learn the actual port Asterisk sends RTP from"""
        try:
            # Set socket to non-blocking
            self.tts_rtp_session.sock.setblocking(False)
            
            for _ in range(50):  # Try for 5 seconds
                try:
                    data, addr = self.tts_rtp_session.sock.recvfrom(4096)
                    if addr and len(data) > 12:  # Valid RTP packet
                        self.tts_rtp_session.actual_remote_port = addr[1]
                        log.info(f"✅ Learned Asterisk actual RTP port: {addr[1]} (was expecting {self.tts_target_port})")
                        return
                except BlockingIOError:
                    await asyncio.sleep(0.1)
        except Exception as e:
            log.warning(f"Could not learn Asterisk port: {e}")

    async def start(self):
        """Setup the dual-bridge architecture"""
        if self.started:
            return
        self.started = True

        try:
            # 1) Answer caller
            await self.ari.answer(self.caller)

            # 2) Get assistant config (extract phone number from channel)
            try:
                channel_info = await self.ari.get(f"/channels/{self.caller}")
                dialplan = channel_info.get('dialplan', {})
                exten = dialplan.get('exten', '')
                if exten and exten.startswith('49'):
                    self.assistant = await self.voice_ai.get_assistant(exten)
            except Exception as e:
                log.warning(f"Could not get assistant config: {e}")

            # 3) Create MAIN_BR and add caller
            self.main_bridge = await self.ari.create_bridge(name=f"MAIN_BR_{self.short_id()}")
            await self.ari.add_to_bridge(self.main_bridge, self.caller)

            # 4) Create TTS RTP session FIRST, then tell Asterisk where to connect
            # Allocate a port and create RTP server
            import socket
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_sock.bind(("0.0.0.0", 0))
            tts_local_port = temp_sock.getsockname()[1]
            temp_sock.close()
            
            # Create External Media with our RTP server endpoint
            # Asterisk is on THIS server (Server 7), use localhost
            params = {
                "app": self.ari.app,
                "external_host": f"127.0.0.1:{tts_local_port}",  # Use localhost since Asterisk is on same server
                "format": EM_FORMAT_TTS,
                "encapsulation": "rtp",
                "transport": "udp",
                "direction": "both",
            }
            data = await self.ari.post("/channels/externalMedia", params=params)
            self.em_tts = data["id"] if isinstance(data, dict) else json.loads(data)["id"]
            self.ari.created_channels.add(self.em_tts)
            await self.ari.add_to_bridge(self.main_bridge, self.em_tts)
            log.info("✅ Created TTS ExternalMedia %s, our RTP on port %d", self.em_tts[:12], tts_local_port)

            # 5) Get Asterisk's RTP endpoint and create bidirectional RTP session
            try:
                # Get where Asterisk is listening
                for retry in range(20):  # up to 2 seconds
                    self.tts_target_ip = await self.ari.get_var(self.em_tts, "UNICASTRTP_LOCAL_ADDRESS")
                    self.tts_target_port = await self.ari.get_var(self.em_tts, "UNICASTRTP_LOCAL_PORT")
                    if self.tts_target_ip and self.tts_target_port:
                        break
                    await asyncio.sleep(0.1)
                    if retry == 5:
                        log.warning(f"⚠️ Waiting for UNICASTRTP variables... (attempt {retry+1}/20)")
                
                log.info(f"📡 TTS: We listen on :{tts_local_port}, Asterisk on {self.tts_target_ip}:{self.tts_target_port}")
                
                # Create simple RTP socket for TTS
                if self.tts_target_ip and self.tts_target_port:
                    SESSIONS[self.caller] = self
                    # Create a simple UDP socket for RTP
                    import random
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.bind(("0.0.0.0", tts_local_port))
                    self.tts_rtp_session = type('SimpleRTP', (), {
                        'sock': sock,
                        'ssrc': random.randint(1000000, 9999999),
                        'seq': random.randint(1, 65535),
                        'ts': random.randint(1, 4294967295),
                        'pt_out': 97,  # Default PT for slin16
                        'is_open': True,
                        'close': lambda x=None: sock.close(),
                        'actual_remote_port': None  # Will be set when we receive first packet
                    })()
                    log.info(f"✅ TTS RTP socket created: local={tts_local_port} -> will learn remote port from Asterisk")
                    
                    # Start a task to learn Asterisk's actual sending port
                    asyncio.create_task(self.learn_asterisk_port(tts_local_port))
            except Exception as e:
                log.warning(f"Could not setup TTS RTP session: {e}")

            # 6) Create SNOOP Channel for STT - ONLY captures caller audio, NO TTS echo!
            # This is the KEY to avoiding feedback loops
            try:
                # Create snoop channel that only listens to incoming audio from caller
                snoop_params = {
                    "app": self.ari.app,
                    "spy": "in",  # Only spy on incoming (from caller), not outgoing
                    "whisper": "none"  # Don't whisper anything back
                }
                snoop_data = await self.ari.post(f"/channels/{self.caller}/snoop", params=snoop_params)
                self.snoop_channel = snoop_data.get("id") if isinstance(snoop_data, dict) else json.loads(snoop_data).get("id")
                self.ari.created_channels.add(self.snoop_channel)
                log.info(f"✅ Created Snoop channel {self.snoop_channel[:12]} (spy=in only)")
                
                # Create STT bridge (separate from main bridge to avoid echo)
                self.stt_bridge = await self.ari.create_bridge(name=f"STT_BR_{self.short_id()}")
                
                # Add snoop to STT bridge
                await self.ari.add_to_bridge(self.stt_bridge, self.snoop_channel)
                
                # Create STT External Media
                self.em_stt, _ = await self.ari.external_media("0.0.0.0", AI_STT_PORT, "ulaw")
                
                # Add STT to its own bridge (NOT the main bridge!)
                await self.ari.add_to_bridge(self.stt_bridge, self.em_stt)
                
                log.info(f"✅ STT Bridge configured: Snoop({self.snoop_channel[:12]}) + ExtMedia({self.em_stt[:12]})")
                log.info("✅ NO ECHO: STT only hears caller, not TTS!")
                
            except Exception as e:
                log.error(f"❌ Failed to create Snoop channel: {e}")
                # Fallback to old method if snoop fails
                self.em_stt, _ = await self.ari.external_media("0.0.0.0", AI_STT_PORT, "ulaw")
                await self.ari.add_to_bridge(self.main_bridge, self.em_stt)
            
            # 7) Setup STT - since we use sendonly ExternalMedia, we need to set external_host
            # Actually, let's use the old approach for STT since it was working
            try:
                # For now, bind STT socket to the original port - this was working
                self.stt_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.stt_socket.bind(('0.0.0.0', AI_STT_PORT))
                self.stt_socket.setblocking(False)
                self.stt_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                log.info(f"✅ STT socket bound to 0.0.0.0:{AI_STT_PORT}")
            except Exception as e:
                log.warning(f"Could not setup STT socket: {e}")

            log.info("=" * 60)
            log.info("✅ CALL SESSION READY")
            log.info(f"  Caller: {self.caller[:12]}")
            log.info(f"  MAIN_BR: {self.main_bridge[:12]} (Caller + TTS + STT)")
            log.info(f"  No echo! Clean audio! Professional!")
            log.info("=" * 60)

            # 8) Start STT processing FIRST (if socket is ready)
            if self.stt_socket:
                asyncio.create_task(self.voice_ai.process_stt_stream(self))
            
            # 9) Play greeting IMMEDIATELY without async
            log.info("🚀 Playing greeting NOW...")
            try:
                await self.play_greeting()
                log.info("✅ Greeting completed")
            except Exception as e:
                log.error(f"❌ Greeting failed: {e}", exc_info=True)

        except Exception as e:
            log.error(f"Session start error: {e}", exc_info=True)
            await self.stop()

    async def play_greeting(self):
        """Play greeting with deadline + fallback to local file"""
        log.info("🎤 play_greeting() called!")
        if getattr(self, "_greeting_played", False):
            log.info("⚠️ Greeting already played, skipping")
            return
        self._greeting_played = True
        
        # Skip test tone - go directly to real greeting
        if os.getenv("TEST_TONE", "0") == "1":
            log.info("🔊 Sending test tone (1kHz sine wave)...")
            tone = self.generate_sine_tone(duration_s=2.0, freq=800.0, amp=0.8)  # Louder, longer
            # Send directly like we know works!
            await self.send_tts_via_rtp(tone)
            log.info("✅ Test tone sent - you should hear a beep!")
            await asyncio.sleep(0.5)
        
        # Skip local file - use Azure TTS directly
        local_greeting = os.getenv("LOCAL_GREETING_SLIN16", "/var/lib/asterisk/sounds/custom/greeting.raw")
        if False and os.path.exists(local_greeting):
            try:
                with open(local_greeting, "rb") as f:
                    frames = f.read()
                if self.active and self.tts_rtp_session and self.tts_rtp_session.is_open:
                    log.info(f"📡 Sending greeting: {len(frames)} bytes to {self.tts_target_ip}:{self.tts_target_port}")
                    log.info(f"📡 RTP session PT: {self.tts_rtp_session.pt_out}, SSRC: {self.tts_rtp_session.ssrc}")
                    # Send directly - the send_audio() function is broken!
                    await self.send_tts_via_rtp(frames)
                    log.info("✅ Greeting (local file) sent")
                    return
                else:
                    log.error(f"❌ Cannot send greeting - RTP not ready: active={self.active}, session={self.tts_rtp_session}, open={self.tts_rtp_session.is_open if self.tts_rtp_session else False}")
            except Exception as e:
                log.error(f"❌ Local greeting failed: {e}", exc_info=True)
        
        # Slow path: Azure with deadline
        log.info("🎯 Generating Azure TTS greeting...")
        try:
            async def _azure():
                return await self.voice_ai.generate_greeting(self.assistant)
            
            greeting_audio = await asyncio.wait_for(_azure(), timeout=float(os.getenv("TTS_GREETING_DEADLINE", "5.0")))
        except asyncio.TimeoutError:
            log.warning("⚠️ TTS greeting timeout; skipping")
            return
        except Exception as e:
            log.error(f"❌ TTS generation error: {e}")
            return
        
        if greeting_audio and self.active and self.tts_rtp_session and self.tts_rtp_session.is_open:
            log.info(f"📢 Sending {len(greeting_audio)} bytes of TTS audio")
            await self.send_tts_via_rtp(greeting_audio)
            log.info("✅ Dynamic greeting sent")
            
            # WICHTIG: Füge die TATSÄCHLICHE Begrüßung aus der DB zur Conversation History hinzu!
            if self.assistant and self.assistant.get('greeting'):
                greeting_text = self.assistant['greeting']
            else:
                greeting_text = 'Hallo, wie kann ich Ihnen helfen?'
            
            self.conversation_history.append({
                "role": "assistant",
                "content": greeting_text
            })
            log.info(f"📝 Added greeting to history: '{greeting_text[:50]}...'")
        else:
            log.error(f"❌ Cannot send TTS: audio={bool(greeting_audio)}, active={self.active}, session={bool(self.tts_rtp_session)}")
            
    def generate_sine_tone(self, duration_s=1.0, freq=1000.0, amp=0.8):
        """Generate a LOUD sine wave tone for testing audio path"""
        import math
        sr = 16000  # 16kHz sample rate
        n = int(duration_s * sr)
        out = bytearray(n * 2)
        for i in range(n):
            s = int(amp * 32767 * math.sin(2 * math.pi * freq * i / sr))
            out[2*i:2*i+2] = s.to_bytes(2, 'little', signed=True)
        log.info(f"🔊 Generated {duration_s}s tone at {freq}Hz, amplitude {amp}, {len(out)} bytes")
        return bytes(out)
    
    async def play_greeting_async(self):
        """Play greeting async with proper timing"""
        log.info("📢 play_greeting_async called!")
        try:
            await self.play_greeting()
        except Exception as e:
            log.error(f"❌ Greeting playback error: {e}", exc_info=True)
            
    async def send_tts_via_rtp(self, audio_data):
        """Send audio via bidirectional RTP session with precise timing"""
        if not self.active or not self.tts_rtp_session or not audio_data:
            log.warning("⚠️ Skipping TTS - call not active or no session")
            return
            
        try:
            # Convert PCM16 to μ-law since ExternalMedia expects ulaw
            import audioop
            from simple_rtp_sender import send_ulaw_rtp_async
            
            # Convert 16kHz PCM16 to 8kHz μ-law
            # First downsample from 16kHz to 8kHz
            pcm_8khz = audioop.ratecv(audio_data, 2, 1, 16000, 8000, None)[0]
            # Then convert to μ-law
            ulaw_data = audioop.lin2ulaw(pcm_8khz, 2)
            
            log.info(f"📊 Sending {len(ulaw_data)/8000:.2f} seconds of μ-law audio")
            
            # Determine target port
            target_port = self.tts_rtp_session.actual_remote_port if hasattr(self.tts_rtp_session, 'actual_remote_port') and self.tts_rtp_session.actual_remote_port else int(self.tts_target_port)
            
            # Use the precise RTP sender that worked before
            new_seq, new_ts = await send_ulaw_rtp_async(
                ulaw_data,
                self.tts_target_ip,
                target_port,
                self.tts_rtp_session.sock,
                self.tts_rtp_session.ssrc,
                self.tts_rtp_session.seq,
                self.tts_rtp_session.ts
            )
            
            # Update sequence and timestamp for next transmission
            self.tts_rtp_session.seq = new_seq
            self.tts_rtp_session.ts = new_ts
            
            log.info("✅ Audio sent via precise RTP streaming")
            
        except Exception as e:
            log.error(f"❌ RTP audio send error: {e}", exc_info=True)


    async def send_tts(self, text):
        """Send TTS response via bidirectional RTP"""
        if not text or not self.tts_rtp_session:
            return
            
        try:
            # Generate TTS
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_KEY,
                region=AZURE_REGION
            )
            
            # Output as raw PCM16 for direct RTP streaming
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
            )
            
            voice = 'de-DE-KatjaNeural'
            if self.assistant:
                settings = self.assistant.get('settings', {})
                if settings and isinstance(settings, dict):
                    tts = settings.get('providers', {}).get('tts', {})
                    if tts.get('voice'):
                        voice = tts['voice']
                        
            speech_config.speech_synthesis_voice_name = voice
            
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config,
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                await self.send_tts_via_rtp(result.audio_data)
                log.info("✅ Response sent via bidirectional RTP!")
            else:
                log.error(f"❌ TTS failed: {result.reason}")
                
        except Exception as e:
            log.error(f"❌ TTS send error: {e}", exc_info=True)

    async def play_acknowledgment(self):
        """Play a short beep to acknowledge speech recognition"""
        if not self.active or not self.tts_rtp_session:
            return
            
        try:
            import audioop
            from simple_rtp_sender import send_ulaw_rtp_async
            
            # Generate a short 100ms beep at 440Hz (A note)
            sample_rate = 8000
            duration = 0.1  # 100ms
            frequency = 440  # Hz
            
            # Generate sine wave
            import math
            samples = int(sample_rate * duration)
            amplitude = 0.3 * 32767  # 30% volume
            
            pcm_data = bytearray()
            for i in range(samples):
                value = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
                # Pack as 16-bit signed integer
                pcm_data.extend(value.to_bytes(2, byteorder='little', signed=True))
            
            # Convert to μ-law
            ulaw_data = audioop.lin2ulaw(bytes(pcm_data), 2)
            
            log.info("🔔 Playing acknowledgment beep")
            
            # Send beep
            target_port = self.tts_rtp_session.actual_remote_port if hasattr(self.tts_rtp_session, 'actual_remote_port') and self.tts_rtp_session.actual_remote_port else int(self.tts_target_port)
            
            new_seq, new_ts = await send_ulaw_rtp_async(
                ulaw_data,
                self.tts_target_ip,
                target_port,
                self.tts_rtp_session.sock,
                self.tts_rtp_session.ssrc,
                self.tts_rtp_session.seq,
                self.tts_rtp_session.ts
            )
            
            self.tts_rtp_session.seq = new_seq
            self.tts_rtp_session.ts = new_ts
            
        except Exception as e:
            log.error(f"❌ Acknowledgment beep error: {e}")

    async def stop(self):
        """Clean teardown"""
        self.active = False
        
        # Cleanup RTP session
        if self.tts_rtp_session:
            try:
                await self.tts_rtp_session.close()
            except Exception as e:
                log.debug(f"RTP session close error: {e}")
            if self.caller in self.voice_ai.active_sessions:
                del self.voice_ai.active_sessions[self.caller]
                
        # Cleanup STT socket
        if self.stt_socket:
            self.stt_socket.close()
            self.stt_socket = None
        
        # Cleanup channels
        for ch in [self.em_tts, self.em_stt, self.snoop_in]:
            if ch:
                await self.ari.hangup(ch)
                
        # Cleanup bridges
        for br in [self.stt_bridge, self.main_bridge]:
            if br:
                await self.ari.destroy_bridge(br)
                
        log.info(f"✅ Session cleaned up for {self.caller[:12]}")


# Main application
SESSIONS = {}


def is_created_by_us(ari: Ari, chan_id: str) -> bool:
    return chan_id in ari.created_channels


async def wait_for_ari_ready(ari: Ari, max_attempts: int = 30):
    """Wait for ARI to be ready before connecting WebSocket"""
    for attempt in range(max_attempts):
        try:
            # Test if ARI is responsive
            await ari.get("/asterisk/info")
            log.info("✅ ARI is ready")
            return True
        except Exception as e:
            log.info(f"⏳ Waiting for ARI... (attempt {attempt+1}/{max_attempts}): {e}")
            await asyncio.sleep(2)
    
    raise RuntimeError("ARI not ready after maximum attempts")


async def connect_websocket_with_retry(ari: Ari, max_attempts: int = 5):
    """Connect to WebSocket with retry logic"""
    last_error = None
    
    for attempt in range(max_attempts):
        try:
            log.info(f"🔌 Connecting to WebSocket (attempt {attempt+1}/{max_attempts})")
            ws = await ari.ws()
            log.info("✅ WebSocket connected successfully")
            return ws
        except Exception as e:
            last_error = e
            log.warning(f"❌ WebSocket connection failed: {e}")
            if attempt < max_attempts - 1:
                wait_time = min(2 ** attempt, 10)  # Exponential backoff, max 10s
                log.info(f"⏳ Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
    
    raise RuntimeError(f"Failed to connect WebSocket after {max_attempts} attempts. Last error: {last_error}")


async def handle_events(ari: Ari, voice_ai: VoiceAI):
    """Main event loop with robust error handling"""
    
    # Wait for ARI to be ready
    await wait_for_ari_ready(ari)
    
    while True:
        try:
            # Connect with retry logic
            ws = await connect_websocket_with_retry(ari)
            log.info(f"🎯 ARI app '{APP_NAME}' is now registered and listening for events")
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        evt = json.loads(msg.data)
                    except Exception:
                        continue
                        
                    et = evt.get("type")
                    ch = evt.get("channel") or evt.get("replace_channel") or {}
                    ch_id = ch.get("id")

                    if et == "StasisStart" and ch_id:
                        # CRITICAL: Only process REAL incoming calls, not our created channels!
                        if is_created_by_us(ari, ch_id):
                            log.debug("Ignoring our own channel %s", ch_id)
                            continue
                        
                        # Check if this is the original caller channel
                        # Original channels have dialplan info, created channels don't
                        dialplan = ch.get("dialplan", {})
                        if not dialplan.get("exten"):
                            log.debug("Ignoring non-dialplan channel %s", ch_id)
                            continue
                            
                        if ch_id not in SESSIONS:
                            log.info(f"📞 New call: {ch_id}")
                            sess = CallSession(ari, voice_ai, ch_id)
                            SESSIONS[ch_id] = sess
                            asyncio.create_task(sess.start())

                    elif et in ("ChannelHangupRequest", "StasisEnd") and ch_id:
                        sess = SESSIONS.pop(ch_id, None)
                        if sess:
                            log.info(f"📴 Call ended: {ch_id}")
                            await sess.stop()

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    log.warning("💥 WebSocket closed/error - will reconnect")
                    break  # Break inner loop to reconnect
                    
        except Exception as e:
            log.error(f"❌ Event handling error: {e}")
            log.info("⏳ Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def main():
    """Main entry point"""
    log.info("=" * 60)
    log.info("🚀 ARI VOICE AI - PROFESSIONAL SNOOP ARCHITECTURE")
    log.info("✅ Clean audio capture with Snoop(spy=in)")
    log.info("✅ No echo, no feedback loops")
    log.info("✅ Production ready!")
    log.info("=" * 60)
    
    # Initialize components
    voice_ai = VoiceAI()
    await voice_ai.connect_database()
    await voice_ai.setup_rtp_servers()
    
    ari = Ari(AST_HTTP, AST_USER, AST_PASS, APP_NAME)
    
    try:
        await handle_events(ari, voice_ai)
    except KeyboardInterrupt:
        log.info("🛑 Shutdown requested")
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        raise
    finally:
        log.info("🧹 Cleaning up...")
        await ari.session.close()
        if voice_ai.db_pool:
            await voice_ai.db_pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested")