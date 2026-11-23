# 03-02 Summary
- SAFE-01: `_determine_files_to_delete(current, existing, folder_path)` now requires the file to be absent from disk. The rebuild step carries forward every existing entry that was not rebuilt and not actually deleted (covers hash failures, newly-ignored files, over-cap, failed soft-delete).
- SAFE-02: `S3Manager.get_object_info` returns None only on 404/NoSuchKey/NotFound and now RAISES on other errors (so "confirmed missing" != "could not check"). `soft_delete_object` returns True for a missing object, False if the check itself fails.
- Tests: 6 new/updated; all verified to FAIL on the pre-change code. Full suite 319 passed, ruff check clean, ruff-format file set unchanged (7 pre-existing unformatted files, not touched by this plan).
- Deviation: legacy entries without s3_key that are truly gone are still carried forward (that is what the old loop did); plan text said they'd drop, but old behavior was kept to avoid a behavior change.
