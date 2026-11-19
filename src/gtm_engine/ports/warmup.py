from typing import Protocol
from gtm_engine.contracts.warmup import DomainWarmupEvaluation


class WarmupMonitorPort(Protocol):
    def evaluate_domain(self, domain: str) -> DomainWarmupEvaluation:
        """Evaluate warm-up health and bounce metrics for a domain and return a rotation decision."""
        ...
