import contextlib
import os
import unittest

from app.auth import hash_token
from app.config import Settings

_TOKEN_KEYS = ("SANDBOX_TOKEN", "SANDBOX_TOKEN_SHA256")


@contextlib.contextmanager
def _token_env(**overrides: str):
    """Run with only the given token env vars set, restoring the prior state."""
    saved = {key: os.environ.get(key) for key in _TOKEN_KEYS}
    for key in _TOKEN_KEYS:
        os.environ.pop(key, None)
    for key, value in overrides.items():
        os.environ[key] = value
    try:
        yield
    finally:
        for key in _TOKEN_KEYS:
            os.environ.pop(key, None)
            if saved[key] is not None:
                os.environ[key] = saved[key]


class TokenResolutionTests(unittest.TestCase):
    def test_plaintext_token_is_hashed(self) -> None:
        with _token_env(SANDBOX_TOKEN="my-secret"):
            self.assertEqual(Settings.from_env().token_sha256, hash_token("my-secret"))

    def test_plaintext_overrides_precomputed_hash(self) -> None:
        with _token_env(SANDBOX_TOKEN="plaintext-wins", SANDBOX_TOKEN_SHA256="0" * 64):
            self.assertEqual(
                Settings.from_env().token_sha256, hash_token("plaintext-wins")
            )

    def test_falls_back_to_precomputed_hash(self) -> None:
        digest = hash_token("some-token")
        with _token_env(SANDBOX_TOKEN_SHA256=digest):
            self.assertEqual(Settings.from_env().token_sha256, digest)

    def test_plaintext_is_stripped_like_bearer_header(self) -> None:
        # The auth path strips "Bearer <token>" before hashing, so the plaintext
        # env var must strip identically to stay in sync.
        with _token_env(SANDBOX_TOKEN="  spaced  "):
            self.assertEqual(Settings.from_env().token_sha256, hash_token("spaced"))

    def test_invalid_precomputed_hash_raises(self) -> None:
        with _token_env(SANDBOX_TOKEN_SHA256="not-a-valid-sha256"):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_unset_yields_empty_hash(self) -> None:
        with _token_env():
            self.assertEqual(Settings.from_env().token_sha256, "")


if __name__ == "__main__":
    unittest.main()
