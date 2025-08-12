#!/usr/bin/env python3
"""
Simple ARI Bridge - Connect Asterisk to WebSocket
Minimal implementation to test the concept
"""

import asyncio
import aiohttp
import json
import logging
import base64

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Configuration
ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "ari-user"
ARI_PASS = "ari123"
APP_NAME = "streaming_test"
WS_URL = "ws://127.0.0.1:8091/ws"

class SimpleARIBridge:
    """Minimal ARI to WebSocket bridge"""
    
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
                raise Exception(f"ARI error {resp.status}")
            
            if resp.content_type and 'json' in resp.content_type:
                return await resp.json()
            return await resp.text()
            
    async def handle_stasis_start(self, session, event):
        """Handle new call"""
        channel_id = event["channel"]["id"]
        caller = event["channel"]["caller"]["number"]
        
        logger.info(f"=== NEW CALL from {caller} ===")
        
        try:
            # Answer the call
            await self.ari_request(session, "POST", f"/channels/{channel_id}/answer")
            logger.info("Call answered")
            
            # Play a beep to indicate we're connected
            await self.ari_request(
                session, "POST", f"/channels/{channel_id}/play",
                json={"media": "sound:beep"}
            )
            
            # Connect to WebSocket agent
            logger.info(f"Connecting to WebSocket at {WS_URL}")
            ws_session = aiohttp.ClientSession()
            ws = await ws_session.ws_connect(WS_URL)
            
            # Notify agent of call start
            await ws.send_json({
                "type": "call_start",
                "call_id": channel_id,
                "caller": caller
            })
            
            logger.info("WebSocket connected and call started")
            
            # Store connection
            self.active_calls[channel_id] = {
                "ws": ws,
                "ws_session": ws_session
            }
            
            # Start audio handler (simplified - just logging for now)
            asyncio.create_task(self.handle_audio_loop(channel_id))
            
        except Exception as e:
            logger.error(f"Error in call setup: {e}")
            
    async def handle_audio_loop(self, channel_id):
        """Simplified audio handling loop"""
        logger.info(f"Audio loop started for {channel_id}")
        
        # Simulate audio chunks being sent
        call_info = self.active_calls.get(channel_id)
        if not call_info:
            return
            
        try:
            for i in range(100):  # Send 100 test chunks
                if channel_id not in self.active_calls:
                    break
                    
                # Send fake audio chunk
                await call_info["ws"].send_json({
                    "type": "audio",
                    "data": base64.b64encode(b"\x00" * 160).decode()  # 20ms of silence
                })
                
                await asyncio.sleep(0.02)  # 20ms intervals
                
        except Exception as e:
            logger.error(f"Audio loop error: {e}")
            
    async def handle_stasis_end(self, session, event):
        """Handle call end"""
        channel_id = event["channel"]["id"]
        logger.info(f"=== CALL ENDED: {channel_id} ===")
        
        if channel_id in self.active_calls:
            call_info = self.active_calls[channel_id]
            
            # Notify agent
            try:
                await call_info["ws"].send_json({
                    "type": "call_end",
                    "call_id": channel_id
                })
                await call_info["ws"].close()
                await call_info["ws_session"].close()
            except:
                pass
                
            del self.active_calls[channel_id]
            
    async def run(self):
        """Main event loop"""
        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
            params = {"app": APP_NAME, "subscribeAll": "true"}
            
            ws_url = f"{ARI_URL}/events"
            logger.info(f"Connecting to ARI WebSocket at {ws_url}")
            
            try:
                async with session.ws_connect(ws_url, auth=auth, params=params) as ws:
                    logger.info("=== ARI WebSocket Connected ===")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            event = json.loads(msg.data)
                            event_type = event.get("type")
                            
                            logger.debug(f"ARI Event: {event_type}")
                            
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
    logger.info("=== Starting Simple ARI Bridge ===")
    bridge = SimpleARIBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())