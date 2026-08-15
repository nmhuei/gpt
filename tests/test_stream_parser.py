import pytest

from gpt.reverse.stream_parser import ObservedStreamParser, SSEDecoder, StreamContract
from gpt.state import ProtocolChanged


def test_sse_decoder_handles_split_utf8_and_records():
    decoder = SSEDecoder()
    encoded = 'data: {"text":"xin chào 👋"}\n\n'.encode()
    split = encoded.index("👋".encode()) + 1
    assert decoder.feed(encoded[:split]) == []
    assert decoder.feed(encoded[split:]) == ['{"text":"xin chào 👋"}']


def test_observed_stream_parser_tolerates_unknown_fields_and_requires_completion():
    contract = StreamContract(
        text_path=("message", "text"),
        status_path=("state",),
        completion_values=frozenset({"complete"}),
    )
    parser = ObservedStreamParser(contract)
    assert parser.feed('data: {"unknown": 1}\n\n') == []
    assert parser.feed('data: {"message":{"text":"Hel"}}\n\n') == ["Hel"]
    assert parser.feed('data: {"message":{"text":"Hello"},"extra":true}\n\n') == ["lo"]
    parser.feed('data: {"state":"complete"}\n\n')
    assert parser.finish() == "Hello"

    incomplete = ObservedStreamParser(contract)
    incomplete.feed('data: {"message":{"text":"partial"}}\n\n')
    with pytest.raises(ProtocolChanged):
        incomplete.finish()
