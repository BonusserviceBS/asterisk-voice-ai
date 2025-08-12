#!/usr/bin/env python3
"""
Real-time ARI Bridge with External Media
Actual audio streaming between Asterisk and Voice AI
"""

import asyncio
import aiohttp
import json
import logging
import base64
import struct
import socket
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "ari-user"
ARI_PASS = "ari123"
APP_NAME = "realtime_voice"
VOICE_AGENT_WS = "ws://127.0.0.1:8092/ws"

class RTPHandler:
    """Handle RTP audio packets"""
    
    def __init__(self, channel_id, voice_ws):
        self.channel_id = channel_id
        self.voice_ws = voice_ws
        self.socket = None
        self.sequence = 0
        self.timestamp = 0
        self.ssrc = 12345678
        
    def create_socket(self, port):
        """Create UDP socket for RTP"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('0.0.0.0', port))
        self.socket.setblocking(False)
        logger.info(f"RTP socket listening on port {port}")
        return self.socket
        
    def parse_rtp_packet(self, data):
        """Parse RTP packet and extract audio"""
        if len(data) < 12:
            return None
            
        # Parse RTP header
        header = struct.unpack('!BBHII', data[:12])
        version = (header[0] >> 6) & 0x3
        padding = (header[0] >> 5) & 0x1
        extension = (header[0] >> 4) & 0x1
        cc = header[0] & 0xF
        marker = (header[1] >> 7) & 0x1
        payload_type = header[1] & 0x7F
        sequence = header[2]
        timestamp = header[3]
        ssrc = header[4]
        
        # Calculate header size
        header_size = 12 + (cc * 4)
        if extension:
            ext_header = struct.unpack('!HH', data[header_size:header_size+4])
            header_size += 4 + (ext_header[1] * 4)
            
        # Extract audio payload
        audio_data = data[header_size:]
        
        return {
            'sequence': sequence,
            'timestamp': timestamp,
            'audio': audio_data,
            'payload_type': payload_type
        }
        
    def create_rtp_packet(self, audio_data, payload_type=0):
        """Create RTP packet from audio data"""
        # RTP header
        version = 2
        padding = 0
        extension = 0
        cc = 0
        marker = 0
        
        header = struct.pack('!BBHII',
            (version << 6) | (padding << 5) | (extension << 4) | cc,
            (marker << 7) | payload_type,
            self.sequence,
            self.timestamp,
            self.ssrc
        )
        
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp += 160  # 20ms at 8kHz
        
        return header + audio_data


class RealtimeARIBridge:
    """Real-time audio bridge using External Media"""
    
    def __init__(self):
        self.active_calls = {}
        
    async def ari_request(self, session, method, path, **kwargs):
        """Make ARI request"""
        url = f"{ARI_URL}{path}"
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        
        async with session.request(method, url, auth=auth, **kwargs) as resp:
            if resp.status >= 400:
                error = await resp.text()
                logger.error(f"ARI error {resp.status}: {error}")
                raise Exception(f"ARI error {resp.status}: {error}")
            
            if resp.content_type and 'json' in resp.content_type:
                return await resp.json()
            return await resp.text()
            
    async def handle_stasis_start(self, session, event):
        """Handle new call with External Media"""
        channel_id = event["channel"]["id"]
        caller = event["channel"]["caller"]["number"] or "Unknown"
        
        logger.info(f"=== NEW CALL from {caller} ===")
        
        try:
            # Answer the call immediately
            await self.ari_request(session, "POST", f"/channels/{channel_id}/answer")
            logger.info("Call answered")
            
            # Create External Media channel for bidirectional audio
            logger.info("Creating External Media channel...")
            external_media = await self.ari_request(
                session, "POST", "/channels/externalMedia",
                json={
                    "app": APP_NAME,
                    "external_host": "127.0.0.1:5000",  # We'll receive RTP here
                    "format": "ulaw"  # G.711 μ-law for telephony
                }
            )
            
            media_channel = external_media["channel"]
            media_id = media_channel["id"]
            
            # Get the RTP connection details
            channel_vars = external_media.get("channelvars", {})
            local_port = int(channel_vars.get("UNICASTRTP_LOCAL_PORT", 5000))
            remote_host = channel_vars.get("UNICASTRTP_REMOTE_ADDRESS", "")
            remote_port = int(channel_vars.get("UNICASTRTP_REMOTE_PORT", 0))
            
            logger.info(f"External Media: Local port {local_port}, Remote {remote_host}:{remote_port}")
            
            # Create mixing bridge
            bridge = await self.ari_request(
                session, "POST", "/bridges",
                json={"type": "mixing", "name": f"call_{channel_id}"}
            )
            bridge_id = bridge["id"]
            
            # Add both channels to bridge
            for ch_id in [channel_id, media_id]:
                await self.ari_request(
                    session, "POST", f"/bridges/{bridge_id}/addChannel",
                    json={"channel": ch_id}
                )
            
            logger.info(f"Bridge created: {channel_id} <-> {media_id}")
            
            # Connect to Voice AI WebSocket
            ws_session = aiohttp.ClientSession()
            ws = await ws_session.ws_connect(VOICE_AGENT_WS)
            
            # Notify agent of call start
            await ws.send_json({
                "type": "call_start",
                "call_id": channel_id,
                "caller": caller
            })
            
            # Create RTP handler
            rtp_handler = RTPHandler(channel_id, ws)
            rtp_socket = rtp_handler.create_socket(local_port)
            
            # Store call info
            self.active_calls[channel_id] = {
                "bridge_id": bridge_id,
                "media_channel_id": media_id,
                "ws": ws,
                "ws_session": ws_session,
                "rtp_handler": rtp_handler,
                "rtp_socket": rtp_socket,
                "remote_addr": (remote_host, remote_port) if remote_host else None
            }
            
            # Start RTP processing
            asyncio.create_task(self.process_rtp_stream(channel_id))
            asyncio.create_task(self.process_websocket_audio(channel_id))
            
            logger.info(f"Real-time streaming established for {channel_id}")
            
        except Exception as e:
            logger.error(f"Error setting up call: {e}")
            
    async def process_rtp_stream(self, channel_id):
        """Process incoming RTP audio and send to Voice AI"""
        if channel_id not in self.active_calls:
            return
            
        call_info = self.active_calls[channel_id]
        rtp_handler = call_info["rtp_handler"]
        rtp_socket = call_info["rtp_socket"]
        ws = call_info["ws"]
        
        logger.info(f"Starting RTP processing for {channel_id}")
        audio_chunks = 0
        
        try:
            while channel_id in self.active_calls:
                try:
                    # Read RTP packet (non-blocking)
                    data, addr = rtp_socket.recvfrom(4096)
                    
                    # Parse RTP packet
                    packet = rtp_handler.parse_rtp_packet(data)
                    if packet and packet['audio']:
                        audio_chunks += 1
                        
                        # Send audio to Voice AI via WebSocket
                        await ws.send_json({
                            "type": "audio",
                            "data": base64.b64encode(packet['audio']).decode(),
                            "timestamp": packet['timestamp']
                        })
                        
                        # Log progress every 50 chunks (~1 second)
                        if audio_chunks % 50 == 0:
                            logger.debug(f"Processed {audio_chunks} audio chunks")
                            
                except socket.error:
                    # No data available (non-blocking)
                    await asyncio.sleep(0.001)  # 1ms sleep
                except Exception as e:
                    logger.error(f"RTP processing error: {e}")
                    
        except Exception as e:
            logger.error(f"RTP stream error: {e}")
        finally:
            logger.info(f"RTP processing stopped for {channel_id} ({audio_chunks} chunks)")
            
    async def process_websocket_audio(self, channel_id):
        """Process audio from Voice AI and send via RTP"""
        if channel_id not in self.active_calls:
            return
            
        call_info = self.active_calls[channel_id]
        rtp_handler = call_info["rtp_handler"]
        rtp_socket = call_info["rtp_socket"]
        ws = call_info["ws"]
        remote_addr = call_info["remote_addr"]
        
        if not remote_addr:
            logger.warning(f"No remote RTP address for {channel_id}")
            return
            
        logger.info(f"Starting WebSocket audio processing for {channel_id}")
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data["type"] == "audio_response":
                        # Decode audio from Voice AI
                        audio_data = base64.b64decode(data["data"])
                        
                        # Create RTP packet and send
                        rtp_packet = rtp_handler.create_rtp_packet(audio_data, payload_type=0)
                        rtp_socket.sendto(rtp_packet, remote_addr)
                        
                    elif data["type"] == "status":
                        logger.info(f"Voice AI status: {data.get('message', '')}")
                        
        except Exception as e:
            logger.error(f"WebSocket audio error: {e}")
            
    async def handle_stasis_end(self, session, event):
        """Handle call end"""
        channel_id = event["channel"]["id"]
        logger.info(f"=== CALL ENDED: {channel_id} ===")
        
        if channel_id in self.active_calls:
            call_info = self.active_calls[channel_id]
            
            # Notify Voice AI
            try:
                await call_info["ws"].send_json({
                    "type": "call_end",
                    "call_id": channel_id
                })
                await call_info["ws"].close()
                await call_info["ws_session"].close()
            except:
                pass
                
            # Close RTP socket
            if call_info.get("rtp_socket"):
                call_info["rtp_socket"].close()
                
            del self.active_calls[channel_id]
            
    async def run(self):
        """Main event loop"""
        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
            params = {"app": APP_NAME, "subscribeAll": "true"}
            
            ws_url = f"{ARI_URL}/events"
            logger.info(f"Connecting to ARI at {ws_url}")
            
            try:
                async with session.ws_connect(ws_url, auth=auth, params=params) as ws:
                    logger.info("=== ARI WebSocket Connected ===")
                    logger.info("Ready for real-time streaming calls!")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            event = json.loads(msg.data)
                            event_type = event.get("type")
                            
                            if event_type == "StasisStart":
                                asyncio.create_task(
                                    self.handle_stasis_start(session, event)
                                )
                            elif event_type == "StasisEnd":
                                asyncio.create_task(
                                    self.handle_stasis_end(session, event)
                                )
                                
            except Exception as e:
                logger.error(f"ARI WebSocket error: {e}")


async def main():
    logger.info("=== Starting Real-time ARI Bridge ===")
    logger.info("This bridges Asterisk calls to Voice AI with <500ms latency")
    bridge = RealtimeARIBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())