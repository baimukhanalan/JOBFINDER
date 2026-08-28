"""backend/interviews/auth.py: bcrypt hashing + itsdangerous signed-cookie session."""
import backend.interviews.auth as auth


def test_bcrypt_roundtrip():
    h = auth.hash_password("s3cret!")
    assert h != "s3cret!"
    assert auth.verify_password("s3cret!", h) is True
    assert auth.verify_password("wrong", h) is False


def test_session_roundtrip():
    token = auth.make_session(42)
    assert auth.read_session(token) == 42


def test_tampered_session_rejected():
    token = auth.make_session(42)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth.read_session(tampered) is None


def test_expired_session_rejected():
    token = auth.make_session(1)
    assert auth.read_session(token, max_age=-1) is None
