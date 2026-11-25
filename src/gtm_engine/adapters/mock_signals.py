from gtm_engine.contracts.signals import JobPosting, SignalBatchReport, SignalTargetBoard, build_signal_report


# Three deterministic sample boards. Target 1 gets board A, target 2 board B, target 3 board C,
# then the cycle repeats, so any targets file works offline.
_SAMPLE_BOARDS: list[list[tuple[str, str | None]]] = [
    [
        ("Senior RevOps Manager", "New York, NY"),
        ("Growth Marketing Lead", "Remote - US"),
        ("Backend Engineer", "Remote - US"),
        ("senior revops  manager", "London, UK"),  # same role again: deduplicated
    ],
    [
        ("Enterprise Account Executive", "Chicago, IL"),
        ("Head of Revenue Operations", "Chicago, IL"),
        ("Data Engineer", "Remote"),
        ("Enterprise Account Executive", "Austin, TX"),  # same role again: deduplicated
    ],
    [
        ("Product Designer", "San Francisco, CA"),
        ("Compliance Analyst", None),
        ("Demand Generation Manager", "Remote"),
        ("Sales Development Representative", "Denver, CO"),
    ],
]

MOCK_DETECTED_AT = "2026-01-05T09:00:00+00:00"


class MockSignalCollectorAdapter:
    """Offline collector with fixed sample postings: no network, same output every run."""

    provider_name = "mock"

    def collect_signals(self, targets: list[SignalTargetBoard], keywords: list[str]) -> SignalBatchReport:
        postings: list[JobPosting] = []
        for index, target in enumerate(targets):
            board = _SAMPLE_BOARDS[index % len(_SAMPLE_BOARDS)]
            for job_number, (title, location) in enumerate(board, start=1):
                postings.append(
                    JobPosting(
                        target=target,
                        title=title,
                        location=location,
                        evidence_url=f"https://jobs.example.com/{target.board_token}/{job_number}",
                    )
                )
        return build_signal_report(self.provider_name, postings, keywords, detected_at=MOCK_DETECTED_AT)
