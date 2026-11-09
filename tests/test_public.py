from munim.config import Settings
from munim.public.break_munim import trace
from munim.public.caps import PublicCaps
from munim.public.pii import contains_pii, redact


def test_pii_redaction():
    t = "call 9876543210 or mail a.b@example.com aadhaar 1234 5678 9012 upi ram@okaxis PAN ABCDE1234F"
    r = redact(t)
    for bad in ("9876543210", "example.com", "1234 5678 9012", "okaxis", "ABCDE1234F"):
        assert bad not in r
    assert contains_pii(t) and not contains_pii("Sharma ji ko 150 udhaar")


def test_caps_user_global_killswitch_and_rollover():
    now = [1_800_000_000.0]
    c = PublicCaps(per_user=3, global_cap=4, clock=lambda: now[0])
    for _ in range(3):
        assert c.check(1)[0]
        c.consume(1)
    assert c.check(1) == (False, "user_cap")
    c.consume(2)
    assert c.check(3) == (False, "global_cap")
    now[0] += 86400
    assert c.check(1)[0] and c.remaining(1) == 3
    c.kill()
    assert c.check(1) == (False, "killed")


def test_trace_clean_and_blocked():
    cfg = Settings()
    ok = trace("Sunita Verma ko dedh sau udhaar", "Sunita Verma ko dedh sau udhaar", 25.0, 3.0, cfg)
    assert "would WRITE" in ok and "₹150" in ok
    bad = trace("Sunita Verma ko dedh sau udhaar", "Sunita Verma ko dedh hazaar udhaar", 25.0, 3.0, cfg)
    assert "scale_risk" in bad and "nothing written" in bad
    amb = trace("Sharma ji ko 100 udhaar", "Sharma ji ko 100 udhaar", 25.0, 3.0, cfg)
    assert "ambiguous" in amb
