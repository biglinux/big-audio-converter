"""Streaming peak envelopes without audio resampling or channel cancellation."""

import math
import numpy as np


class PeakEnvelope:
    """Keep every channel/frame visible using a bounded block-peak array.

    Points represent peak absolute amplitude, not resampled audio. Opposite-phase
    stereo remains visible. Underestimated durations coarsen completed blocks.
    """

    def __init__(self, sample_rate, channels, duration=None, capacity=262144):
        if not 1 <= channels <= 64 or not 1 <= sample_rate <= 768000:
            raise ValueError("Unsupported waveform channel count or sample rate")
        if capacity < 2 or capacity % 2:
            raise ValueError("Envelope capacity must be positive and even")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.capacity = capacity
        estimated = duration * sample_rate if duration and math.isfinite(duration) else 0
        self.block_size = max(1, math.ceil(estimated / capacity))
        self.values = np.empty(capacity, dtype=np.float32)
        self.used = 0
        self.total_frames = 0
        self.pending_count = 0
        self.pending_peak = 0.0
        self.remainder = b""

    def _coarsen(self):
        if self.pending_count:
            raise RuntimeError("Only complete blocks may be coarsened")
        count = self.used // 2
        self.values[:count] = self.values[:self.used].reshape(count, 2).max(axis=1)
        self.used = count
        self.block_size *= 2

    def feed(self, data):
        data = self.remainder + data
        frame_bytes = self.channels * 4
        usable = len(data) // frame_bytes * frame_bytes
        self.remainder = data[usable:]
        if not usable:
            return
        frames = np.frombuffer(data, dtype="<f4", count=usable // 4).reshape(-1, self.channels)
        if not np.isfinite(frames).all():
            raise ValueError("The decoded audio contains non-finite samples")
        peaks = np.abs(frames).max(axis=1)
        self.total_frames += len(peaks)
        offset = 0
        while offset < len(peaks):
            if self.used == self.capacity:
                self._coarsen()
            if self.pending_count:
                count = min(self.block_size - self.pending_count, len(peaks) - offset)
                self.pending_peak = max(self.pending_peak, float(peaks[offset:offset + count].max()))
                self.pending_count += count
                offset += count
                if self.pending_count == self.block_size:
                    self.values[self.used] = self.pending_peak
                    self.used += 1
                    self.pending_count = 0
                    self.pending_peak = 0.0
                continue
            blocks = min((len(peaks) - offset) // self.block_size, self.capacity - self.used)
            if blocks:
                stop = offset + blocks * self.block_size
                self.values[self.used:self.used + blocks] = peaks[offset:stop].reshape(blocks, self.block_size).max(axis=1)
                self.used += blocks
                offset = stop
            else:
                self.pending_count = len(peaks) - offset
                self.pending_peak = float(peaks[offset:].max())
                break

    def finish(self):
        if self.remainder:
            raise ValueError("The decoded audio ends with an incomplete sample frame")
        if self.pending_count:
            self.values[self.used] = self.pending_peak
            self.used += 1
            self.pending_count = 0
        result = self.values[:self.used].copy()
        result.flags.writeable = False
        return result, self.sample_rate / self.block_size, self.total_frames / self.sample_rate
