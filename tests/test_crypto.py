import pytest

from amagi_crm.config import ConfigError, Settings
from amagi_crm.crypto import DecryptionError, FieldCipher, generate_fernet_key, generate_secret


def test_round_trip_with_a_fresh_iv_per_write():
    c = FieldCipher([generate_fernet_key()], generate_secret())
    a, b = c.encrypt("Dana Whitfield"), c.encrypt("Dana Whitfield")
    assert a != b
    assert c.decrypt(a) == c.decrypt(b) == "Dana Whitfield"
    assert "Dana" not in a


def test_wrong_key_cannot_decrypt():
    secret = generate_secret()
    token = FieldCipher([generate_fernet_key()], secret).encrypt("private")
    with pytest.raises(DecryptionError):
        FieldCipher([generate_fernet_key()], secret).decrypt(token)


def test_key_list_reads_old_values_and_writes_with_the_first_key():
    old, new, secret = generate_fernet_key(), generate_fernet_key(), generate_secret()
    token = FieldCipher([old], secret).encrypt("value")
    rotated = FieldCipher([new, old], secret)
    assert rotated.decrypt(token) == "value"
    assert not rotated.is_primary(token)
    assert rotated.is_primary(rotated.encrypt("value"))


def test_blind_index_is_stable_normalized_and_keyed():
    secret = generate_secret()
    c = FieldCipher([generate_fernet_key()], secret)
    assert c.blind_index(" Lee@Example.com ") == c.blind_index("lee@example.com")
    assert len(c.blind_index("lee@example.com")) == 64
    assert c.blind_index(None) is None and c.blind_index("") is None
    other = FieldCipher([generate_fernet_key()], generate_secret())
    assert other.blind_index("lee@example.com") != c.blind_index("lee@example.com")


def test_audit_key_differs_from_index_key():
    c = FieldCipher([generate_fernet_key()], generate_secret())
    assert c.audit_key != c._index_key


def test_invalid_or_missing_key_rejected():
    with pytest.raises(ValueError):
        FieldCipher(["not-a-fernet-key"], generate_secret())
    with pytest.raises(ValueError):
        FieldCipher([], generate_secret())


def test_settings_fail_closed_without_keys():
    with pytest.raises(ConfigError, match="AMAGI_CRM_ENCRYPTION_KEYS"):
        Settings.load(env_file=None, environ={}).require_keys()
    short = Settings.load(env_file=None, environ={"AMAGI_CRM_ENCRYPTION_KEYS": generate_fernet_key(),
                                                  "AMAGI_CRM_SECRET_KEY": "too-short"})
    with pytest.raises(ConfigError, match="AMAGI_CRM_SECRET_KEY"):
        short.require_keys()


def test_key_list_is_comma_separated():
    a, b = generate_fernet_key(), generate_fernet_key()
    s = Settings.load(env_file=None, environ={"AMAGI_CRM_ENCRYPTION_KEYS": f"{a}, {b},"})
    assert s.encryption_keys == [a, b]


def test_env_vars_override_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('AMAGI_CRM_TIMEZONE="Europe/Lisbon"\n# a comment\nAMAGI_CRM_SLOT_MINUTES=45\n'
                   "AMAGI_CRM_PUBLIC_BOOKING=yes\n", encoding="utf-8")
    s = Settings.load(env, environ={"AMAGI_CRM_SLOT_MINUTES": "20"})
    assert s.timezone == "Europe/Lisbon"
    assert s.slot_minutes == 20
    assert s.public_booking is True
    assert s.feed_details is False
    with pytest.raises(ConfigError):
        Settings.load(env, environ={"AMAGI_CRM_SLOT_MINUTES": "half an hour"})
