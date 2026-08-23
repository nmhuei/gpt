from gpt.auth.authenticator import LoginCredentials
from gpt.auth.totp import generate_totp_code


def test_generate_totp_code_direct_numeric():
    assert generate_totp_code("123456") == "123456"
    assert generate_totp_code(" 654321 ") == "654321"

def test_generate_totp_code_from_base32():
    # Standard RFC base32 secret
    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp_code(secret)
    assert len(code) == 6
    assert code.isdigit()

def test_login_credentials_from_string():
    cred = LoginCredentials.from_string("test@example.com|mypassword|TOTP123")
    assert cred.username == "test@example.com"
    assert cred.password == "mypassword"
    assert cred.totp_secret_or_code == "TOTP123"
