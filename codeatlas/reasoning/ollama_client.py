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
        """Initialize the client with default values for model, host, and timeout."""
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str, format=None, temperature: float = 0.0) -> str:
        """Send a single-turn prompt to the Ollama server and return the response text.

        Args:
            prompt (str): The input text to send to the model.
            format (str, optional): The desired output format. Defaults to None.
            temperature (float, optional): Controls randomness of generated text. Defaults to 0.0.

        Returns:
            str: The text response from the Ollama model.
        """
        # Create a payload dictionary with necessary parameters
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format:
            payload["format"] = format

        # Prepare the request to the Ollama server
        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        try:
            # Send the request and receive the response
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.URLError as exc:
            # Raise a custom error if the server is not reachable
            raise OllamaError(
                f"Could not reach Ollama at {self.host}. Is `ollama serve` running? ({exc})"
            ) from exc

        # Return the response text from the model
        return data["response"]