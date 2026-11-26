import itertools
import json
import os
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from common import state_manager
from common.state_manager import load_state, mark_posted, prune_and_save, save_state


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


def _precondition():
    return ClientError(
        {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
        "PutObject",
    )


class FakeR2:
    """In-memory S3/R2 stand-in with ETag + conditional-put semantics."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self._ids = itertools.count(1)
        self.exceptions = type("E", (), {"NoSuchKey": type("NoSuchKey", (Exception,), {})})()

    def _new_etag(self) -> str:
        return f'"etag-{next(self._ids)}"'

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body, etag = self.objects[Key]
        return {"Body": _Body(body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **_):
        exists = Key in self.objects
        current = self.objects[Key][1] if exists else None
        if IfNoneMatch == "*" and exists:
            raise _precondition()
        if IfMatch is not None and (not exists or current != IfMatch):
            raise _precondition()
        etag = self._new_etag()
        data = Body if isinstance(Body, bytes) else Body.encode("utf-8")
        self.objects[Key] = (data, etag)
        return {"ETag": etag}

    # test helper: write a raw state object out-of-band (simulates another runner)
    def seed(self, key, state):
        etag = self._new_etag()
        self.objects[key] = (json.dumps(state).encode("utf-8"), etag)
        return etag

    def load(self, key) -> dict:
        return json.loads(self.objects[key][0].decode("utf-8"))


class MarkPostedTests(unittest.TestCase):
    def test_new_ids_are_prepended_newest_first(self):
        state = {"posted_ids": ["old2", "old1"]}
        mark_posted(state, "new1")
        mark_posted(state, "new2")
        self.assertEqual(state["posted_ids"], ["new2", "new1", "old2", "old1"])

    def test_duplicate_id_is_not_added_again(self):
        state = {"posted_ids": ["a", "b"]}
        mark_posted(state, "a")
        self.assertEqual(state["posted_ids"], ["a", "b"])


class R2StateTests(unittest.TestCase):
    def setUp(self):
        self.r2 = FakeR2()
        state_manager._etags.clear()
        env = patch.dict(os.environ, {"R2_DATA_PREFIX": "pfx", "R2_BUCKET": "b", "APP_CODE": "airlines70"})
        env.start()
        self.addCleanup(env.stop)
        client = patch("common.publish._r2_client", return_value=self.r2)
        client.start()
        self.addCleanup(client.stop)

    def key(self, provider, app="airlines70"):
        return f"pfx/state/{app}/{provider}.json"

    def test_round_trip_at_scoped_key(self):
        save_state("appstore", {"last_review_id": "r1", "reviews": {"r1": {"slack_ts": "1.0"}}})
        self.assertIn(self.key("appstore"), self.r2.objects)
        loaded = load_state("appstore")
        self.assertEqual(loaded["last_review_id"], "r1")
        self.assertEqual(loaded["reviews"]["r1"]["slack_ts"], "1.0")

    def test_missing_object_is_fresh_initial_sync(self):
        loaded = load_state("playstore")
        self.assertEqual(loaded["reviews"], {})
        self.assertEqual(loaded["posted_ids"], [])
        self.assertIsNone(loaded["last_review_id"])

    def test_invalid_json_fails_with_actionable_error(self):
        self.r2.objects[self.key("appstore")] = (b"{", '"e"')
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            load_state("appstore")

    def test_legacy_key_when_app_code_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_CODE", None)
            save_state("playstore", {"reviews": {}})
        self.assertIn("pfx/state/playstore_reviews.json", self.r2.objects)

    def test_first_write_uses_create_only_then_updates(self):
        # First save creates with If-None-Match; a second save reuses the ETag.
        save_state("appstore", {"reviews": {"a": {}}, "posted_ids": ["a"]})
        save_state("appstore", {"reviews": {"a": {}, "b": {}}, "posted_ids": ["b", "a"]})
        self.assertEqual(set(self.r2.load(self.key("appstore"))["reviews"]), {"a", "b"})

    def test_conflict_triggers_merge_and_retry_without_data_loss(self):
        # We create the object, then another runner overwrites it with a DIFFERENT
        # review. Our next save has a stale ETag -> 412 -> re-read + merge + retry.
        save_state("appstore", {"reviews": {"mine": {"posted_at": "t"}}, "posted_ids": ["mine"]})
        # another writer lands a reply on a different review:
        self.r2.seed(
            self.key("appstore"),
            {"reviews": {"theirs": {"posted_at": "t", "google_reply_sent": True}}, "posted_ids": ["theirs"]},
        )
        # our in-memory state adds yet another field to "mine" and saves:
        mine = {"reviews": {"mine": {"posted_at": "t", "note": "x"}}, "posted_ids": ["mine"]}
        save_state("appstore", mine)
        persisted = self.r2.load(self.key("appstore"))
        # both reviews survived the race, posted_ids unioned
        self.assertEqual(set(persisted["reviews"]), {"mine", "theirs"})
        self.assertEqual(set(persisted["posted_ids"]), {"mine", "theirs"})
        # and the in-memory dict was updated to the merged result
        self.assertEqual(set(mine["reviews"]), {"mine", "theirs"})

    def test_prune_and_save_skips_pristine_state(self):
        prune_and_save("appstore", {"reviews": {}, "posted_ids": []})
        self.assertNotIn(self.key("appstore"), self.r2.objects)  # no empty object created

    def test_prune_and_save_drops_inactive_reviews_but_keeps_ids(self):
        old = "2000-01-01T00:00:00+00:00"
        state = {
            "reviews": {"stale": {"posted_at": old}},  # way past the open-poll window
            "posted_ids": ["stale"],
            "baselined": True,
        }
        prune_and_save("appstore", state)
        persisted = self.r2.load(self.key("appstore"))
        self.assertEqual(persisted["reviews"], {})  # pruned off the dashboard set
        self.assertIn("stale", persisted["posted_ids"])  # but never re-posted


if __name__ == "__main__":
    unittest.main()
