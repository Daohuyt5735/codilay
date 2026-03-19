import pytest

from codilay.error_tracker import ErrorEntry, ErrorTracker, Severity


def test_empty_tracker():
    et = ErrorTracker()
    assert et.is_empty()
    assert not et.has_issues()
    assert et.entries == []


def test_counts_all_zero_when_empty():
    et = ErrorTracker()
    c = et.counts()
    assert c[Severity.CRITICAL] == 0
    assert c[Severity.WARNING] == 0
    assert c[Severity.SKIPPED] == 0
    assert c[Severity.INFO] == 0


def test_add_critical():
    et = ErrorTracker()
    et.critical("auth failed", why="invalid key", action="paused run", file="cli.py")
    assert not et.is_empty()
    assert et.has_issues()
    assert et.counts()[Severity.CRITICAL] == 1
    entry = et.entries[0]
    assert entry.severity == Severity.CRITICAL
    assert entry.what == "auth failed"
    assert entry.why == "invalid key"
    assert entry.action == "paused run"
    assert entry.file == "cli.py"


def test_add_warning():
    et = ErrorTracker()
    et.warning("file unreadable", why="permission denied")
    assert et.has_issues()
    assert et.counts()[Severity.WARNING] == 1
    assert et.counts()[Severity.CRITICAL] == 0


def test_add_skipped():
    et = ErrorTracker()
    et.skipped("binary file", why="not text")
    assert not et.has_issues()  # SKIPPED does not count as an issue
    assert et.counts()[Severity.SKIPPED] == 1


def test_add_info():
    et = ErrorTracker()
    et.info("resumed from backup state")
    assert not et.has_issues()
    assert et.counts()[Severity.INFO] == 1


def test_mixed_entries_counts():
    et = ErrorTracker()
    et.critical("c1")
    et.warning("w1")
    et.warning("w2")
    et.skipped("s1")
    et.info("i1")
    et.info("i2")
    et.info("i3")

    c = et.counts()
    assert c[Severity.CRITICAL] == 1
    assert c[Severity.WARNING] == 2
    assert c[Severity.SKIPPED] == 1
    assert c[Severity.INFO] == 3
    assert et.has_issues()


def test_has_issues_false_when_only_skipped_and_info():
    et = ErrorTracker()
    et.skipped("binary")
    et.info("note")
    assert not et.has_issues()


def test_entries_returns_copy():
    """Mutating the returned list must not affect the tracker's internal state."""
    et = ErrorTracker()
    et.warning("w1")
    entries = et.entries
    entries.clear()
    assert len(et.entries) == 1


def test_optional_fields_default_empty():
    et = ErrorTracker()
    et.critical("bare")
    entry = et.entries[0]
    assert entry.why == ""
    assert entry.action == ""
    assert entry.file is None


def test_severity_is_string_enum():
    """Severity values should be usable as plain strings (str, Enum)."""
    assert Severity.CRITICAL == "CRITICAL"
    assert Severity.WARNING == "WARNING"
