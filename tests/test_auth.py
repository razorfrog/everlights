import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth


class AuthTests(unittest.TestCase):
    def test_password(self):
        stored = auth.hash_password("correct horse")
        self.assertTrue(auth.verify_password("correct horse", stored))
        self.assertFalse(auth.verify_password("wrong horse", stored))
        self.assertFalse(auth.verify_password("x", None))
        self.assertNotEqual(stored, auth.hash_password("correct horse"))  # salted
        with self.assertRaises(ValueError):
            auth.hash_password("short")

    def test_session(self):
        s = auth.make_session("secret", now=1000)
        self.assertTrue(auth.check_session("secret", s, now=2000))
        self.assertFalse(auth.check_session("other", s, now=2000))
        self.assertFalse(auth.check_session("secret", s, now=1000 + 200 * 86400))
        self.assertFalse(auth.check_session("secret", "99999999999.forged", now=2000))
        self.assertFalse(auth.check_session("", s, now=2000))

    def test_token(self):
        self.assertTrue(auth.check_token("abc", "Bearer abc"))
        self.assertFalse(auth.check_token("abc", "Bearer abd"))
        self.assertFalse(auth.check_token("", "Bearer "))
        self.assertFalse(auth.check_token("abc", None))

    def test_limiter(self):
        lim = auth.LoginLimiter()
        for _ in range(auth.MAX_FAILURES):
            self.assertFalse(lim.blocked("1.2.3.4", now=100))
            lim.failed("1.2.3.4", now=100)
        self.assertTrue(lim.blocked("1.2.3.4", now=101))
        self.assertFalse(lim.blocked("5.6.7.8", now=101))
        self.assertFalse(lim.blocked("1.2.3.4", now=100 + auth.FAILURE_WINDOW + 1))


if __name__ == "__main__":
    unittest.main()
