import logging
import os
from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RunContext,
    cli,
    metrics,
    room_io,
)
from livekit.agents.llm import function_tool, StopResponse
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel


import re
from typing import Optional, Iterable


class InterruptionHandler:
    """
    Decide whether a transcription should interrupt the agent.

    - If the agent is not speaking: always interrupt (any speech is valid).
    - If the agent is speaking:
      - Ignore if all words are in the ignored filler list.
      - Interrupt if there is at least one non-filler word.
    """

    def __init__(
        self,
        ignored_words: Iterable[str],
        min_non_filler_chars: int = 2,
    ) -> None:
        # Normalized filler list (words to ignore)
        self.ignored = {w.strip().lower() for w in ignored_words if w.strip()}
        self.min_non_filler_chars = min_non_filler_chars

    def add_ignored_word(self, word: str) -> None:
        """Add a word to the ignored (filler) list."""
        normalized = word.strip().lower()
        if normalized:
            self.ignored.add(normalized)
            logger.info(f"Added '{word}' to ignored words list")

    def remove_ignored_word(self, word: str) -> None:
        """Remove a word from the ignored (filler) list."""
        normalized = word.strip().lower()
        if normalized in self.ignored:
            self.ignored.remove(normalized)
            logger.info(f"Removed '{word}' from ignored words list")

    def update_ignored_words(self, words: Iterable[str]) -> None:
        """Replace the entire ignored words list."""
        self.ignored = {w.strip().lower() for w in words if w.strip()}
        logger.info(f"Updated ignored words list: {self.ignored}")

    def get_ignored_words(self) -> set[str]:
        """Get the current ignored words list."""
        return self.ignored.copy()

    def should_interrupt(
        self,
        text: str,
        agent_is_speaking: bool,
    ) -> bool:
        """
        Return True if this transcription should interrupt the agent.
        """

        # If agent is not speaking, ANY speech is considered valid.
        if not agent_is_speaking:
            return True

        # Basic "low-confidence" / noise heuristic when confidence is not available:
        # treat extremely short transcripts as background murmur.
        normalized = text.strip()
        if len(normalized) < self.min_non_filler_chars:
            return False

        # Tokenize into alphabetic words
        words = re.findall(r"[a-zA-Z]+", normalized.lower())

        if not words:
            # No recognizable words -> treat as noise
            return False

        # If there is at least one non-filler word, treat this as a real interruption.
        for w in words:
            if w not in self.ignored:
                return True

        # All words are fillers from the ignored list -> ignore.
        return False


# uncomment to enable Krisp background voice/noise cancellation
# from livekit.plugins import noise_cancellation

logger = logging.getLogger("basic-agent")

load_dotenv()


