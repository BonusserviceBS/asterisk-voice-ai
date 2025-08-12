#!/usr/bin/env python3
"""
Asterisk WebSocket Bridge for Real-time Audio Streaming
Connects Asterisk to the streaming voice agent via WebSocket
"""

import asyncio
import aiohttp
import websockets
import json
import base64
import logging
import struct
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "ari-user"
ARI_PASS = "ari123"
APP_NAME = "streaming_voice"
VOICE_AGENT_WS = "ws://127.0.0.1:8090/ws"

class AsteriskWebSocketBridge:
    """
    Bridges Asterisk calls to streaming voice agent
    Uses ARI External Media for real-time audio
    """
    
    def __init__(self):
        self.active_calls = {}
        
    async def on_stasis_start(self, session, event):
        """Handle new call"""
        channel_id = event["channel"]["id"]
        caller = event["channel"]["caller"]["number"]
        
        logger.info(f"Call from {caller} - Setting up streaming bridge")
        
        try:
            # Answer immediately
            await self.ari_request(session, "POST", f"/channels/{channel_id}/answer")
            
            # Create External Media channel for audio streaming
            external_media = await self.ari_request(
                session, "POST", "/channels/externalMedia",
                json={
                    "app": APP_NAME,
                    "format": "slin16",  # 16-bit linear PCM at 16kHz
                    "encapsulation": "rtp",
                    "transport": "udp",
                    "connection_type": "client",
                    "direction": "both"
                }
            )
            
            media_channel_id = external_media["channel"]["id"]
            media_address = external_media["channelvars"]["UNICASTRTP_LOCAL_ADDRESS"]
            media_port = external_media["channelvars"]["UNICASTRTP_LOCAL_PORT"]
            
            logger.info(f"External Media created: {media_address}:{media_port}")
            
            # Create bridge
            bridge = await self.ari_request(
                session, "POST", "/bridges",
                json={"type": "mixing", "name": f"call_{channel_id}"}
            )
            bridge_id = bridge["id"]
            
            # Add both channels to bridge
            for ch_id in [channel_id, media_channel_id]:
                await self.ari_request(
                    session, "POST", f"/bridges/{bridge_id}/addChannel",
                    json={"channel": ch_id}
                )
            
            # Connect to voice agent WebSocket
            voice_ws = await websockets.connect(VOICE_AGENT_WS)
            
            # Notify agent of call start
            await voice_ws.send(json.dumps({
                "type": "call_start",
                "call_id": channel_id,
                "caller": caller
            }))
            
            # Store call info
            self.active_calls[channel_id] = {
                "bridge_id": bridge_id,
                "media_channel_id": media_channel_id,
                "voice_ws": voice_ws,
                "rtp_port": media_port
            }
            
            # Start RTP handler
            asyncio.create_task(self.handle_rtp_stream(
                channel_id, 
                int(media_port),
                voice_ws
            ))
            
            logger.info(f"Streaming bridge established for {channel_id}")
            
        except Exception as e:
            logger.error(f"Error setting up bridge: {e}")
            
    async def handle_rtp_stream(self, call_id, rtp_port, voice_ws):
        """Handle RTP audio stream"""
        try:
            # Create UDP socket for RTP
            transport, protocol = await asyncio.get_event_loop().create_datagram_endpoint(
                lambda: RTPProtocol(voice_ws),
                local_addr=('0.0.0.0', rtp_port)
            )
            
            logger.info(f"RTP handler listening on port {rtp_port}")
            
            # Keep running until call ends
            while call_id in self.active_calls:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"RTP handler error: {e}")
            
    async def on_stasis_end(self, session, event):
        """Handle call end"""
        channel_id = event["channel"]["id"]
        
        if channel_id in self.active_calls:
            call_info = self.active_calls[channel_id]
            
            # Notify agent
            if call_info["voice_ws"]:
                await call_info["voice_ws"].send(json.dumps({
                    "type": "call_end",
                    "call_id": channel_id
                }))
                await call_info["voice_ws"].close()
            
            # Cleanup
            del self.active_calls[channel_id]
            logger.info(f"Call {channel_id} ended")
            
    async def ari_request(self, session, method, path, **kwargs):
        """Make ARI API request"""
        url = f"{ARI_URL}{path}"
        auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        
        async with session.request(method, url, auth=auth, **kwargs) as resp:
            if resp.status >= 400:
                error = await resp.text()
                raise Exception(f"ARI error {resp.status}: {error}")
            
            if resp.content_type and 'json' in resp.content_type:
                return await resp.json()
            return await resp.text()
            
    async def run(self):
        """Main event loop"""
        async with aiohttp.ClientSession() as session:
            # Connect to ARI WebSocket
            auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
            params = {"app": APP_NAME, "subscribeAll": "true"}
            
            logger.info(f"Connecting to ARI at {ARI_URL}/events")
            
            async with session.ws_connect(
                f"{ARI_URL}/events",
                auth=auth,
                params=params
            ) as ws:
                logger.info("Connected to ARI WebSocket")
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(msg.data)
                        event_type = event.get("type")
                        
                        if event_type == "StasisStart":
                            asyncio.create_task(
                                self.on_stasis_start(session, event)
                            )
                        elif event_type == "StasisEnd":
                            asyncio.create_task(
                                self.on_stasis_end(session, event)
                            )


class RTPProtocol(asyncio.DatagramProtocol):
    """RTP protocol handler"""
    
    def __init__(self, voice_ws):
        self.voice_ws = voice_ws
        self.transport = None
        
    def connection_made(self, transport):
        self.transport = transport
        
    def datagram_received(self, data, addr):
        """Handle incoming RTP packet"""
        if len(data) < 12:
            return  # Invalid RTP packet
            
        # Parse RTP header
        header = struct.unpack('!BBHII', data[:12])
        payload_type = header[1] & 0x7f
        
        # Extract audio payload (skip RTP header)
        audio_data = data[12:]
        
        # Send to voice agent via WebSocket
        if self.voice_ws:
            asyncio.create_task(self.send_audio(audio_data))
            
    async def send_audio(self, audio_data):
        """Send audio to voice agent"""
        try:
            await self.voice_ws.send(json.dumps({
                "type": "audio",
                "data": base64.b64encode(audio_data).decode()
            }))
        except Exception as e:
            logger.error(f"Error sending audio: {e}")


async def main():
    bridge = AsteriskWebSocketBridge()
    await bridge.run()

if __name__ == "__main__":
    logger.info("Starting Asterisk WebSocket Bridge")
    asyncio.run(main())