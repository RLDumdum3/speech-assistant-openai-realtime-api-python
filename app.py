import os
import json
import base64
import asyncio
import websockets
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timezone
import hashlib

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
PORT = int(os.getenv('PORT', 5050))
SYSTEM_MESSAGE = (
    "You are a helpful and bubbly AI assistant who loves to chat about "
    "anything the user is interested in and is prepared to offer them facts. "
    "You have a penchant for dad jokes, owl jokes, and rickrolling – subtly. "
    "Always stay positive, but work in a joke when appropriate.")
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]
SHOW_TIMING_MATH = False
WEBHOOK_URL = "https://sdg-us.app.n8n.cloud/webhook-test/3257f53e-81a3-4544-89a3-b0626e3857dd"

app = FastAPI()

# Validate required environment variables
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError('Missing Supabase credentials. Please set SUPABASE_URL and SUPABASE_KEY in the .env file.')

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Store conversation transcripts by session
conversation_transcripts = {}

class CallerDatabase:
    """Handle caller recognition and data persistence with Supabase."""
    
    @staticmethod
    async def get_caller_by_phone(phone_number: str):
        """Retrieve caller information by phone number."""
        try:
            # Hash the phone number for privacy
            phone_hash = hashlib.sha256(phone_number.encode()).hexdigest()
            
            response = supabase.table('callers').select('*').eq('phone_hash', phone_hash).execute()
            
            if response.data and len(response.data) > 0:
                return response.data[0]
            return None
        except Exception as e:
            print(f"Error retrieving caller: {e}")
            return None
    
    @staticmethod
    async def create_or_update_caller(phone_number: str, caller_data: dict):
        """Create a new caller or update existing caller information."""
        try:
            phone_hash = hashlib.sha256(phone_number.encode()).hexdigest()
            
            # Check if caller exists
            existing_caller = await CallerDatabase.get_caller_by_phone(phone_number)
            
            caller_record = {
                'phone_hash': phone_hash,
                'name': caller_data.get('customerName', ''),
                'availability': caller_data.get('customerAvailability', ''),
                'special_notes': caller_data.get('specialNotes', ''),
                'last_call_date': datetime.now(timezone.utc).isoformat(),
                'call_count': 1
            }
            
            if existing_caller:
                # Update existing caller
                caller_record['call_count'] = existing_caller.get('call_count', 0) + 1
                caller_record['id'] = existing_caller['id']
                
                response = supabase.table('callers').update(caller_record).eq('id', existing_caller['id']).execute()
            else:
                # Create new caller
                response = supabase.table('callers').insert(caller_record).execute()
            
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error creating/updating caller: {e}")
            return None
    
    @staticmethod
    async def save_call_transcript(caller_id: int, session_id: str, transcript: str, extracted_data: dict = None):
        """Save call transcript to database."""
        try:
            call_record = {
                'caller_id': caller_id,
                'session_id': session_id,
                'transcript': transcript,
                'extracted_data': extracted_data or {},
                'call_date': datetime.now(timezone.utc).isoformat()
            }
            
            response = supabase.table('call_transcripts').insert(call_record).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error saving call transcript: {e}")
            return None
    
    @staticmethod
    async def get_caller_history(phone_number: str, limit: int = 5):
        """Get recent call history for a caller."""
        try:
            phone_hash = hashlib.sha256(phone_number.encode()).hexdigest()
            
            # First get the caller
            caller = await CallerDatabase.get_caller_by_phone(phone_number)
            if not caller:
                return []
            
            # Get recent call transcripts
            response = supabase.table('call_transcripts').select('*').eq('caller_id', caller['id']).order('call_date', desc=True).limit(limit).execute()
            
            return response.data or []
        except Exception as e:
            print(f"Error retrieving caller history: {e}")
            return []

