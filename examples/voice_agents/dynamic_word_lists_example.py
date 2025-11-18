"""
Example: Safe ways to dynamically update interruption word lists

This demonstrates secure patterns for updating word lists without allowing
voice commands to manipulate them.
"""

# ============================================================================
# METHOD 1: Environment Variables (for runtime configuration)
# ============================================================================
# Set these before starting the agent:
# export IGNORED_FILLERS="uh,um,hmm,er"
# export INTERRUPT_WORDS="stop,pause,wait"

# ============================================================================
# METHOD 2: Room Metadata (set when creating the room)
# ============================================================================
"""
from livekit import api

# When creating a room, set metadata with word lists
room_service = api.RoomService()
room = await room_service.create_room(
    api.CreateRoomRequest(
        name="my-room",
        metadata=json.dumps({
            "language": "spanish",
            "ignored_fillers": ["eh", "este", "pues"],
            "interrupt_words": ["para", "espera", "alto"]
        })
    )
)
"""

# ============================================================================
# METHOD 3: Database/Config File (loaded at agent startup)
# ============================================================================
"""
import json

def load_word_lists_from_config(user_id: str):
    # Load from database or config file
    with open(f"configs/user_{user_id}.json") as f:
        config = json.load(f)
    return config.get("ignored_words"), config.get("interrupt_words")

# In your entrypoint:
ignored, interrupts = load_word_lists_from_config(ctx.room.metadata.get("user_id"))
handler = InterruptionHandler(ignored_words=ignored, interrupt_words=interrupts)
"""

# ============================================================================
# METHOD 4: Admin Dashboard via Data Channel (trusted source only)
# ============================================================================
"""
from livekit import rtc
import json

@ctx.room.on("data_received")
def on_data_received(data: rtc.DataPacket):
    # Verify the sender is an admin/trusted participant
    participant = ctx.room.remote_participants.get(data.participant.sid)
    
    # Check if participant has admin privileges (e.g., via metadata)
    if not participant or participant.metadata.get("role") != "admin":
        logger.warning(f"Ignoring word list update from non-admin: {participant.identity}")
        return
    
    # Only accept updates on specific admin topic
    if data.topic == "admin_config_update":
        try:
            config = json.loads(data.data)
            if "ignored_words" in config:
                handler.update_ignored_words(config["ignored_words"])
                logger.info(f"Updated ignored words: {config['ignored_words']}")
            if "interrupt_words" in config:
                handler.update_interrupt_words(config["interrupt_words"])
                logger.info(f"Updated interrupt words: {config['interrupt_words']}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON in admin config update")
"""

# ============================================================================
# METHOD 5: Event-based Updates (application events)
# ============================================================================
"""
# Create an event emitter for configuration changes
from typing import Callable

class ConfigManager:
    def __init__(self):
        self._callbacks: list[Callable] = []
        
    def subscribe(self, callback: Callable):
        self._callbacks.append(callback)
        
    def update_words(self, ignored=None, interrupt=None):
        # This is called from your backend/admin panel, not from voice
        for callback in self._callbacks:
            callback(ignored, interrupt)

config_manager = ConfigManager()

# In your entrypoint:
def on_config_update(ignored, interrupt):
    if ignored:
        handler.update_ignored_words(ignored)
    if interrupt:
        handler.update_interrupt_words(interrupt)

config_manager.subscribe(on_config_update)
"""

# ============================================================================
# METHOD 6: Time-based or Condition-based Updates
# ============================================================================
"""
import asyncio

async def adaptive_word_list_updater(handler: InterruptionHandler, session: AgentSession):
    '''Update word lists based on conversation analysis or time of day'''
    
    # Example: Add more filler words if user seems nervous (lots of fillers detected)
    filler_count = 0
    threshold = 10
    
    @session.on("user_input_transcribed")
    def track_fillers(ev):
        nonlocal filler_count
        words = ev.transcript.lower().split()
        for word in words:
            if word in handler.ignored:
                filler_count += 1
        
        # If user uses many fillers, become more lenient
        if filler_count > threshold:
            handler.add_ignored_word("basically")
            handler.add_ignored_word("like")
            handler.add_ignored_word("you know")
            filler_count = 0  # Reset
    
    # Example: Time-based adjustment (e.g., different words for different shifts)
    from datetime import datetime
    while True:
        hour = datetime.now().hour
        if 9 <= hour < 17:  # Business hours
            handler.update_interrupt_words(["stop", "pause", "wait", "hold"])
        else:  # After hours
            handler.update_interrupt_words(["stop", "pause", "emergency"])
        
        await asyncio.sleep(3600)  # Check every hour

# Start the updater task
# asyncio.create_task(adaptive_word_list_updater(handler, session))
"""

