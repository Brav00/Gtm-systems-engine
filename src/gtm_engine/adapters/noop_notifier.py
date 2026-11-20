from gtm_engine.contracts.readiness import ReadinessBatchReport


class NoopReadinessNotifier:
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        return None
