"""Anti-bot block detection: header markers, gated body markers, status fallbacks."""

from __future__ import annotations

from websearch.layer2_extract.blocks import detect_block, title_looks_like_error


def test_clean_200_is_not_blocked():
    blocked, reason = detect_block(200, "<html><body><p>real content</p></body></html>", {})
    assert blocked is False
    assert reason is None


def test_cf_mitigated_header_fires_regardless_of_status():
    blocked, reason = detect_block(200, "<html>ok</html>", {"cf-mitigated": "challenge"})
    assert blocked is True
    assert reason == "cloudflare_challenge"


def test_cloudflare_body_marker_on_403():
    blocked, reason = detect_block(
        403, "<title>Just a moment...</title> checking your browser before you access", {}
    )
    assert blocked is True
    assert reason == "cloudflare_challenge"


def test_datadome_header():
    blocked, reason = detect_block(403, "denied", {"x-datadome": "protected"})
    assert (blocked, reason) == (True, "datadome")


def test_perimeterx_header():
    blocked, reason = detect_block(403, "denied", {"x-px-authorization": "3"})
    assert (blocked, reason) == (True, "perimeterx")


def test_imperva_blocks_with_http_200_via_body():
    # Imperva is the exception: it serves a block with a 200, so its body markers
    # must be scanned even on a successful status.
    body = "Request unsuccessful. Incapsula incident ID: 1234-5678"
    blocked, reason = detect_block(200, body, {})
    assert (blocked, reason) == (True, "imperva")


def test_imperva_xiinfo_header_alone_is_not_a_block():
    # x-iinfo / x-cdn:incapsula are on EVERY Imperva-proxied response, not just blocks,
    # so a header-only rule would false-positive on all Imperva-fronted sites.
    blocked, reason = detect_block(
        200, "<html><body><p>real content</p></body></html>", {"x-iinfo": "9-12345"}
    )
    assert blocked is False
    blocked, _ = detect_block(200, "<html><body><p>real</p></body></html>", {"x-cdn": "Incapsula"})
    assert blocked is False


def test_datadome_interstitial_on_large_200_is_caught():
    # A >30KB DataDome page on HTTP 200 without a vendor header is still detected
    # because the vendor domain is an always-scanned marker.
    body = "<html><body>" + ("filler " * 5000) + "geo.captcha-delivery.com</body></html>"
    assert len(body) > 30_000
    blocked, reason = detect_block(200, body, {})
    assert (blocked, reason) == (True, "datadome")


def test_akamai_body_marker_is_classified_as_akamai():
    blocked, reason = detect_block(403, "<html>powered by AkamaiGHost edge</html>", {})
    assert (blocked, reason) == (True, "akamai")


def test_large_clean_page_mentioning_cloudflare_is_not_blocked():
    # False-positive guard: a big 200 article that merely links to cloudflare must
    # not be flagged (body markers are gated behind is_short / status shortlist).
    body = (
        "<html><body>" + ("real article text " * 4000) + "challenges.cloudflare.com</body></html>"
    )
    assert len(body) > 30_000
    blocked, reason = detect_block(200, body, {})
    assert blocked is False


def test_429_is_rate_limited_terminal():
    blocked, reason = detect_block(429, "slow down", {})
    assert (blocked, reason) == (True, "rate_limited")


def test_401_is_auth_required_not_a_block():
    blocked, reason = detect_block(401, "login required", {})
    assert blocked is False
    assert reason == "auth_required"


def test_451_is_legal_geo_not_a_block():
    blocked, reason = detect_block(451, "unavailable for legal reasons", {})
    assert blocked is False
    assert reason == "legal_geo_block"


def test_403_short_nonsubstantive_is_suspected_bot():
    blocked, reason = detect_block(403, "Forbidden", {})
    assert (blocked, reason) == (True, "forbidden_suspected_bot")


def test_503_short_without_retry_after_is_suspected_block():
    blocked, reason = detect_block(503, "<html><body>nope</body></html>", {})
    assert (blocked, reason) == (True, "unavailable_suspected_block")


def test_503_with_retry_after_is_transient_not_block():
    blocked, reason = detect_block(
        503, "<html><body>maintenance</body></html>", {"retry-after": "30"}
    )
    assert blocked is False


def test_akamai_server_header_on_403():
    blocked, reason = detect_block(403, "Access Denied", {"server": "AkamaiGHost"})
    assert (blocked, reason) == (True, "akamai")


def test_title_looks_like_error():
    assert title_looks_like_error("404 Not Found")
    assert title_looks_like_error("Just a moment...")
    assert title_looks_like_error("Access Denied")
    assert not title_looks_like_error("Understanding Rust Ownership")
    assert not title_looks_like_error(None)
