import asyncio
import os
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import ratelimit
from ratelimit import RateLimiter, classify


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limiter(clock, **limits):
    base = {k: v for k, v in ratelimit.DEFAULTS.items()}
    base.update(limits)
    return RateLimiter(base, clock)


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_limit_and_recovers_after_window(self):
        clk = FakeClock()
        rl = limiter(clk, commands=(3, 60))
        self.assertTrue(all(rl.allow(1, "commands") for _ in range(3)))
        self.assertFalse(rl.allow(1, "commands"))
        clk.t += 61
        self.assertTrue(rl.allow(1, "commands"))

    def test_users_are_isolated(self):
        clk = FakeClock()
        rl = limiter(clk, commands=(1, 60))
        self.assertTrue(rl.allow(1, "commands"))
        self.assertFalse(rl.allow(1, "commands"))
        self.assertTrue(rl.allow(2, "commands"))   # other user unaffected

    def test_categories_are_isolated(self):
        clk = FakeClock()
        rl = limiter(clk, commands=(1, 60))
        self.assertTrue(rl.allow(1, "commands"))
        self.assertTrue(rl.allow(1, "photos"))

    def test_album_of_ten_photos_is_allowed_by_default(self):
        rl = RateLimiter(ratelimit.DEFAULTS, FakeClock())
        self.assertTrue(all(rl.allow(7, "photos") for _ in range(10)))

    def test_notice_only_once_per_cooldown(self):
        clk = FakeClock()
        rl = limiter(clk)
        self.assertTrue(rl.should_notify(1, "commands"))
        self.assertFalse(rl.should_notify(1, "commands"))
        clk.t += ratelimit.NOTICE_COOLDOWN + 1
        self.assertTrue(rl.should_notify(1, "commands"))

    def test_env_override_and_bad_values_fall_back(self):
        with mock.patch.dict(os.environ, {"RATE_LIMIT_COMMANDS": "5/10",
                                          "RATE_LIMIT_PHOTOS": "garbage"}):
            limits = ratelimit.load_limits()
        self.assertEqual(limits["commands"], (5, 10.0))
        self.assertEqual(limits["photos"], ratelimit.DEFAULTS["photos"])

    def test_memory_is_bounded(self):
        clk = FakeClock()
        rl = limiter(clk)
        with mock.patch.object(ratelimit, "MAX_TRACKED", 10):
            for uid in range(50):
                rl.allow(uid, "messages")
                clk.t += 1000
            self.assertLess(len(rl._hits), 20)


class ClassifyTests(unittest.TestCase):
    def test_classification(self):
        user = NS(id=5)
        self.assertEqual(classify(NS(effective_user=user, callback_query=object(),
                                     effective_message=None)), (5, "callbacks"))
        self.assertEqual(classify(NS(effective_user=user, callback_query=None,
                                     effective_message=NS(photo=[1], text=None))), (5, "photos"))
        self.assertEqual(classify(NS(effective_user=user, callback_query=None,
                                     effective_message=NS(photo=None, text="/vibe"))), (5, "commands"))
        self.assertEqual(classify(NS(effective_user=user, callback_query=None,
                                     effective_message=NS(photo=None, text="hi"))), (5, "messages"))
        self.assertEqual(classify(NS(effective_user=None)), (None, None))


class GateTests(unittest.TestCase):
    def test_gate_blocks_then_notifies_once(self):
        try:
            from telegram.ext import ApplicationHandlerStop
        except ImportError:
            self.skipTest("python-telegram-bot not installed here")
        replies = []

        async def reply_text(text):
            replies.append(text)

        update = NS(effective_user=NS(id=9), callback_query=None,
                    effective_message=NS(photo=None, text="/vibe", reply_text=reply_text))
        gate = ratelimit.make_gate(limiter(FakeClock(), commands=(1, 60)))

        async def run():
            await gate(update, None)                      # allowed
            for _ in range(3):
                with self.assertRaises(ApplicationHandlerStop):
                    await gate(update, None)              # blocked

        asyncio.run(run())
        self.assertEqual(len(replies), 1)                 # one notice, not a flood


if __name__ == "__main__":
    unittest.main()
