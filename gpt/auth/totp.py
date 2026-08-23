from __future__ import annotations

import pyotp


def generate_totp_code(totp_secret_or_code: str) -> str:
    """Compute 6-digit TOTP code if given a secret seed, or return clean 6-digit string."""
    cleaned = totp_secret_or_code.replace(" ", "").strip()
    if cleaned.isdigit() and len(cleaned) in (6, 8):
        return cleaned

    try:
        totp = pyotp.TOTP(cleaned)
        return totp.now()
    except Exception:
        try:
            padded = cleaned + "=" * ((8 - len(cleaned) % 8) % 8)
            totp = pyotp.TOTP(padded)
            return totp.now()
        except Exception as exc:
            raise ValueError(f"Invalid 2FA TOTP secret key: {exc}") from exc
