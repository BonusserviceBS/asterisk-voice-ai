#!/usr/bin/env python3
"""Test ARI WebSocket connection"""

import asyncio
import aiohttp
import json

async def test_ari():
    url = "ws://127.0.0.1:8088/ari/events?app=voice-ai&api_key=ari-user:ari123"
    
    print(f"Connecting to: {url}")
    
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(url)
        print("✅ WebSocket connected successfully!")
        print("✅ Application 'voice-ai' is now registered")
        
        # Read a few messages
        for i in range(5):
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                print(f"Received event: {data.get('type', 'unknown')}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                print(f"WebSocket error: {ws.exception()}")
                break
                
    except asyncio.TimeoutError:
        print("No messages received (timeout) - but connection is working")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(test_ari())