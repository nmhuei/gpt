import pyotp

from gpt.auth import AutoLoginManager, LoginCredentials


def test_login_credentials_parsing():
    # Format: user|pass|2fa
    c1 = LoginCredentials.from_string("myuser@test.com|secret_pass|JBSWY3DPEHPK3PXP")
    assert c1.username == "myuser@test.com"
    assert c1.password == "secret_pass"
    assert c1.totp_secret_or_code == "JBSWY3DPEHPK3PXP"

    # Format: user:pass
    c2 = LoginCredentials.from_string("admin:password123")
    assert c2.username == "admin"
    assert c2.password == "password123"
    assert c2.totp_secret_or_code is None


def test_generate_totp_code():
    # 1. Plain 6-digit code returns unchanged
    assert AutoLoginManager.generate_totp_code("123456") == "123456"

    # 2. Secret seed generates valid 6-digit TOTP
    secret = pyotp.random_base32()
    expected_code = pyotp.TOTP(secret).now()
    generated_code = AutoLoginManager.generate_totp_code(secret)
    assert generated_code == expected_code
    assert len(generated_code) == 6
    assert generated_code.isdigit()