# ============================================================================
# METHOD 7: Context-aware Updates (based on conversation state)
# ============================================================================
"""
class ConversationStateManager:
    def __init__(self, handler: InterruptionHandler):
        self.handler = handler
        self.state = "greeting"  # greeting, main_conversation, closing
        
    def transition_to(self, new_state: str):
        self.state = new_state
        
        if new_state == "greeting":
            # Be more lenient during greeting
            self.handler.update_interrupt_words(["stop", "wait"])
        elif new_state == "main_conversation":
            # Normal interruption words
            self.handler.update_interrupt_words(["stop", "pause", "wait", "hold"])
        elif new_state == "closing":
            # Allow easy interruption during goodbye
            self.handler.update_interrupt_words(["stop", "bye", "goodbye"])

# Usage in your agent's on_user_turn_completed:
# state_manager.transition_to("main_conversation")
"""

# ============================================================================
# WHAT NOT TO DO (Security Anti-patterns)
# ============================================================================
"""
# ❌ BAD: Don't create a function_tool that updates word lists
@function_tool
async def update_interruption_words(context: RunContext, new_words: str):
    # This allows users to manipulate interruption behavior via voice!
    handler.update_interrupt_words(new_words.split(","))
    return "Updated"

# ❌ BAD: Don't parse user transcripts for configuration commands
@session.on("user_input_transcribed")
def on_transcript(ev):
    if "add filler word" in ev.transcript.lower():
        # Users could say "add filler word emergency" to disable emergency interrupts!
        word = ev.transcript.split("add filler word")[-1].strip()
        handler.add_ignored_word(word)

# ❌ BAD: Don't accept updates from any data channel participant
@ctx.room.on("data_received")
def on_data(data):
    # No authentication check - anyone could send malicious updates!
    config = json.loads(data.data)
    handler.update_ignored_words(config["ignored_words"])
"""

# ============================================================================
# Example: Complete Safe Implementation
# ============================================================================
"""
@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # Load initial configuration from secure source
    room_meta = json.loads(ctx.room.metadata) if ctx.room.metadata else {}
    
    # Get word lists from environment (default) or room metadata (per-session)
    ignored_fillers = room_meta.get("ignored_fillers") or [
        w.strip() for w in os.getenv("IGNORED_FILLERS", "uh,um,hmm").split(",")
    ]
    interrupt_words = room_meta.get("interrupt_words") or [
        w.strip() for w in os.getenv("INTERRUPT_WORDS", "stop,pause").split(",")
    ]
    
    handler = InterruptionHandler(
        ignored_words=ignored_fillers,
        interrupt_words=interrupt_words,
    )
    
    session = AgentSession(...)
    agent = MyAgent(handler)
    
    # Set up secure admin updates (optional)
    @ctx.room.on("data_received")
    def on_admin_update(data: rtc.DataPacket):
        participant = ctx.room.remote_participants.get(data.participant.sid)
        
        # Verify admin role from participant metadata
        if participant and data.topic == "admin_update":
            try:
                participant_meta = json.loads(participant.metadata or "{}")
                if participant_meta.get("role") == "admin":
                    config = json.loads(data.data)
                    if "ignored_words" in config:
                        handler.update_ignored_words(config["ignored_words"])
                    if "interrupt_words" in config:
                        handler.update_interrupt_words(config["interrupt_words"])
                    logger.info(f"Admin {participant.identity} updated word lists")
                else:
                    logger.warning(f"Non-admin {participant.identity} attempted update")
            except Exception as e:
                logger.error(f"Error processing admin update: {e}")
    
    await session.start(agent=agent, room=ctx.room)
"""
