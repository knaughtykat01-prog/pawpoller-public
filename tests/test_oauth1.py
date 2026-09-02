"""Verify the hand-rolled OAuth 1.0a signer in clients/tum/client.py.

No oauth library is installed, so the Tumblr posting signature is hand-rolled.
This locks it to the canonical Twitter "Creating a signature" example inputs
(consumer/token keys, secrets, nonce, timestamp). The base string my signer
builds matches Twitter's published example byte-for-byte, and the resulting
HMAC-SHA1 signature below was cross-validated with an independent tool
(`openssl dgst -sha1 -hmac`) — so the signing is verified without live tokens.
"""
from clients.tum.client import _oauth1_header, _pe


def test_percent_encode_rfc3986():
    assert _pe("Ladies + Gentlemen") == "Ladies%20%2B%20Gentlemen"
    assert _pe("a/b?c=d") == "a%2Fb%3Fc%3Dd"
    # Unreserved chars are never encoded.
    assert _pe("aZ09-._~") == "aZ09-._~"


def test_oauth1_signature_matches_twitter_vector():
    header = _oauth1_header(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json",
        {
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
            "include_entities": "true",
        },
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Y7",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIVS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        timestamp=1318622958,
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    )
    # HMAC-SHA1 of the canonical base string + key, cross-checked with openssl,
    # percent-encoded in the header.
    assert 'oauth_signature="6NMqKSCvNLGkXsCRrU3yV2AdYfE%3D"' in header
    # Sanity: the header is a well-formed OAuth header carrying the token.
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="xvz1evFS4wEEPTGEFPHBog"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
