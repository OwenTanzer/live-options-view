"""Offline object store that enforces the archive publisher's conditionals."""
from hashlib import sha256
from io import BytesIO


class StoreError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeR2Client:
    def __init__(self):
        self.objects = {}
        self.fail_next = None

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise StoreError("NoSuchKey")
        body = self.objects[Key]
        return {"Body": BytesIO(body), "ETag": sha256(body).hexdigest()}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **kwargs):
        if self.fail_next == Key:
            self.fail_next = None
            raise RuntimeError("Injected failure")
        old = self.objects.get(Key)
        if IfNoneMatch and old is not None:
            raise StoreError("PreconditionFailed")
        if IfMatch and (old is None or sha256(old).hexdigest() != IfMatch):
            raise StoreError("PreconditionFailed")
        self.objects[Key] = Body
        return {"ETag": sha256(Body).hexdigest()}
