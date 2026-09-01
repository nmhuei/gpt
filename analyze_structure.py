"""Analyze the TriKDF structure for weaknesses."""
import hashlib
import math
import numpy as np

MASTER_SECRET = b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

MIN_SIDE = 1
MAX_SIDE = 65535
ROUNDS = 8
FIXED_ANGLE = math.atan2(4.0, 3.0)


def initial_state():
    digest = hashlib.sha512(b"TriKDF secret state\x00" + MASTER_SECRET).digest()
    state = []
    for position in range(0, len(digest), 8):
        word = int.from_bytes(digest[position:position+8], "big")
        value = ((word + 0.5) / 2**64) * 2.0 - 1.0
        state.append(np.float64(value))
    return np.array(state, dtype=np.float64)


def rotate(state, first, second, angle):
    cosine = np.cos(angle)
    sine = np.sin(angle)
    x = state[first]
    y = state[second]
    state[first] = cosine * x - sine * y
    state[second] = sine * x + cosine * y


# What happens if alpha = 0, beta = 0, gamma = pi (degenerate 1,1,2 triangle)?
# - rotate(0,1,0): identity
# - rotate(2,3,0): identity
# - rotate(4,5,0): identity
# - rotate(6,7,pi): negate pair
# Then FIXED_ANGLE rotations

# Important: in a valid triangle alpha+beta+gamma = pi.
# So for ANY valid triangle, rotate(6,7) is by pi = negation.

# Now what about INVALID triangles? NaN angles propagate through everything as NaN.

# What if we use a triangle like (X, X, X+1)? Valid if X+X > X+1, i.e., X > 1.
# What about (X, X, X)? Always valid (equilateral).

# CRUCIAL: What if we choose triangle (a, b, c) such that the SAME final state is
# produced as some other triangle? Unlikely due to triangle serialization being
# part of the key.

# Let's analyze the relationship between angles and state transformation.
# For a fixed initial_state s, state_final = M(T) * s.
# We have 8-dim state, 8x8 transformation matrix.

# IDEA: What if we use a triangle whose angles make M(T) = I (identity matrix)?
# Then state_final = initial_state, which is unknown.

# But wait - the key includes triangle.serialize(), so even if M=I for two triangles,
# the keys would differ (different serialize() bytes).

# IDEA 2: What if we can find a triangle T where the state_final is all NaN?
# Then abs(state_final) = all NaN, .tobytes() = specific bit pattern (always 0x7ff8...)
# Wait - np.abs(NaN) = NaN, and NaN.tobytes() is 0x7ff8000000000000 (positive quiet NaN).

# So for an invalid triangle, all state components become NaN, and abs(state) = NaN,
# which has a specific bit pattern.

# But - that bit pattern is the same for ALL invalid triangles. And the key derivation
# includes triangle.serialize(), so different triangles give different keys.

# However - what if we can use a "NaN triangle" to encrypt something, and then use
# the SAME key (which we can compute without MASTER_SECRET) to encrypt our known plaintext?
# Yes - this is the attack!

# Let me verify: when alpha, beta, gamma are all NaN, cos(NaN) = NaN, sin(NaN) = NaN.
# Rotations become (NaN*x - NaN*y, NaN*x + NaN*y) = (NaN, NaN).
# So state becomes all NaN regardless of initial_state.
# This means derive_key(triangle) depends ONLY on:
# - triangle.serialize() (we know)
# - np.abs(NaN).tobytes() (which is all 0x00ff8... pattern)

# This is huge! For ANY triangle where alpha, beta, gamma are NaN, the key is the same!
# Wait, no - the key includes triangle.serialize() which differs between triangles.
# So different NaN triangles give different keys.

# But - the key for triangle T with NaN angles is COMPUTABLE without MASTER_SECRET.
# We can compute K_T locally because state_final is fully determined (all NaN).

# Let me verify this is correct.

def derive_key_local(triangle_serialize_bytes):
    """Derive key assuming state is all NaN (no MASTER_SECRET)."""
    nan_state = np.full(8, np.nan, dtype=np.float64)
    return hashlib.sha256(
        b"TriKDF-v1 AES-256\x00"
        + triangle_serialize_bytes
        + np.abs(nan_state).astype("<f8", copy=False).tobytes()
    ).digest()


# Verify NaN handling
print("np.nan bytes:", np.array([np.nan]*8, dtype=np.float64).tobytes().hex())
print("np.abs(nan) bytes:", np.abs(np.full(8, np.nan, dtype=np.float64)).tobytes().hex())
print("np.nan == np.nan:", np.nan == np.nan)
print()

# Test triangle (1, 2, 10) - invalid
import struct
serialize = struct.pack(">III", 1, 2, 10)
key_invalid = derive_key_local(serialize)
print(f"Key for triangle (1,2,10): {key_invalid.hex()}")

# Test triangle (5, 5, 1) - valid? 5+5>1 yes, 5+1>5 no. Invalid.
serialize = struct.pack(">III", 5, 5, 1)
key_invalid2 = derive_key_local(serialize)
print(f"Key for triangle (5,5,1): {key_invalid2.hex()}")

# Both should have NaN bytes but different serialize bytes.
print()
print("NaN bytes are identical:", np.abs(np.full(8, np.nan, dtype=np.float64)).tobytes().hex())
