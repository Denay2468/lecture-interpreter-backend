"""Lecture Interpreter — backend (Flow A vertical slice).

Scope of this slice, per the technical architecture spec:
    Teacher Audio Stream -> Jitter Buffer -> Hybrid Segmentation ->
    Translation Pipeline -> Ordered Student Playback

This module currently implements Realtime Transport, Teacher Audio
Ingest, and the Jitter Buffer (spec Sections 1 & 2). Segmentation,
the Translation Queue/Pipeline, and Student Playback are stubbed with
a clear extension point (`on_smoothed_frame` in TeacherAudioIngest) so
the next build phase plugs in without restructuring this one.

No Question Mode / Ask button logic lives here yet — this slice is
Flow A only, matching the architecture spec's "what to build first"
recommendation (prove the continuous, non-blocking lecture pipeline
before adding the Ask/Question Mode logic on top of it).
"""