async def initialize_database():
    """Initialize database tables if they don't exist."""
    try:
        # Create callers table
        supabase.table('callers').select('id').limit(1).execute()
    except:
        print("Database tables may need to be created. Please run the following SQL in your Supabase dashboard:")
        print("""
        -- Create callers table
        CREATE TABLE IF NOT EXISTS callers (
            id SERIAL PRIMARY KEY,
            phone_hash VARCHAR(64) UNIQUE NOT NULL,
            name VARCHAR(255),
            availability TEXT,
            special_notes TEXT,
            last_call_date TIMESTAMP WITH TIME ZONE,
            call_count INTEGER DEFAULT 1,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );

        -- Create call_transcripts table
        CREATE TABLE IF NOT EXISTS call_transcripts (
            id SERIAL PRIMARY KEY,
            caller_id INTEGER REFERENCES callers(id),
            session_id VARCHAR(255),
            transcript TEXT,
            extracted_data JSONB,
            call_date TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );

        -- Create indexes for better performance
        CREATE INDEX IF NOT EXISTS idx_callers_phone_hash ON callers(phone_hash);
        CREATE INDEX IF NOT EXISTS idx_call_transcripts_caller_id ON call_transcripts(caller_id);
        CREATE INDEX IF NOT EXISTS idx_call_transcripts_call_date ON call_transcripts(call_date);
        """)

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    await initialize_database()

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server with Caller Recognition is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect directly to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname
    
    # Get caller's phone number from Twilio
    form_data = await request.form()
    caller_phone = form_data.get('From', '')
    
    # Check if caller is recognized
    caller_info = await CallerDatabase.get_caller_by_phone(caller_phone)
    
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream?phone={caller_phone}')
    
    if caller_info and caller_info.get('name'):
        # Returning caller
        greeting = f"Hi {caller_info['name']}, welcome back! David AI agent here. How can I help you today?"
    else:
        # New caller
        greeting = "Hi, hello, David AI agent here. How may I assist you today?"
    
    response.say(greeting)
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()
    
    # Extract phone number from query parameters
    query_params = dict(websocket.query_params)
    caller_phone = query_params.get('phone', '')
    
    # Get caller info for context
    caller_info = await CallerDatabase.get_caller_by_phone(caller_phone) if caller_phone else None
    caller_history = await CallerDatabase.get_caller_history(caller_phone) if caller_phone else []
    
    # Build system message with caller context
    system_message = SYSTEM_MESSAGE
    if caller_info:
        system_message += f"\n\nCaller Context: You are speaking with {caller_info.get('name', 'a returning caller')}. "
        if caller_info.get('special_notes'):
            system_message += f"Previous notes: {caller_info['special_notes']}. "
        if caller_info.get('call_count', 0) > 1:
            system_message += f"This is their {caller_info['call_count']} call. "
        if caller_history:
            system_message += "Recent interaction themes: " + ", ".join([
                call.get('extracted_data', {}).get('specialNotes', '')[:50] 
                for call in caller_history[:2] 
                if call.get('extracted_data', {}).get('specialNotes')
            ])

    async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }) as openai_ws:
        await initialize_session(openai_ws, system_message)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        conversation_transcript = []

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        conversation_transcripts[stream_sid] = []
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
                    elif data['event'] == 'stop':
                        print(f"Call ended for stream {stream_sid}")
                        # Process transcript when call ends
                        if stream_sid in conversation_transcripts:
                            full_transcript = " ".join(conversation_transcripts[stream_sid])
                            if full_transcript.strip():
                                await process_transcript_and_send(full_transcript, stream_sid, caller_phone)
                            del conversation_transcripts[stream_sid]
            except WebSocketDisconnect:
                print("Client disconnected.")
                # Process transcript on disconnect
                if stream_sid and stream_sid in conversation_transcripts:
                    full_transcript = " ".join(conversation_transcripts[stream_sid])
                    if full_transcript.strip():
                        await process_transcript_and_send(full_transcript, stream_sid, caller_phone)
                    del conversation_transcripts[stream_sid]
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    # Capture transcript from conversation items
                    if response.get('type') == 'conversation.item.created':
                        item = response.get('item', {})
                        if item.get('type') == 'message':
                            role = item.get('role')
                            content = item.get('content', [])
                            for content_part in content:
                                if content_part.get('type') == 'input_text':
                                    text = content_part.get('text', '')
                                    if text and stream_sid:
                                        conversation_transcripts.setdefault(stream_sid, []).append(f"User: {text}")
                                elif content_part.get('type') == 'text':
                                    text = content_part.get('text', '')
                                    if text and stream_sid:
                                        conversation_transcripts.setdefault(stream_sid, []).append(f"Assistant: {text}")

                    # Also capture from response.text events
                    if response.get('type') == 'response.text.delta':
                        delta = response.get('delta', '')
                        if delta and stream_sid:
                            # Append to last assistant message or create new one
                            if (stream_sid in conversation_transcripts
                                    and conversation_transcripts[stream_sid]
                                    and conversation_transcripts[stream_sid][-1].startswith("Assistant:")):
                                conversation_transcripts[stream_sid][-1] += delta
                            else:
                                conversation_transcripts.setdefault(stream_sid, []).append(f"Assistant: {delta}")

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {
                        "name": "responsePart"
                    }
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws, caller_info=None):
    """Send initial conversation item if AI talks first."""
    greeting_text = "Greet the user with 'Hi, hello, David AI agent here. How can I help you?'"
    
    if caller_info and caller_info.get('name'):
        greeting_text = f"Greet the returning user with 'Hi {caller_info['name']}, welcome back! David AI agent here. How can I help you today?'"
    
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": greeting_text
            }]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def initialize_session(openai_ws, system_message=None):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad"
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": system_message or SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # Keep this commented out to prevent AI from speaking first
    await send_initial_conversation_item(openai_ws)

