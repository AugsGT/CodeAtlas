"""Thin client for a local Ollama server's /api/generate endpoint.

Uses only the stdlib (urllib) rather than adding the `ollama` or
`requests` package as a dependency for what is a single JSON POST.
"""

import json
import urllib.error
import urllib.request


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, model="qwen2.5-coder:7b", host="http://localhost:11434", timeout=180):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str, format=None, temperature: float = 0.0) -> str:
        """Send a single-turn prompt and return the model's text response."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format:
            payload["format"] = format

        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"Could not reach Ollama at {self.host}. Is `ollama serve` running? ({exc})"
            ) from exc

        return data["response"]
