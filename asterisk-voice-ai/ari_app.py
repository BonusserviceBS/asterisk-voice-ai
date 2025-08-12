#!/usr/bin/env python3
"""
ARI Bridge Application
Connects incoming calls to Python RTP Agent via ExternalMedia
"""
import asyncio
import aiohttp
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "ari-user"
ARI_PASS = "ari123"
APP = "kiagent"

MEDIA_HOST = "127.0.0.1:4000"  # Python RTP Service

async def ari_request(sess, method, path, **kw):
    url = f"{ARI_URL}{path}"
    auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
    async with sess.request(method, url, auth=auth, **kw) as r:
        if r.status >= 300:
            raise RuntimeError(f"ARI {method} {path} {r.status}: {await r.text()}")
        if r.content_type and 'json' in r.content_type:
            return await r.json()
        return await r.text()

async def on_stasis_start(sess, ev):
    ch = ev["channel"]["id"]
    caller_num = ev["channel"]["caller"]["number"]
    called_num = ev["channel"]["dialplan"]["exten"]
    
    logger.info(f"Call from {caller_num} to {called_num}")
    
    # Answer the call
    await ari_request(sess, "POST", f"/channels/{ch}/answer")
    
    # Create mixing bridge
    br = await ari_request(sess, "POST", "/bridges", params={"type": "mixing"})
    bridge_id = br["id"]
    
    # Create ExternalMedia channel (RTP PCMU) to Python engine
    em = await ari_request(sess, "POST", "/channels/externalMedia",
        params={
            "app": APP,
            "external_host": MEDIA_HOST,
            "format": "ulaw",  # G.711 µ-law
            "transport": "udp"
        })
    em_id = em["channel"]["id"]
    
    # Add both channels to bridge
    for cid in (ch, em_id):
        await ari_request(sess, "POST", f"/bridges/{bridge_id}/addChannel",
                          params={"channel": cid})
    
    logger.info(f"Bridged {ch} <-> {em_id} via {bridge_id}")

async def on_stasis_end(sess, ev):
    ch = ev["channel"]["id"]
    logger.info(f"Call ended: {ch}")

async def events():
    auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
    params = {"app": APP, "subscribeAll": "true"}
    
    async with aiohttp.ClientSession() as sess:
        logger.info(f"Connecting to ARI WebSocket at {ARI_URL}/events")
        
        async with sess.ws_connect(f"{ARI_URL}/events", auth=auth, params=params) as ws:
            logger.info("ARI WebSocket connected")
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    ev = json.loads(msg.data)
                    event_type = ev.get("type")
                    
                    if event_type == "StasisStart":
                        asyncio.create_task(on_stasis_start(sess, ev))
                    elif event_type == "StasisEnd":
                        asyncio.create_task(on_stasis_end(sess, ev))
                    elif event_type:
                        logger.debug(f"Event: {event_type}")

if __name__ == "__main__":
    logger.info("Starting ARI Bridge Application")
    asyncio.run(events())