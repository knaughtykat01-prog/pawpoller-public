"""Posting module — multi-platform story upload, edit, and publication tracking.

Uploads stories from the local m_x/Archives/Complete_Stories/ directory to 6
platforms (Inkbunny, FurAffinity, Weasyl, SoFurry, SquidgeWorld, Bluesky).
Tracks publications in the database, detects file changes, and supports
retroactive claiming of already-live submissions.

Sub-packages:
    platforms/      Platform-specific poster implementations (one per site).

Key modules:
    manager         Orchestrates uploads: resolves files, dispatches to posters,
                    records results in the publications registry.
    scheduler       Daemon thread that processes the posting_queue table on a
                    60-second check interval.
    story_reader    Reads story archives (story.json, split_manifest.json,
                    tags_upload.txt) and builds StoryUploadPackage objects.
    sync            Retroactive sync (claim existing submissions) and change
                    detection (file hash comparison against publications).
    generate_story_json
                    CLI tool that generates story.json files from existing
                    archive data sources (tags_upload.txt, split_manifest.json).
"""