class MyAgent(Agent):
    def __init__(self, handler: InterruptionHandler) -> None:
        super().__init__(
            instructions="Your name is Kelly. You would interact with users via voice."
            "with that in mind keep your responses concise and to the point."
            "do not use emojis, asterisks, markdown, or other special characters in your responses."
            "You are curious and friendly, and have a sense of humor."
            "you will speak english to the user",
        )
        self.handler = handler

    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply()

    async def on_user_turn_completed(self, turn_ctx, new_message):
        """
        Override to filter out filler words before they reach the LLM.
        If the user's input is only filler words, we raise StopResponse to skip response generation.
        """
        # Extract text from the new message (only process string content)
        user_text = ""
        for content in new_message.content:
            if isinstance(content, str):
                user_text += content

        # Check if this is only filler words
        normalized = user_text.strip()

        if len(normalized) < self.handler.min_non_filler_chars:
            logger.info(f"Ignoring too-short input: '{user_text}'")
            raise StopResponse()

        # Tokenize into alphabetic words
        words = re.findall(r"[a-zA-Z]+", normalized.lower())

        if not words:
            logger.info(f"Ignoring non-word input: '{user_text}'")
            raise StopResponse()

        # Check if ALL words are fillers
        has_non_filler = False
        for w in words:
            if w not in self.handler.ignored:
                has_non_filler = True
                break

        if not has_non_filler:
            logger.info(f"Ignoring filler-only input: '{user_text}'")
            raise StopResponse()

        # If it's a valid message, proceed with normal response generation
        await super().on_user_turn_completed(turn_ctx, new_message)

    # all functions annotated with @function_tool will be passed to the LLM when this
    # agent is active
    @function_tool
    async def lookup_weather(
        self, context: RunContext, location: str, latitude: str, longitude: str
    ):
        """Called when the user asks for weather related information.
        Ensure the user's location (city or region) is provided.
        When given a location, please estimate the latitude and longitude of the location and
        do not ask the user for them.

        Args:
            location: The location they are asking for
            latitude: The latitude of the location, do not ask user for it
            longitude: The longitude of the location, do not ask user for it
        """

        logger.info(f"Looking up weather for {location}")

        return "sunny with a temperature of 70 degrees."

    def update_handler_words(
        self,
        ignored_words: Optional[list[str]] = None,
    ):
        """Programmatically update the interruption handler word lists.

        This method is NOT a function_tool, so it cannot be called by the LLM
        or triggered by user voice commands. It can only be called programmatically
        from your application code.

        Args:
            ignored_words: New list of words to ignore (optional)
        """
        if ignored_words is not None:
            self.handler.update_ignored_words(ignored_words)


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # each log entry will include these fields
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        stt="deepgram/nova-3",
        # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
        # See all available models at https://docs.livekit.io/agents/models/llm/
        llm="openai/gpt-4.1-mini",
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        tts="cartesia/sonic-2:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        # VAD and turn detection are used to determine when the user is speaking and when the agent should respond
        # See more at https://docs.livekit.io/agents/build/turns
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
        # Interruption strategy: Disable VAD-based auto-interrupts
        # Only allow explicit manual interrupts based on transcript validation
        allow_interruptions=True,
        # Significantly increase the required speech duration to effectively disable VAD auto-interrupt
        # We'll manually control interruptions based on transcript content
        min_interruption_duration=5.0,  # Very high - VAD won't auto-interrupt for normal speech
        min_interruption_words=5,  # Very high - system won't auto-commit interruptions
        resume_false_interruption=False,  # We'll handle everything manually
    )

    # Load filler words to ignore from environment variable
    ignored_fillers_env = os.getenv(
        "IGNORED_FILLERS", "uh,umm,hmm,haan,um,ah,er,like,yeah,mhm,mm,mhmm"
    )
    ignored_fillers = [w.strip() for w in ignored_fillers_env.split(",") if w.strip()]

    handler = InterruptionHandler(
        ignored_words=ignored_fillers,
        min_non_filler_chars=2,
    )

    # Create the agent instance with the handler
    agent_instance = MyAgent(handler)

    # --- SAFE WAYS TO UPDATE WORD LISTS DYNAMICALLY (NOT via voice) ---

    # Method 1: Update via agent method (programmatically)
    # Example: Update based on detected user language or context
    # agent_instance.update_handler_words(
    #     ignored_words=["uh", "um", "er"]
    # )

    # Method 2: Direct handler updates (programmatically)
    # handler.add_ignored_word("whatever")
    # handler.remove_ignored_word("hmm")

    # Method 3: Subscribe to room events and update based on data messages
    # @ctx.room.on("data_received")
    # def on_data_received(data: rtc.DataPacket):
    #     # Only accept word list updates from trusted sources (e.g., admin dashboard)
    #     # NOT from participant voice/chat
    #     if data.topic == "admin_update_words":
    #         config = json.loads(data.data)
    #         if config.get("ignored_words"):
    #             handler.update_ignored_words(config["ignored_words"])

    agent_is_speaking = False

    # log metrics as they are emitted, and total usage after session is over
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev):
        nonlocal agent_is_speaking
        logger.info(f"Agent state: {ev.old_state} → {ev.new_state}")
        if ev.new_state == "speaking":
            agent_is_speaking = True
        else:
            agent_is_speaking = False

    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        logger.info(
            f"User state: {ev.old_state} → {ev.new_state} (agent_is_speaking={agent_is_speaking})"
        )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev):
        text = ev.transcript
        is_final = ev.is_final

        logger.info(
            f"{'FINAL' if is_final else 'INTERIM'} transcript: '{text}' (agent_is_speaking={agent_is_speaking})"
        )

        # Process interim transcripts for manual interruption control
        if not is_final and agent_is_speaking:
            # Check if this transcript should trigger an interruption
            should_interrupt = handler.should_interrupt(text, agent_is_speaking=True)

            if should_interrupt:
                # Valid interruption - manually trigger it
                logger.info(f"  → Interrupting (valid speech)")
                session.interrupt()
            else:
                # Filler detected - ignore
                logger.info(f"  → Ignoring (filler word)")
        elif is_final:
            logger.info(f"  → Final transcript")

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    # shutdown callbacks are triggered when the session is over
    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=agent_instance,  # Use the agent instance we created
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # uncomment to enable the Krisp BVC noise cancellation
                # noise_cancellation=noise_cancellation.BVC(),
            ),
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
