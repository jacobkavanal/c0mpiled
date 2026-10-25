"""Minimal stand-in for AsyncOpenAI's streaming chat completions."""

from types import SimpleNamespace


def text_chunk(text, finish_reason=None):
    return chunk(SimpleNamespace(content=text, tool_calls=None), finish_reason)


def tool_chunk(index, *, call_id=None, name=None, arguments=None, extra=None, finish_reason=None):
    call = SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
        model_extra=extra or {},
    )
    return chunk(SimpleNamespace(content=None, tool_calls=[call]), finish_reason)


def finish_chunk(finish_reason):
    return chunk(SimpleNamespace(content=None, tool_calls=None), finish_reason)


def chunk(delta, finish_reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response

        async def stream():
            for item in response:
                yield item

        return stream()


def fake_openai(*responses):
    completions = FakeCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions
