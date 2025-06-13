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
OPENAI_WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError(
        'Missing the OpenAI API key. Please set it in the .env file.')

# Store conversation transcripts by session
conversation_transcripts = {}


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect directly to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }) as openai_ws:
        await initialize_session(openai_ws)

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
                        latest_media_timestamp = int(
                            data['media']['timestamp'])
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
                            full_transcript = " ".join(
                                conversation_transcripts[stream_sid])
                            if full_transcript.strip():
                                await process_transcript_and_send(
                                    full_transcript, stream_sid)
                            del conversation_transcripts[stream_sid]
            except WebSocketDisconnect:
                print("Client disconnected.")
                # Process transcript on disconnect
                if stream_sid and stream_sid in conversation_transcripts:
                    full_transcript = " ".join(
                        conversation_transcripts[stream_sid])
                    if full_transcript.strip():
                        await process_transcript_and_send(
                            full_transcript, stream_sid)
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
                                        conversation_transcripts.setdefault(
                                            stream_sid,
                                            []).append(f"User: {text}")
                                elif content_part.get('type') == 'text':
                                    text = content_part.get('text', '')
                                    if text and stream_sid:
                                        conversation_transcripts.setdefault(
                                            stream_sid,
                                            []).append(f"Assistant: {text}")

                    # Also capture from response.text events
                    if response.get('type') == 'response.text.delta':
                        delta = response.get('delta', '')
                        if delta and stream_sid:
                            # Append to last assistant message or create new one
                            if (stream_sid in conversation_transcripts
                                    and conversation_transcripts[stream_sid]
                                    and conversation_transcripts[stream_sid]
                                [-1].startswith("Assistant:")):
                                conversation_transcripts[stream_sid][
                                    -1] += delta
                            else:
                                conversation_transcripts.setdefault(
                                    stream_sid,
                                    []).append(f"Assistant: {delta}")

                    if response.get(
                            'type'
                    ) == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(
                            base64.b64decode(
                                response['delta'])).decode('utf-8')
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
                                print(
                                    f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms"
                                )

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get(
                            'type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(
                                f"Interrupting response with id: {last_assistant_item}"
                            )
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
                    print(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                    )

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(
                            f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms"
                        )

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


async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type":
            "message",
            "role":
            "assistant",
            "content": [{
                "type": "text",
                "text": "Hi, David AI Agent Here. How can I help you?'"
            }]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
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
            "instructions": SYSTEM_MESSAGE,
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
        "model":
        "gpt-4o-2024-08-06",
        "messages": [{
            "role":
            "system",
            "content":
            "Extract customer details: name, availability, and any special notes from the transcript."
        }, {
            "role": "user",
            "content": transcript
        }],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "customer_details_extraction",
                "schema": {
                    "type":
                    "object",
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
                    "required":
                    ["customerName", "customerAvailability", "specialNotes"]
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


async def process_transcript_and_send(transcript, session_id=None):
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

                    success = await send_to_webhook(parsed)
                    if success:
                        print(
                            "Successfully processed and sent transcript data")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
