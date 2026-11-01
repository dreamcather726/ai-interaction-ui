"""Bounded audio job admission and deterministic replay detection; no ROS node."""

from collections import deque
import hashlib
import json


class AudioJobQueue:
    def __init__(self):
        self.queue = deque()
        self.seen_requests = {}
        self.seen_inputs = {}

    def __bool__(self):
        return bool(self.queue)

    def __len__(self):
        return len(self.queue)

    def popleft(self):
        return self.queue.popleft()

    def clear(self):
        self.queue.clear()

    def enqueue(self, msg, path, pcm, active=False):
        """Keep source context and reject replay conflicts before any AI call."""
        job = {'msg': msg, 'path': path.resolve()}
        # Hash semantic values, not CDR padding bytes, which may vary per receive.
        metadata = [getattr(msg, field) for field in (
            'request_id', 'session_id', 'segment_id', 'input_context_id',
            'sample_rate', 'channels', 'encoding')]
        metadata.extend((getattr(msg, field).sec, getattr(msg, field).nanosec)
                        for field in ('stamp', 'capture_started_at', 'capture_ended_at'))
        digest = hashlib.sha256(json.dumps(metadata).encode('utf-8'))
        digest.update(pcm)
        fingerprint = digest.hexdigest()
        key = (msg.session_id, msg.segment_id)
        for cache, identity, code in (
                (self.seen_requests, msg.request_id, 'REQUEST_ID_CONFLICT'),
                (self.seen_inputs, key, 'INPUT_ID_CONFLICT')):
            if identity in cache:
                same = cache[identity] == fingerprint
                return (same, '' if same else code,
                        'Duplicate input: already registered, not resubmitted' if same
                        else 'Input identity was reused with different content')
        if len(self.queue) + bool(active) >= 10:
            return False, 'PIPELINE_BUSY', 'Audio saved; AI queue is full'
        if len(self.seen_requests) >= 1024:
            return False, 'RECOVERY_REQUIRED', 'Audio saved; deduplication cache is full'
        self.seen_requests[msg.request_id] = fingerprint
        self.seen_inputs[key] = fingerprint
        self.queue.append(job)
        return True, '', 'Audio accepted by AI; processing queued'
