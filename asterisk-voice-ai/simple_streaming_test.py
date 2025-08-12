#!/usr/bin/env python3
"""
Simplified Streaming Test - Minimal WebSocket audio bridge
Tests the concept without all the complexity
"""

import asyncio
import aiohttp
from aiohttp import web
import json
import base64
import logging
import time
import os

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class SimpleStreamingAgent:
    """Minimal streaming agent for testing"""
    
    def __init__(self):
        self.call_active = False
        self.audio_chunks_received = 0
        self.start_time = None
        
    async def handle_websocket(self, ws):
        """Handle WebSocket connection"""
        logger.info("WebSocket connected!")
        self.start_time = time.time()
        self.call_active = True
        
        # Send immediate greeting
        await ws.send_json({
            "type": "greeting",
            "text": "Streaming connected!"
        })
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data["type"] == "audio":
                        self.audio_chunks_received += 1
                        
                        # Log every 50 chunks (~1 second)
                        if self.audio_chunks_received % 50 == 0:
                            elapsed = time.time() - self.start_time
                            logger.info(f"Received {self.audio_chunks_received} audio chunks in {elapsed:.1f}s")
                            
                            # Echo back a simple beep every 2 seconds
                            if self.audio_chunks_received % 100 == 0:
                                await ws.send_json({
                                    "type": "beep",
                                    "frequency": 440,
                                    "duration": 100
                                })
                                
                    elif data["type"] == "call_start":
                        logger.info(f"Call started: {data.get('caller', 'unknown')}")
                        
                    elif data["type"] == "call_end":
                        logger.info("Call ended")
                        break
                        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            self.call_active = False
            if self.audio_chunks_received > 0:
                elapsed = time.time() - self.start_time
                logger.info(f"Total: {self.audio_chunks_received} chunks in {elapsed:.1f}s")


async def websocket_handler(request):
    """WebSocket endpoint handler"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    agent = SimpleStreamingAgent()
    await agent.handle_websocket(ws)
    
    return ws


async def health_handler(request):
    """Health check endpoint"""
    return web.json_response({
        "status": "healthy",
        "type": "simple_streaming_test"
    })


async def main():
    """Start WebSocket server"""
    app = web.Application()
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/health', health_handler)
    
    port = 8091  # Different port to avoid conflicts
    logger.info(f"Starting Simple Streaming Test on port {port}")
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f"Server ready at ws://localhost:{port}/ws")
    
    # Keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())