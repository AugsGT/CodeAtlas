from codeatlas.reasoning.ollama_client import OllamaClient, OllamaError


def test_generate_returns_text(ollama_client):
    response = ollama_client.generate("Reply with exactly the word: pong", temperature=0.0)
    assert isinstance(response, str)
    assert len(response) > 0


def test_generate_respects_json_format(ollama_client):
    response = ollama_client.generate(
        'Respond with only this JSON object: {"status": "ok"}',
        format="json",
        temperature=0.0,
    )
    import json
    data = json.loads(response)
    assert "status" in data


def test_unreachable_host_raises_ollama_error():
    client = OllamaClient(host="http://localhost:1", timeout=2)
    try:
        client.generate("hi")
        assert False, "expected OllamaError"
    except OllamaError:
        pass
