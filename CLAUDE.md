# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Key Commands

### System Services Management
```bash
# Check service status
systemctl status voice-ai-core
systemctl status teli24-middleware
systemctl status voice-ai-external-media
systemctl status voice-ai-control-api

# Restart services (requires sudo)
sudo systemctl restart voice-ai-core
sudo systemctl restart teli24-middleware

# View service logs
journalctl -u voice-ai-core -f
journalctl -u teli24-middleware -f
```

### Database Operations
```bash
# Connect to PostgreSQL (Server 5)
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development

# Check assistant configuration for a phone number
/opt/voice-ai-core/venv/bin/python -c "
import asyncio
from db_connection_pool import get_connection
asyncio.run(async lambda: await (await get_connection()).fetchrow(
    'SELECT * FROM \"Assistant\" a JOIN \"PhoneNumber\" p ON a.id = p.\"assignedToAssistantId\" WHERE p.number = \'+4920189098723\''
))()
"
```

### Development Commands (teli24-middleware)
```bash
cd /home/admini/teli24-middleware

# Run application
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

# Code quality
black app/
flake8 app/
isort app/

# Testing
pytest
pytest --cov=app --cov-report=html
```

### Infrastructure Deployment
```bash
cd /home/admini/teli24-infrastructure
./deploy.sh production
ansible-playbook -i ansible/inventory/production.yml ansible/playbooks/site.yml
```

## Architecture Overview

### Distributed System Architecture (5 Servers)

**Server 6 (10.0.0.6) - Asterisk/Telephony**
- Asterisk PBX for SIP telephony  
- ARI-Bridge Service managing External Media channels
- RTP port management (15000-16000 range)
- Bridges SIP and External Media channels

**Server 7 (10.0.0.7) - AI Processing**
- Voice AI Core Service (Port 8000) - Main AI processing engine
- Teli24 Middleware (Port 8080) - FastAPI bridge to frontend
- Azure STT/TTS and Mistral AI integration
- RTP client for audio streaming

**Server 5 (10.0.0.5) - Database**
- PostgreSQL 15 for data persistence
- Redis for caching
- Stores CallRecords, Transcripts, Assistant configurations

**Server 8 (10.0.0.8) - Frontend**
- Deno Fresh web application
- Nginx reverse proxy
- Direct database access for call records

**Monitoring Server**
- Prometheus metrics collection
- Grafana dashboards

### Two-Channel Audio Architecture

The system uses a unique two-channel approach for optimal audio processing:

**IN Channel (Even ports: 15000, 15002...)**
- Direction: Caller → AI
- Format: slin16 (16kHz PCM) for optimal STT
- Purpose: Speech recognition

**OUT Channel (Odd ports: 15001, 15003...)**  
- Direction: AI → Caller
- Format: alaw (8kHz G.711) for telephony
- Purpose: TTS playback

### Call Flow
1. Placetel SIP → Asterisk (Server 6)
2. Asterisk Stasis App → ARI-Bridge
3. ARI-Bridge creates two External Media channels
4. Voice AI Core processes audio (STT → AI → TTS)
5. Real-time transcripts saved to database
6. Frontend displays call records and transcripts

## Critical System Components

### Voice AI Core (`/opt/voice-ai-core/`)
- `main.py` - FastAPI application handling Stasis events
- `streaming_processor.py` - Real-time audio processing
- `session_recorder.py` - Call recording and transcription
- `recording_integration_asyncpg.py` - Database persistence
- `assistant_lookup.py` - Assistant configuration retrieval
- `rtp_client.py` - RTP audio streaming client

### Teli24 Middleware (`/home/admini/teli24-middleware/`)
- `app/main.py` - FastAPI entry point
- `app/routers/websocket_call.py` - WebSocket call handling
- `app/services/asterisk/` - AMI integration
- `app/services/azure/` - Azure AI services
- `app/services/database/` - Database operations
- `app/config/` - Audio and telephony configuration

### Shared Library (`/home/admini/teli24-shared/`)
- `teli24_shared/models.py` - SQLAlchemy models
- `teli24_shared/config.py` - Shared configuration
- Installed as editable package in all services

