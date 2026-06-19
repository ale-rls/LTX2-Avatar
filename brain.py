"""
brain.py
--------
The avatar's "brain": turns a transcript of what the person said into a short
spoken reply, in character.

Provider-swappable by design. Set LLM_PROVIDER and the matching env vars;
nothing else changes. Runs on the GPU box alongside the rest of the stack.

  LLM_PROVIDER = qwen3 (default) | openai | anthropic | openai_compatible

  qwen3 (default):
    Qwen3 via an OpenAI-compatible endpoint (Ollama, vLLM, etc.).
    LLM_BASE_URL   default: http://localhost:11434/v1   (Ollama)
    LLM_MODEL      default: qwen3
    LLM_API_KEY    default: ollama  (Ollama ignores it; set for vLLM/cloud)
    Thinking blocks (<think>…</think>) are stripped automatically.

  openai:
    OPENAI_API_KEY, LLM_MODEL (default gpt-4o-mini)

  anthropic:
    ANTHROPIC_API_KEY, LLM_MODEL (default claude-3-5-haiku-latest)

  openai_compatible:
    LLM_BASE_URL, LLM_API_KEY (any string), LLM_MODEL

LATENCY: reply length is the biggest lever. MAX_REPLY_WORDS caps it hard
because a shorter line → shorter TTS → shorter LTX clip → shorter render.
"""

from __future__ import annotations
import os
import re
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

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Remove Qwen3 <think>…</think> blocks and tidy whitespace."""
    return _THINK_RE.sub("", text).strip()


class Brain:
    def __init__(self, system_prompt: str | None = None):
        self.provider = os.environ.get("LLM_PROVIDER", "qwen3").lower()
        self.system = system_prompt or os.environ.get("AVATAR_PERSONA", DEFAULT_SYSTEM)
        self.history: list[dict] = []   # [{role, content}, ...]
        self.max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "8"))

    # -- public -------------------------------------------------------------
    def reply(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        self._trim()
        try:
            if self.provider in ("qwen3", "openai_compatible"):
                base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
                key  = os.environ.get("LLM_API_KEY", "ollama")
                out  = self._openai_compat(base=base, key=key)
            elif self.provider == "anthropic":
                out = self._anthropic()
            else:  # openai
                out = self._openai_compat(base="https://api.openai.com/v1",
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
    def _openai_compat(self, base: str, key: str) -> str:
        default_model = "qwen3" if self.provider == "qwen3" else "gpt-4o-mini"
        model = os.environ.get("LLM_MODEL", default_model)
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
        text = r.json()["choices"][0]["message"]["content"].strip()
        return _strip_thinking(text)

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
