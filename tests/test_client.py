import asyncio

import httpx
import pytest

from munim.gnani.client import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    CreditBudgetGuard,
    CreditMeter,
    GnaniAuthError,
    GnaniBadRequest,
    GnaniClient,
    GnaniCreditsExhausted,
    GnaniUnavailable,
)
from munim.gnani.stt import parse_stt


async def _nosleep(_):
    return None


def make(handler, **kw):
    return GnaniClient("test-key", transport=httpx.MockTransport(handler), sleep=_nosleep, **kw)


async def test_stt_ok_sends_headers_and_form():
    seen = {}

    def handler(req: httpx.Request):
        seen["key"] = req.headers["X-API-Key-ID"]
        seen["rid"] = req.headers["X-API-Request-ID"]
        seen["body"] = req.content
        return httpx.Response(200, json={"transcript": "dedh sau", "request_id": "r1"})

    c = make(handler)
    out = await c.stt_raw(b"abc", language_code="hi-IN", keyterms=["Sharma"])
    assert out["transcript"] == "dedh sau"
    assert seen["key"] == "test-key" and len(seen["rid"]) == 32
    assert b"prisma-v3.0" in seen["body"] and b"keyterms" in seen["body"]
    assert c.meter.remaining == 4999


async def test_v25_omits_model_and_format():
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"output": {"literal": "sava sau"}})

    c = make(handler)
    raw = await c.stt_raw(b"abc", language_code="hi-IN", model=None)
    assert b"prisma" not in seen["body"] and b'name="format"' not in seen["body"]
    assert parse_stt(raw, "v2.5").transcript == "sava sau"


async def test_retry_429_then_success():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json={"transcript": "x"})

    c = make(handler)
    assert (await c.stt_raw(b"a", language_code="hi-IN"))["transcript"] == "x"
    assert calls["n"] == 3
    rids = {x["request_id"] for x in c.meter.calls}
    assert len(rids) == 3  # fresh request id per attempt


async def test_gives_up_after_backoffs():
    c = make(lambda r: httpx.Response(503))
    with pytest.raises(GnaniUnavailable):
        await c.stt_raw(b"a", language_code="hi-IN")
    assert len(c.meter.calls) == 4


async def test_504_retried_once_only():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(504)

    c = make(handler)
    with pytest.raises(GnaniUnavailable):
        await c.stt_raw(b"a", language_code="hi-IN")
    assert n["c"] == 2


@pytest.mark.parametrize("status,exc", [(401, GnaniAuthError), (403, GnaniAuthError),
                                        (402, GnaniCreditsExhausted), (400, GnaniBadRequest)])
async def test_fatal_statuses_not_retried(status, exc):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(status, text="nope")

    c = make(handler)
    with pytest.raises(exc):
        await c.tts_raw("hello", voice="Shivani")
    assert n["c"] == 1


async def test_missing_key_is_auth_error():
    c = GnaniClient("", transport=httpx.MockTransport(lambda r: httpx.Response(200)), sleep=_nosleep)
    with pytest.raises(GnaniAuthError):
        await c.stt_raw(b"a", language_code="hi-IN")


async def test_only_one_request_in_flight_and_priority_order():
    active = {"now": 0, "max": 0}
    order = []

    async def slow_handler(req: httpx.Request):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        order.append(req.url.path + ":" + req.headers.get("x-tag", ""))
        active["now"] -= 1
        return httpx.Response(200, json={"transcript": "t"})

    c = GnaniClient("k", transport=httpx.MockTransport(slow_handler), sleep=_nosleep)

    async def call(tag, prio):
        # tag only for ordering assertions
        await c.stt_raw(tag, language_code="hi-IN", priority=prio)

    first = asyncio.create_task(call(b"first", PRIORITY_BATCH))
    await asyncio.sleep(0.005)
    batch = [asyncio.create_task(call(b"batch%d" % i, PRIORITY_BATCH)) for i in range(3)]
    inter = asyncio.create_task(call(b"interactive", PRIORITY_INTERACTIVE))
    await asyncio.gather(first, inter, *batch)
    assert active["max"] == 1


async def test_priority_gate_serves_interactive_first():
    from munim.gnani.client import PriorityGate

    g = PriorityGate()
    await g.acquire(0)  # take the slot
    got = []

    async def waiter(name, prio):
        await g.acquire(prio)
        got.append(name)
        g.release()

    ts = [asyncio.create_task(waiter("batch", PRIORITY_BATCH)),
          asyncio.create_task(waiter("interactive", PRIORITY_INTERACTIVE))]
    await asyncio.sleep(0.01)
    g.release()
    await asyncio.gather(*ts)
    assert got == ["interactive", "batch"]


async def test_credit_floor_blocks_batch_but_not_interactive():
    meter = CreditMeter(remaining=300.5, floor=300.0)
    c = make(lambda r: httpx.Response(200, json={"transcript": "x"}), meter=meter)
    with pytest.raises(CreditBudgetGuard):
        await c.stt_raw(b"a", language_code="hi-IN", priority=PRIORITY_BATCH)
    await c.stt_raw(b"a", language_code="hi-IN", priority=PRIORITY_INTERACTIVE)


async def test_tts_clamps_speed_and_returns_bytes():
    seen = {}

    def handler(req):
        import json

        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OggS....")

    c = make(handler)
    audio = await c.tts_raw("namaste", voice="Shivani", speed=3.0)
    assert audio.startswith(b"OggS")
    assert seen["speed"] == 1.15 and seen["model"] == "timbre-v3.0"
    assert seen["audio_config"] == {"container": "ogg"}
