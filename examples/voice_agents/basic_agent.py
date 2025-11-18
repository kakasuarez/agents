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
from livekit.agents.llm import function_tool
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel


import re
from typing import Optional, Iterable


class InterruptionHandler:
    """
    Decide whether a transcription should interrupt the agent.

    - If the agent is not speaking: always interrupt (any speech is valid).
    - If the agent is speaking:
      - Always interrupt if text contains explicit interrupt words (stop, pause, etc.).
      - Ignore if all words are in the ignored filler list.
      - Interrupt if there is at least one non-filler word.
    """

    def __init__(
        self,
        ignored_words: Iterable[str],
        interrupt_words: Iterable[str],
        min_non_filler_chars: int = 2,
    ) -> None:
        # Normalized filler list (words to ignore)
        self.ignored = {w.strip().lower() for w in ignored_words if w.strip()}
        # Normalized interrupt words (words that always trigger interruption)
        self.interrupt = {w.strip().lower() for w in interrupt_words if w.strip()}
        self.min_non_filler_chars = min_non_filler_chars

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

        # Check for explicit interrupt words first (stop, pause, etc.)
        for w in words:
            if w in self.interrupt:
                return True  # Always interrupt on these words

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
    def __init__(self) -> None:
        super().__init__(
            instructions="Your name is Kelly. You would interact with users via voice."
            "with that in mind keep your responses concise and to the point."
            "do not use emojis, asterisks, markdown, or other special characters in your responses."
            "You are curious and friendly, and have a sense of humor."
            "you will speak english to the user",
        )

    async def on_enter(self):
        # when the agent is added to the session, it'll generate a reply
        # according to its instructions
        self.session.generate_reply()

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
        # sometimes background noise could interrupt the agent session, these are considered false positive interruptions
        # when it's detected, you may resume the agent's speech
        resume_false_interruption=True,
        false_interruption_timeout=1.0,
        # Set high min_interruption_duration to prevent automatic VAD-based interruptions
        # We'll manually control interruptions via the user_input_transcribed handler
        min_interruption_duration=999.0,  # Very high to disable auto-interrupt
        # Require at least 1 word - interruptions will only happen via transcripts
        min_interruption_words=1,
        # allow_interruptions=False,
    )

    # Load filler words to ignore from environment variable
    ignored_fillers_env = os.getenv(
        "IGNORED_FILLERS", "uh,umm,hmm,haan,um,ah,er,like,yeah,mhm,mm"
    )
    ignored_fillers = [w.strip() for w in ignored_fillers_env.split(",") if w.strip()]

    # Load explicit interrupt words from environment variable
    interrupt_words_env = os.getenv("INTERRUPT_WORDS", "stop,pause,wait,hold,hold on")
    interrupt_words = [w.strip() for w in interrupt_words_env.split(",") if w.strip()]

    handler = InterruptionHandler(
        ignored_words=ignored_fillers,
        interrupt_words=interrupt_words,
        min_non_filler_chars=2,
    )

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
        print(f"[AGENT STATE] {ev.old_state} → {ev.new_state}")
        if ev.new_state == "speaking":
            agent_is_speaking = True
        else:
            agent_is_speaking = False

    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        print(
            f"[USER STATE] {ev.old_state} → {ev.new_state} (agent_is_speaking={agent_is_speaking})"
        )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev):
        text = ev.transcript
        is_final = ev.is_final

        print(
            f"[{'FINAL' if is_final else 'INTERIM'}] Transcript: '{text}' (agent_is_speaking={agent_is_speaking})"
        )

        # Only process interim transcripts for real-time interruptions
        # Final transcripts are processed for end-of-turn logic
        if not is_final:
            if handler.should_interrupt(text, agent_is_speaking):
                print("  → INTERRUPTED (valid interruption)")
                session.interrupt()
            else:
                print("  → IGNORED (filler word or noise)")
        else:
            # For final transcripts, show what would have happened
            if agent_is_speaking:
                if handler.should_interrupt(text, agent_is_speaking):
                    print("  → (would have interrupted if this was interim)")
                else:
                    print("  → (filler detected - wouldn't interrupt)")

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    # shutdown callbacks are triggered when the session is over
    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=MyAgent(),
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
