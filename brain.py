"""
brain.py
--------
The avatar's "brain": turns a transcript of what the person said into a short
spoken reply, in character.

Provider-swappable by design (your requirement). Set LLM_PROVIDER and the
matching API key in the environment; nothing else changes. Runs OFF the GPU box
so it costs no VRAM and stays fast.

  LLM_PROVIDER = openai | anthropic | openai_compatible
  - openai:            OPENAI_API_KEY,  LLM_MODEL (default gpt-4o-mini)
  - anthropic:         ANTHROPIC_API_KEY, LLM_MODEL (default claude-3-5-haiku-latest)
  - openai_compatible: LLM_BASE_URL (e.g. a local vLLM/Ollama OpenAI endpoint),
                       LLM_API_KEY (any string if unused), LLM_MODEL

Uses each provider's HTTP API directly via `requests` so you don't have to pin
SDK versions on the GPU box. (FluxRT lesson: depend on the real, stable API
surface, not on a guessed SDK signature.)

LATENCY: reply length is the biggest lever you control in code. MAX_REPLY_WORDS
caps it hard, because a shorter line -> shorter TTS -> shorter LTX clip ->
shorter render. Keep the system prompt instructing brevity too.
"""

from __future__ import annotations
import os
import json
import requests


DEFAULT_SYSTEM = (
    "You are a live theatrical avatar in a stage performance. You are speaking "
    "OUT LOUD to a person in front of you. Reply with ONE or TWO short spoken "
    "sentences, in character, conversational and vivid. No stage directions, no "
    "emojis, no markdown, no quotation marks around your reply, no narration of "
    "your own actions. Just the words you say aloud."
)

MAX_REPLY_WORDS = int(os.environ.get("MAX_REPLY_WORDS", "40"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30"))


class Brain:
    def __init__(self, system_prompt: str | None = None):
        self.provider = os.environ.get("LLM_PROVIDER", "openai").lower()
        self.system = system_prompt or os.environ.get("AVATAR_PERSONA", DEFAULT_SYSTEM)
        self.history: list[dict] = []   # [{role, content}, ...]
        self.max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "8"))

    # -- public -------------------------------------------------------------
    def reply(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        self._trim()
        try:
            if self.provider == "anthropic":
                out = self._anthropic()
            elif self.provider == "openai_compatible":
                out = self._openai(base=os.environ["LLM_BASE_URL"],
                                   key=os.environ.get("LLM_API_KEY", "x"))
            else:  # openai
                out = self._openai(base="https://api.openai.com/v1",
                                   key=os.environ["OPENAI_API_KEY"])
        except Exception as e:
            # Never let the show hang on a brain error; say something neutral.
            print(f"[brain] error: {e}")
            out = "Hm. Say that again?"
        out = self._cap(out)
        self.history.append({"role": "assistant", "content": out})
        return out

    def reset(self):
        self.history.clear()

    # -- providers ----------------------------------------------------------
    def _openai(self, base: str, key: str) -> str:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        msgs = [{"role": "system", "content": self.system}] + self.history
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": msgs,
                  "max_tokens": 120, "temperature": 0.8},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _anthropic(self) -> str:
        model = os.environ.get("LLM_MODEL", "claude-3-5-haiku-latest")
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": model, "system": self.system,
                  "messages": self.history, "max_tokens": 120,
                  "temperature": 0.8},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    # -- helpers ------------------------------------------------------------
    def _trim(self):
        # keep last N user+assistant pairs
        keep = self.max_turns * 2
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    @staticmethod
    def _cap(text: str) -> str:
        text = text.strip().strip('"').strip()
        words = text.split()
        if len(words) > MAX_REPLY_WORDS:
            text = " ".join(words[:MAX_REPLY_WORDS]).rstrip(",;:") + "."
        return text


if __name__ == "__main__":
    b = Brain()
    print(b.reply("Who are you and why are you here?"))