## Database Schema

Key tables:
- `Assistant` - AI assistant configurations
- `PhoneNumber` - Phone number to assistant mapping
- `CallRecord` - Call metadata and status
- `CallTranscript` - Real-time conversation transcripts
- `CallRecording` - MP3 audio recordings

## Common Troubleshooting

### Voice AI Not Responding
1. Check service status: `systemctl status voice-ai-core`
2. Check RTP ports: Ensure 15000-16000 range is available
3. Verify assistant configuration in database
4. Check Asterisk ARI connection

### Database Connection Issues
- Password: `BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW`
- Host: `10.0.0.5`
- Database: `teli24_development`
- User: `teli24_user`

### Audio Quality Problems
- Verify codec configuration (slin16 for IN, alaw for OUT)
- Check sample rate conversion (48kHz → 16kHz for STT)
- Monitor VAD processor settings

## Recent Fixes and Improvements

### Call Recording System (2025-08-04)
- Fixed duplicate transcript entries
- Corrected timezone handling (local time instead of UTC)
- Implemented call duration tracking
- Added assistant transcript saving

### Voice AI Architecture (2025-06-23)
- Implemented SnoopChannel + External Media hybrid
- Fixed RTP port conflicts between servers
- Eliminated audio feedback loops
- Added Call-ID management for concurrent calls

### Multi-Provider Support (2025-06-24)
- Added Google Cloud Speech as alternative to Azure
- Implemented provider factory pattern
- Dynamic provider selection per assistant

## LiveKit SIP Gateway Integration (2025-08-08)

### NEW: LiveKit Cloud SIP Gateway
Complete replacement for RTP bridge with superior quality and features:

#### Key Commands
```bash
# LiveKit Production Agent
cd /home/admini/voice-ai-livekit
source venv/bin/activate

# Start production agent with database integration
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' python livekit_production_agent.py

# Development mode with auto-reload
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' python livekit_production_agent.py dev

# Test complete system
python test_complete_system.py

# Setup SIP Gateway configuration
python setup_livekit_sip.py
```

#### Architecture Components

**LiveKit Production Agent (`livekit_production_agent.py`)**
- Multi-provider TTS/STT (Azure, Edge-TTS, Google)
- Database integration for assistant configuration
- Local recording for DSGVO compliance
- SIP participant attribute reading
- Tool calling support framework

**TTS Service (`tts_service.py`)**
- Azure Cognitive Services
- Edge-TTS (free Microsoft TTS)
- 48kHz audio output for LiveKit
- SSML support

**Configuration Files**
- `asterisk_pjsip_config.conf` - Asterisk SIP trunk for LiveKit
- `setup_livekit_sip.py` - Automated setup script
- `test_complete_system.py` - System verification

#### LiveKit Advantages over RTP Bridge
- No audio conversion chain (direct Opus codec)
- Better quality (WebRTC echo cancellation)
- Lower latency (no intermediate processing)
- Better interruptions (native WebRTC VAD)
- Scalable (LiveKit Cloud handles scaling)

#### Testing LiveKit System
```bash
# 1. Check agent is running
ps aux | grep livekit_production_agent

# 2. Monitor agent logs
tail -f agent.log

# 3. Test call
# Call +4920189098723 or use:
asterisk -rx 'channel originate PJSIP/+4920189098723@livekit-gateway application MusicOnHold'

# 4. Check database for recordings
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development -c "SELECT * FROM \"CallRecord\" ORDER BY \"createdAt\" DESC LIMIT 5;"
```

#### Environment Variables for LiveKit
```bash
export LIVEKIT_URL=wss://teltest-02nsqrnq.livekit.cloud
export LIVEKIT_API_KEY=APICfHqH5i7X8iE
export LIVEKIT_API_SECRET=<secret>
export DB_PASSWORD=BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW
export AZURE_SPEECH_KEY=<azure-key>
export AZURE_SPEECH_REGION=germanywestcentral
```

### LiveKit SIP Domain
- Domain: `2t7d51i67hk.sip.livekit.cloud`
- Server: Frankfurt (low latency)
- Codecs: Opus (preferred), G.711 (fallback)