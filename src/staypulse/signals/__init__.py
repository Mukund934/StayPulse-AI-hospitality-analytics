"""External and calendar demand signals.

Signals live here rather than in `analytics/` because they are *inputs* to
analysis, not analysis itself: a signal describes the world outside the booking
system, and everything in this package must be traceable to a documented source.

Current members:
    calendar -- public-holiday effects measured from booking data.

Deliberately absent: weather and events. The generator contains no term that
responds to either, so any correlation measured against them would be spurious by
construction. See docs/ROADMAP.md PART B.2.
"""
