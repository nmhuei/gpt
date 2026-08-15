from gpt.reverse.redact import Redactor


def test_redacts_cookie_and_authorization():
    redactor = Redactor()
    headers = {
        "Host": "chatgpt.com",
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M",
        "Cookie": "__Secure-next-auth.session-token=secret123; cf_clearance=abc",
        "Content-Type": "application/json",
        "X-Custom-Token": "topsecret",
    }
    redacted = redactor.redact_headers(headers)
    assert redacted["authorization"] == "<REDACTED>"
    assert redacted["cookie"] == "<REDACTED>"
    assert redacted["content-type"] == "application/json"
    assert redacted["x-custom-token"] == "<REDACTED>"
    assert redacted["host"] == "chatgpt.com"


def test_redacts_secret_headers_and_body():
    redactor = Redactor()
    body = {
        "prompt": "Hello world",
        "access_token": "secret_token_val",
        "nested": {
            "password": "mypassword",
            "safe_field": 12345,
        },
    }
    redacted = redactor.redact_json(body)
    assert redacted["access_token"] == "<REDACTED>"
    assert redacted["nested"]["password"] == "<REDACTED>"
    assert redacted["nested"]["safe_field"] == 12345
    assert redacted["prompt"] == "Hello world"


def test_preserves_body_shape_and_normalizes_ids():
    redactor = Redactor()
    payload = {
        "action": "next",
        "conversation_id": "conv-1234-5678",
        "message_id": "msg-9999-0000",
        "model": "auto",
        "content": "test text",
    }
    normalized = redactor.redact_json(payload, normalize_ids=True)
    assert normalized["conversation_id"] == "<CONV_1>"
    assert normalized["message_id"] == "<MSG_1>"
    assert normalized["model"] == "auto"
    assert normalized["content"] == "test text"

    # Same raw IDs should get identical symbols
    payload2 = {
        "conversation_id": "conv-1234-5678",
        "message_id": "msg-different",
    }
    normalized2 = redactor.redact_json(payload2, normalize_ids=True)
    assert normalized2["conversation_id"] == "<CONV_1>"
    assert normalized2["message_id"] == "<MSG_2>"


def test_preserves_non_secret_auth_metadata_and_redacts_query_secrets():
    redactor = Redactor()
    payload = {
        "auth_status": "authenticated",
        "author": "assistant",
        "url": "https://example.test/path?access_token=secret-value&mode=ok",
    }
    result = redactor.redact_json(payload)
    assert result["auth_status"] == "authenticated"
    assert result["author"] == "assistant"
    assert "secret-value" not in result["url"]
    assert "mode=ok" in result["url"]

    challenge = redactor.redact_string(
        "https://chatgpt.com/?__cf_chl_rt_tk=challenge-secret&x=1"
    )
    assert "challenge-secret" not in challenge
    assert "x=1" in challenge
