from agent_wake_claude.wake_hmac import load_keys, sign_body, verify_body


def test_valid_signature_is_accepted():
    body = b"permission"
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert verify_body(body, header, [b"current"], now=1_100)


def test_tampered_body_is_rejected():
    header = sign_body(b"permission", [b"current"], timestamp=1_000)

    assert not verify_body(b"tampered", header, [b"current"], now=1_100)


def test_stale_timestamp_is_rejected_as_replay():
    body = b"permission"
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert not verify_body(body, header, [b"current"], now=1_301)


def test_wrong_key_is_rejected():
    body = b"permission"
    header = sign_body(body, [b"current"], timestamp=1_000)

    assert not verify_body(body, header, [b"wrong"], now=1_100)


def test_missing_header_is_rejected_by_authenticated_receiver():
    assert not verify_body(b"permission", None, [b"current"], now=1_000)


def test_rotation_accepts_previous_key_and_signs_with_first():
    body = b"permission"
    previous = sign_body(body, [b"previous"], timestamp=1_000)
    current = sign_body(body, [b"current", b"previous"], timestamp=1_000)

    assert verify_body(body, previous, [b"current", b"previous"], now=1_100)
    assert verify_body(body, current, [b"current"], now=1_100)
    assert not verify_body(body, current, [b"previous"], now=1_100)


def test_environment_and_config_keys_are_combined_in_order():
    config = {"wake": {"hmac_secret": "config-current,config-old"}}

    assert load_keys(config, {"WAKE_HMAC_SECRET": "env-current,env-old"}) == (
        b"env-current",
        b"env-old",
        b"config-current",
        b"config-old",
    )
