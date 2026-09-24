"""SECRET_PATTERNS (2026-09-18, code review): every family matches one synthetic sample and ordinary prose does not.
Samples are BUILT at run time by concatenation so the repository never carries a token-shaped literal."""
import pytest

from flux_brain.lib.secrets import SECRET_PATTERNS

A, D = "A" * 40, "1" * 20
SAMPLES = {
    "discord webhook": "https://discord" + ".com/api/webhooks/" + D + "/" + A + A,
    "cloudflare token": "cf" + "ut_" + A,
    "private key header": "BEGIN " + "RSA PRIVATE KEY",
    "google api key": "AIza" + "Sy" + A,
    "google oauth access token": "ya" + "29." + A,
    "google oauth refresh token": "1//" + "0" + A,
    "jwt": "ey" + "J" + A + "." + "ey" + "J" + A + "." + A,
    "keep master token": "aas" + "_et/" + A,
    "github token": "gh" + "p_" + A,
    "github fine-grained": "github" + "_pat_" + A,
    "anthropic key": "sk-" + "ant-" + A,
    "openrouter key": "sk-" + "or-" + A,
    "slack token": "xox" + "b-" + D + "-" + D,
    "aws access key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "stripe live key": "sk_" + "live_" + A,
    "gitlab pat": "glpat" + "-" + A,
    "npm token": "npm" + "_" + ("a1" * 18),
}
CLEAN = [
    "the Discord token is read at run time from flux.env, never written to disk",
    "the trigger token lives in flux.env (mode 600); token: rotated 2026-09-15",
    "password reset link sent; API key management is in the Settings page",
    "Drive folder 1AbCdEfGhIjKlMnOpQrStUvWxYz012345, shared drive 0AZzYyXxWwVvUuUk9PVA",
    "message id 1550276575860363285, session 1a0a9752acb.49cbaa21ee94a049",
    "https://discord.com/channels/100000000000000002/100000000000000003",
    "ya29 is the prefix Google uses; eyJ starts a base64 JSON header",
]


@pytest.mark.parametrize("family", sorted(SAMPLES))
def test_matches(family):
    assert SECRET_PATTERNS.search("see " + SAMPLES[family] + " here") is not None


@pytest.mark.parametrize("text", CLEAN)
def test_clean_prose_does_not_match(text):
    assert SECRET_PATTERNS.search(text) is None


def test_redaction_keeps_the_surrounding_text():
    assert SECRET_PATTERNS.sub("[REDACTED]", "key " + SAMPLES["jwt"] + " end") == "key [REDACTED] end"
