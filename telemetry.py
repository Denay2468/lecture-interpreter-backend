import time
import math
from typing import List, Dict, Any

class TelemetryTracker:
    def __init__(self):
        self.latencies_ms: List[float] = []
        self.total_segments: int = 0
        self.successful_segments: int = 0
        self.failed_segments: int = 0
        self.total_audio_seconds: float = 0.0
        self.deepl_char_count: int = 0
        self.elevenlabs_char_count: int = 0

    def record_segment(self, latency_ms: float, success: bool = True, audio_duration_sec: float = 0.0, deepl_chars: int = 0, tts_chars: int = 0):
        self.total_segments += 1
        if success:
            self.successful_segments += 1
            if latency_ms > 0:
                self.latencies_ms.append(latency_ms)
        else:
            self.failed_segments += 1

        self.total_audio_seconds += audio_duration_sec
        self.deepl_char_count += deepl_chars
        self.elevenlabs_char_count += tts_chars

    def get_avg_latency(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return round(sum(self.latencies_ms) / len(self.latencies_ms), 2)

    def get_percentile_latency(self, percentile: float) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_latencies = sorted(self.latencies_ms)
        index = int(math.ceil((percentile / 100.0) * len(sorted_latencies))) - 1
        index = max(0, min(index, len(sorted_latencies) - 1))
        return round(sorted_latencies[index], 2)

    def get_success_rate(self) -> float:
        if self.total_segments == 0:
            return 100.0
        return round((self.successful_segments / self.total_segments) * 100.0, 2)

    def calculate_estimated_costs(self) -> Dict[str, float]:
        hours_recorded = max(self.total_audio_seconds / 3600.0, 0.01)
        deepgram_cost = (self.total_audio_seconds / 60.0) * 0.0043
        deepl_cost = (self.deepl_char_count / 1000000.0) * 20.0
        elevenlabs_cost = (self.elevenlabs_char_count / 1000.0) * 0.18

        total_cost = deepgram_cost + deepl_cost + elevenlabs_cost
        hourly_rate = total_cost / hours_recorded

        return {
            "deepgram_usd": round(deepgram_cost, 4),
            "deepl_usd": round(deepl_cost, 4),
            "elevenlabs_usd": round(elevenlabs_cost, 4),
            "total_cost_usd": round(total_cost, 4),
            "estimated_hourly_cost_usd": round(hourly_rate, 4),
        }

    def get_metrics_summary(self) -> Dict[str, Any]:
        costs = self.calculate_estimated_costs()
        return {
            "avg_latency_ms": self.get_avg_latency(),
            "p50_latency_ms": self.get_percentile_latency(50),
            "p95_latency_ms": self.get_percentile_latency(95),
            "success_rate_percent": self.get_success_rate(),
            "total_segments": self.total_segments,
            "successful_segments": self.successful_segments,
            "failed_segments": self.failed_segments,
            "costs": costs,
            "estimated_hourly_cost_usd": costs["estimated_hourly_cost_usd"]
        }

telemetry_tracker = TelemetryTracker()