async def make_chatgpt_completion(transcript):
    """Extract customer details from transcript using ChatGPT."""
    print("Starting ChatGPT API call...")

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-2024-08-06",
        "messages": [{
            "role": "system",
            "content": "Extract customer details: name, availability, and any special notes from the transcript. If the customer is returning and mentions previous conversations, include that context."
        }, {
            "role": "user",
            "content": transcript
        }],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "customer_details_extraction",
                "schema": {
                    "type": "object",
                    "properties": {
                        "customerName": {
                            "type": "string"
                        },
                        "customerAvailability": {
                            "type": "string"
                        },
                        "specialNotes": {
                            "type": "string"
                        }
                    },
                    "required": ["customerName", "customerAvailability", "specialNotes"]
                }
            }
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            print("ChatGPT API response status:", response.status_code)

            if response.status_code == 200:
                data = response.json()
                print("Full ChatGPT API response:", json.dumps(data, indent=2))
                return data
            else:
                print("ChatGPT API error:", response.text)
                return None
    except Exception as e:
        print(f"Error calling ChatGPT API: {e}")
        return None

async def send_to_webhook(payload):
    """Send extracted data to Make.com webhook."""
    print("Sending data to webhook:", json.dumps(payload, indent=2))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(WEBHOOK_URL, json=payload)

            print("Webhook response status:", response.status_code)
            if response.status_code == 200:
                print("Data successfully sent to webhook.")
                return True
            else:
                print("Failed to send data to webhook:", response.text)
                return False
    except Exception as e:
        print(f"Error sending to webhook: {e}")
        return False

async def process_transcript_and_send(transcript, session_id=None, caller_phone=None):
    """Main function to process transcript and forward to webhook."""
    print(f"Starting transcript processing for session {session_id}...")
    print(f"Transcript content: {transcript}")

    try:
        result = await make_chatgpt_completion(transcript)

        if result and result.get("choices") and len(result["choices"]) > 0:
            choice = result["choices"][0]
            if choice.get("message") and choice["message"].get("content"):
                try:
                    parsed = json.loads(choice["message"]["content"])
                    print("Parsed content:", json.dumps(parsed, indent=2))

                    # Add session ID to the payload
                    parsed["sessionId"] = session_id
                    parsed["timestamp"] = asyncio.get_event_loop().time()
                    
                    # Handle caller recognition and database storage
                    if caller_phone:
                        # Create or update caller in database
                        caller_record = await CallerDatabase.create_or_update_caller(caller_phone, parsed)
                        
                        if caller_record:
                            # Save call transcript
                            await CallerDatabase.save_call_transcript(
                                caller_record['id'], 
                                session_id, 
                                transcript, 
                                parsed
                            )
                            
                            # Add caller info to webhook payload
                            parsed["callerId"] = caller_record['id']
                            parsed["callerPhone"] = caller_phone
                            parsed["isReturningCaller"] = caller_record.get('call_count', 1) > 1

                    success = await send_to_webhook(parsed)
                    if success:
                        print("Successfully processed and sent transcript data")
                    else:
                        print("Failed to send data to webhook")

                except json.JSONDecodeError as e:
                    print("Error parsing JSON from ChatGPT response:", str(e))
                    print("Raw content:", choice["message"]["content"])
            else:
                print("No content in ChatGPT response message")
        else:
            print("Unexpected or empty response structure from ChatGPT API")

    except Exception as e:
        print("Error in process_transcript_and_send:", str(e))

# API endpoints for caller management
@app.get("/api/callers")
async def get_all_callers():
    """Get all callers from database."""
    try:
        response = supabase.table('callers').select('*').order('last_call_date', desc=True).execute()
        return {"callers": response.data}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/caller/{phone_number}/history")
async def get_caller_history_endpoint(phone_number: str):
    """Get call history for a specific caller."""
    try:
        history = await CallerDatabase.get_caller_history(phone_number)
        return {"history": history}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)