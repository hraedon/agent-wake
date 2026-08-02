from agent_waked.wake_hmac import load_keys, sign_body, verify_body


def test_valid_signature_is_accepted():
    body = b'{"status":"ok"}'
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert verify_body(body, header, [b"current"], now=1_100)


def test_tampered_body_is_rejected():
    header = sign_body(b"original", [b"current"], timestamp=1_000)

    assert not verify_body(b"tampered", header, [b"current"], now=1_100)


def test_stale_timestamp_is_rejected_as_replay():
    body = b"payload"
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert not verify_body(body, header, [b"current"], now=1_301)


def test_wrong_key_is_rejected():
    body = b"payload"
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert not verify_body(body, header, [b"other"], now=1_100)


def test_missing_header_is_rejected_when_verifying():
    assert not verify_body(b"payload", None, [b"current"], now=1_000)


def test_rotation_accepts_any_key_but_signs_with_first():
    body = b"payload"
    old_header = sign_body(body, [b"previous"], timestamp=1_000)
    new_header = sign_body(body, [b"current", b"previous"], timestamp=1_000)

    assert verify_body(body, old_header, [b"current", b"previous"], now=1_100)
    assert verify_body(body, new_header, [b"current"], now=1_100)
    assert not verify_body(body, new_header, [b"previous"], now=1_100)


def test_keys_combine_environment_and_config_in_rotation_order():
    config = {"wake": {"hmac_secret": ["config-current, config-old"]}}

    assert load_keys(config, {"WAKE_HMAC_SECRET": "env-current, env-old"}) == (
        b"env-current",
        b"env-old",
        b"config-current",
        b"config-old",
    )


def test_no_configured_key_disables_signing():
    assert load_keys({}, {}) == ()
    assert sign_body(b"payload", []) is None
